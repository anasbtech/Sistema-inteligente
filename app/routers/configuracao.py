"""Leitura e ajuste dos parâmetros do sistema."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige
from app.models import Usuario
from app.services import configuracao

router = APIRouter(prefix="/configuracao", tags=["Configuração"])


class AjusteIn(BaseModel):
    valores: Dict[str, Any] = Field(..., min_length=1)


@router.get("", summary="Parâmetros do sistema e suas faixas")
async def ler(usuario: Usuario = Depends(exige("settings"))):
    return configuracao.retrato()


@router.put("", summary="Ajustar parâmetros do sistema")
async def ajustar(dados: AjusteIn, request: Request,
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("settings"))):
    try:
        alteracoes = await configuracao.gravar(db, dados.valores, usuario.id)
    except configuracao.ValorInvalido as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None

    if alteracoes:
        await audit.registrar(
            db, acao="CONFIG_EDIT", severidade="warn", usuario=usuario, request=request,
            descricao="Parâmetros do sistema alterados",
            alvo=", ".join(sorted(alteracoes)),
            metadados={c: {"de": a, "para": n} for c, (a, n) in alteracoes.items()},
        )

    return {"alterados": sorted(alteracoes), **configuracao.retrato()}
