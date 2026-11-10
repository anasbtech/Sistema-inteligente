"""Marca/modelo dos carros: análise contínua, em processo próprio.

A cada volta:

1. pega as passagens de carro (moto não) ainda sem análise, da mais nova
   para a mais antiga, e calcula o vetor do CLIP grande (modelo_fino);
2. guarda as três sugestões do catálogo, que a ficha oferece para rotular
   com um clique — sugestão, nunca afirmação;
3. aplica a sonda treinada: só afirma marca/modelo de classe validada a 95%;
4. quando há rótulos novos, treina a sonda de novo e reaplica a todas as
   passagens já analisadas — carro que passou ontem ganha o modelo hoje, se
   o modelo dele passou a ser conhecido.

O que uma pessoa rotulou continua valendo acima de tudo (a tela mostra o
rótulo como "conferido"); a IA nunca escreve sobre o rótulo.

    py -3.13 -m scripts.veiculos_modelo_rodar               # contínuo
    py -3.13 -m scripts.veiculos_modelo_rodar --uma-volta   # processa o que há e sai
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.config import settings  # noqa: E402

LOG = RAIZ / "logs" / "veiculos_modelo.log"
PASTA_SONDA = Path(settings.VEICULO_MARCA_MODELO_DIR).parent / "modelo_carro"


def log(msg: str) -> None:
    linha = f"[{datetime.now():%d/%m %H:%M:%S}] {msg}"
    print(linha, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(linha + "\n")


def conectar() -> sqlite3.Connection:
    c = sqlite3.connect(settings.DATABASE_URL.split(":///")[-1], timeout=30)
    c.execute("pragma busy_timeout = 30000")
    colunas = {r[1] for r in c.execute("pragma table_info(passagens_veiculo)")}
    if "vetor_modelo" not in colunas:
        # O servidor faz a mesma migração ao subir; ela é idempotente.
        # 'null' (texto) também conta como não analisado: é como a coluna
        # JSON do servidor grava o vazio.
        c.execute("alter table passagens_veiculo add column vetor_modelo JSON")
        c.commit()
    return c


def _obs(texto) -> dict:
    # A coluna JSON grava "vazio" como o texto 'null', não como NULL.
    try:
        d = json.loads(texto) if texto else {}
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def aplicar(c: sqlite3.Connection, pid: str, v: np.ndarray, obs: dict) -> bool:
    from app.services.veiculos import modelo_fino
    marca, modelo, conf, motivo = modelo_fino.afirmar(v)
    if motivo:
        obs["marca_modelo"] = motivo
    else:
        obs.pop("marca_modelo", None)
    c.execute("update passagens_veiculo set marca=?, modelo=?, marca_modelo_conf=?, observacoes=? where id=?",
              (marca, modelo, conf, json.dumps(obs, ensure_ascii=False), pid))
    return bool(marca)


def analisar_pendentes(c: sqlite3.Connection, lote: int) -> int:
    from app.services import evidencias
    from app.services.veiculos import modelo_fino
    linhas = c.execute("""select id, recorte_key, observacoes from passagens_veiculo
                          where (vetor_modelo is null or vetor_modelo = 'null') and categoria <> 'motocicleta' and recorte_key <> ''
                          order by inicio desc limit ?""", (lote,)).fetchall()
    feitas = 0
    for pid, chave, obs_txt in linhas:
        obs = _obs(obs_txt)
        jpeg = evidencias.ler(chave)
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) if jpeg else None
        if img is None:
            obs["marca_modelo"] = "recorte não está mais em disco"
            c.execute("update passagens_veiculo set vetor_modelo='[]', observacoes=? where id=?",
                      (json.dumps(obs, ensure_ascii=False), pid))
            c.commit()
            continue
        v = modelo_fino.vetor(img)
        obs["sugestoes_modelo"] = modelo_fino.sugestoes(v)
        c.execute("update passagens_veiculo set vetor_modelo=? where id=?",
                  (json.dumps([round(float(x), 5) for x in v]), pid))
        aplicar(c, pid, v, obs)
        c.commit()
        feitas += 1
    return feitas


def rotulos_de_carro(c: sqlite3.Connection) -> int:
    return c.execute("""select count(*) from rotulos_veiculo r join passagens_veiculo p on p.id = r.passagem_id
                        where r.marca <> '' and p.vetor_modelo is not null and p.vetor_modelo not in ('[]', 'null')""").fetchone()[0]


def treinar_e_reaplicar(c: sqlite3.Connection) -> None:
    from scripts import veiculos_treinar
    rel = veiculos_treinar.rodar("modelo_carro", simular=False)
    liberadas = [k for k, v in (rel.get("classes") or {}).items() if v.get("liberada")]
    afirmados = 0
    linhas = c.execute("""select id, vetor_modelo, observacoes from passagens_veiculo
                          where vetor_modelo is not null and vetor_modelo not in ('[]', 'null')""").fetchall()
    for pid, vet, obs_txt in linhas:
        afirmados += aplicar(c, pid, np.asarray(json.loads(vet), np.float32), _obs(obs_txt))
    c.commit()
    log(f"sonda de marca/modelo treinada: {len(liberadas)} modelo(s) liberado(s) "
        f"{liberadas or ''}; {afirmados} de {len(linhas)} carros com modelo afirmado")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uma-volta", action="store_true")
    ap.add_argument("--lote", type=int, default=20)
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()
    from app.services.veiculos import modelo_fino
    modelo_fino.carregar(a.threads)
    modelo_fino.textos()
    c = conectar()
    log("analisador de marca/modelo iniciado")
    vistos = None
    while True:
        try:
            n = analisar_pendentes(c, a.lote)
            if n:
                log(f"{n} carro(s) analisado(s)")
            # Rótulo novo na tela ou base inicial nova em disco: treina de novo.
            base = PASTA_SONDA / "base_inicial.npz"
            rot = (rotulos_de_carro(c), base.stat().st_mtime if base.exists() else 0)
            if rot != vistos:
                treinar_e_reaplicar(c)
                vistos = rot
            if a.uma_volta and n == 0:
                break
            if n == 0:
                time.sleep(60)
        except sqlite3.OperationalError as exc:
            log(f"banco ocupado ({exc}); nova tentativa em 30 s")
            time.sleep(30)
    log("fim")


if __name__ == "__main__":
    main()
