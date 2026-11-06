#! python3.13
"""Abre o teste de corrida súbita (vídeo 04 do Avenue) para apresentação.

Duplo clique neste arquivo abre só a janela do vídeo: a extensão .pyw roda
sem a tela preta do console. Teclas: espaço pausa, p grava print, q fecha.
Se a janela não abrir, o motivo fica em resultados_teste/ao_vivo/apresentar.log.
"""
import os
import sys
import traceback
from pathlib import Path

# O atalho fica em apresentacao/; a raiz do projeto é a pasta de cima.
RAIZ = Path(__file__).resolve().parent.parent
os.chdir(RAIZ)
sys.path.insert(0, str(RAIZ))
LOG = RAIZ / "resultados_teste" / "ao_vivo" / "apresentar.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
LOG.unlink(missing_ok=True)          # o log só descreve a última abertura

try:
    from scripts import ver_acao_video

    AVENUE = RAIZ / "videos_teste" / "Avenue Dataset"
    sys.argv = ["ver_acao_video", str(AVENUE / "testing_videos" / "04.avi"),
                "--acao", "corrida", "--gabarito", str(AVENUE / "gabarito_corrida.csv")]
    ver_acao_video.main()
except Exception:  # noqa: BLE001 — sem console, o erro só aparece no log
    LOG.write_text(traceback.format_exc(), encoding="utf-8")
