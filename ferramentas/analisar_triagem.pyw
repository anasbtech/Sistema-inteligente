import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
os.chdir(RAIZ)
PASTA = RAIZ / "data" / "cadastro_triagem"
PASTA.mkdir(parents=True, exist_ok=True)
py = Path(sys.executable).with_name("python.exe")   # pythonw -> python, sem janela
sem_janela = 0x08000000                              # CREATE_NO_WINDOW
with open(PASTA / "extrair.log", "a", encoding="utf-8") as log:
    r = subprocess.run([str(py), "-m", "scripts.triagem_extrair"], stdout=log,
                       stderr=subprocess.STDOUT, creationflags=sem_janela)
    if r.returncode == 0:
        subprocess.run([str(py), "-m", "scripts.triagem_agrupar"], stdout=log,
                       stderr=subprocess.STDOUT, creationflags=sem_janela)
