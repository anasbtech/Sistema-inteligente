"""Gestão de usuários, perfis e matriz de permissões."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige
from app.models import PERMISSOES, Perfil, Usuario
from app.schemas import UsuarioCreate, UsuarioOut, UsuarioUpdate
from app.security import hash_senha, senha_provisoria

router = APIRouter(prefix="/usuarios", tags=["Usuários"])


def _saida(u: Usuario) -> UsuarioOut:
    dados = UsuarioOut.model_validate(u)
    dados.permissoes = sorted(u.permissoes)
    return dados


@router.get("/perfis", summary="Matriz de permissões por perfil")
async def perfis(usuario: Usuario = Depends(exige("users"))):
    return {
        "perfis": [
            {"codigo": p.value, "permissoes": sorted(perms)}
            for p, perms in PERMISSOES.items()
        ],
        "permissoes": sorted({x for perms in PERMISSOES.values() for x in perms}),
    }


@router.get("", response_model=List[UsuarioOut], summary="Listar usuários")
async def listar(q: Optional[str] = None, db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("users"))):
    filtros = []
    if q:
        termo = f"%{q.strip()}%"
        filtros.append(or_(Usuario.nome.ilike(termo), Usuario.email.ilike(termo),
                           Usuario.matricula.ilike(termo), Usuario.unidade.ilike(termo)))
    linhas = (
        await db.execute(select(Usuario).where(*filtros).order_by(Usuario.nome))
    ).scalars().all()
    return [_saida(u) for u in linhas]


@router.post("", response_model=UsuarioOut, status_code=status.HTTP_201_CREATED,
             summary="Criar usuário")
async def criar(dados: UsuarioCreate, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("users"))):
    email = dados.email.strip().lower()
    existente = (
        await db.execute(
            select(Usuario).where(or_(Usuario.email == email,
                                      Usuario.matricula == dados.matricula))
        )
    ).scalar_one_or_none()
    if existente:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Já existe usuário com este e-mail ou matrícula.")

    provisoria = dados.senha or senha_provisoria()
    novo = Usuario(
        nome=dados.nome.strip(), email=email, matricula=dados.matricula,
        senha_hash=hash_senha(provisoria), perfil=dados.perfil,
        unidade=dados.unidade, telefone=dados.telefone,
        ativo=dados.ativo, mfa_ativo=dados.mfa_ativo,
        forcar_troca_senha=dados.senha is None,
        senha_alterada_em=datetime.now(timezone.utc),
    )
    db.add(novo)
    await db.flush()

    await audit.registrar(db, acao="USR_ADD", descricao="Usuário criado", severidade="warn",
                          usuario=usuario, alvo=novo.email, request=request,
                          metadados={"perfil": novo.perfil.value})

    saida = _saida(novo)
    if dados.senha is None:
        # Entregue por canal seguro; o usuário troca no primeiro acesso.
        return {**saida.model_dump(), "senha_provisoria": provisoria}
    return saida


@router.patch("/{usuario_id}", response_model=UsuarioOut, summary="Editar usuário")
async def atualizar(usuario_id: uuid.UUID, dados: UsuarioUpdate, request: Request,
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("users"))):
    alvo = await db.get(Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuário não encontrado.")

    campos = dados.model_dump(exclude_unset=True)

    # Trava contra auto-rebaixamento: o último admin ativo não pode se remover.
    if alvo.id == usuario.id and (campos.get("perfil") not in (None, Perfil.ADMIN)
                                  or campos.get("ativo") is False):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Você não pode rebaixar ou desativar a própria conta.")

    perfil_antigo = alvo.perfil
    for campo, valor in campos.items():
        setattr(alvo, campo, valor.strip().lower() if campo == "email" and valor else valor)

    await audit.registrar(
        db, acao="USR_EDIT", descricao="Cadastro ou permissões de usuário alterados",
        severidade="warn", usuario=usuario, alvo=alvo.email, request=request,
        metadados={"campos": sorted(campos.keys()),
                   "perfil_anterior": perfil_antigo.value,
                   "perfil_atual": alvo.perfil.value},
    )
    return _saida(alvo)


@router.post("/{usuario_id}/forcar-troca-senha", summary="Exigir nova senha no próximo acesso")
async def forcar_troca(usuario_id: uuid.UUID, request: Request,
                       db: AsyncSession = Depends(get_db),
                       usuario: Usuario = Depends(exige("users"))):
    alvo = await db.get(Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuário não encontrado.")

    alvo.forcar_troca_senha = True
    await audit.registrar(db, acao="SENHA_RESET",
                          descricao="Troca de senha forçada pelo administrador",
                          severidade="warn", usuario=usuario, alvo=alvo.email, request=request)
    return {"mensagem": f"{alvo.nome} deverá redefinir a senha no próximo acesso."}


@router.delete("/{usuario_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Desativar usuário")
async def desativar(usuario_id: uuid.UUID, request: Request,
                    motivo: str = Query(..., min_length=8),
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("users"))):
    """Desativação lógica — contas nunca são apagadas, para preservar o vínculo
    com a trilha de auditoria."""
    alvo = await db.get(Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuário não encontrado.")
    if alvo.id == usuario.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Você não pode desativar a própria conta.")

    alvo.ativo = False
    await audit.registrar(db, acao="USR_DEL", descricao="Usuário desativado", severidade="warn",
                          usuario=usuario, alvo=alvo.email, finalidade=motivo, request=request)
