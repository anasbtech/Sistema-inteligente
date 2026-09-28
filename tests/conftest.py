"""Configuração da suíte.

As variáveis precisam existir **antes** de qualquer import de `app.*`, porque
`app.config.settings` é resolvido uma vez no import. Por isso este arquivo não
importa nada da aplicação.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

BANCO_TESTE = Path(tempfile.gettempdir()) / "seguranca_teste.db"

# Banco próprio e descartável: a suíte nunca toca no seguranca.db de desenvolvimento.
for sufixo in ("", "-wal", "-shm"):
    arq = Path(str(BANCO_TESTE) + sufixo)
    if arq.exists():
        arq.unlink()

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{BANCO_TESTE}"
os.environ["SECRET_KEY"] = "chave-de-teste-com-mais-de-32-caracteres-ok"
os.environ["SIMULADOR"] = "false"
os.environ["ENV"] = "development"
os.environ["REDIS_URL"] = ""

