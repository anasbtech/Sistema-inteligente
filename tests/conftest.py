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
# Recortes também em pasta própria: a suíte gravava na `evidencias/` real, e
# um teste recomeçava a numeração e caía sobre o arquivo de outra rodada.
EVIDENCIAS_TESTE = Path(tempfile.mkdtemp(prefix="seguranca_evid_"))
os.environ["SNAPSHOT_DIR"] = str(EVIDENCIAS_TESTE)
os.environ["SECRET_KEY"] = "chave-de-teste-com-mais-de-32-caracteres-ok"
os.environ["SIMULADOR"] = "false"
# O laço veicular abriria RTSP e carregaria o detector para as câmeras de
# teste; os testes de veículos exercitam as partes dele diretamente.
os.environ["VEICULO_ATIVO"] = "false"
# Os testes medem o comportamento do detector, não a velocidade: no PyTorch
# eles não pagam a compilação do vídeo integrado e não dependem da máquina.
os.environ["OBJECT_MOTOR"] = "pytorch"
os.environ["ENV"] = "development"
os.environ["REDIS_URL"] = ""
# Desabilita SMTP nos testes para evitar tentativas reais de envio de email
# aos endereços de teste. Testes que precisam validar comportamento de envio
# configuram SMTP_HOST manualmente com monkeypatch.
os.environ["SMTP_HOST"] = ""

