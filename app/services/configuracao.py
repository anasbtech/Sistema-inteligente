"""Parâmetros do sistema ajustáveis pela interface.

Só entra aqui o que a aplicação relê a cada uso. Um parâmetro lido na subida
do processo — dispositivo, modelo facial, dimensão do vetor, limiares que são
o padrão de câmera nova — ficaria gravado sem passar a valer, e a tela
mostraria um número que o sistema não está usando. Esses continuam no `.env`
e aparecem na tela como fixos, com o nome da variável ao lado.

A faixa de cada parâmetro não é decoração: um zero a mais no piso de
confiança desliga o registro, e um a menos enche a lista de falso positivo.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Configuracao, Perfil

log = logging.getLogger("seguranca.configuracao")


@dataclass(frozen=True)
class Parametro:
    chave: str          # nome do atributo em `settings`
    rotulo: str
    grupo: str          # retencao | acesso | deteccao
    tipo: str           # int | float | perfis
    ajuda: str
    unidade: str = ""
    minimo: Optional[float] = None
    maximo: Optional[float] = None


PARAMETROS: List[Parametro] = [
    # --- Retenção e privacidade ---
    Parametro(
        "RETENTION_DAYS_UNMATCHED", "Retenção de não identificadas", "retencao",
        "int", unidade="dias", minimo=1, maximo=3650,
        ajuda="Prazo para o expurgo apagar captura sem identidade, junto com o "
              "arquivo da evidência. Vale para capturas gravadas a partir de agora.",
    ),
    Parametro(
        "MASK_PII_FOR_ROLES", "Mascaramento de PII", "retencao",
        "perfis",
        ajuda="Perfis que veem o dado pessoal mascarado na interface. Passa a "
              "valer na requisição seguinte, sem novo login.",
    ),
    # --- Acesso e sessão ---
    Parametro(
        "ACCESS_TOKEN_MINUTES", "Duração da sessão", "acesso",
        "int", unidade="minutos", minimo=5, maximo=480,
        ajuda="Tempo de validade do token de acesso. Sessões já abertas "
              "mantêm a duração com que nasceram.",
    ),
    Parametro(
        "MAX_LOGIN_ATTEMPTS", "Tentativas de login", "acesso",
        "int", unidade="tentativas", minimo=3, maximo=20,
        ajuda="Senhas erradas seguidas antes de a conta ser bloqueada "
              "temporariamente.",
    ),
    Parametro(
        "PASSWORD_EXPIRE_DAYS", "Expiração de senha", "acesso",
        "int", unidade="dias", minimo=30, maximo=365,
        ajuda="Idade máxima da senha. Passado o prazo, o login exige "
              "redefinição antes de seguir.",
    ),
    # --- Detecção ---
    Parametro(
        "INFERENCIA_CONFIANCA_MIN", "Piso de confiança do objeto", "deteccao",
        "float", minimo=0.50, maximo=0.95,
        ajuda="Abaixo disso o modelo acerta que há algo e erra o que é. Foi "
              "em 0,70 que apareceram pessoas registradas como animal.",
    ),
    Parametro(
        "INFERENCIA_LADO_MIN_PX", "Lado mínimo do objeto", "deteccao",
        "int", unidade="px", minimo=16, maximo=200,
        ajuda="Caixa menor que isto tem poucos pixels para classificar, e é "
              "de onde vem a maior parte das trocas de classe.",
    ),
    Parametro(
        "MIN_FACE_QUALITY", "Qualidade mínima do rosto", "deteccao",
        "float", minimo=0.20, maximo=0.90,
        ajuda="Nota mínima para a captura entrar no registro. Alta demais, "
              "passagens reais deixam de ser gravadas.",
    ),
    Parametro(
        "MIN_FACE_PIXELS", "Altura mínima do rosto", "deteccao",
        "int", unidade="px", minimo=24, maximo=200,
        ajuda="Rosto menor que isto é esticado até os 112x112 que o modelo "
              "recebe, e o vetor não casa nem com outra captura da mesma pessoa.",
    ),
]

POR_CHAVE: Dict[str, Parametro] = {p.chave: p for p in PARAMETROS}

# Lidos quando o processo sobe. Ficam na tela como leitura, com o nome da
# variável, para quem precisa mudar saber onde mexer.
FIXOS: List[tuple[str, str]] = [
    ("Dispositivo de inferência", "DEVICE"),
    ("Modelo facial", "FACE_MODEL"),
    ("Dimensão do vetor", "EMBEDDING_DIM"),
    ("Limiar facial de câmera nova", "FACE_THRESHOLD"),
    ("Limiar de objeto de câmera nova", "OBJECT_THRESHOLD"),
    ("Intervalo da inferência", "INFERENCIA_INTERVALO"),
]


class ValorInvalido(ValueError):
    """Valor recusado antes de encostar em `settings`."""


def _converter(p: Parametro, bruto: Any) -> Any:
    if p.tipo == "perfis":
        nomes = [x.strip().upper() for x in str(bruto).split(",") if x.strip()]
        validos = {x.value for x in Perfil}
        for n in nomes:
            if n not in validos:
                raise ValorInvalido(f"{p.rotulo}: perfil '{n}' não existe.")
        return ",".join(nomes)

    try:
        valor = int(bruto) if p.tipo == "int" else float(bruto)
    except (TypeError, ValueError):
        raise ValorInvalido(f"{p.rotulo}: '{bruto}' não é um número.") from None

    if p.minimo is not None and valor < p.minimo:
        raise ValorInvalido(f"{p.rotulo}: mínimo {_formatar(p, p.minimo)}.")
    if p.maximo is not None and valor > p.maximo:
        raise ValorInvalido(f"{p.rotulo}: máximo {_formatar(p, p.maximo)}.")
    return valor


def _formatar(p: Parametro, valor: float) -> str:
    texto = str(int(valor)) if p.tipo == "int" else f"{valor:.2f}".replace(".", ",")
    return f"{texto} {p.unidade}".strip()


def valor_atual(p: Parametro) -> Any:
    return getattr(settings, p.chave)


async def carregar(db: AsyncSession) -> int:
    """Aplica em `settings` o que está gravado. Chamado na subida."""
    linhas = (await db.execute(select(Configuracao))).scalars().all()
    aplicados = 0
    for linha in linhas:
        p = POR_CHAVE.get(linha.chave)
        if p is None:
            # Parâmetro que deixou de existir numa versão nova: ignorar é
            # melhor que impedir o sistema de subir por causa dele.
            log.warning("Configuração gravada desconhecida, ignorada: %s", linha.chave)
            continue
        try:
            setattr(settings, p.chave, _converter(p, linha.valor))
            aplicados += 1
        except ValorInvalido as exc:
            log.warning("Configuração gravada fora da faixa, ignorada: %s", exc)
    if aplicados:
        log.info("%d parâmetro(s) de configuração aplicados do banco", aplicados)
    return aplicados


async def gravar(db: AsyncSession, mudancas: Dict[str, Any],
                 usuario_id: Optional[uuid.UUID]) -> Dict[str, tuple]:
    """Valida tudo antes de aplicar qualquer coisa.

    Metade aplicada é o pior resultado possível: a tela mostraria sucesso
    parcial e ninguém saberia quais campos valeram.
    """
    desconhecidos = [c for c in mudancas if c not in POR_CHAVE]
    if desconhecidos:
        raise ValorInvalido(f"Parâmetro não configurável: {', '.join(sorted(desconhecidos))}.")

    convertidos = {c: _converter(POR_CHAVE[c], v) for c, v in mudancas.items()}

    agora = datetime.now(timezone.utc)
    alteracoes: Dict[str, tuple] = {}
    for chave, novo in convertidos.items():
        anterior = getattr(settings, chave)
        if anterior == novo:
            continue
        linha = await db.get(Configuracao, chave)
        if linha is None:
            db.add(Configuracao(chave=chave, valor=str(novo),
                                alterado_em=agora, alterado_por=usuario_id))
        else:
            linha.valor = str(novo)
            linha.alterado_em = agora
            linha.alterado_por = usuario_id
        setattr(settings, chave, novo)
        alteracoes[chave] = (anterior, novo)
    return alteracoes


def retrato() -> dict:
    """O que a tela precisa para desenhar e validar do lado de cá."""
    return {
        "parametros": [
            {
                "chave": p.chave, "rotulo": p.rotulo, "grupo": p.grupo,
                "tipo": p.tipo, "unidade": p.unidade, "ajuda": p.ajuda,
                "minimo": p.minimo, "maximo": p.maximo,
                "valor": valor_atual(p),
                # Vai junto para a tela não precisar da permissão de usuários
                # só para saber quais perfis existem.
                "opcoes": [x.value for x in Perfil] if p.tipo == "perfis" else None,
            }
            for p in PARAMETROS
        ],
        "fixos": [
            {"rotulo": rotulo, "variavel": var, "valor": getattr(settings, var)}
            for rotulo, var in FIXOS
        ],
    }
