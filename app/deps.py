"""Dependências compartilhadas: usuário autenticado, RBAC e rate limit."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, get_redis
from app.models import Usuario
from app.security import decodificar_token

bearer = HTTPBearer(auto_error=False)

CREDENCIAIS_INVALIDAS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Sessão inválida ou expirada. Autentique-se novamente.",
    headers={"WWW-Authenticate": "Bearer"},
)


async def usuario_atual(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    if cred is None:
        raise CREDENCIAIS_INVALIDAS
    try:
        payload = decodificar_token(cred.credentials, "access")
        usuario_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise CREDENCIAIS_INVALIDAS

    # Lista de revogação (logout / troca de senha invalidam tokens emitidos antes).
    redis = get_redis()
    if await redis.exists(f"jwt:revogado:{payload.get('jti')}"):
        raise CREDENCIAIS_INVALIDAS

    usuario = await db.get(Usuario, usuario_id)
    if usuario is None or not usuario.ativo:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conta desativada. Procure a chefia imediata.")

    if usuario.forcar_troca_senha:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "É necessário redefinir a senha antes de continuar.",
        )
    return usuario


def exige(*permissoes: str) -> Callable:
    """Uso: `Depends(exige("biometrics", "edit"))` — exige TODAS as permissões."""
    async def _checar(usuario: Usuario = Depends(usuario_atual)) -> Usuario:
        faltando = [p for p in permissoes if not usuario.pode(p)]
        if faltando:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Seu perfil ({usuario.perfil.value}) não possui a permissão: {', '.join(faltando)}.",
            )
        return usuario
    return _checar


async def limitar_taxa(chave: str, limite: int, janela_s: int) -> None:
    """Contador simples em Redis. Levanta 429 ao estourar a janela."""
    redis = get_redis()
    atual = await redis.incr(chave)
    if atual == 1:
        await redis.expire(chave, janela_s)
    if atual > limite:
        ttl = await redis.ttl(chave)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Muitas tentativas. Aguarde {max(ttl, 1)} segundo(s).",
            headers={"Retry-After": str(max(ttl, 1))},
        )


def ip_de(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "desconhecido"


def pode_ver_pii(usuario: Usuario) -> bool:
    """Perfis operacionais veem CPF/RG mascarados quando a política exige."""
    return usuario.perfil.value not in settings.mask_roles


def agora() -> datetime:
    return datetime.now(timezone.utc)
