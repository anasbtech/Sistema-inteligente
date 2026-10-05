"""Envio do código de recuperação.

Sai por SMTP quando `SMTP_HOST` e `SMTP_USER` estão preenchidos. Sem eles,
o código vai para o log — que é o bastante para desenvolver, e é o que
evita exigir um servidor de e-mail de quem só quer rodar o sistema.

Usa `smtplib` da biblioteca padrão numa thread, em vez de um cliente async.
Redefinição de senha é evento raro: a thread custa menos que mais uma
dependência para manter.
"""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import settings

log = logging.getLogger("seguranca.notificacoes")

MODELO = """\
{saudacao},

Recebemos um pedido de redefinição de senha para sua conta no SEGURANÇA.

    Código de verificação: {codigo}

O código expira em {minutos} minutos e só pode ser usado uma vez.
Se você não fez este pedido, ignore esta mensagem e avise a chefia imediata —
a solicitação já foi registrada na trilha de auditoria.

Centro Integrado de Videomonitoramento
"""


class FalhaNoEnvio(RuntimeError):
    """O servidor de e-mail recusou ou não respondeu."""


def _montar(destino: str, assunto: str, corpo: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.SMTP_FROM_NOME, settings.smtp_remetente))
    msg["To"] = destino
    msg["Subject"] = assunto
    # Sem Message-ID próprio, alguns provedores tratam a mensagem como suspeita.
    msg["Message-ID"] = make_msgid(domain=settings.smtp_remetente.split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(corpo)
    return msg


def _entregar(msg: EmailMessage) -> None:
    """Parte síncrona, chamada de uma thread."""
    contexto = ssl.create_default_context()
    if settings.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT,
                              timeout=settings.SMTP_TIMEOUT, context=contexto) as s:
            if settings.SMTP_PASSWORD:
                s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            s.send_message(msg)
        return

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT,
                      timeout=settings.SMTP_TIMEOUT) as s:
        s.ehlo()
        if settings.SMTP_STARTTLS:
            s.starttls(context=contexto)
            s.ehlo()
        if settings.SMTP_PASSWORD:
            s.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        s.send_message(msg)


async def _enviar_email(destino: str, assunto: str, corpo: str) -> None:
    if not settings.email_configurado:
        # Sem transporte não há o que entregar. Em desenvolvimento o código
        # no log é o caminho previsto; em produção, calar seria pior que
        # falhar, porque ninguém receberia nada e ninguém saberia disso.
        if settings.ENV == "development":
            log.warning("[E-MAIL NÃO CONFIGURADO] para=%s assunto=%s\n%s",
                        destino, assunto, corpo)
            return
        raise FalhaNoEnvio(
            "SMTP_HOST e SMTP_USER não estão configurados — o e-mail não foi enviado."
        )

    msg = _montar(destino, assunto, corpo)
    try:
        await asyncio.to_thread(_entregar, msg)
    except smtplib.SMTPAuthenticationError as exc:
        # A causa quase sempre é a mesma, e o erro cru do provedor não diz.
        raise FalhaNoEnvio(
            "O servidor recusou as credenciais. No Gmail é preciso uma senha "
            "de app de 16 caracteres, com a verificação em duas etapas ativa; "
            "a senha da conta não funciona."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise FalhaNoEnvio(f"Falha ao falar com {settings.SMTP_HOST}: {exc}") from exc

    log.info("Código de recuperação enviado para %s via %s",
             destino, settings.SMTP_HOST)


async def enviar_codigo_recuperacao(email: str, nome: str, codigo: str, minutos: int) -> None:
    saudacao = f"Prezado(a) {nome.split()[0]}" if nome else "Prezado(a)"
    await _enviar_email(
        email,
        "SEGURANÇA — código de redefinição de senha",
        MODELO.format(saudacao=saudacao, codigo=codigo, minutos=minutos),
    )


async def notificar_alerta_critico(titulo: str, detalhe: str, camera: str) -> None:
    """Gancho para push/rádio/despacho. Implementação a cargo da integração local."""
    log.warning("ALERTA CRÍTICO | %s | %s | %s", camera, titulo, detalhe)
