"""Trilha de auditoria append-only com encadeamento por hash.

Cada linha grava `hash_atual = sha256(hash_anterior || payload_canônico)`.
Alterar ou remover uma linha quebra todas as seguintes — `verificar_cadeia()`
detecta onde.

Chamar `registrar()` em: autenticação, consulta a dado biométrico, exportação,
mudança de limiar, criação/edição de pessoa, encerramento de alerta.
"""
from __future__ import annotations

import asyncio
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

# Um append de cada vez.
#
# `registrar` lê a última linha e usa o hash dela como elo. Sem serializar, duas
# requisições concorrentes leem a *mesma* última linha e a segunda grava um elo
# apontando para antes da primeira: a cadeia bifurca, e `verificar_cadeia`
# acusa "a trilha pode ter sido alterada fora da aplicação" sobre uma trilha
# que ninguém tocou.
#
# Não é hipótese. Na trilha deste sistema, 79 colisões envolvendo 162 das 2.152
# linhas — pares gravados com 26 ms de diferença compartilhando o mesmo
# `hash_anterior`. E reproduzido em banco limpo: 12 escritores simultâneos por
# 6 rodadas renderam 66 elos quebrados em 72 linhas.
#
# O custo é desprezível: a trava cobre uma leitura por chave primária e um
# insert, e o SQLite já aceita um escritor de cada vez.
#
# O que ela **não** cobre: mais de um processo escrevendo no mesmo banco. Aqui
# a API é um processo só; num cenário com vários, isto precisa virar trava no
# banco (`SELECT ... FOR UPDATE` sobre uma linha-sentinela).
_trava_cadeia = asyncio.Lock()


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
    ip, ua = contexto_requisicao(request)
    agora = datetime.now(timezone.utc)
    meta = metadados or {}

    # Truncado ANTES de entrar no hash. Hashear o texto inteiro e gravar o
    # corte faria toda descrição acima de 300 caracteres nascer impossível de
    # verificar — `verificar_cadeia()` recalcula a partir do que está gravado.
    descricao = descricao[:300]
    alvo = alvo[:120]
    finalidade = finalidade[:200]

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

    # Ler o elo e gravar a linha é uma operação só: quem entrar no meio lê um
    # elo que já vai estar desatualizado quando gravar. Ver `_trava_cadeia`.
    async with _trava_cadeia:
        ultimo = (
            await db.execute(select(Auditoria).order_by(Auditoria.id.desc()).limit(1))
        ).scalar_one_or_none()
        hash_anterior = ultimo.hash_atual if ultimo else GENESIS

        linha = Auditoria(
            ocorrido_em=agora,
            acao=acao,
            descricao=descricao,
            severidade=severidade,
            usuario_id=usuario.id if usuario else None,
            usuario_email=usuario.email if usuario else "",
            usuario_nome=usuario.nome if usuario else "",
            perfil=usuario.perfil.value if usuario else "",
            alvo=alvo,
            finalidade=finalidade,
            ip=ip,
            user_agent=ua,
            metadados=meta,
            hash_anterior=hash_anterior,
            hash_atual=calcular_hash(hash_anterior, corpo),
        )
        db.add(linha)
        # Dentro da trava: o `flush` é o que torna a linha visível para a
        # leitura seguinte. Soltar antes faria a próxima requisição ler o elo
        # de novo sem enxergar esta, que é exatamente a corrida.
        await db.flush()
    return linha


def _corpo_da_linha(linha: Auditoria) -> dict:
    """O payload que foi hasheado quando a linha nasceu."""
    return {
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


def _vinculo_zerado(linha: Auditoria) -> bool:
    """O `usuario_id` sumiu, mas quem agiu continua gravado.

    Assinatura do `ON DELETE SET NULL`: a conta foi apagada e o banco zerou a
    chave estrangeira nas linhas de auditoria — depois que o hash já a
    cobria. O e-mail e o perfil ficam, então a autoria não se perde; o que se
    perde é o vínculo com a conta, e com ele o hash.

    É o motivo de a exclusão de usuário hoje ser lógica, com identificadores
    neutralizados em vez de `DELETE`. Estas linhas são anteriores a isso.
    """
    return linha.usuario_id is None and bool(linha.usuario_email)


async def verificar_cadeia(db: AsyncSession, limite: int = 10_000) -> VerificacaoCadeia:
    """Recalcula a cadeia do início e separa o que divergiu, por causa.

    Antes isto parava na primeira divergência e dizia sempre a mesma frase —
    "a trilha pode ter sido alterada fora da aplicação" — para causas de
    gravidade muito diferente. Nesta instalação havia as três ao mesmo tempo,
    e a mais grave (conteúdo adulterado) ficava indistinguível das outras
    duas, que são defeito conhecido e não adulteração. Um alarme que não
    separa o grave do benigno acaba ignorado, e aí deixa de alarmar.

    Agora a varredura vai até o fim e conta cada tipo. `integra` segue
    estrito: só verdadeiro quando nada diverge.
    """
    linhas = (
        await db.execute(select(Auditoria).order_by(Auditoria.id.asc()).limit(limite))
    ).scalars().all()

    anterior = GENESIS
    conteudo = elos = vinculos = 0
    primeira: Optional[int] = None

    for linha in linhas:
        corpo = _corpo_da_linha(linha)
        # Contra o elo **gravado**, não contra o recalculado: assim um elo
        # bifurcado não contamina a conferência do conteúdo, que é a que diz
        # se o texto da linha foi reescrito.
        if linha.hash_atual != calcular_hash(linha.hash_anterior, corpo):
            conteudo += 1
            if _vinculo_zerado(linha):
                vinculos += 1
            if primeira is None:
                primeira = linha.id
        if linha.hash_anterior != anterior:
            elos += 1
            if primeira is None:
                primeira = linha.id
        anterior = linha.hash_atual

    if not conteudo and not elos:
        return VerificacaoCadeia(
            integra=True, linhas_verificadas=len(linhas),
            mensagem=f"Cadeia íntegra em {len(linhas)} linha(s).",
        )

    partes = []
    if conteudo:
        adulteradas = conteudo - vinculos
        if adulteradas:
            partes.append(
                f"{adulteradas} linha(s) com conteúdo que não corresponde ao "
                "hash registrado — investigar")
        if vinculos:
            partes.append(
                f"{vinculos} linha(s) perderam o vínculo com a conta porque o "
                "usuário foi excluído do banco; a autoria segue gravada no "
                "e-mail e no perfil")
    if elos:
        partes.append(
            f"{elos} elo(s) apontam para fora da sequência, assinatura de "
            "gravações simultâneas")

    return VerificacaoCadeia(
        integra=False,
        linhas_verificadas=len(linhas),
        primeira_divergencia=primeira,
        conteudo_divergente=conteudo,
        elos_bifurcados=elos,
        vinculo_de_usuario_perdido=vinculos,
        mensagem=f"A partir da linha {primeira}: " + "; ".join(partes) + ".",
    )
