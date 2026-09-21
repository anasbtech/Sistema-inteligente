"""WebSocket de eventos ao vivo e endereços de reprodução de vídeo.

O vídeo em si não passa pela API: as câmeras publicam RTSP num gateway
(MediaMTX), que reempacota para WebRTC/LL-HLS. A API só entrega a URL
assinada que o mosaico usa no `<video>`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import exige
from app.models import Camera, StatusCamera, Usuario
from app.security import decodificar_token
from app.services import video
from app.services.events import gerenciador

log = logging.getLogger("vigia.stream")
router = APIRouter(tags=["Tempo real"])

GATEWAY = "http://localhost:8889"  # MediaMTX WebRTC (WHEP)

# Idade tolerada num quadro de pré-visualização. Acima disso vale reabrir o
# fluxo; abaixo, a miniatura reaproveita o que já foi capturado.
IDADE_MAXIMA_SNAPSHOT_S = 15.0


@router.websocket("/ws/eventos")
async def ws_eventos(ws: WebSocket, token: str = Query(..., description="Access token JWT")):
    """Empurra cada detecção e mudança de alerta para o painel.

    O navegador não envia cabeçalho Authorization em WebSocket, então o token
    vai na query string — por isso ele é curto e revogável.
    """
    try:
        payload = decodificar_token(token, "access")
        uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await gerenciador.conectar(ws)
    try:
        await ws.send_text(json.dumps({"tipo": "conectado", "dados": {"canal": settings.EVENT_CHANNEL}}))
        while True:
            # Mantém a conexão viva e aceita ping do cliente.
            msg = await ws.receive_text()
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("WebSocket encerrado: %s", exc)
    finally:
        await gerenciador.desconectar(ws)


def _assinar_url(caminho: str, expira_em: int = 300) -> str:
    """Token curto para o gateway validar a reprodução."""
    exp = int(time.time()) + expira_em
    assinatura = hmac.new(
        settings.SECRET_KEY.encode(), f"{caminho}:{exp}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"?exp={exp}&sig={assinatura}"


@router.get("/stream/{codigo}", summary="Endereços de reprodução da câmera")
async def stream(codigo: str, db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("live"))):
    cam = (
        await db.execute(select(Camera).where(Camera.codigo == codigo))
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
    if cam.status != StatusCamera.ONLINE:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Câmera indisponível ({cam.status.value}).")

    caminho = cam.codigo.lower()
    qs = _assinar_url(caminho)
    return {
        "codigo": cam.codigo,
        "nome": cam.nome,
        # WHEP: latência sub-segundo, é o que o mosaico usa.
        "webrtc": f"{GATEWAY}/{caminho}/whep{qs}",
        # Alternativa compatível com mais navegadores, ~2-4s de atraso.
        "hls": f"{GATEWAY}/{caminho}/index.m3u8{qs}",
        "expira_em_s": 300,
        "resolucao": cam.resolucao,
        "fps": cam.fps,
        "codec": cam.codec,
    }


async def _espectador(token: str, db: AsyncSession) -> Usuario:
    """Valida o token que veio na query string.

    `<img>` e `<video>` não enviam cabeçalho Authorization, então o token
    viaja na URL — a mesma concessão já feita no WebSocket de eventos, e a
    razão de o access token ser curto e revogável.
    """
    try:
        payload = decodificar_token(token, "access")
        usuario_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sessão inválida ou expirada.")

    usuario = await db.get(Usuario, usuario_id)
    if usuario is None or not usuario.ativo:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conta desativada.")
    if not usuario.pode("live"):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"Seu perfil ({usuario.perfil.value}) não permite ver o vídeo ao vivo.")
    return usuario


async def _quadros(leitor: video.Leitor):
    anterior = None
    espera = 1.0 / (video.FPS_SAIDA * 2)
    while leitor.vivo and not leitor.erro:
        leitor.tocar()
        atual = leitor.quadro
        if atual is not None and atual is not anterior:
            anterior = atual
            yield (b"--quadro\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(atual)).encode() + b"\r\n\r\n" + atual + b"\r\n")
        await asyncio.sleep(espera)


async def _leitor_da_camera(codigo: str, db: AsyncSession) -> video.Leitor:
    """Leitor pronto, ou o erro correspondente como status HTTP."""
    cam = (
        await db.execute(select(Camera).where(Camera.codigo == codigo))
    ).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
    if not cam.rtsp_url:
        raise HTTPException(status.HTTP_409_CONFLICT, "Câmera sem endereço RTSP cadastrado.")

    try:
        leitor = video.obter(cam.codigo, cam.rtsp_url)
    except video.LeitorIndisponivel as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))

    # O primeiro quadro só chega depois do handshake RTSP; esperar aqui deixa
    # o erro visível como status HTTP, em vez de um fluxo que nunca começa.
    await asyncio.get_running_loop().run_in_executor(None, leitor.pronto.wait, 12.0)
    if leitor.erro:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{codigo}: {leitor.erro}")
    return leitor


@router.get("/stream/{codigo}/mjpeg", summary="Vídeo ao vivo (MJPEG, desenvolvimento)")
async def mjpeg(codigo: str, token: str = Query(..., description="Access token JWT"),
                db: AsyncSession = Depends(get_db)):
    """Fluxo MJPEG para o mosaico enquanto não há gateway WebRTC no ar."""
    await _espectador(token, db)
    leitor = await _leitor_da_camera(codigo, db)
    return StreamingResponse(
        _quadros(leitor),
        media_type="multipart/x-mixed-replace; boundary=quadro",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/stream/{codigo}/snapshot", summary="Quadro único da câmera (JPEG)")
async def snapshot(codigo: str, token: str = Query(..., description="Access token JWT"),
                   db: AsyncSession = Depends(get_db)):
    """Um quadro só, para a pré-visualização da lista de câmeras.

    Uma lista com muitas linhas não pode abrir um fluxo por linha: o navegador
    limita as conexões simultâneas por servidor e travaria as chamadas da API.
    """
    await _espectador(token, db)

    # Um quadro de alguns segundos atrás serve para uma miniatura, e evita
    # reabrir o RTSP — que custa segundos — cada vez que a lista é aberta.
    quadro = video.ultimo_quadro(codigo, IDADE_MAXIMA_SNAPSHOT_S)
    if quadro is None:
        quadro = (await _leitor_da_camera(codigo, db)).quadro
    if quadro is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"{codigo}: nenhum quadro disponível.")

    return Response(content=quadro, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/stream", summary="Endereços de todas as câmeras on-line")
async def streams(setor: Optional[str] = None, db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("live"))):
    filtros = [Camera.status == StatusCamera.ONLINE]
    if setor:
        filtros.append(Camera.setor == setor)
    linhas = (
        await db.execute(select(Camera).where(*filtros).order_by(Camera.codigo))
    ).scalars().unique().all()

    return [
        {
            "codigo": c.codigo, "nome": c.nome, "setor": c.setor,
            "webrtc": f"{GATEWAY}/{c.codigo.lower()}/whep{_assinar_url(c.codigo.lower())}",
            "analiticos": {"facial": c.analitico_facial,
                           "objeto": c.analitico_objeto,
                           "acao": c.analitico_acao},
            "ptz": c.ptz,
        }
        for c in linhas
    ]
