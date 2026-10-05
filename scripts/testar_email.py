"""Testa o envio de e-mail sem passar pela tela de recuperação.

    python scripts/testar_email.py destino@exemplo.com

Diz o que está configurado, tenta entregar uma mensagem de verdade e, se o
servidor recusar, explica o que costuma ser a causa. Útil para separar
"a configuração está errada" de "o sistema não enviou".
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import settings                      # noqa: E402
from app.services import notificacoes                # noqa: E402


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    destino = sys.argv[1].strip()

    print("Configuração atual")
    print(f"  SMTP_HOST      {settings.SMTP_HOST or '(vazio)'}")
    print(f"  SMTP_PORT      {settings.SMTP_PORT}")
    print(f"  SMTP_USER      {settings.SMTP_USER or '(vazio)'}")
    print(f"  SMTP_PASSWORD  {'definida (' + str(len(settings.SMTP_PASSWORD)) + ' caracteres)' if settings.SMTP_PASSWORD else '(vazia)'}")
    print(f"  remetente      {settings.smtp_remetente or '(vazio)'}")
    print(f"  STARTTLS       {settings.SMTP_STARTTLS}")
    print()

    if not settings.email_configurado:
        print("SMTP_HOST e SMTP_USER precisam estar preenchidos no .env.")
        print("Enquanto não estiverem, o código de recuperação só vai para o log.")
        return 1

    # A senha de app do Gmail tem 16 caracteres; com espaços ela falha, e o
    # erro que volta do servidor não diz isso.
    if "gmail" in settings.SMTP_HOST and " " in settings.SMTP_PASSWORD:
        print("Aviso: a senha tem espaços. O Gmail mostra a senha de app em")
        print("blocos de quatro, mas ela deve ser gravada sem os espaços.\n")

    print(f"Enviando para {destino}…")
    try:
        await notificacoes.enviar_codigo_recuperacao(
            destino, "Teste do Sistema", "123456", settings.OTP_TTL_SECONDS // 60)
    except notificacoes.FalhaNoEnvio as exc:
        print(f"\nFALHOU: {exc}")
        return 1

    print("\nEnviado. Confira a caixa de entrada e o spam —")
    print("a primeira mensagem de um remetente novo costuma cair lá.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
