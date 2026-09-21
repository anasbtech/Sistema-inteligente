"""WebSocket de eventos ao vivo e endereços de reprodução de vídeo.

O vídeo em si não passa pela API: as câmeras publicam RTSP num gateway
(MediaMTX), que reempacota para WebRTC/LL-HLS. A API só entrega a URL
assinada que o mosaico usa no `<video>`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.deps import exige
from app.models import Camera, StatusCamera, Usuario
from app.security import decodificar_token
from app.services.events import gerenciador

log = logging.getLogger("vigia.stream")
router = APIRouter(tags=["Tempo real"])

GATEWAY = "http://localhost:8889"  # MediaMTX WebRTC (WHEP)


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
