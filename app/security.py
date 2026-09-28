"""Senhas, JWT, OTP e bloqueio por tentativas.

Escolhas:
* Argon2id para senhas (resistente a GPU) via passlib.
* JWT curto para acesso + refresh opaco-ish com `jti` e tipo declarado,
  para que um refresh não possa ser usado como access.
* OTP de recuperação guardado como SHA-256 com pepper — o código em claro
  só existe no e-mail/SMS enviado.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from passlib.context import CryptContext

from app.config import settings

pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
    argon2__time_cost=3,
    argon2__memory_cost=65536,
    argon2__parallelism=4,
)


# ----------------------------------------------------------------------
# Senhas
# ----------------------------------------------------------------------
def hash_senha(senha: str) -> str:
    return pwd_context.hash(senha)


def verificar_senha(senha: str, hash_armazenado: str) -> bool:
    try:
        return pwd_context.verify(senha, hash_armazenado)
    except Exception:
        return False


def precisa_rehash(hash_armazenado: str) -> bool:
    return pwd_context.needs_update(hash_armazenado)


def senha_provisoria(tamanho: int = 14) -> str:
    """Gera senha provisória que já satisfaz a política de complexidade."""
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    minus = "abcdefghijkmnpqrstuvwxyz"
    nums = "23456789"
    simb = "!@#$%&*?-_"
    base = [
        secrets.choice(alfabeto), secrets.choice(minus),
        secrets.choice(nums), secrets.choice(simb),
    ]
    pool = alfabeto + minus + nums + simb
    base += [secrets.choice(pool) for _ in range(max(0, tamanho - 4))]
    secrets.SystemRandom().shuffle(base)
    return "".join(base)


# ----------------------------------------------------------------------
# JWT
# ----------------------------------------------------------------------
def _criar_token(sub: str, tipo: str, expira: timedelta, extra: Optional[dict] = None) -> str:
    agora = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": sub,
        "typ": tipo,
        "iat": agora,
        "nbf": agora,
        "exp": agora + expira,
        "jti": str(uuid.uuid4()),
        "iss": "seguranca",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def criar_access_token(usuario_id: uuid.UUID, perfil: str) -> str:
    return _criar_token(
        str(usuario_id), "access",
        timedelta(minutes=settings.ACCESS_TOKEN_MINUTES),
        {"perfil": perfil},
    )


def criar_refresh_token(usuario_id: uuid.UUID) -> str:
    return _criar_token(str(usuario_id), "refresh", timedelta(days=settings.REFRESH_TOKEN_DAYS))


def decodificar_token(token: str, tipo_esperado: str = "access") -> dict:
    """Levanta jwt.PyJWTError em token inválido, expirado ou de tipo errado."""
    payload = jwt.decode(
        token, settings.SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
        issuer="seguranca",
        options={"require": ["exp", "sub", "typ", "iat"]},
    )
    if payload.get("typ") != tipo_esperado:
        raise jwt.InvalidTokenError(f"Esperado token do tipo '{tipo_esperado}'")
    return payload


# ----------------------------------------------------------------------
# OTP de recuperação
# ----------------------------------------------------------------------
def gerar_codigo_otp() -> str:
    """6 dígitos criptograficamente seguros (inclui os com zero à esquerda)."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(codigo: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), codigo.encode(), hashlib.sha256).hexdigest()


def conferir_otp(codigo: str, hash_armazenado: str) -> bool:
    return hmac.compare_digest(hash_otp(codigo), hash_armazenado)


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def hash_evidencia(conteudo: bytes) -> str:
    """SHA-256 do quadro bruto, para cadeia de custódia da imagem."""
    return hashlib.sha256(conteudo).hexdigest()


def mascarar_cpf(cpf: Optional[str]) -> str:
    if not cpf:
        return ""
    digitos = "".join(c for c in cpf if c.isdigit())
    if len(digitos) != 11:
        return "***"
    return f"***.{digitos[3:6]}.***-**"


def mascarar_rg(rg: Optional[str]) -> str:
    return "**.***.***-*" if rg else ""

