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
import html
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from app.config import settings

log = logging.getLogger("seguranca.notificacoes")

MARCA = "AizonVMS"
INSTITUICAO = "Centro Integrado de Videomonitoramento"

# Texto puro: o que leem os clientes que não abrem HTML, e o que o filtro de
# spam compara com a versão HTML. Os dois têm de dizer a mesma coisa.
MODELO = """\
{saudacao},

Recebemos um pedido de redefinição de senha para a sua conta no AizonVMS.

Para definir uma nova senha, acesse o endereço abaixo:

{link}

Este link é válido por {minutos} minutos e pode ser usado uma única vez.

Se você não fez este pedido, ignore esta mensagem: sua senha atual continua
valendo. Por segurança, comunique o administrador do sistema — a solicitação
já foi registrada na trilha de auditoria.

Atenciosamente,
AizonVMS · {instituicao}

Mensagem automática. Não responda a este e-mail.
"""


MODELO_PRIMEIRO_ACESSO = """\
{saudacao},

Sua conta no AizonVMS foi criada. Para definir sua senha e fazer o
primeiro acesso, acesse o endereço abaixo:

{link}

Este link é válido por {minutos} minutos e pode ser usado uma única vez.
A senha que você escolher não é conhecida por mais ninguém, nem pelo
administrador que criou a conta.

Se o link expirar antes de ser usado, peça ao administrador que emita um
novo — a conta continua ativa.

Atenciosamente,
AizonVMS · {instituicao}

Mensagem automática. Não responda a este e-mail.
"""


def _html(*, titulo: str, saudacao: str, paragrafos: list[str], botao: str,
          link: str, minutos: int, nota: str) -> str:
    """Versão HTML no formato que os clientes de e-mail aceitam.

    Tabelas e estilo em linha, sem CSS externo nem imagem: Gmail e Outlook
    descartam `<style>` e bloqueiam imagens por padrão, e um logotipo que não
    carrega deixa um quadro vazio no topo. O botão é uma célula de tabela com
    cor de fundo, que é o único botão que o Outlook desenha.

    Só o botão leva ao link: o endereço por extenso saiu do HTML a pedido
    (11/11/2026). Quem não abre HTML recebe a versão em texto, que traz o
    endereço inteiro. Tudo que vem de fora (nome, link) é escapado.
    """
    e = html.escape
    corpo = "".join(
        f'<p style="margin:0 0 16px;font-size:15px;line-height:24px;color:#33475B">{p}</p>'
        for p in paragrafos
    )
    return f"""\
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{e(titulo)}</title>
</head>
<body style="margin:0;padding:0;background:#EEF2F6;font-family:Segoe UI,Helvetica,Arial,sans-serif">
<div style="display:none;max-height:0;overflow:hidden;opacity:0">{e(titulo)} — link válido por {minutos} minutos.</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#EEF2F6">
<tr><td align="center" style="padding:32px 16px">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#FFFFFF;border-radius:10px;overflow:hidden;border:1px solid #DDE4EB">
    <tr><td style="background:#0B121A;padding:22px 32px;border-bottom:3px solid #35A7DE">
      <div style="font-size:20px;font-weight:700;letter-spacing:.5px;color:#FFFFFF">Aizon<span style="color:#35A7DE">VMS</span></div>
      <div style="font-size:12px;letter-spacing:.5px;color:#9BB0C3;margin-top:4px">{e(INSTITUICAO)}</div>
    </td></tr>
    <tr><td style="padding:32px 32px 8px">
      <h1 style="margin:0 0 20px;font-size:21px;line-height:28px;font-weight:600;color:#0B121A">{e(titulo)}</h1>
      <p style="margin:0 0 16px;font-size:15px;line-height:24px;color:#33475B">{e(saudacao)},</p>
      {corpo}
    </td></tr>
    <tr><td align="center" style="padding:8px 32px 8px">
      <table role="presentation" cellpadding="0" cellspacing="0"><tr>
        <td align="center" bgcolor="#1F7FB8" style="border-radius:6px">
          <a href="{e(link, quote=True)}" target="_blank"
             style="display:inline-block;padding:14px 34px;font-size:15px;font-weight:600;color:#FFFFFF;text-decoration:none;border-radius:6px">{e(botao)}</a>
        </td>
      </tr></table>
    </td></tr>
    <tr><td style="padding:16px 32px 8px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F4F7FA;border-radius:6px">
        <tr><td style="padding:14px 16px;font-size:13px;line-height:20px;color:#4A5F73">
          Válido por <b style="color:#0B121A">{minutos} minutos</b> e para <b style="color:#0B121A">um único uso</b>.
        </td></tr>
      </table>
    </td></tr>
    <tr><td style="padding:16px 32px 28px">
      <p style="margin:0;padding-top:20px;border-top:1px solid #E3E9EF;font-size:13px;line-height:20px;color:#4A5F73">{nota}</p>
    </td></tr>
    <tr><td style="background:#F4F7FA;padding:18px 32px;border-top:1px solid #E3E9EF">
      <p style="margin:0;font-size:12px;line-height:18px;color:#6B7F92">Atenciosamente,<br><b style="color:#33475B">{e(MARCA)} · {e(INSTITUICAO)}</b></p>
      <p style="margin:10px 0 0;font-size:11px;line-height:16px;color:#8A9BAB">Mensagem automática. Não responda a este e-mail.</p>
    </td></tr>
  </table>
</td></tr>
</table>
</body>
</html>"""


class FalhaNoEnvio(RuntimeError):
    """O servidor de e-mail recusou ou não respondeu."""


def _montar(destino: str, assunto: str, corpo: str,
            corpo_html: str | None = None) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((settings.SMTP_FROM_NOME, settings.smtp_remetente))
    msg["To"] = destino
    msg["Subject"] = assunto
    # Sem Message-ID próprio, alguns provedores tratam a mensagem como suspeita.
    msg["Message-ID"] = make_msgid(domain=settings.smtp_remetente.split("@")[-1])
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(corpo)
    if corpo_html:
        # multipart/alternative: o cliente escolhe. Texto primeiro, HTML por
        # último — a ordem é a de preferência, e o último é o preferido.
        msg.add_alternative(corpo_html, subtype="html")
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


class EmailNaoConfigurado(RuntimeError):
    """Não há transporte de e-mail nesta instalação.

    Separado de `FalhaNoEnvio` porque quem chama responde diferente: isto não
    é defeito, é uma instalação que ainda não configurou envio — e a tela
    precisa oferecer o caminho alternativo em vez de alarmar.
    """


async def _enviar_email(destino: str, assunto: str, corpo: str,
                        corpo_html: str | None = None) -> None:
    if not settings.email_configurado:
        # Sem transporte não há o que entregar — e isso não é falha de entrega.
        # Em desenvolvimento o link no log é o caminho previsto; em produção,
        # calar seria pior, porque ninguém receberia nada e ninguém saberia.
        log.warning("[E-MAIL NÃO CONFIGURADO] %s — para=%s assunto=%s",
                    settings.falta_para_email, destino, assunto)
        if settings.ENV == "development":
            log.warning("Conteúdo que sairia:\n%s", corpo)
        raise EmailNaoConfigurado(settings.falta_para_email)

    msg = _montar(destino, assunto, corpo, corpo_html)
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

    # Pelo assunto, não pelo tipo presumido. Esta função entrega qualquer
    # mensagem do sistema — redefinição, primeiro acesso — e a linha dizia
    # "Link de recuperação enviado" para todas. Log que descreve errado o que
    # saiu é pior que log nenhum: ele é lido como prova do que aconteceu.
    log.info("E-mail enviado para %s via %s — %s",
             destino, settings.SMTP_HOST, assunto)


async def enviar_link_recuperacao(email: str, nome: str, link: str, minutos: int) -> None:
    saudacao = f"Prezado(a) {nome}" if nome else "Prezado(a)"
    await _enviar_email(
        email,
        f"{MARCA} — redefinição de senha",
        MODELO.format(saudacao=saudacao, link=link, minutos=minutos,
                      instituicao=INSTITUICAO),
        _html(
            titulo="Redefinição de senha",
            saudacao=saudacao,
            paragrafos=[
                "Recebemos um pedido de redefinição de senha para a sua conta "
                f"no {MARCA}. Para definir uma nova senha, clique no botão abaixo.",
            ],
            botao="Redefinir senha",
            link=link, minutos=minutos,
            nota=("<b style=\"color:#0B121A\">Não foi você?</b> Ignore esta "
                  "mensagem: sua senha atual continua valendo. Por segurança, "
                  "comunique o administrador do sistema — a solicitação já foi "
                  "registrada na trilha de auditoria."),
        ),
    )


async def enviar_primeiro_acesso(email: str, nome: str, link: str,
                                 minutos: int) -> None:
    """Convite de primeiro acesso: a pessoa define a própria senha.

    Texto próprio, e não o de redefinição: aquele abre com "recebemos um
    pedido de redefinição", que a pessoa não fez — ela acabou de ser
    cadastrada. Quem recebe uma mensagem que descreve errado o que houve
    hesita em clicar, que é exatamente o reflexo que se quer preservar.
    """
    saudacao = f"Prezado(a) {nome}" if nome else "Prezado(a)"
    await _enviar_email(
        email,
        f"{MARCA} — primeiro acesso",
        MODELO_PRIMEIRO_ACESSO.format(saudacao=saudacao, link=link,
                                      minutos=minutos, instituicao=INSTITUICAO),
        _html(
            titulo=f"Bem-vindo(a) ao {MARCA}",
            saudacao=saudacao,
            paragrafos=[
                f"Sua conta no {MARCA} foi criada. Para definir sua senha e "
                "fazer o primeiro acesso, clique no botão abaixo.",
                "A senha que você escolher não é conhecida por mais ninguém, "
                "nem pelo administrador que criou a conta.",
            ],
            botao="Definir minha senha",
            link=link, minutos=minutos,
            nota=("Se o link expirar antes de ser usado, peça ao administrador "
                  "que emita um novo — a conta continua ativa."),
        ),
    )


async def notificar_alerta_critico(titulo: str, detalhe: str, camera: str) -> None:
    """Gancho para push/rádio/despacho. Implementação a cargo da integração local."""
    log.warning("ALERTA CRÍTICO | %s | %s | %s", camera, titulo, detalhe)
