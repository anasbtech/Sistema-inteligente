"""Conexão com o banco e armazenamento volátil (Redis ou memória)."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, Optional

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import settings

# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------
if settings.usa_sqlite:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=StaticPool if ":memory:" in settings.DATABASE_URL else None,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _configurar_sqlite(dbapi_conn, _):
        """WAL permite leitura concorrente com escrita — importante porque o
        simulador grava enquanto o painel consulta."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()
else:
    engine = create_async_engine(
        settings.DATABASE_URL,
        echo=settings.DEBUG,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_pre_ping=True,
    )

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependência FastAPI: uma sessão por requisição, commit no fim."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def db_context() -> AsyncGenerator[AsyncSession, None]:
    """Uso fora do ciclo de requisição (simulador, scripts)."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ----------------------------------------------------------------------
# Armazenamento volátil: Redis quando configurado, memória caso contrário.
# Usado para rate limit, revogação de token e pub/sub de eventos.
# ----------------------------------------------------------------------
class MemoriaVolatil:
    """Substituto de Redis para rodar em um processo só.

    Implementa apenas o que o VIGIA usa. Ao configurar REDIS_URL, a classe
    some do caminho e o cliente real entra no lugar — a interface é a mesma.
    """

    def __init__(self) -> None:
        self._dados: Dict[str, Any] = {}
        self._expira: Dict[str, float] = {}
        self._assinantes: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    def _vencido(self, chave: str) -> bool:
        venc = self._expira.get(chave)
        if venc is not None and venc <= time.time():
            self._dados.pop(chave, None)
            self._expira.pop(chave, None)
            return True
        return False

    async def ping(self) -> bool:
        return True

    async def exists(self, chave: str) -> int:
        return 0 if self._vencido(chave) else int(chave in self._dados)

    async def get(self, chave: str) -> Optional[str]:
        return None if self._vencido(chave) else self._dados.get(chave)

    async def setex(self, chave: str, segundos: int, valor: Any) -> None:
        self._dados[chave] = valor
        self._expira[chave] = time.time() + segundos

    async def incr(self, chave: str) -> int:
        if self._vencido(chave):
            self._dados[chave] = 0
        atual = int(self._dados.get(chave, 0)) + 1
        self._dados[chave] = atual
        return atual

    async def expire(self, chave: str, segundos: int) -> None:
        self._expira[chave] = time.time() + segundos

    async def ttl(self, chave: str) -> int:
        venc = self._expira.get(chave)
        return -1 if venc is None else max(0, int(venc - time.time()))

    async def delete(self, chave: str) -> None:
        self._dados.pop(chave, None)
        self._expira.pop(chave, None)

    async def publish(self, _canal: str, mensagem: str) -> None:
        async with self._lock:
            mortos = []
            for fila in self._assinantes:
                try:
                    fila.put_nowait(mensagem)
                except asyncio.QueueFull:
                    mortos.append(fila)
            for f in mortos:
                self._assinantes.remove(f)

    async def assinar(self) -> asyncio.Queue:
        fila: asyncio.Queue = asyncio.Queue(maxsize=500)
        async with self._lock:
            self._assinantes.append(fila)
        return fila

    async def desassinar(self, fila: asyncio.Queue) -> None:
        async with self._lock:
            if fila in self._assinantes:
                self._assinantes.remove(fila)

    async def aclose(self) -> None:
        self._dados.clear()
        self._expira.clear()
        self._assinantes.clear()


_volatil: Any = None


def get_redis():
    """Devolve o cliente Redis real ou o substituto em memória."""
    global _volatil
    if _volatil is None:
        if settings.usa_redis:
            import redis.asyncio as aioredis
            _volatil = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
        else:
            _volatil = MemoriaVolatil()
    return _volatil


async def close_connections() -> None:
    global _volatil
    if _volatil is not None:
        await _volatil.aclose()
        _volatil = None
    await engine.dispose()
