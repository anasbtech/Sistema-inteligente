"""Envio do código de recuperação.

Em desenvolvimento apenas registra no log. Em produção, troque `_enviar_email`
por SMTP institucional, API transacional ou gateway de SMS — a assinatura da
função permanece a mesma.
"""
from __future__ import annotations

import logging

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


async def _enviar_email(destino: str, assunto: str, corpo: str) -> None:
    if settings.ENV == "development":
        log.warning("[E-MAIL SIMULADO] para=%s assunto=%s\n%s", destino, assunto, corpo)
        return

    # Produção — exemplo com SMTP institucional:
    #
    # import aiosmtplib
    # from email.message import EmailMessage
    #
    # msg = EmailMessage()
    # msg["From"] = settings.SMTP_FROM
    # msg["To"] = destino
    # msg["Subject"] = assunto
    # msg.set_content(corpo)
    # await aiosmtplib.send(
    #     msg, hostname=settings.SMTP_HOST, port=settings.SMTP_PORT,
    #     username=settings.SMTP_USER, password=settings.SMTP_PASS, start_tls=True,
    # )
    raise NotImplementedError(
        "Configure o transporte de e-mail antes de subir em produção."
    )


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

