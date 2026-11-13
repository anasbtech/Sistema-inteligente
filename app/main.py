"""Ponto de entrada da aplicação SEGURANÇA — API + interface no mesmo processo.

    python run.py                       # tudo pronto, modo demonstração
    uvicorn app.main:app --reload       # se preferir controlar você mesmo
"""
from __future__ import annotations

import logging
import re
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

from app import web
from app.config import settings
from app.database import MemoriaVolatil, close_connections, db_context, engine, get_redis
from app.routers import (
    alertas, auditoria, auth, cameras, configuracao, deteccoes, pessoas, stream,
    triagem_cadastro, usuarios, epi as rotas_epi, veiculos as rotas_veiculos,
    mapa_calor, guardian as rotas_guardian,
)
from app.services import video
from app.services.configuracao import carregar as carregar_configuracao
from app.services.events import gerenciador
from app.services.inferencia import inferencia
from app.services.veiculos.servico import servico as servico_veicular
from app.services.guardian.servico import servico as servico_guardian
from app.services.retencao import retencao
from app.services.simulador import simulador

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("seguranca")


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    log.info("%s v%s", settings.APP_NAME, settings.APP_VERSION)
    log.info("Modo: %s", settings.modo)

    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
    await get_redis().ping()

    # Antes de qualquer motor subir: a inferência lê os pisos na primeira
    # volta, e subir com o valor do .env faria a primeira rodada usar um
    # número que a tela não mostra mais.
    async with db_context() as db:
        await carregar_configuracao(db)

    await gerenciador.iniciar_escuta()
    # Um exclui o outro: detecção sorteada misturada à real deixa o registro
    # sem como distinguir o que a câmera viu do que foi inventado.
    if settings.INFERENCIA:
        if settings.SIMULADOR:
            log.warning("SIMULADOR ignorado: INFERENCIA está ligada.")
        await inferencia.iniciar()
    elif settings.SIMULADOR:
        await simulador.iniciar()
    # Veículos: módulo independente, com laço próprio, só nas câmeras com o
    # interruptor ligado. Não depende da análise geral, mas não roda junto
    # com o simulador: veículo real misturado a sorteado não se distingue.
    if settings.VEICULO_ATIVO and (settings.INFERENCIA or not settings.SIMULADOR):
        await servico_veicular.iniciar()
    # Guardian: o laço do gesto precisa das pessoas que a análise geral acha.
    if settings.GUARDIAN_ATIVO and settings.INFERENCIA:
        await servico_guardian.iniciar()

    # Depois dos motores: ele consulta o banco e, se estiver ligado, apaga.
    # Mesmo desligado avisa quanto já passou do prazo — material vencido
    # acumulando em silêncio é o que se quer deixar de ter.
    await retencao.iniciar()
    # Mapa de calor histórico: grava a cada minuto a ocupação que a análise somou.
    from app.services.ocupacao import acumulador as ocupacao
    await ocupacao.iniciar()

    # Chave sorteada morre com o processo: no próximo reinício toda sessão cai
    # e todo link de redefinição pendente passa a ser recusado. Em
    # desenvolvimento é só incômodo; em produção, é gente sem conseguir entrar
    # sem ninguém entender por quê.
    # Sem envio de e-mail a recuperação de senha depende inteiramente de um
    # administrador emitir o link à mão. Isso funciona, mas quem opera precisa
    # saber disso antes de alguém ficar trancado para fora — não ao descobrir
    # na tela, com a pessoa esperando.
    if settings.falta_para_email:
        log.warning(
            "Envio de e-mail indisponível: %s. A recuperação de senha segue "
            "funcionando pelo link emitido em Usuários → Emitir link.",
            settings.falta_para_email,
        )

    # Com envio de e-mail funcionando e APP_URL em branco, o link de
    # redefinição sai apontando para o laço local — chega à caixa de entrada
    # e não abre, porque na máquina de quem recebeu `localhost` é ela mesma.
    # Falha silenciosa do pior tipo: o envio é bem-sucedido e o sistema não
    # tem como saber que o link é inútil.
    if settings.falta_url_publica:
        log.warning("%s. Defina APP_URL com o endereço pelo qual as pessoas "
                    "acessam o sistema (ex.: APP_URL=http://10.0.0.5:8000).",
                    settings.falta_url_publica)

    if settings.chave_efemera:
        log.warning(
            "SECRET_KEY não está no .env — uma chave nova é sorteada a cada "
            "reinício, e com ela caem as sessões abertas e os links de "
            "redefinição pendentes. Defina SECRET_KEY para operar."
        )

    log.info("Interface em %s", settings.url_local)
    log.info("Documentação da API em %s/docs", settings.url_local)
    yield

    log.info("Encerrando…")
    await retencao.parar()
    await simulador.parar()
    await servico_veicular.parar()
    await servico_guardian.parar()
    await inferencia.parar()
    try:
        from app.services.ocupacao import acumulador as ocupacao
        await ocupacao.parar()        # depois dos motores: o último minuto vai para o banco
    except Exception as exc:  # noqa: BLE001
        log.warning("Ocupação do último minuto não gravada: %s", exc)
    # Depois dos motores e antes do banco: cada leitor RTSP é uma thread com
    # uma conexão aberta na câmera, e `video.encerrar_todos` existia sem
    # ninguém chamar. Sem isto, encerrar o servidor deixava as 24 conexões
    # penduradas até a câmera derrubá-las por conta própria — e um arranque
    # logo em seguida disputava com os leitores do processo anterior.
    video.encerrar_todos()
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
# Vídeo e imagem ficam de fora: JPEG não comprime, e o MJPEG do mosaico —
# 16 tiles a 6 quadros por segundo — era compactado na thread principal do
# servidor, a mesma que atende todas as requisições (visto no perfil em
# 05/11/2026). Era CPU jogada fora e atraso em tudo o mais.
app.add_middleware(GZipMiddleware, minimum_size=1024, exclude_content_types=(
    "text/event-stream", "multipart/x-mixed-replace", "image/jpeg", "image/png",
    "image/webp", "video/mp4"))


class CabecalhosETempo:
    """Carimba a resposta com o tempo de processamento e cabeçalhos de segurança.

    Escrito como middleware ASGI, e não com `@app.middleware("http")`, por
    causa do vídeo: aquele decorador embrulha tudo no BaseHTTPMiddleware, que
    não suporta o cliente desistir no meio de uma resposta em streaming — e
    era o que acontecia a cada troca de tela, com o navegador cortando um
    fluxo MJPEG. Cada corte desses deixava no log um traceback de
    `anyio.EndOfStream` ou `RuntimeError: No response returned` sobre uma
    conexão que terminou normalmente, e com um parque de doze câmeras isso
    soterra os erros que importam.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inicio = time.perf_counter()

        async def enviar(mensagem):
            if mensagem["type"] == "http.response.start":
                cabecalhos = MutableHeaders(scope=mensagem)
                cabecalhos["X-Tempo-Resposta-ms"] = (
                    f"{(time.perf_counter() - inicio) * 1000:.1f}"
                )
                cabecalhos["X-Content-Type-Options"] = "nosniff"
                cabecalhos["Referrer-Policy"] = "no-referrer"
                if settings.ENV != "development":
                    cabecalhos["X-Frame-Options"] = "DENY"
                    cabecalhos["Strict-Transport-Security"] = (
                        "max-age=63072000; includeSubDomains"
                    )
            await send(mensagem)

        await self.app(scope, receive, enviar)


app.add_middleware(CabecalhosETempo)


# O Pydantic explica em inglês e em vocabulário próprio. A tela mostra isso a
# quem opera câmera, não a quem escreve API: "Input should be less than or
# equal to 120" não diz a ninguém que o campo é quadros por segundo.
#
# Só o que aparece de fato. Mensagem sem tradução passa como veio — é melhor
# um texto em inglês que o campo certo do que uma frase inventada.
_TRADUCAO_VALIDACAO: list[tuple[str, str]] = [
    (r"^Field required$", "campo obrigatório"),
    (r"^Extra inputs are not permitted$", "campo não reconhecido"),
    (r"^Input should be a valid integer.*$", "informe um número inteiro"),
    (r"^Input should be a valid number.*$", "informe um número"),
    (r"^Input should be a valid string$", "informe um texto"),
    (r"^Input should be a valid list$", "informe uma lista"),
    (r"^Input should be a valid boolean.*$", "informe sim ou não"),
    (r"^Input should be less than or equal to (.+)$", r"o máximo é \1"),
    (r"^Input should be greater than or equal to (.+)$", r"o mínimo é \1"),
    (r"^Input should be less than (.+)$", r"precisa ser menor que \1"),
    (r"^Input should be greater than (.+)$", r"precisa ser maior que \1"),
    (r"^String should have at least (\d+) characters?$", r"precisa de ao menos \1 caracteres"),
    (r"^String should have at most (\d+) characters?$", r"o limite é \1 caracteres"),
    (r"^Input should be '?(.+?)'?$", r"valor aceito: \1"),
    (r"^value is not a valid email address.*$", "e-mail inválido"),
]


def _traduzir_validacao(msg: str) -> str:
    # "Value error, X" é o envelope que o Pydantic põe à frente das mensagens
    # que o próprio sistema escreveu — e essas já estão em português.
    msg = re.sub(r"^Value error,\s*", "", msg).strip()
    for padrao, troca in _TRADUCAO_VALIDACAO:
        novo, n = re.subn(padrao, troca, msg, flags=re.IGNORECASE)
        if n:
            return novo
    return msg


@app.exception_handler(RequestValidationError)
async def erro_validacao(request: Request, exc: RequestValidationError):
    """Mensagem em português apontando o campo — erro de API também é interface."""
    problemas = [
        {"campo": " → ".join(str(p) for p in e["loc"] if p != "body"),
         "problema": _traduzir_validacao(e["msg"])}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Dados inválidos na requisição.", "problemas": problemas},
    )


# --- API ---
app.include_router(auth.router)
app.include_router(cameras.router)
app.include_router(triagem_cadastro.router)   # antes de pessoas: /pessoas/{id} engoliria a rota
app.include_router(pessoas.router)
app.include_router(deteccoes.router)
app.include_router(alertas.router)
app.include_router(usuarios.router)
app.include_router(auditoria.router)
app.include_router(configuracao.router)
app.include_router(stream.router)
app.include_router(rotas_epi.router)
app.include_router(rotas_veiculos.router)
app.include_router(mapa_calor.router)
app.include_router(rotas_guardian.router)

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
            "inferencia_gerados": inferencia.gerados,
            # Quanto leva uma volta pelo parque — e, portanto, de quanto em
            # quanto tempo cada câmera é olhada. É o número que explica por
            # que alguém que atravessa um corredor em três segundos pode não
            # aparecer: não é o detector que falha, é que ninguém olhou.
            # Sem isto medido, qualquer conversa sobre atraso é palpite.
            "inferencia_ciclo_s": round(inferencia.ciclo_real, 1),
            "inferencia_cameras": inferencia.ultimo_total,
            "inferencia_falhas": inferencia.falhas,
            "inferencia_ultima_falha": inferencia.ultima_falha,
            # Estado de cada leitor RTSP. Um tile preto pode ser leitor
            # morto, leitor com erro ou entrega falhando — e da tela os
            # três são idênticos. Sem isto, diagnosticar "a câmera não
            # aparece" é adivinhação.
            "leitores": {
                cod: {"vivo": l.vivo, "erro": l.erro or "",
                      "tem_quadro": l.quadro is not None,
                      # Idade do último quadro de exibição. Um leitor
                      # vivo com quadro velho é tile preto na tela, e
                      # "tem_quadro" sozinho não distingue os dois.
                      "quadro_s": round(
                          time.monotonic() - video._ultimos[cod][0], 1)
                      if cod in video._ultimos else None,
                      "exibindo": l.exibindo}
                for cod, l in sorted(video._leitores.items())
            },
        },
    )

