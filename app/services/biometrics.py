"""Emissão e gestão do identificador biométrico permanente.

REGRA CENTRAL DO SISTEMA
------------------------
O identificador tem a forma ``BIO-<M|F|N>-NNNNNN`` e é emitido **uma única
vez**, no cadastro da pessoa, a partir do sexo informado no registro civil.

Três consequências, todas implementadas aqui:

1. **O motor nunca emite ID.** O reconhecimento facial devolve o vizinho mais
   próximo já existente na base (ou nada). Ele não classifica sexo a partir da
   imagem, então não há como a mesma pessoa aparecer ora como BIO-M ora como
   BIO-F entre capturas.
2. **Sequências independentes por prefixo.** BIO-M-000001 e BIO-F-000001
   coexistem; cada contador anda sozinho, sob trava de linha.
3. **Corrigir o sexo preserva o ID.** `Pessoa.sexo` muda; `Pessoa.sexo_emissao`
   e `Pessoa.id_biometrico` permanecem, para não órfãos no histórico de
   capturas. A correção fica registrada na trilha de auditoria.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pessoa, SequenciaBiometrica, Sexo
from app.tipos import travar_linha

FORMATO_ID = re.compile(r"^BIO-([MFN])-(\d{6})$")

PREFIXO = {Sexo.M: "BIO-M", Sexo.F: "BIO-F", Sexo.N: "BIO-N"}

ROTULO_SEXO = {Sexo.M: "Masculino", Sexo.F: "Feminino", Sexo.N: "Não informado"}


class IdentificadorInvalido(ValueError):
    pass


async def emitir_id_biometrico(db: AsyncSession, sexo: Sexo) -> str:
    """Reserva o próximo número da sequência do sexo informado.

    Usa ``SELECT ... FOR UPDATE`` na linha do contador: dois cadastros
    simultâneos do mesmo sexo serializam em vez de colidir. A unicidade final
    ainda é garantida pelo índice único em ``pessoas.id_biometrico``.
    """
    linha = (
        await db.execute(
            travar_linha(
                select(SequenciaBiometrica).where(SequenciaBiometrica.sexo == sexo),
                db,
            )
        )
    ).scalar_one_or_none()

    if linha is None:
        linha = SequenciaBiometrica(sexo=sexo, proximo=1)
        db.add(linha)
        await db.flush()

    numero = linha.proximo
    linha.proximo = numero + 1
    await db.flush()

    return f"{PREFIXO[sexo]}-{numero:06d}"


async def proximo_id_previsto(db: AsyncSession, sexo: Sexo) -> str:
    """Somente leitura — o que a interface mostra antes de confirmar o cadastro."""
    linha = (
        await db.execute(
            select(SequenciaBiometrica).where(SequenciaBiometrica.sexo == sexo)
        )
    ).scalar_one_or_none()
    proximo = linha.proximo if linha else 1
    return f"{PREFIXO[sexo]}-{proximo:06d}"


async def contagem_por_sexo(db: AsyncSession) -> dict[str, dict]:
    """Resumo usado no painel: quantos registros e qual o próximo ID de cada prefixo."""
    from sqlalchemy import func

    contagens = dict(
        (await db.execute(select(Pessoa.sexo, func.count()).group_by(Pessoa.sexo))).all()
    )
    saida: dict[str, dict] = {}
    for sexo in Sexo:
        saida[sexo.value] = {
            "prefixo": PREFIXO[sexo],
            "rotulo": ROTULO_SEXO[sexo],
            "registros": int(contagens.get(sexo, 0)),
            "proximo": await proximo_id_previsto(db, sexo),
        }
    return saida


def validar_id(id_biometrico: str) -> tuple[Sexo, int]:
    m = FORMATO_ID.match(id_biometrico or "")
    if not m:
        raise IdentificadorInvalido(
            f"'{id_biometrico}' não segue o formato BIO-<M|F|N>-NNNNNN"
        )
    return Sexo(m.group(1)), int(m.group(2))


def sexo_do_id(id_biometrico: str) -> Sexo:
    return validar_id(id_biometrico)[0]


async def corrigir_sexo(db: AsyncSession, pessoa: Pessoa, novo_sexo: Sexo) -> dict:
    """Corrige o sexo do cadastro **sem** reemitir o identificador.

    Devolve um resumo para a trilha de auditoria. Se o novo sexo divergir do
    prefixo emitido, isso é esperado e documentado: o prefixo registra o que
    constava no ato da emissão, não o estado atual do cadastro.
    """
    anterior = pessoa.sexo
    if anterior == novo_sexo:
        return {"alterado": False, "id_biometrico": pessoa.id_biometrico}

    pessoa.sexo = novo_sexo  # sexo_emissao e id_biometrico permanecem intactos

    return {
        "alterado": True,
        "id_biometrico": pessoa.id_biometrico,
        "sexo_anterior": anterior.value,
        "sexo_atual": novo_sexo.value,
        "prefixo_emissao": PREFIXO[pessoa.sexo_emissao],
        "observacao": (
            "Identificador preservado por decisão de projeto: reemitir quebraria o "
            "vínculo com as capturas já registradas."
        ),
    }


async def sincronizar_sequencias(db: AsyncSession) -> dict[str, int]:
    """Realinha os contadores ao maior número já usado por prefixo.

    Útil depois de uma carga em massa ou restauração de backup.
    """
    from sqlalchemy import func

    resultado: dict[str, int] = {}
    for sexo in Sexo:
        maior = (
            await db.execute(
                select(
                    func.max(func.cast(func.substr(Pessoa.id_biometrico, 7), __import__("sqlalchemy").Integer))
                ).where(Pessoa.id_biometrico.like(f"{PREFIXO[sexo]}-%"))
            )
        ).scalar()
        proximo = (maior or 0) + 1

        linha = (
            await db.execute(
                travar_linha(
                    select(SequenciaBiometrica).where(SequenciaBiometrica.sexo == sexo),
                    db,
                )
            )
        ).scalar_one_or_none()
        if linha is None:
            db.add(SequenciaBiometrica(sexo=sexo, proximo=proximo))
        else:
            linha.proximo = max(linha.proximo, proximo)
        resultado[PREFIXO[sexo]] = proximo

    await db.flush()
    return resultado
