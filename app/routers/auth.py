"""Autenticação e recuperação de senha em três etapas.

Etapa 1  POST /auth/recuperar          -> gera e envia o código
Etapa 2  POST /auth/verificar-codigo   -> confere o código, não troca a senha
Etapa 3  POST /auth/redefinir-senha    -> exige o código de novo e grava

Decisões de segurança que valem nota:
* Resposta **neutra** na etapa 1: a API responde igual exista ou não a conta,
  para não permitir enumeração de usuários.
* O código vai para o banco como HMAC-SHA256, nunca em claro.
* Tentativas de login e de OTP têm limite por IP (Redis) e por conta (coluna).
* Redefinir a senha revoga as sessões abertas daquela conta.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import settings
from app.database import get_db, get_redis
from app.deps import ip_de, limitar_taxa, usuario_atual
from app.models import SenhaHistorico, TokenRecuperacao, Usuario
from app.schemas import (
    LoginIn, RecuperarIn, RedefinirSenhaIn, RefreshIn, TokenOut,
    TrocarSenhaIn, UsuarioOut, VerificarCodigoIn,
)
from app.security import (
    conferir_otp, criar_access_token, criar_refresh_token, decodificar_token,
    gerar_codigo_otp, hash_otp, hash_senha, precisa_rehash, verificar_senha,
)
from app.services.notificacoes import FalhaNoEnvio, enviar_codigo_recuperacao
from app.tipos import como_utc

log = logging.getLogger("seguranca.auth")
router = APIRouter(prefix="/auth", tags=["Autenticação"])


def _saida_usuario(u: Usuario) -> UsuarioOut:
    dados = UsuarioOut.model_validate(u)
    dados.permissoes = sorted(u.permissoes)
    return dados


def _tokens(u: Usuario) -> TokenOut:
    return TokenOut(
        access_token=criar_access_token(u.id, u.perfil.value),
        refresh_token=criar_refresh_token(u.id),
        expires_in=settings.ACCESS_TOKEN_MINUTES * 60,
        usuario=_saida_usuario(u),
    )


# ======================================================================
# Login
# ======================================================================
@router.post("/login", response_model=TokenOut, summary="Autenticar operador")
async def login(dados: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    ip = ip_de(request)
    await limitar_taxa(f"rl:login:{ip}", limite=20, janela_s=300)

    ident = dados.identificador.strip().lower()
    usuario = (
        await db.execute(
            select(Usuario).where(
                or_(Usuario.email == ident, Usuario.matricula.ilike(ident))
            )
        )
    ).scalar_one_or_none()

    agora = datetime.now(timezone.utc)

    bloqueio = como_utc(usuario.bloqueado_ate) if usuario else None
    if bloqueio and bloqueio > agora:
        restante = int((bloqueio - agora).total_seconds() // 60) + 1
        await audit.registrar(
            db, acao="LOGIN_BLOQUEADO", descricao="Tentativa em conta bloqueada",
            severidade="err", alvo=ident, request=request,
        )
        raise HTTPException(
            status.HTTP_423_LOCKED,
            f"Conta bloqueada por mais {restante} minuto(s). Contate o administrador.",
        )

    if usuario is None or not verificar_senha(dados.senha, usuario.senha_hash):
        if usuario is not None:
            usuario.tentativas_falhas += 1
            restantes = settings.MAX_LOGIN_ATTEMPTS - usuario.tentativas_falhas
            if restantes <= 0:
                usuario.bloqueado_ate = agora + timedelta(minutes=settings.LOCKOUT_MINUTES)
                usuario.tentativas_falhas = 0
                detalhe = f"Conta bloqueada por {settings.LOCKOUT_MINUTES} minutos."
            else:
                detalhe = f"Credenciais inválidas. {restantes} tentativa(s) restante(s)."
        else:
            detalhe = "Credenciais inválidas."

        await audit.registrar(
            db, acao="LOGIN_FALHA", descricao="Tentativa de autenticação rejeitada",
            severidade="err", usuario=usuario, alvo=ident, request=request,
        )
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detalhe)

    if not usuario.ativo:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conta desativada. Procure a chefia imediata.")

    # Sucesso
    usuario.tentativas_falhas = 0
    usuario.bloqueado_ate = None
    usuario.ultimo_acesso = agora
    if precisa_rehash(usuario.senha_hash):
        usuario.senha_hash = hash_senha(dados.senha)

    vencida = (agora - como_utc(usuario.senha_alterada_em)).days > settings.PASSWORD_EXPIRE_DAYS
    if vencida:
        usuario.forcar_troca_senha = True

    await audit.registrar(
        db, acao="LOGIN", descricao="Autenticação bem-sucedida", severidade="ok",
        usuario=usuario, request=request,
    )

    if usuario.forcar_troca_senha:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            "Senha expirada ou marcada para troca. Use o fluxo de redefinição.",
        )
    return _tokens(usuario)


@router.post("/refresh", response_model=TokenOut, summary="Renovar sessão")
async def refresh(dados: RefreshIn, db: AsyncSession = Depends(get_db)):
    try:
        payload = decodificar_token(dados.refresh_token, "refresh")
        usuario = await db.get(Usuario, uuid.UUID(payload["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token inválido ou expirado.")

    if usuario is None or not usuario.ativo:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conta indisponível.")
    return _tokens(usuario)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Encerrar sessão")
async def logout(request: Request, usuario: Usuario = Depends(usuario_atual),
                 db: AsyncSession = Depends(get_db)):
    """Revoga o token atual (lista de revogação com TTL igual ao que resta dele)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            payload = decodificar_token(auth.split(" ", 1)[1], "access")
            ttl = int(payload["exp"] - datetime.now(timezone.utc).timestamp())
            if ttl > 0:
                await get_redis().setex(f"jwt:revogado:{payload['jti']}", ttl, "1")
        except jwt.PyJWTError:
            pass
    await audit.registrar(db, acao="LOGOUT", descricao="Encerramento de sessão",
                          severidade="info", usuario=usuario, request=request)


@router.get("/eu", response_model=UsuarioOut, summary="Dados da sessão atual")
async def eu(usuario: Usuario = Depends(usuario_atual)):
    return _saida_usuario(usuario)


# ======================================================================
# Recuperação de senha — etapa 1
# ======================================================================
@router.post("/recuperar", summary="Etapa 1 — solicitar código")
async def recuperar(dados: RecuperarIn, request: Request, db: AsyncSession = Depends(get_db)):
    ip = ip_de(request)
    await limitar_taxa(f"rl:recuperar:{ip}", limite=5, janela_s=900)

    email = dados.email.strip().lower()
    usuario = (
        await db.execute(select(Usuario).where(Usuario.email == email))
    ).scalar_one_or_none()

    # Resposta idêntica exista ou não a conta — impede enumeração de usuários.
    resposta = {
        "mensagem": "Se houver conta vinculada a este e-mail, o código foi enviado.",
        "expira_em_segundos": settings.OTP_TTL_SECONDS,
    }

    if usuario is None or not usuario.ativo:
        log.info("Recuperação solicitada para e-mail sem conta ativa: %s", email)
        return resposta

    # Invalida códigos anteriores ainda abertos
    anteriores = (
        await db.execute(
            select(TokenRecuperacao).where(
                TokenRecuperacao.usuario_id == usuario.id,
                TokenRecuperacao.usado.is_(False),
            )
        )
    ).scalars().all()
    for t in anteriores:
        t.usado = True

    codigo = gerar_codigo_otp()
    db.add(TokenRecuperacao(
        usuario_id=usuario.id,
        codigo_hash=hash_otp(codigo),
        expira_em=datetime.now(timezone.utc) + timedelta(seconds=settings.OTP_TTL_SECONDS),
        ip_solicitante=ip,
    ))

    entregue, falha = True, ""
    try:
        await enviar_codigo_recuperacao(usuario.email, usuario.nome, codigo,
                                        settings.OTP_TTL_SECONDS // 60)
    except FalhaNoEnvio as exc:
        # Não vira erro HTTP: o endpoint responde igual exista ou não a conta,
        # e um 500 só aqui diria ao curioso que este e-mail está cadastrado.
        # Quem precisa saber da falha é quem opera, pelo log e pela trilha.
        entregue, falha = False, str(exc)
        log.error("Código gerado mas não entregue a %s: %s", usuario.email, falha)

    await audit.registrar(
        db, acao="SENHA_RESET", severidade="warn", usuario=usuario,
        alvo=usuario.email, request=request,
        descricao="Código de redefinição solicitado" if entregue
                  else "Código de redefinição solicitado, mas o e-mail falhou",
        metadados={"entregue": entregue, "erro": falha} if not entregue else None,
    )

    if settings.ENV == "development":
        # Conveniência de desenvolvimento — nunca ativa fora dele.
        resposta["codigo_debug"] = codigo
        if not entregue:
            resposta["erro_envio"] = falha
    return resposta


# ======================================================================
# Recuperação de senha — etapa 2
# ======================================================================
async def _token_valido(db: AsyncSession, email: str, codigo: str) -> tuple[Usuario, TokenRecuperacao]:
    usuario = (
        await db.execute(select(Usuario).where(Usuario.email == email.strip().lower()))
    ).scalar_one_or_none()
    if usuario is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Código inválido ou expirado.")

    token = (
        await db.execute(
            select(TokenRecuperacao)
            .where(TokenRecuperacao.usuario_id == usuario.id, TokenRecuperacao.usado.is_(False))
            .order_by(TokenRecuperacao.criado_em.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if token is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nenhum código pendente. Solicite um novo.")

    if como_utc(token.expira_em) < datetime.now(timezone.utc):
        token.usado = True
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Código expirado. Solicite um novo envio.")

    if token.tentativas >= settings.OTP_MAX_ATTEMPTS:
        token.usado = True
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"{settings.OTP_MAX_ATTEMPTS} tentativas incorretas. Solicite um novo código.",
        )

    if not conferir_otp(codigo, token.codigo_hash):
        token.tentativas += 1
        restantes = settings.OTP_MAX_ATTEMPTS - token.tentativas
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Código incorreto. {max(restantes, 0)} tentativa(s) restante(s).",
        )

    return usuario, token


@router.post("/verificar-codigo", summary="Etapa 2 — conferir código")
async def verificar_codigo(dados: VerificarCodigoIn, request: Request,
                           db: AsyncSession = Depends(get_db)):
    await limitar_taxa(f"rl:otp:{ip_de(request)}", limite=15, janela_s=900)
    usuario, token = await _token_valido(db, dados.email, dados.codigo)
    restante = int((como_utc(token.expira_em) - datetime.now(timezone.utc)).total_seconds())
    return {
        "valido": True,
        "mensagem": "Código validado. Defina a nova senha.",
        "expira_em_segundos": max(restante, 0),
    }


# ======================================================================
# Recuperação de senha — etapa 3
# ======================================================================
@router.post("/redefinir-senha", summary="Etapa 3 — gravar nova senha")
async def redefinir_senha(dados: RedefinirSenhaIn, request: Request,
                          db: AsyncSession = Depends(get_db)):
    await limitar_taxa(f"rl:redefinir:{ip_de(request)}", limite=10, janela_s=900)
    usuario, token = await _token_valido(db, dados.email, dados.codigo)

    # Não pode repetir as últimas N senhas
    historico = (
        await db.execute(
            select(SenhaHistorico)
            .where(SenhaHistorico.usuario_id == usuario.id)
            .order_by(SenhaHistorico.criado_em.desc())
            .limit(settings.PASSWORD_HISTORY)
        )
    ).scalars().all()

    if verificar_senha(dados.nova_senha, usuario.senha_hash) or any(
        verificar_senha(dados.nova_senha, h.senha_hash) for h in historico
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"A nova senha não pode repetir as últimas {settings.PASSWORD_HISTORY} utilizadas.",
        )

    db.add(SenhaHistorico(usuario_id=usuario.id, senha_hash=usuario.senha_hash))
    usuario.senha_hash = hash_senha(dados.nova_senha)
    usuario.senha_alterada_em = datetime.now(timezone.utc)
    usuario.forcar_troca_senha = False
    usuario.tentativas_falhas = 0
    usuario.bloqueado_ate = None
    token.usado = True

    # Poda o histórico
    excedentes = (
        await db.execute(
            select(SenhaHistorico)
            .where(SenhaHistorico.usuario_id == usuario.id)
            .order_by(SenhaHistorico.criado_em.desc())
            .offset(settings.PASSWORD_HISTORY)
        )
    ).scalars().all()
    for h in excedentes:
        await db.delete(h)

    await audit.registrar(
        db, acao="SENHA_RESET", descricao="Senha redefinida com sucesso",
        severidade="warn", usuario=usuario, alvo=usuario.email, request=request,
    )

    return {"mensagem": "Senha redefinida. Faça login com a nova senha."}


@router.post("/trocar-senha", summary="Trocar a própria senha (autenticado)")
async def trocar_senha(dados: TrocarSenhaIn, request: Request,
                       usuario: Usuario = Depends(usuario_atual),
                       db: AsyncSession = Depends(get_db)):
    if not verificar_senha(dados.senha_atual, usuario.senha_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Senha atual incorreta.")

    db.add(SenhaHistorico(usuario_id=usuario.id, senha_hash=usuario.senha_hash))
    usuario.senha_hash = hash_senha(dados.nova_senha)
    usuario.senha_alterada_em = datetime.now(timezone.utc)
    usuario.forcar_troca_senha = False

    await audit.registrar(db, acao="SENHA_RESET", descricao="Senha alterada pelo próprio usuário",
                          severidade="warn", usuario=usuario, request=request)
    return {"mensagem": "Senha alterada."}

