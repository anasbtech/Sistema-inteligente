"""Confere se o envio de e-mail está funcionando, e diz o que falta quando não está.

    python scripts/testar_email.py                      # testa como está hoje
    python scripts/testar_email.py abcd efgh ijkl mnop  # testa a senha de app e, se
                                                        # ela servir, grava no .env
    python scripts/testar_email.py --para outro@mail.com

Existe porque a falha de SMTP é silenciosa onde mais importa: em modo de
desenvolvimento, sem servidor configurado, o pedido de recuperação responde
"enviado" e o link vai só para o log. Quem está esperando o e-mail fica sem
saber se o sistema falhou, se a mensagem caiu no spam, ou se demora.

Aqui a resposta é direta: ou a mensagem chega na caixa, ou sai o motivo exato
de não ter chegado.

A senha entra por argumento, e não editando o .env à mão, por dois motivos: a
senha de app aparece na tela do Google partida em quatro blocos de quatro — e
quem copia daquela tela cola os espaços junto, que o Gmail recusa sem dizer por
quê; e gravar só depois de o Gmail aceitar evita deixar no .env uma senha
errada que o sistema vai usar em silêncio no próximo pedido de recuperação.
"""
from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import settings                      # noqa: E402
from app.services import notificacoes                # noqa: E402

ENV = RAIZ / ".env"


def _argumentos(argv: list[str]) -> tuple[str, str]:
    """Separa o destino (--para) do resto, que é a senha de app.

    Os blocos da senha vêm colados porque o Google a mostra partida em quatro,
    e colar daquela tela traz os espaços; o Gmail recusa a senha com espaço sem
    explicar, então juntar aqui poupa uma investigação inteira.
    """
    destino, blocos = "", []
    i = 0
    while i < len(argv):
        if argv[i] in ("--para", "--destino") and i + 1 < len(argv):
            destino, i = argv[i + 1], i + 2
            continue
        blocos.append(argv[i])
        i += 1
    return destino, re.sub(r"\s+", "", "".join(blocos))


def _gravar_no_env(senha: str) -> bool:
    """Escreve SMTP_PASSWORD no .env preservando o resto do arquivo.

    Reescreve a linha existente em vez de acrescentar outra: duas linhas com a
    mesma chave fazem o pydantic-settings usar a primeira, e a senha nova
    ficaria no arquivo sem nunca ser lida.
    """
    if not ENV.exists():
        print(f"  Nao achei {ENV} — crie o arquivo antes.")
        return False

    linhas = ENV.read_text(encoding="utf-8").splitlines(keepends=True)
    for n, linha in enumerate(linhas):
        if linha.lstrip().startswith("SMTP_PASSWORD="):
            fim = "\n" if linha.endswith("\n") else ""
            linhas[n] = f"SMTP_PASSWORD={senha}{fim}"
            ENV.write_text("".join(linhas), encoding="utf-8")
            return True

    sep = "" if (not linhas or linhas[-1].endswith("\n")) else "\n"
    ENV.write_text("".join(linhas) + f"{sep}SMTP_PASSWORD={senha}\n", encoding="utf-8")
    return True


def _provar_senha(senha: str) -> str:
    """Tenta autenticar de verdade. Devolve "" se deu, ou o motivo da recusa.

    Conectar e autenticar são tratados como duas fases porque falham pelo mesmo
    tipo de exceção e têm donos diferentes: a primeira é rede ou firewall, a
    segunda é a senha. E o Gmail, diante de uma senha de app inválida, às vezes
    derruba a conexão em vez de responder 535 — sem separar as fases, isso sai
    na tela como "não deu para falar com o servidor", mandando quem lê conferir
    a internet quando o que está errado são os 16 caracteres.
    """
    contexto = ssl.create_default_context()
    try:
        if settings.SMTP_PORT == 465:
            s = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT,
                                 timeout=settings.SMTP_TIMEOUT, context=contexto)
        else:
            s = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT,
                             timeout=settings.SMTP_TIMEOUT)
            s.ehlo()
            if settings.SMTP_STARTTLS:
                s.starttls(context=contexto)
                s.ehlo()
    except (smtplib.SMTPException, OSError) as e:
        return (f"nao deu para chegar em {settings.SMTP_HOST}:{settings.SMTP_PORT} "
                f"({type(e).__name__}: {e}) — isso e rede ou firewall, nao a senha")

    try:
        s.login(settings.SMTP_USER, senha)
    except smtplib.SMTPAuthenticationError as e:
        bruto = e.smtp_error.decode(errors="replace") if e.smtp_error else str(e)
        return f"o servidor recusou a senha ({e.smtp_code}): {bruto.splitlines()[0]}"
    except smtplib.SMTPServerDisconnected:
        return ("o servidor derrubou a conexao durante a autenticacao — e assim "
                "que o Gmail responde a uma senha de app invalida. Confira os 16 "
                "caracteres, e que a senha foi gerada para esta mesma conta "
                f"({settings.SMTP_USER})")
    except (smtplib.SMTPException, OSError) as e:
        return f"falha ao autenticar ({type(e).__name__}): {e}"
    finally:
        try:
            s.quit()
        except Exception:                             # noqa: BLE001
            pass                                      # sessão já encerrada pelo servidor
    return ""


def _diagnostico() -> str:
    """O que impede o envio. Vem do config: a tela de recuperação, o aviso de
    arranque e este script precisam dizer a mesma coisa, e três cópias da
    mesma regra divergem na primeira vez que uma delas muda."""
    falta = settings.falta_para_email
    if falta and "SMTP_PASSWORD" in falta:
        falta += ("\n      Com ela em maos, rode:  "
                  "python scripts/testar_email.py <os 16 caracteres>")
    return falta


async def principal() -> int:
    destino, senha = _argumentos(sys.argv[1:])
    destino = destino or settings.SMTP_USER

    print(f"  servidor : {settings.SMTP_HOST or '(vazio)'}:{settings.SMTP_PORT}")
    print(f"  remetente: {settings.SMTP_USER or '(vazio)'}")
    print(f"  senha    : {'definida' if settings.SMTP_PASSWORD else '(vazia)'}")
    print(f"  destino  : {destino or '(vazio)'}")
    print()

    if senha:
        if len(senha) != 16:
            print(f"  A senha de app do Gmail tem 16 caracteres; recebi {len(senha)}.")
            print("  Confira se copiou os quatro blocos inteiros.")
            return 1
        if not settings.SMTP_HOST or not settings.SMTP_USER:
            print("  Preencha SMTP_HOST e SMTP_USER no .env antes de testar a senha.")
            return 1

        print(f"  Testando a senha em {settings.SMTP_HOST}...")
        recusa = _provar_senha(senha)
        if recusa:
            print(f"  FALHOU: {recusa}")
            print()
            print("  Nao gravei nada no .env. Se a senha foi gerada agora, o Google")
            print("  leva alguns minutos para liberar; tente de novo em seguida.")
            return 1

        print("  Senha aceita pelo Gmail.")
        if not _gravar_no_env(senha):
            return 1
        print(f"  Gravada em {ENV.name}.")
        settings.SMTP_PASSWORD = senha   # o envio abaixo ja usa a senha nova
        print()

    falta = _diagnostico()
    if falta:
        print("  NAO DA PARA ENVIAR.")
        print(f"    - {falta}")
        print()
        print("  Preencha no arquivo .env e rode de novo.")
        return 1

    if not destino:
        print("  Informe um destino: python scripts/testar_email.py --para voce@exemplo.com")
        return 1

    minutos = settings.RESET_LINK_TTL_SECONDS // 60
    try:
        await notificacoes.enviar_link_recuperacao(
            destino,
            "Teste de configuracao",
            f"{settings.url_local}/#redefinir=teste-de-configuracao",
            minutos,
        )
    except (notificacoes.FalhaNoEnvio, notificacoes.EmailNaoConfigurado) as e:
        print(f"  FALHOU: {e}")
        return 1
    except Exception as e:                            # noqa: BLE001
        print(f"  FALHOU ({type(e).__name__}): {e}")
        return 1

    print(f"  ENVIADO para {destino}.")
    print("  Confira a caixa de entrada — e o spam, que e onde a primeira costuma cair.")
    if senha:
        print()
        print("  Reinicie o sistema para ele passar a usar a senha nova.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(principal()))
