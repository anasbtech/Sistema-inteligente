"""Trilha de auditoria append-only com encadeamento por hash.

Cada linha grava `hash_atual = sha256(hash_anterior || payload_canônico)`.
Alterar ou remover uma linha quebra todas as seguintes — `verificar_cadeia()`
detecta onde.

Chamar `registrar()` em: autenticação, consulta a dado biométrico, exportação,
mudança de limiar, criação/edição de pessoa, encerramento de alerta.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Auditoria, Usuario
from app.tipos import como_utc
from app.schemas import VerificacaoCadeia

GENESIS = "0" * 64


def _canonico(dados: dict) -> str:
    """Serialização estável — ordena chaves e não deixa espaço supérfluo."""
    return json.dumps(dados, sort_keys=True, separators=(",", ":"), default=str)


def calcular_hash(hash_anterior: str, dados: dict) -> str:
    return hashlib.sha256((hash_anterior + _canonico(dados)).encode()).hexdigest()


def contexto_requisicao(request: Optional[Request]) -> tuple[str, str]:
    """IP real (respeitando X-Forwarded-For do proxy) e user-agent."""
    if request is None:
        return "", ""
    xff = request.headers.get("x-forwarded-for", "")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
    return ip, request.headers.get("user-agent", "")[:300]


async def registrar(
    db: AsyncSession,
    *,
    acao: str,
    descricao: str,
    severidade: str = "info",
    usuario: Optional[Usuario] = None,
    alvo: str = "",
    finalidade: str = "",
    request: Optional[Request] = None,
    metadados: Optional[dict] = None,
) -> Auditoria:
    """Insere uma linha na trilha. Não faz commit — segue a transação do chamador."""
    ultimo = (
        await db.execute(select(Auditoria).order_by(Auditoria.id.desc()).limit(1))
    ).scalar_one_or_none()
    hash_anterior = ultimo.hash_atual if ultimo else GENESIS

    ip, ua = contexto_requisicao(request)
    agora = datetime.now(timezone.utc)
    meta = metadados or {}

    corpo = {
        "ocorrido_em": agora.isoformat(),
        "acao": acao,
        "descricao": descricao,
        "severidade": severidade,
        "usuario_id": str(usuario.id) if usuario else None,
        "usuario_email": usuario.email if usuario else "",
        "perfil": usuario.perfil.value if usuario else "",
        "alvo": alvo,
        "finalidade": finalidade,
        "ip": ip,
        "metadados": meta,
    }

    linha = Auditoria(
        ocorrido_em=agora,
        acao=acao,
        descricao=descricao[:300],
        severidade=severidade,
        usuario_id=usuario.id if usuario else None,
        usuario_email=usuario.email if usuario else "",
        usuario_nome=usuario.nome if usuario else "",
        perfil=usuario.perfil.value if usuario else "",
        alvo=alvo[:120],
        finalidade=finalidade[:200],
        ip=ip,
        user_agent=ua,
        metadados=meta,
        hash_anterior=hash_anterior,
        hash_atual=calcular_hash(hash_anterior, corpo),
    )
    db.add(linha)
    await db.flush()
    return linha


async def verificar_cadeia(db: AsyncSession, limite: int = 10_000) -> VerificacaoCadeia:
    """Recalcula a cadeia do início e aponta a primeira linha divergente."""
    linhas = (
        await db.execute(select(Auditoria).order_by(Auditoria.id.asc()).limit(limite))
    ).scalars().all()

    anterior = GENESIS
    for linha in linhas:
        corpo = {
            # `como_utc` porque o SQLite devolve a data sem fuso; sem isso o
            # hash recalculado nunca bateria com o gravado.
            "ocorrido_em": como_utc(linha.ocorrido_em).isoformat(),
            "acao": linha.acao,
            "descricao": linha.descricao,
            "severidade": linha.severidade,
            "usuario_id": str(linha.usuario_id) if linha.usuario_id else None,
            "usuario_email": linha.usuario_email,
            "perfil": linha.perfil,
            "alvo": linha.alvo,
            "finalidade": linha.finalidade,
            "ip": linha.ip,
            "metadados": linha.metadados or {},
        }
        esperado = calcular_hash(anterior, corpo)
        if linha.hash_anterior != anterior or linha.hash_atual != esperado:
            return VerificacaoCadeia(
                integra=False,
                linhas_verificadas=len(linhas),
                primeira_divergencia=linha.id,
                mensagem=(
                    f"Divergência na linha {linha.id}: o conteúdo não corresponde ao hash "
                    "registrado. A trilha pode ter sido alterada fora da aplicação."
                ),
            )
        anterior = linha.hash_atual

    return VerificacaoCadeia(
        integra=True,
        linhas_verificadas=len(linhas),
        mensagem=f"Cadeia íntegra em {len(linhas)} linha(s).",
    )
