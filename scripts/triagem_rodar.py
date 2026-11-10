"""Triagem contínua: analisa as capturas e atualiza os grupos para aprovação.

Roda a extração (scripts/triagem_extrair.py) e, enquanto ela trabalha,
refaz o agrupamento (scripts/triagem_agrupar.py) logo no início e depois a
cada `--intervalo` minutos. Quem aprova vê grupos novos ao longo do dia, sem
esperar as ~20 mil capturas.

Refazer os grupos não reapresenta nada: a tela ignora capturas que já viraram
ficha e as descartadas, e o agrupamento pula grupo parecido com quem já tem
ficha — aprovar alguém de manhã impede que o mesmo rosto volte à tarde como
"pessoa nova".

Mantém o Windows acordado enquanto roda (tampa fechada ainda suspende).

    py -3.13 -m scripts.triagem_rodar                 # agrupa a cada 60 min
    py -3.13 -m scripts.triagem_rodar --intervalo 30
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
PASTA = RAIZ / "data" / "cadastro_triagem"


def log(msg: str) -> None:
    linha = f"[{datetime.now():%d/%m %H:%M}] {msg}"
    print(linha, flush=True)
    with open(PASTA / "rodar.log", "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def agrupar() -> None:
    if not (PASTA / "vetores.npz").exists():
        return
    r = subprocess.run([sys.executable, "-m", "scripts.triagem_agrupar"], cwd=RAIZ,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    saida = " | ".join(l for l in (r.stdout or "").splitlines() if l.strip())
    if r.returncode == 0:
        log(f"grupos atualizados: {saida}")
    else:
        log(f"agrupamento falhou ({r.returncode}): {(r.stderr or '')[-400:]}")


def main() -> None:
    from scripts.triagem_extrair import manter_acordado

    ap = argparse.ArgumentParser()
    ap.add_argument("--intervalo", type=float, default=60.0, help="minutos entre agrupamentos")
    ap.add_argument("--prioridade", choices=["baixa", "normal"], default="baixa")
    a = ap.parse_args()
    PASTA.mkdir(parents=True, exist_ok=True)
    manter_acordado()
    log("início: grupos com o que já foi analisado")
    agrupar()
    with open(PASTA / "extrair.log", "a", encoding="utf-8") as out, \
            open(PASTA / "extrair.err.log", "a", encoding="utf-8") as err:
        extracao = subprocess.Popen([sys.executable, "-m", "scripts.triagem_extrair",
                                     "--prioridade", a.prioridade],
                                    cwd=RAIZ, stdout=out, stderr=err)
        log(f"extração iniciada (PID {extracao.pid})")
        proximo = time.monotonic() + a.intervalo * 60
        while extracao.poll() is None:
            time.sleep(15)
            if time.monotonic() >= proximo:
                agrupar()
                proximo = time.monotonic() + a.intervalo * 60
    log(f"extração terminou (código {extracao.returncode}); agrupamento final")
    agrupar()
    log("fim")


if __name__ == "__main__":
    main()
