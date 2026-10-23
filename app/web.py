"""Serve a interface e expõe os metadados que ela consome no boot.

A interface é HTML/CSS/JS estático — sem build, sem npm. O FastAPI entrega os
arquivos e o `/api/info` diz ao front-end em que modo o sistema está rodando.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RAIZ, settings
from app.database import get_db
from app.models import Alerta, Auditoria, Camera, Deteccao, Pessoa, StatusCamera, Usuario
from app.services import extras

FRONTEND = RAIZ / "frontend"
router = APIRouter(tags=["Interface"])


class EstaticosRevalidados(StaticFiles):
    """Arquivos estáticos que o navegador sempre reconfirma antes de usar.

    Sem `Cache-Control`, o navegador aplica cache heurístico: ele mesmo arbitra
    um prazo — em geral uma fração do tempo desde o `Last-Modified` — e nesse
    prazo nem pergunta se mudou. Depois de uma alteração no JS, a interface
    continuava rodando o código antigo por horas, e nem recarregar resolvia.

    `no-cache` não proíbe guardar; obriga a revalidar. Com o ETag que o
    StaticFiles já emite, a resposta vira um 304 de alguns bytes quando nada
    mudou — o custo é uma ida ao servidor por arquivo, e a garantia é que a
    tela nunca roda uma versão que não é a do disco.
    """

    def file_response(self, *args, **kwargs):
        resposta = super().file_response(*args, **kwargs)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


def montar_estaticos(app) -> None:
    (FRONTEND / "static").mkdir(parents=True, exist_ok=True)
    app.mount("/static", EstaticosRevalidados(directory=FRONTEND / "static"), name="static")


@router.get("/", include_in_schema=False)
async def index():
    # A página carrega os scripts por caminho fixo, então é ela que ancora a
    # versão de tudo: guardada, nem os `no-cache` dos estáticos são
    # consultados, porque a requisição não chega a acontecer.
    return FileResponse(FRONTEND / "templates" / "index.html",
                        headers={"Cache-Control": "no-store"})


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(FRONTEND / "static" / "favicon.svg")


@router.get("/api/info", summary="Modo de operação e estatísticas públicas")
async def info(db: AsyncSession = Depends(get_db)):
    """Consumido pela tela de login e pela aba de configurações.

    Não exige autenticação de propósito: são contagens agregadas e o modo de
    operação, nada que identifique pessoa.
    """
    from app.services import face_engine, simulador
    from app.services.vision import CATALOGO_ACOES, detalhe_acao, yolo_disponivel
    from app.tipos import dialeto_suporta_vetor

    async def contar(modelo):
        return int((await db.execute(select(func.count()).select_from(modelo))).scalar_one())

    cameras = await contar(Camera)
    online = int((await db.execute(
        select(func.count()).select_from(Camera).where(Camera.status == StatusCamera.ONLINE)
    )).scalar_one())

    demo = settings.ENV == "development" and settings.SIMULADOR

    # As senhas de demonstração não saem por aqui: esta rota é pública, e
    # devolvê-las seria entregar credenciais válidas a quem não se autenticou.
    # Quem sobe o sistema as encontra na saída de `run.py`.
    return {
        "sistema": settings.APP_NAME,
        "versao": settings.APP_VERSION,
        "modo": settings.modo,
        "demo": demo,
        "simulador": settings.SIMULADOR,
        "simulador_gerados": simulador.simulador.gerados,
        "busca_vetorial": "pgvector (HNSW)" if dialeto_suporta_vetor(db) else "numpy (força bruta)",
        "modelos": {
            "face": face_engine.modelos_disponiveis(),
            "yolo": yolo_disponivel(),
        },
        "config": {
            "limiar_facial": settings.FACE_THRESHOLD,
            "limiar_objeto": settings.OBJECT_THRESHOLD,
            "embedding_dim": settings.EMBEDDING_DIM,
            "device": settings.DEVICE,
            "retencao_dias": settings.RETENTION_DAYS_UNMATCHED,
            "mascarar_para": settings.mask_roles,
            "senha_expira_dias": settings.PASSWORD_EXPIRE_DAYS,
            "max_tentativas": settings.MAX_LOGIN_ATTEMPTS,
            "sessao_minutos": settings.ACCESS_TOKEN_MINUTES,
        },
        # O catálogo de ações vai inteiro, com os indisponíveis e o motivo.
        # A tela precisa poder mostrar "Fogo" na lista e dizer, ali mesmo, que
        # não há detector — esconder a opção faria o operador procurá-la.
        "acoes": [
            {
                "codigo": f.codigo,
                "rotulo": f.rotulo,
                "severidade": f.severidade,
                "disponivel": f.disponivel,
                "indisponivel": f.indisponivel,
                # Em que condição ela dispara, com os números que estão
                # valendo. É o que permite decidir antes de ligar.
                "detalhe": detalhe_acao(f.codigo),
                "exige_continua": f.exige_continua,
                "exige_zona": f.exige_zona,
                "exige_bagagem": f.exige_bagagem,
            }
            for f in CATALOGO_ACOES
        ],
        # A janela que vale para a câmera que não definiu a sua. A tela
        # precisa dela para dizer, no campo em branco, o que vai acontecer —
        # senão "--:--" parece ação sem horário nenhum.
        "horario_padrao": {
            "inicio": settings.ACAO_HORARIO_INICIO,
            "fim": settings.ACAO_HORARIO_FIM,
        },
        # Pelo mesmo motivo: no campo em branco a tela precisa dizer quantas
        # pessoas vão disparar o alerta, senão o vazio parece "sem limite".
        "aglomeracao_padrao": settings.ACAO_AGLOMERACAO_N,
        # Capacete e arma de fogo dependem de um modelo que não vem embarcado.
        # A tela precisa saber disso para não oferecer uma chave que não liga
        # nada — e para dizer o que falta, em vez de deixar o operador ligar e
        # esperar por um registro que nunca vem.
        "detectores_extras": extras.disponiveis(),
        "estatisticas": {
            "cameras": cameras,
            "cameras_online": online,
            "disponibilidade": f"{online / cameras * 100:.1f}%".replace(".", ",") if cameras else "—",
            "pessoas": await contar(Pessoa),
            "deteccoes": await contar(Deteccao),
            "alertas": await contar(Alerta),
            "auditoria": await contar(Auditoria),
            "usuarios": await contar(Usuario),
        },
    }
