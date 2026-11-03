"""Gestão de usuários, perfis e matriz de permissões."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import settings
from app.database import get_db
from app.deps import exige, ip_de
from app.models import PERMISSOES, Perfil, TokenRecuperacao, Usuario
from app.schemas import UsuarioCreate, UsuarioCriado, UsuarioOut, UsuarioUpdate
from app.security import gerar_token_link, hash_otp, hash_senha, senha_provisoria
from app.services.notificacoes import (
    EmailNaoConfigurado, FalhaNoEnvio, enviar_primeiro_acesso,
)

log = logging.getLogger("seguranca.usuarios")

router = APIRouter(prefix="/usuarios", tags=["Usuários"])


def _saida(u: Usuario) -> UsuarioOut:
    dados = UsuarioOut.model_validate(u)
    dados.permissoes = sorted(u.permissoes)
    return dados


async def _alvo(db: AsyncSession, usuario_id: uuid.UUID,
                usuario: Usuario, acao: str) -> Usuario:
    """Carrega o usuário que vai sofrer a ação e barra os casos sem volta.

    Só ADMIN tem a permissão `users`, então quem chega aqui é administrador;
    com a própria conta fora de alcance, sempre resta um admin ativo.
    """
    alvo = await db.get(Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuário não encontrado.")
    if alvo.id == usuario.id:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"Você não pode {acao} a própria conta.")
    if alvo.excluido_em is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{alvo.nome} foi excluído e não aceita mais alterações.")
    return alvo


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
async def listar(q: Optional[str] = None, incluir_excluidos: bool = False,
                 db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("users"))):
    filtros = []
    if not incluir_excluidos:
        filtros.append(Usuario.excluido_em.is_(None))
    if q:
        termo = f"%{q.strip()}%"
        filtros.append(or_(Usuario.nome.ilike(termo), Usuario.email.ilike(termo),
                           Usuario.matricula.ilike(termo), Usuario.unidade.ilike(termo)))
    linhas = (
        await db.execute(select(Usuario).where(*filtros).order_by(Usuario.nome))
    ).scalars().all()
    return [_saida(u) for u in linhas]


@router.post("", response_model=UsuarioCriado, status_code=status.HTTP_201_CREATED,
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
        # Qual dos dois, e em que estado. "E-mail ou matrícula" obriga a
        # adivinhar, e a conta pode estar apenas inativa — caso em que
        # recriar é o caminho errado: o certo é reativar, que preserva
        # trilha, sessões e histórico. Dizer isto não vaza nada: só quem tem
        # a permissão `users` chega aqui.
        qual = []
        if existente.email == email:
            qual.append("e-mail")
        if existente.matricula == dados.matricula:
            qual.append("matrícula")
        # Conta excluída não chega aqui: a exclusão libera e-mail e
        # matrícula para quem vier depois.
        if not existente.ativo:
            estado = (f" A conta de {existente.nome} está INATIVA — reative-a em "
                      "vez de criar outra, para não perder a trilha e o "
                      "histórico dela.")
        else:
            estado = ""
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Já existe usuário com {' e '.join(qual)}: {existente.nome} "
            f"({existente.matricula}).{estado}")

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
    if dados.senha is not None:
        return saida

    # Sem senha definida pelo administrador, o caminho é o link de primeiro
    # acesso — não a senha provisória por e-mail.
    #
    # Senha por e-mail chega legível na caixa de entrada e fica lá para
    # sempre, porque mensagem não expira: quem tiver acesso àquela caixa
    # depois, ou a um backup dela, tem a conta. O link guarda só o hash no
    # banco, vale trinta minutos, serve uma vez, e a senha que a pessoa
    # escolhe não é conhecida por mais ninguém — nem por quem a cadastrou.
    segredo = gerar_token_link()
    db.add(TokenRecuperacao(
        usuario_id=novo.id,
        codigo_hash=hash_otp(segredo),
        expira_em=datetime.now(timezone.utc)
                  + timedelta(seconds=settings.RESET_LINK_TTL_SECONDS),
        ip_solicitante=ip_de(request),
    ))
    # `url_publica`, não `url_local`: este link vai por e-mail para outra
    # máquina, e ali `localhost` é a máquina de quem recebeu.
    link = f"{settings.url_publica}/#redefinir={segredo}"
    minutos = settings.RESET_LINK_TTL_SECONDS // 60

    # Três desfechos, como no reenvio: enviado, não configurado, falhou.
    # Nenhum deles desfaz o cadastro — a conta existe, e deixar o
    # administrador sem caminho seria pior que o e-mail não ter saído.
    envio, falha = "enviado", ""
    try:
        await enviar_primeiro_acesso(novo.email, novo.nome, link, minutos)
    except EmailNaoConfigurado as exc:
        envio, falha = "nao_configurado", str(exc)
        log.warning("Conta criada para %s — envio não configurado (%s)",
                    novo.email, falha)
    except FalhaNoEnvio as exc:
        envio, falha = "falhou", str(exc)
        log.warning("Conta criada para %s — e-mail não saiu: %s",
                    novo.email, falha)

    return {
        **saida.model_dump(),
        "primeiro_acesso": {
            "envio": envio,
            "erro": falha,
            "expira_em_segundos": settings.RESET_LINK_TTL_SECONDS,
            # O link só acompanha a resposta quando o e-mail não saiu: é o
            # caminho alternativo para a conta não nascer inacessível. Com o
            # envio funcionando, o segredo não precisa passar pela tela.
            "link": link if envio != "enviado" else "",
        },
    }


@router.patch("/{usuario_id}", response_model=UsuarioOut, summary="Editar usuário")
async def atualizar(usuario_id: uuid.UUID, dados: UsuarioUpdate, request: Request,
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("users"))):
    alvo = await db.get(Usuario, usuario_id)
    if alvo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuário não encontrado.")
    if alvo.excluido_em is not None:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"{alvo.nome} foi excluído e não aceita mais alterações.")

    campos = dados.model_dump(exclude_unset=True)

    # Trava contra auto-rebaixamento: o último admin não pode se tirar o perfil.
    if alvo.id == usuario.id and campos.get("perfil") not in (None, Perfil.ADMIN):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Você não pode rebaixar a própria conta.")

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


@router.post("/{usuario_id}/link-redefinicao", summary="Emitir link de redefinição de senha")
async def link_redefinicao(usuario_id: uuid.UUID, request: Request,
                           motivo: str = Query(..., min_length=8),
                           db: AsyncSession = Depends(get_db),
                           usuario: Usuario = Depends(exige("users"))):
    """Emite o link de redefinição para o administrador entregar em mãos.

    O caminho por e-mail depende de uma credencial SMTP, e credencial é coisa
    que nem toda instalação tem no dia em que precisa — sem ela, ninguém que
    esqueceu a senha volta a entrar. Aqui o administrador emite o mesmo link e
    o repassa pelo canal que já usa com a equipe: presencialmente, por rádio,
    por telefone.

    Nada no mecanismo muda: mesmo token, mesma validade, mesma tela de troca.
    O que muda é o transporte, e com ele quem responde pela entrega — por isso
    a justificativa é obrigatória e o ato fica na trilha com o nome de quem
    emitiu. Um administrador que emite um link toma posse temporária do acesso
    de outra pessoa; isso não pode acontecer sem deixar rastro.
    """
    alvo = await _alvo(db, usuario_id, usuario, "emitir link de redefinição para")
    if not alvo.ativo:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{alvo.nome} está bloqueado e não conseguiria entrar nem com a senha "
            "nova. Desbloqueie a conta antes de emitir o link.",
        )

    # Invalida os anteriores ainda abertos: dois links válidos ao mesmo tempo
    # dobram a janela de quem interceptar um deles — e o fluxo por e-mail já
    # fecha os antigos, então deixar estes abertos abriria pela lateral o que
    # lá é fechado.
    anteriores = (
        await db.execute(
            select(TokenRecuperacao).where(
                TokenRecuperacao.usuario_id == alvo.id,
                TokenRecuperacao.usado.is_(False),
            )
        )
    ).scalars().all()
    for t in anteriores:
        t.usado = True

    segredo = gerar_token_link()
    db.add(TokenRecuperacao(
        usuario_id=alvo.id,
        codigo_hash=hash_otp(segredo),
        expira_em=datetime.now(timezone.utc)
                  + timedelta(seconds=settings.RESET_LINK_TTL_SECONDS),
        ip_solicitante=ip_de(request),
    ))

    # O segredo não entra na trilha. Ela é consultável por auditor, e um link
    # vivo gravado ali daria a quem apenas lê o registro o acesso à conta.
    # Fica o fato: que foi emitido, por quem, para quem e por quê.
    await audit.registrar(
        db, acao="SENHA_RESET", severidade="warn", usuario=usuario,
        alvo=alvo.email, finalidade=motivo, request=request,
        descricao="Link de redefinição emitido pelo administrador",
    )
    return {
        # O administrador copia este link e manda por fora — WhatsApp,
        # mensagem, ditado por telefone. Sai da máquina igual ao do e-mail.
        "link": f"{settings.url_publica}/#redefinir={segredo}",
        "expira_em_segundos": settings.RESET_LINK_TTL_SECONDS,
        "usuario": {"nome": alvo.nome, "email": alvo.email},
    }


@router.post("/{usuario_id}/bloqueio", response_model=UsuarioOut,
             summary="Bloquear usuário")
async def bloquear(usuario_id: uuid.UUID, request: Request,
                   motivo: str = Query(..., min_length=8),
                   db: AsyncSession = Depends(get_db),
                   usuario: Usuario = Depends(exige("users"))):
    """Suspende o acesso sem desfazer nada: o login passa a recusar a conta e a
    sessão em curso cai na próxima requisição. Reversível por `desbloquear`."""
    alvo = await _alvo(db, usuario_id, usuario, "bloquear")
    if not alvo.ativo:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{alvo.nome} já está bloqueado.")

    alvo.ativo = False
    await audit.registrar(db, acao="USR_BLOCK", descricao="Usuário bloqueado", severidade="warn",
                          usuario=usuario, alvo=alvo.email, finalidade=motivo, request=request)
    return _saida(alvo)


@router.delete("/{usuario_id}/bloqueio", response_model=UsuarioOut,
               summary="Desbloquear usuário")
async def desbloquear(usuario_id: uuid.UUID, request: Request,
                      db: AsyncSession = Depends(get_db),
                      usuario: Usuario = Depends(exige("users"))):
    alvo = await _alvo(db, usuario_id, usuario, "desbloquear")
    if alvo.ativo:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{alvo.nome} não está bloqueado.")

    alvo.ativo = True
    await audit.registrar(db, acao="USR_UNBLOCK", descricao="Usuário desbloqueado",
                          severidade="warn", usuario=usuario, alvo=alvo.email, request=request)
    return _saida(alvo)


@router.delete("/{usuario_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Excluir usuário")
async def excluir(usuario_id: uuid.UUID, request: Request,
                  motivo: str = Query(..., min_length=8),
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("users"))):
    """Exclusão definitiva, mas sem apagar a linha.

    `auditoria.usuario_id` é ON DELETE SET NULL: apagar a conta de verdade
    zeraria essa coluna nas linhas que o usuário assinou, e como o hash de cada
    linha é calculado sobre ela, a cadeia inteira passaria a acusar adulteração.
    A conta sai da lista, perde o acesso e não volta.
    """
    alvo = await _alvo(db, usuario_id, usuario, "excluir")

    email_real, matricula_real = alvo.email, alvo.matricula
    alvo.ativo = False
    alvo.excluido_em = datetime.now(timezone.utc)

    # A trilha primeiro, com os valores verdadeiros: é ela que guarda quem
    # era o titular, e ela é gravada antes de os identificadores saírem.
    await audit.registrar(db, acao="USR_DEL", descricao="Usuário excluído", severidade="warn",
                          usuario=usuario, alvo=email_real, finalidade=motivo, request=request,
                          metadados={"perfil": alvo.perfil.value,
                                     "matricula": matricula_real})

    # E-mail e matrícula são liberados para quem vier depois.
    #
    # A linha fica — `auditoria.usuario_id` é ON DELETE SET NULL e o hash de
    # cada linha é calculado sobre ela, então apagar de verdade faria a
    # cadeia inteira acusar adulteração. Mas segurar o endereço para sempre
    # não tem defesa: a conta nunca mais entra (`ativo` falso e
    # `excluido_em` preenchido, e o login recusa ambos), e quando alguém
    # assume o e-mail funcional de quem saiu, o sistema travava o cadastro
    # com um erro que não oferecia saída nenhuma.
    #
    # O que some daqui segue na trilha, que é onde o histórico mora. A
    # identidade da conta antiga nunca foi o e-mail: é o `usuario_id`, que
    # não muda.
    marca = str(alvo.id)[:8]
    alvo.email = f"excluido+{marca}@invalido.local"
    alvo.matricula = f"EXC-{marca}"
