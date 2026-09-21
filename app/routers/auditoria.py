"""Consulta e verificação da trilha de auditoria.

A trilha não tem rota de escrita nem de exclusão — só a aplicação grava, por
`audit.registrar()`, dentro da mesma transação da operação auditada.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige
from app.models import Auditoria, Usuario
from app.schemas import AuditoriaOut, Pagina, VerificacaoCadeia

router = APIRouter(prefix="/auditoria", tags=["Auditoria"])


@router.get("", response_model=Pagina, summary="Consultar trilha")
async def listar(
    q: Optional[str] = None,
    acao: Optional[str] = None,
    usuario_email: Optional[str] = None,
    desde: Optional[datetime] = None,
    ate: Optional[datetime] = None,
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("audit")),
):
    filtros = []
    if q:
        termo = f"%{q.strip()}%"
        filtros.append(or_(
            Auditoria.usuario_nome.ilike(termo), Auditoria.usuario_email.ilike(termo),
            Auditoria.acao.ilike(termo), Auditoria.descricao.ilike(termo),
            Auditoria.alvo.ilike(termo), Auditoria.ip.ilike(termo),
        ))
    if acao:
        filtros.append(Auditoria.acao == acao)
    if usuario_email:
        filtros.append(Auditoria.usuario_email == usuario_email.lower())
    if desde:
        filtros.append(Auditoria.ocorrido_em >= desde)
    if ate:
        filtros.append(Auditoria.ocorrido_em <= ate)

    total = (await db.execute(
        select(func.count()).select_from(Auditoria).where(*filtros)
    )).scalar_one()
    linhas = (
        await db.execute(
            select(Auditoria).where(*filtros).order_by(Auditoria.id.desc())
            .offset((pagina - 1) * por_pagina).limit(por_pagina)
        )
    ).scalars().all()

    return Pagina(total=total, pagina=pagina, por_pagina=por_pagina,
                  itens=[AuditoriaOut.model_validate(a) for a in linhas])


@router.get("/acoes", summary="Ações distintas registradas")
async def acoes(db: AsyncSession = Depends(get_db), usuario: Usuario = Depends(exige("audit"))):
    linhas = (
        await db.execute(select(Auditoria.acao, func.count())
                         .group_by(Auditoria.acao).order_by(func.count().desc()))
    ).all()
    return [{"acao": a, "total": n} for a, n in linhas]


@router.get("/verificar", response_model=VerificacaoCadeia,
            summary="Verificar integridade do encadeamento")
async def verificar(limite: int = Query(10000, ge=1, le=200000),
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("audit"))):
    """Recalcula toda a cadeia de hashes e aponta a primeira divergência.

    Rode periodicamente por cron e alarme em caso de falha — é o que dá valor
    probatório à trilha.
    """
    return await audit.verificar_cadeia(db, limite)


@router.get("/exportar", summary="Exportar trilha em CSV")
async def exportar(request: Request, desde: Optional[datetime] = None,
                   ate: Optional[datetime] = None,
                   finalidade: str = Query(..., min_length=8,
                                           description="Justificativa — vai para a própria trilha"),
                   db: AsyncSession = Depends(get_db),
                   usuario: Usuario = Depends(exige("audit", "export"))):
    filtros = []
    if desde:
        filtros.append(Auditoria.ocorrido_em >= desde)
    if ate:
        filtros.append(Auditoria.ocorrido_em <= ate)

    linhas = (
        await db.execute(select(Auditoria).where(*filtros).order_by(Auditoria.id))
    ).scalars().all()

    buffer = io.StringIO()
    escritor = csv.writer(buffer, delimiter=";")
    escritor.writerow(["id", "ocorrido_em", "acao", "descricao", "severidade",
                       "usuario", "email", "perfil", "alvo", "finalidade",
                       "ip", "hash_anterior", "hash_atual"])
    for a in linhas:
        escritor.writerow([a.id, a.ocorrido_em.isoformat(), a.acao, a.descricao,
                           a.severidade, a.usuario_nome, a.usuario_email, a.perfil,
                           a.alvo, a.finalidade, a.ip, a.hash_anterior, a.hash_atual])
    buffer.seek(0)

    # A própria exportação é auditada.
    await audit.registrar(db, acao="EXPORTACAO",
                          descricao=f"Exportação da trilha ({len(linhas)} linhas)",
                          severidade="warn", usuario=usuario,
                          alvo=f"{len(linhas)} registro(s)", finalidade=finalidade,
                          request=request)

    nome = f"auditoria_{datetime.now():%Y%m%d_%H%M%S}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )
