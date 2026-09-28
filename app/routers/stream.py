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
from app.deps import exige, usuario_de_query
from app.models import Camera, StatusCamera, Usuario
from app.security import decodificar_token
from app.services import video
from app.services.events import gerenciador

log = logging.getLogger("seguranca.stream")
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
        # A da ficha é a cadastrada; esta é a que o fluxo realmente entregou.
        # Quando divergem, quem está certo é esta.
        "resolucao_medida": _resolucao_medida(cam.codigo),
    }


def _resolucao_medida(codigo: str) -> str:
    leitor = video._leitores.get(codigo)
    if leitor is None or not leitor.largura_nativa:
        return ""
    return f"{leitor.largura_nativa}x{leitor.altura_nativa}"


async def _quadros(leitor: video.Leitor, detalhe: bool = False):
    anterior = None
    espera = 1.0 / (video.FPS_SAIDA * 2)
    while leitor.vivo and not leitor.erro:
        # `tocar_detalhe` é o que mantém o encode em resolução nativa ligado:
        # o leitor o desliga sozinho quando o último espectador fecha a aba.
        leitor.tocar_detalhe() if detalhe else leitor.tocar()
        atual = leitor.quadro_detalhe if detalhe else leitor.quadro
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
                hd: bool = Query(False, description="Resolução nativa da câmera, sem redução"),
                db: AsyncSession = Depends(get_db)):
    """Fluxo MJPEG para o mosaico enquanto não há gateway WebRTC no ar.

    Com `hd=1` sai a resolução nativa, para a ficha da câmera e o ajuste de
    enquadramento — ali o quadro ocupa a tela inteira e a redução do mosaico
    apareceria como borrão. É uma conexão por vez, não dezesseis, e o encode
    extra cessa sozinho quando o espectador sai.
    """
    await usuario_de_query(token, db, "live")
    leitor = await _leitor_da_camera(codigo, db)
    if hd:
        # Pedir antes de abrir o fluxo: o encode nativo começa no próximo
        # quadro, e sem isto o gerador esperaria um buffer que ninguém enche.
        leitor.tocar_detalhe()
        await asyncio.get_running_loop().run_in_executor(
            None, leitor.pronto_detalhe.wait, 5.0
        )
    return StreamingResponse(
        _quadros(leitor, detalhe=hd),
        media_type="multipart/x-mixed-replace; boundary=quadro",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/stream/{codigo}/snapshot", summary="Quadro único da câmera (JPEG)")
async def snapshot(codigo: str, token: str = Query(..., description="Access token JWT"),
                   hd: bool = Query(False, description="Resolução nativa da câmera, sem redução"),
                   db: AsyncSession = Depends(get_db)):
    """Um quadro só, para a pré-visualização da lista de câmeras.

    Uma lista com muitas linhas não pode abrir um fluxo por linha: o navegador
    limita as conexões simultâneas por servidor e travaria as chamadas da API.

    Com `hd=1` vem a resolução nativa, e aí não há atalho pelo último quadro
    guardado: aquele é o do mosaico, já reduzido.
    """
    await usuario_de_query(token, db, "live")

    if hd:
        leitor = await _leitor_da_camera(codigo, db)
        leitor.tocar_detalhe()
        await asyncio.get_running_loop().run_in_executor(
            None, leitor.pronto_detalhe.wait, 5.0
        )
        quadro = leitor.quadro_detalhe
        if quadro is None:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                                f"{codigo}: nenhum quadro em resolução nativa.")
        return Response(content=quadro, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

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

