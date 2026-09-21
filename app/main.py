"""Ponto de entrada da aplicação VIGIA — API + interface no mesmo processo.

    python run.py                       # tudo pronto, modo demonstração
    uvicorn app.main:app --reload       # se preferir controlar você mesmo
"""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import web
from app.config import settings
from app.database import MemoriaVolatil, close_connections, engine, get_redis
from app.routers import alertas, auditoria, auth, cameras, deteccoes, pessoas, stream, usuarios
from app.services.events import gerenciador
from app.services.simulador import simulador

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vigia")


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    log.info("%s v%s", settings.APP_NAME, settings.APP_VERSION)
    log.info("Modo: %s", settings.modo)

    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    await get_redis().ping()

    await gerenciador.iniciar_escuta()
    if settings.SIMULADOR:
        await simulador.iniciar()

    log.info("Interface em http://%s:%s", settings.HOST, settings.PORT)
    log.info("Documentação da API em http://%s:%s/docs", settings.HOST, settings.PORT)
    yield

    log.info("Encerrando…")
    await simulador.parar()
    await gerenciador.parar()
    await close_connections()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "API do Centro Integrado de Videomonitoramento.\n\n"
        "**Identificadores biométricos** seguem o formato `BIO-<M|F|N>-NNNNNN`, "
        "emitidos uma única vez no cadastro a partir do sexo do registro civil. "
        "O motor de reconhecimento não infere sexo a partir da imagem — ele apenas "
        "devolve o identificador já existente, ou nada, quando a similaridade fica "
        "abaixo do limiar da câmera."
    ),
    lifespan=ciclo_de_vida,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.middleware("http")
async def cabecalhos_e_tempo(request: Request, call_next):
    inicio = time.perf_counter()
    resposta = await call_next(request)
    resposta.headers["X-Tempo-Resposta-ms"] = f"{(time.perf_counter() - inicio) * 1000:.1f}"
    resposta.headers["X-Content-Type-Options"] = "nosniff"
    resposta.headers["Referrer-Policy"] = "no-referrer"
    if settings.ENV != "development":
        resposta.headers["X-Frame-Options"] = "DENY"
        resposta.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return resposta


@app.exception_handler(RequestValidationError)
async def erro_validacao(request: Request, exc: RequestValidationError):
    """Mensagem em português apontando o campo — erro de API também é interface."""
    problemas = [
        {"campo": " → ".join(str(p) for p in e["loc"] if p != "body"), "problema": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Dados inválidos na requisição.", "problemas": problemas},
    )


# --- API ---
app.include_router(auth.router)
app.include_router(cameras.router)
app.include_router(pessoas.router)
app.include_router(deteccoes.router)
app.include_router(alertas.router)
app.include_router(usuarios.router)
app.include_router(auditoria.router)
app.include_router(stream.router)

# --- Interface ---
web.montar_estaticos(app)
app.include_router(web.router)


@app.get("/saude", tags=["Sistema"], summary="Health check")
async def saude():
    estado = {"api": "ok", "banco": "desconhecido", "eventos": "desconhecido"}
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        estado["banco"] = "ok"
    except Exception as exc:  # noqa: BLE001
        estado["banco"] = f"falha: {exc}"
    try:
        volatil = get_redis()
        await volatil.ping()
        estado["eventos"] = "memória" if isinstance(volatil, MemoriaVolatil) else "redis"
    except Exception as exc:  # noqa: BLE001
        estado["eventos"] = f"falha: {exc}"

    saudavel = estado["banco"] == "ok" and not estado["eventos"].startswith("falha")
    return JSONResponse(
        status_code=200 if saudavel else 503,
        content={
            "saudavel": saudavel,
            "componentes": estado,
            "websockets_ativos": len(gerenciador.ativos),
            "simulador_gerados": simulador.gerados,
        },
    )
