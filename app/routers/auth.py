"""Autenticação e recuperação de senha em três etapas.

Etapa 1  POST /auth/recuperar          -> gera e envia o código
Etapa 2  POST /auth/verificar-codigo   -> confere o código, não troca a senha
Etapa 3  POST /auth/redefinir-senha    -> exige o código de novo e grava

Decisões de segurança que valem nota:
* Etapa 1 responde **igual** exista ou não a conta — no texto, nas chaves e
  no tempo. Houve uma versão que dizia "não há conta" na tela; ela entregava
  a quem estivesse na rede a lista de quais e-mails têm cadastro, testando um
  por um. Para o tempo também não contar, o e-mail sai em segundo plano,
  depois da resposta. Pedido para endereço sem conta vai para a trilha, com a
  severidade subindo na repetição vinda do mesmo lugar.
* O link **nunca** volta na resposta, nem em desenvolvimento. Voltava, como
  atalho, e com o servidor aberto na rede isso deixava qualquer um trocar a
  senha de qualquer conta só sabendo o e-mail. Sem SMTP, o link sai no log
  do servidor (em desenvolvimento) ou o administrador o emite pela tela de
  usuários.
* Endereço sem conta **não dispara e-mail nenhum**. Despachar mensagem para
  endereço arbitrário, informado por quem não está autenticado, faria do
  sistema um relé de spam assinado por ele mesmo — e alcançaria quem nunca
  pediu nada.
* O código vai para o banco como HMAC-SHA256, nunca em claro.
* Tentativas de login e de OTP têm limite por IP (Redis) e por conta (coluna).
* Redefinir a senha revoga as sessões abertas daquela conta.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import settings
from app.database import db_context, get_db, get_redis
from app.deps import ip_de, limitar_taxa, usuario_atual
from app.models import SenhaHistorico, TokenRecuperacao, Usuario
from app.schemas import (
    LoginIn, RecuperarIn, RedefinirSenhaIn, RefreshIn, TokenOut,
    TrocarSenhaIn, UsuarioOut, VerificarLinkIn,
)
from app.security import (
    criar_access_token, criar_refresh_token, decodificar_token,
    gerar_token_link, hash_otp, hash_senha, precisa_rehash, verificar_senha,
)
from app.services.notificacoes import (
    EmailNaoConfigurado, FalhaNoEnvio, enviar_link_recuperacao,
)
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Conta desativada. Procure o administrador do sistema.")

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
def _resposta_neutra() -> dict:
    """A mesma resposta para quem tem conta e para quem não tem.

    Nada aqui pode depender da conta existir — nem uma chave a mais. É o que
    impede alguém na rede de descobrir, e-mail por e-mail, quem está
    cadastrado. Inclusive o desfecho do envio fica de fora: "o e-mail falhou"
    só pode ser dito de uma conta que existe.
    """
    return {
        "mensagem": ("Se houver uma conta ativa com este e-mail, enviamos um "
                     "link de redefinição. Confira a caixa de entrada e o spam."),
        "expira_em_segundos": settings.RESET_LINK_TTL_SECONDS,
    }


async def _enviar_em_segundo_plano(email: str, nome: str, link: str) -> None:
    """Entrega o link depois que a resposta já saiu.

    Esperar o SMTP dentro da requisição fazia o pedido com conta levar
    segundos e o sem conta voltar na hora — a diferença de tempo contava o
    que a resposta neutra esconde.

    O desfecho vai para o log e para a trilha, que é onde quem opera olha.
    """
    envio, falha = "enviado", ""
    try:
        await enviar_link_recuperacao(email, nome, link,
                                      settings.RESET_LINK_TTL_SECONDS // 60)
    except EmailNaoConfigurado as exc:
        envio, falha = "nao_configurado", str(exc)
        log.warning("Link gerado para %s — envio de e-mail não configurado (%s). "
                    "Peça a um administrador que emita o link pela tela de usuários.",
                    email, falha)
    except FalhaNoEnvio as exc:
        envio, falha = "falhou", str(exc)
        log.error("Link gerado mas não entregue a %s: %s", email, falha)
    except Exception as exc:                                 # noqa: BLE001
        envio, falha = "falhou", str(exc)
        log.exception("Erro inesperado ao enviar o link para %s", email)

    if envio == "enviado":
        return
    try:
        async with db_context() as db:
            await audit.registrar(
                db, acao="SENHA_RESET_FALHA_ENVIO", severidade="warn", alvo=email,
                descricao={
                    "nao_configurado": "Link de redefinição gerado — envio de e-mail não configurado",
                    "falhou": "Link de redefinição gerado, mas o e-mail falhou",
                }[envio],
                metadados={"envio": envio, "motivo": falha},
            )
    except Exception:                                        # noqa: BLE001
        log.exception("Não foi possível registrar a falha de envio na trilha")


async def _registrar_sem_conta(db: AsyncSession, email: str, ip: str,
                               request: Request) -> None:
    """Deixa na trilha cada pedido para endereço sem conta.

    A tela não conta nada — responde igual para os dois casos. Mas quem tenta
    endereço por endereço continua tentando, e sem este registro a sondagem
    seria silenciosa. Uma tentativa isolada é rotina — gente
    erra a digitação do próprio e-mail. O que denuncia varredura é a
    repetição vinda do mesmo lugar, e é isso que a severidade marca, para
    quem audita não precisar ler linha por linha.

    O teto de 5 por IP a cada 15 minutos continua valendo por cima: quem
    insiste colhe 429, que também fica registrado.
    """
    try:
        tentativas = int(await get_redis().get(f"rl:recuperar:{ip}") or 1)
    except Exception:                                        # noqa: BLE001
        # O contador é conveniência para a severidade, não a razão do
        # registro: sem ele a linha ainda tem de nascer.
        tentativas = 1

    await audit.registrar(
        db, acao="SENHA_RESET_SEM_CONTA",
        descricao="Pedido de redefinição para e-mail sem conta ativa",
        severidade="warn" if tentativas >= 3 else "info",
        alvo=email, request=request,
        metadados={"tentativas_na_janela": tentativas},
    )


@router.post("/recuperar", summary="Etapa 1 — enviar link de redefinição")
async def recuperar(dados: RecuperarIn, request: Request, tarefas: BackgroundTasks,
                    db: AsyncSession = Depends(get_db)):
    ip = ip_de(request)
    await limitar_taxa(f"rl:recuperar:{ip}", limite=5, janela_s=900)

    email = dados.email.strip().lower()
    usuario = (
        await db.execute(select(Usuario).where(Usuario.email == email))
    ).scalar_one_or_none()

    if usuario is None or not usuario.ativo:
        # Resposta neutra, e **nenhum e-mail**.
        #
        # Mandar aviso de "não há conta" para o endereço digitado faria do
        # sistema um relé de spam e escreveria para quem nunca pediu nada.
        # Dizer na tela entregaria a lista de quem tem cadastro. Sobra a trilha,
        # somada ao teto de 5 por IP a cada 15 minutos aplicado acima.
        log.info("Recuperação solicitada para e-mail sem conta ativa: %s", email)
        await _registrar_sem_conta(db, email, ip, request)
        await db.commit()       # o mesmo passo do outro caminho, para o tempo não contar
        return _resposta_neutra()

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

    # Segredo do link, não código de seis dígitos: numa URL não há digitação
    # para limitar por tentativa, então a defesa é a entropia.
    segredo = gerar_token_link()
    db.add(TokenRecuperacao(
        usuario_id=usuario.id,
        codigo_hash=hash_otp(segredo),
        expira_em=datetime.now(timezone.utc) + timedelta(seconds=settings.RESET_LINK_TTL_SECONDS),
        ip_solicitante=ip,
    ))
    # `url_publica`, não `url_local`: este link vai por e-mail para outra
    # máquina, e ali `localhost` é a máquina de quem recebeu.
    link = f"{settings.url_publica}/#redefinir={segredo}"

    await audit.registrar(
        db, acao="SENHA_RESET", severidade="warn", usuario=usuario,
        alvo=usuario.email, request=request,
        descricao="Link de redefinição gerado",
    )

    # Grava já: a tarefa pode rodar antes de `get_db` fechar a transação, e
    # aí o e-mail sairia com um link que ainda não vale — e, no SQLite, o
    # registro de falha da tarefa esbarraria no banco travado.
    await db.commit()

    # Depois da resposta: o tempo do SMTP não pode separar os dois caminhos.
    tarefas.add_task(_enviar_em_segundo_plano, usuario.email, usuario.nome, link)
    return _resposta_neutra()


# ======================================================================
# Recuperação de senha — etapa 2
# ======================================================================
async def _token_do_link(db: AsyncSession, segredo: str) -> tuple[Usuario, TokenRecuperacao]:
    """Acha o token pelo segredo do link, e com ele o usuário.

    Antes a busca era por e-mail e o código conferido depois, com contagem de
    tentativas — seis dígitos exigem isso. Aqui o segredo tem 32 bytes de
    entropia e é procurado direto pelo HMAC, então não há o que adivinhar nem
    tentativa para contar: ou o link está certo, ou não existe.

    Mensagem única para todos os casos — inexistente, expirado, já usado. Dizer
    "este link já foi usado" contaria a um estranho que o endereço tem conta.
    """
    alvo = hash_otp(segredo)
    token = (
        await db.execute(
            select(TokenRecuperacao)
            .where(TokenRecuperacao.codigo_hash == alvo,
                   TokenRecuperacao.usado.is_(False))
            .order_by(TokenRecuperacao.criado_em.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    recusa = HTTPException(
        status.HTTP_400_BAD_REQUEST,
        "Link inválido ou expirado. Peça um novo na tela de recuperação.",
    )
    if token is None:
        raise recusa
    if como_utc(token.expira_em) < datetime.now(timezone.utc):
        token.usado = True
        raise recusa

    usuario = (
        await db.execute(select(Usuario).where(Usuario.id == token.usuario_id))
    ).scalar_one_or_none()
    if usuario is None or not usuario.ativo:
        raise recusa
    return usuario, token


@router.post("/verificar-link", summary="Etapa 2 — conferir o link recebido")
async def verificar_link(dados: VerificarLinkIn, request: Request,
                         db: AsyncSession = Depends(get_db)):
    """Confere o link antes de mostrar o formulário.

    Sem isto, quem abre um link vencido só descobre depois de escolher e
    digitar a senha nova duas vezes.
    """
    await limitar_taxa(f"rl:link:{ip_de(request)}", limite=15, janela_s=900)
    usuario, token = await _token_do_link(db, dados.token)
    restante = int((como_utc(token.expira_em) - datetime.now(timezone.utc)).total_seconds())
    return {
        "valido": True,
        "email": usuario.email,
        "nome": usuario.nome,
        "mensagem": "Link validado. Defina a nova senha.",
        "expira_em_segundos": max(restante, 0),
    }


# ======================================================================
# Recuperação de senha — etapa 3
# ======================================================================
@router.post("/redefinir-senha", summary="Etapa 2 — gravar nova senha")
async def redefinir_senha(dados: RedefinirSenhaIn, request: Request,
                          db: AsyncSession = Depends(get_db)):
    await limitar_taxa(f"rl:redefinir:{ip_de(request)}", limite=10, janela_s=900)
    usuario, token = await _token_do_link(db, dados.token)

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

