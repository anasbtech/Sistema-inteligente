"""Emissão de códigos sequenciais (detecção, alerta).

Um contador por série, reservado sob trava de linha. Não se deriva número de
sequência de ``COUNT(*)``: contagem cai quando alguém apaga um registro, e a
série passa a reemitir código que já existe.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SequenciaCodigo
from app.tipos import travar_linha


async def proximo_numero(
    db: AsyncSession,
    chave: str,
    semente: Optional[Callable[[], Awaitable[int]]] = None,
    em_uso: Optional[Callable[[int], Awaitable[bool]]] = None,
) -> int:
    """Reserva e devolve o próximo número da série ``chave``.

    ``semente`` é consultada uma única vez, quando o contador ainda não
    existe, e deve devolver o maior número já emitido na série. É o que faz
    a migração do esquema antigo ser silenciosa: o contador nasce acima do
    que já está gravado, em vez de recomeçar do começo e colidir.

    ``em_uso`` diz se um número já foi gravado. Uma importação que insere
    registros sem adiantar o contador o deixa atrás do que existe — e a
    semente não corrige isso, porque só roda quando o contador não existe.
    A série então reemite código que já está gravado e **toda** inserção
    passa a falhar por unicidade, sem nada além de um aviso no log. Quando
    isso é detectado, o contador é ressincronizado pela semente.
    """
    linha = (
        await db.execute(
            travar_linha(
                select(SequenciaCodigo).where(SequenciaCodigo.chave == chave), db
            )
        )
    ).scalar_one_or_none()

    if linha is None:
        inicio = await semente() if semente is not None else 0
        linha = SequenciaCodigo(chave=chave, proximo=inicio + 1)
        db.add(linha)
        await db.flush()

    numero = linha.proximo
    if em_uso is not None and semente is not None and await em_uso(numero):
        numero = await semente() + 1

    linha.proximo = numero + 1
    await db.flush()
    return numero
