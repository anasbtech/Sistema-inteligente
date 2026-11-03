"""Assistente de configuração do envio de e-mail.

    python scripts/configurar_email.py

Abre a página certa no navegador, espera a senha de app, confere no servidor,
grava no .env e manda uma mensagem de teste — nesta ordem, parando no primeiro
passo que falhar e dizendo o que fazer.

Existe porque a parte que sobra para a pessoa é pequena mas tem atrito demais:
a senha de app vive atrás de duas telas do Google, só aparece se a verificação
em duas etapas estiver ativa, é mostrada uma única vez, e o Gmail recusa sem
explicar quando ela vem com os espaços que a própria tela dele coloca. Cada um
desses detalhes já derrubou uma tentativa. Aqui eles são tratados um a um.

O `testar_email.py` continua servindo para conferir uma configuração que já
existe; este aqui é para montá-la da primeira vez.
"""
from __future__ import annotations

import asyncio
import re
import sys
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import exige_autenticacao, settings      # noqa: E402
from app.services import notificacoes                    # noqa: E402

from testar_email import _gravar_no_env, _provar_senha    # noqa: E402

PAGINA_SENHAS = "https://myaccount.google.com/apppasswords"
PAGINA_2FA = "https://myaccount.google.com/signinoptions/twosv"


def _risco(texto: str = "") -> None:
    print(f"\n{'─' * 68}")
    if texto:
        print(f"  {texto}\n")


def _perguntar(pergunta: str) -> bool:
    try:
        return input(f"  {pergunta} [s/N] ").strip().lower() in ("s", "sim", "y")
    except (EOFError, KeyboardInterrupt):
        return False


def _ler_senha() -> str:
    """Lê os 16 caracteres, tolerando o formato que o Google mostra.

    A tela do Google parte a senha em quatro blocos de quatro e quem copia
    traz os espaços junto; o Gmail recusa isso sem dizer o motivo. Também
    aceita colar com aspas, que é o que acontece quando se copia de um
    e-mail ou de um bloco de notas.
    """
    while True:
        try:
            bruto = input("  Cole os 16 caracteres (ou ENTER para desistir): ")
        except (EOFError, KeyboardInterrupt):
            return ""
        if not bruto.strip():
            return ""
        senha = re.sub(r"[\s'\"]", "", bruto)
        if len(senha) == 16:
            return senha
        print(f"    São 16 caracteres; recebi {len(senha)}. "
              "Confira se copiou os quatro blocos inteiros.\n")


async def principal() -> int:
    print()
    print("  ASSISTENTE DE ENVIO DE E-MAIL — SEGURANÇA")
    _risco()

    if not settings.SMTP_HOST or not settings.SMTP_USER:
        print("  Antes de tudo, preencha no .env:")
        print("    SMTP_HOST=smtp.gmail.com")
        print("    SMTP_USER=seu-endereco@gmail.com")
        print("    SMTP_FROM=seu-endereco@gmail.com")
        return 1

    conta = settings.SMTP_USER
    print(f"  Servidor : {settings.SMTP_HOST}:{settings.SMTP_PORT}")
    print(f"  Conta    : {conta}")

    if settings.email_configurado:
        print("  Senha    : já definida")
        _risco("O envio já está configurado.")
        if not _perguntar("Quer trocar a senha de app mesmo assim?"):
            print("\n  Nada alterado. Para só testar o envio atual:")
            print("    python scripts/testar_email.py")
            return 0
    else:
        print("  Senha    : ainda não definida")

    if not exige_autenticacao(settings.SMTP_HOST):
        print(f"\n  {settings.SMTP_HOST} não exige autenticação — não há senha a configurar.")
        return 0

    _risco("PASSO 1 de 3 — gerar a senha de app no Google")
    print("  A senha de app é diferente da senha da sua conta: são 16 caracteres")
    print("  que o Google gera só para um programa, e que podem ser revogados")
    print("  sem mexer na sua senha de verdade.")
    print()
    print("  Ela só existe se a verificação em duas etapas estiver ativa.")
    print()
    print(f"  Vou abrir:  {PAGINA_SENHAS}")
    print()

    if _perguntar("Abrir essa página no navegador agora?"):
        try:
            webbrowser.open(PAGINA_SENHAS)
            print("    Abri. Se pedir sua senha do Gmail, é normal — ele reconfirma.")
        except Exception:                               # noqa: BLE001
            print(f"    Não consegui abrir. Acesse à mão: {PAGINA_SENHAS}")
    else:
        print(f"    Tudo bem. Acesse quando puder: {PAGINA_SENHAS}")

    print()
    print("  Na página:")
    print("    1. Digite um nome qualquer no campo — por exemplo SEGURANCA")
    print("    2. Clique em Criar")
    print("    3. Copie os 16 caracteres antes de fechar (o Google mostra uma vez só)")
    print()
    print("  Se aparecer 'a configuração não está disponível para sua conta',")
    print("  é a verificação em duas etapas que está desligada. Ative em:")
    print(f"    {PAGINA_2FA}")
    print("  e rode este assistente de novo.")
    print()

    senha = _ler_senha()
    if not senha:
        print("\n  Encerrado sem alterar nada.")
        print("  Enquanto isso, a recuperação de senha funciona pelo link do")
        print("  administrador: Usuários → abrir a pessoa → Emitir link.")
        return 1

    _risco(f"PASSO 2 de 3 — conferindo a senha em {settings.SMTP_HOST}")
    recusa = _provar_senha(senha)
    if recusa:
        print(f"  Não deu: {recusa}")
        print()
        print("  Não gravei nada. Se a senha foi gerada agora mesmo, o Google leva")
        print("  alguns minutos para liberá-la — espere um pouco e rode de novo.")
        return 1
    print("  O Gmail aceitou a senha.")

    if not _gravar_no_env(senha):
        return 1
    print("  Gravada no .env.")
    settings.SMTP_PASSWORD = senha      # o teste abaixo já usa a senha nova

    _risco("PASSO 3 de 3 — enviando uma mensagem de teste")
    minutos = settings.RESET_LINK_TTL_SECONDS // 60
    try:
        await notificacoes.enviar_link_recuperacao(
            conta, "Teste de configuração",
            f"{settings.url_local}/#redefinir=teste-de-configuracao", minutos)
    except (notificacoes.FalhaNoEnvio, notificacoes.EmailNaoConfigurado) as e:
        print(f"  A senha serviu, mas o envio falhou: {e}")
        return 1
    except Exception as e:                              # noqa: BLE001
        print(f"  A senha serviu, mas o envio falhou ({type(e).__name__}): {e}")
        return 1

    print(f"  Enviado para {conta}.")
    _risco("PRONTO")
    print("  Confira a caixa de entrada — e o spam, que é onde a primeira")
    print("  costuma cair. Marque como 'não é spam' para as próximas virem direto.")
    print()
    print("  Falta um passo: reinicie o sistema para ele passar a usar a senha.")
    print("    Ctrl+C  e depois  python run.py")
    print()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(principal()))
    except KeyboardInterrupt:
        print("\n\n  Interrompido. Nada foi alterado.")
        raise SystemExit(1)
