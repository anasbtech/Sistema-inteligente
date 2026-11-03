"""Retenção: apagar o que já passou do prazo, e saber quanto está vencido.

A política existia e o botão existia. O que não existia era quem apertasse o
botão: o endpoint `POST /deteccoes/expurgo` tem no próprio texto "agende via
cron", e a trilha de auditoria desta instalação não tem **uma única** linha
`EXPURGO` em 2.152 registros. Retenção declarada e nunca executada é a pior
das três posições possíveis — pior que não ter política, porque promete por
escrito o que não cumpre.

Dois serviços aqui, e a separação é de propósito:

* `vencidas` só conta. É barato, não apaga nada, e é o que permite avisar no
  arranque que há material vencido esperando. Visibilidade sem risco.
* `expurgar` apaga. É a mesma rotina que o endpoint manual usa — extraída
  para cá justamente para não existirem duas implementações da retenção, que
  é onde elas divergem e uma delas passa a apagar o que a outra guarda.

O laço automático vem desligado. Apagar é irreversível, e num sistema de
segurança prisional quem decide quando a política começa a valer de fato é
quem responde por ela — não o código. Com `RETENCAO_INTERVALO_HORAS` em zero
o sistema apenas avisa; acima de zero, executa.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import settings
from app.database import db_context
from app.models import Deteccao
from app.services import evidencias

log = logging.getLogger("seguranca.retencao")

# Teto por execução. O expurgo compete com a inferência pelo escritor único do
# SQLite, e apagar dezenas de milhares de linhas numa transação só seguraria a
# trava por tempo demais — a ficha biométrica, que também escreve (auditoria),
# ficaria na fila. Com laço periódico, sobra para a rodada seguinte.
LOTE = 5000


def _criterio():
    """O que pode ser apagado — e, por omissão, o que nunca pode.

    Fora do alcance do expurgo, de propósito:

    * captura **vinculada a uma pessoa** (`pessoa_id`), porque deixou de ser
      passagem anônima e pode integrar inquérito;
    * captura **revisada** por um operador, pelo mesmo motivo;
    * captura **sem prazo** (`expurgo_em` nulo), que é o caso de tudo que foi
      promovido a registro identificado.

    O prazo é gravado na própria linha quando ela nasce, não calculado aqui:
    mudar a configuração depois não pode encurtar retroativamente o prazo de
    material já coletado.
    """
    agora = datetime.now(timezone.utc)
    return (
        Deteccao.expurgo_em.isnot(None),
        Deteccao.expurgo_em <= agora,
        Deteccao.pessoa_id.is_(None),
        Deteccao.revisado.is_(False),
    )


async def vencidas(db: AsyncSession) -> int:
    """Quantas capturas já passaram do prazo e seguem gravadas."""
    return int((await db.execute(
        select(func.count()).select_from(Deteccao).where(*_criterio())
    )).scalar_one())


async def expurgar(db: AsyncSession, usuario=None, origem: str = "manual") -> dict:
    """Apaga as capturas vencidas — registro e recorte, juntos.

    Apagar a linha e deixar o JPEG no disco transformaria a retenção em
    promessa vazia: a imagem é o dado pessoal, a linha é só o índice dela.
    """
    alvos = (await db.execute(
        select(Deteccao).where(*_criterio()).limit(LOTE)
    )).scalars().unique().all()

    imagens = 0
    for det in alvos:
        if evidencias.apagar(det.snapshot_key):
            imagens += 1
        await db.delete(det)

    if alvos:
        await audit.registrar(
            db, acao="EXPURGO",
            descricao="Expurgo de capturas não identificadas vencidas",
            severidade="warn", usuario=usuario,
            alvo=f"{len(alvos)} registro(s), {imagens} recorte(s)",
            finalidade=(f"Retenção de {settings.RETENTION_DAYS_UNMATCHED} dias "
                        f"({origem})"),
        )
    return {"removidos": len(alvos), "imagens_removidas": imagens,
            "prazo_dias": settings.RETENTION_DAYS_UNMATCHED}


class Retencao:
    """Laço que executa a política no prazo dela, se estiver ligado."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self.ultima_execucao: str = ""
        self.removidos_no_total = 0

    async def iniciar(self) -> None:
        # Sempre avisa, mesmo desligado: material vencido acumulando em
        # silêncio é o estado que esta nota existe para não deixar repetir.
        async with db_context() as db:
            pendentes = await vencidas(db)

        if settings.RETENCAO_INTERVALO_HORAS <= 0:
            if pendentes:
                log.warning(
                    "%d captura(s) passaram do prazo de retenção de %d dias e "
                    "seguem gravadas. O expurgo automático está desligado — "
                    "ligue em RETENCAO_INTERVALO_HORAS, ou execute "
                    "POST /deteccoes/expurgo.",
                    pendentes, settings.RETENTION_DAYS_UNMATCHED,
                )
            else:
                log.info(
                    "Retenção: nada vencido. Expurgo automático desligado "
                    "(RETENCAO_INTERVALO_HORAS=0).")
            return

        log.info("Retenção automática a cada %dh (%d vencida(s) agora)",
                 settings.RETENCAO_INTERVALO_HORAS, pendentes)
        self._task = asyncio.create_task(self._loop())

    async def parar(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # Depois do arranque: o expurgo escreve, e disputar o banco com a
        # subida da API atrasaria a primeira tela.
        await asyncio.sleep(60)
        while True:
            try:
                async with db_context() as db:
                    saida = await expurgar(db, origem="automático")
                    await db.commit()
                if saida["removidos"]:
                    self.removidos_no_total += saida["removidos"]
                    self.ultima_execucao = datetime.now(timezone.utc).isoformat()
                    log.info("Expurgo: %d registro(s) e %d recorte(s) removidos",
                             saida["removidos"], saida["imagens_removidas"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:                         # noqa: BLE001
                # Contado e logado, nunca silencioso: uma política de retenção
                # que falha calada é indistinguível de uma que funciona.
                log.error("Expurgo falhou: %s", exc)
            await asyncio.sleep(settings.RETENCAO_INTERVALO_HORAS * 3600)


retencao = Retencao()
