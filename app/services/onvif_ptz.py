"""Controle PTZ via ONVIF.

Usa a biblioteca `onvif-zeep-async`. As credenciais da câmera devem vir de um
cofre (Vault, AWS Secrets Manager) — nunca do banco em claro. Aqui lemos de
variáveis de ambiente por simplicidade de exemplo.
"""
from __future__ import annotations

import logging
import os
from typing import Optional
from urllib.parse import urlparse

from app.models import Camera

log = logging.getLogger("vigia.ptz")

VETORES = {
    "left":     (-1.0, 0.0),
    "right":    (1.0, 0.0),
    "up":       (0.0, 1.0),
    "down":     (0.0, -1.0),
}


def _credenciais(cam: Camera) -> tuple[str, str]:
    usuario = os.getenv(f"ONVIF_USER_{cam.codigo.replace('-', '_')}") or os.getenv("ONVIF_USER", "admin")
    senha = os.getenv(f"ONVIF_PASS_{cam.codigo.replace('-', '_')}") or os.getenv("ONVIF_PASS", "")
    return usuario, senha


def _host_porta(cam: Camera) -> tuple[str, int]:
    if cam.onvif_url:
        u = urlparse(cam.onvif_url)
        return u.hostname or cam.ip, u.port or 80
    return cam.ip, 80


async def executar_ptz(cam: Camera, comando: str, velocidade: float = 0.5,
                       preset: Optional[int] = None) -> None:
    """Move a câmera. Erros de rede viram RuntimeError com mensagem legível."""
    try:
        from onvif import ONVIFCamera
    except ImportError:  # pragma: no cover
        log.warning("onvif-zeep-async ausente — comando '%s' para %s ignorado", comando, cam.codigo)
        return

    host, porta = _host_porta(cam)
    usuario, senha = _credenciais(cam)

    try:
        dispositivo = ONVIFCamera(host, porta, usuario, senha)
        await dispositivo.update_xaddrs()
        media = await dispositivo.create_media_service()
        ptz = await dispositivo.create_ptz_service()
        perfil = (await media.GetProfiles())[0]

        if comando == "home":
            req = ptz.create_type("GotoHomePosition")
            req.ProfileToken = perfil.token
            await ptz.GotoHomePosition(req)
            return

        if comando == "preset":
            req = ptz.create_type("GotoPreset")
            req.ProfileToken = perfil.token
            req.PresetToken = str(preset or 1)
            await ptz.GotoPreset(req)
            return

        req = ptz.create_type("ContinuousMove")
        req.ProfileToken = perfil.token

        if comando in ("zoom_in", "zoom_out"):
            z = velocidade if comando == "zoom_in" else -velocidade
            req.Velocity = {"Zoom": {"x": z}}
        else:
            x, y = VETORES.get(comando, (0.0, 0.0))
            req.Velocity = {"PanTilt": {"x": x * velocidade, "y": y * velocidade}}

        await ptz.ContinuousMove(req)

        # Movimento contínuo precisa de parada explícita.
        import asyncio
        await asyncio.sleep(0.35)
        parar = ptz.create_type("Stop")
        parar.ProfileToken = perfil.token
        parar.PanTilt = True
        parar.Zoom = True
        await ptz.Stop(parar)

    except Exception as exc:  # noqa: BLE001
        log.error("PTZ %s em %s falhou: %s", comando, cam.codigo, exc)
        raise RuntimeError(f"Não foi possível comandar a câmera {cam.codigo}: {exc}") from exc
