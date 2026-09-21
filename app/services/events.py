"""Barramento de eventos: Redis pub/sub ou fila em memória, e fan-out WebSocket.

Trocar por Kafka é substituir `publicar` e o laço de escuta por produtor e
consumidor do tópico `deteccoes.v1`; o resto da aplicação não muda.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Set

from fastapi import WebSocket

from app.config import settings
from app.database import MemoriaVolatil, get_redis

log = logging.getLogger("vigia.events")


class GerenciadorConexoes:
    """Mantém os WebSockets abertos e distribui os eventos do barramento."""

    def __init__(self) -> None:
        self.ativos: Set[WebSocket] = set()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def conectar(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.ativos.add(ws)
        log.info("WebSocket conectado (%d ativos)", len(self.ativos))

    async def desconectar(self, ws: WebSocket) -> None:
        async with self._lock:
            self.ativos.discard(ws)

    async def transmitir(self, mensagem: Dict[str, Any]) -> None:
        if not self.ativos:
            return
        texto = json.dumps(mensagem, default=str)
        mortos = []
        for ws in list(self.ativos):
            try:
                await ws.send_text(texto)
            except Exception:  # noqa: BLE001
                mortos.append(ws)
        if mortos:
            async with self._lock:
                for ws in mortos:
                    self.ativos.discard(ws)

    async def iniciar_escuta(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        volatil = get_redis()
        if isinstance(volatil, MemoriaVolatil):
            await self._loop_memoria(volatil)
        else:
            await self._loop_redis(volatil)

    async def _loop_memoria(self, volatil: MemoriaVolatil) -> None:
        fila = await volatil.assinar()
        log.info("Barramento de eventos em memória")
        try:
            while True:
                bruto = await fila.get()
                try:
                    await self.transmitir(json.loads(bruto))
                except json.JSONDecodeError:
                    log.warning("Evento malformado: %.120s", bruto)
        except asyncio.CancelledError:
            pass
        finally:
            await volatil.desassinar(fila)

    async def _loop_redis(self, redis) -> None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(settings.EVENT_CHANNEL)
        log.info("Assinando canal Redis '%s'", settings.EVENT_CHANNEL)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    await self.transmitir(json.loads(msg["data"]))
                except json.JSONDecodeError:
                    log.warning("Evento malformado no canal")
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(settings.EVENT_CHANNEL)
            await pubsub.aclose()

    async def parar(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


gerenciador = GerenciadorConexoes()


async def publicar(tipo: str, dados: Dict[str, Any]) -> None:
    """Publica um evento no barramento — chega ao painel pelo WebSocket."""
    await get_redis().publish(
        settings.EVENT_CHANNEL,
        json.dumps({"tipo": tipo, "dados": dados}, default=str),
    )
