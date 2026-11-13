"""Análise do mapa de calor histórico de uma câmera.

Duas bases, ambas reais e gravadas pelo sistema:

* **ocupação** (`OcupacaoHora`): segundos de presença por ponto do chão,
  pico simultâneo, aglomerações. É o mapa de "onde as pessoas ficam". Existe
  desde que o acumulador passou a gravar.
* **passagens** (`Deteccao` de pessoa): uma linha por presença, pelo ponto
  em que a pessoa apareceu. Cobre todo o histórico do registro.

Nada aqui identifica ninguém: contagens e segundos por célula.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from app.services.ocupacao import COLUNAS, LINHAS

DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
ZONAS_C, ZONAS_L = 6, 4


def granularidade(inicio: datetime, fim: datetime) -> str:
    dias = (fim - inicio).total_seconds() / 86400
    return "hora" if dias <= 2.01 else ("dia" if dias <= 62 else "semana")


def _local(t: datetime) -> datetime:
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone()


def _balde(t: datetime, grao: str) -> datetime:
    t = _local(t)
    if grao == "hora":
        return t.replace(minute=0, second=0, microsecond=0)
    d = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday()) if grao == "semana" else d


def baldes(inicio: datetime, fim: datetime, grao: str) -> list[datetime]:
    passo = {"hora": timedelta(hours=1), "dia": timedelta(days=1), "semana": timedelta(days=7)}[grao]
    t, out = _balde(inicio, grao), []
    while t < _local(fim) and len(out) < 2000:
        out.append(t)
        t = (t + passo).astimezone()    # atravessa horário de verão sem escorregar
    return out


def _grade_densa(esparsa: dict, G: np.ndarray) -> None:
    for k, v in (esparsa or {}).items():
        i = int(k)
        if 0 <= i < G.size:
            G.flat[i] += float(v)


def _celula_passagem(bbox) -> Optional[int]:
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except ValueError:
            return None
    if not bbox or len(bbox) < 4:
        return None
    x, y, w, h = (float(v) for v in bbox[:4])
    px, py = x + w / 2, y + h * 0.97
    if not (0.0 <= px <= 1.0 and 0.0 <= py <= 1.0):
        return None
    return min(LINHAS - 1, int(py * LINHAS)) * COLUNAS + min(COLUNAS - 1, int(px * COLUNAS))


def suavizar(G: np.ndarray, sigma: float = 1.6) -> np.ndarray:
    r = int(sigma * 3)
    k = np.exp(-np.arange(-r, r + 1) ** 2 / (2 * sigma * sigma)); k /= k.sum()
    out = np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 1, G)
    return np.apply_along_axis(lambda v: np.convolve(v, k, mode="same"), 0, out)


def regiao(cx: float, cy: float) -> str:
    v = "no alto" if cy < 1 / 3 else ("embaixo" if cy >= 2 / 3 else "no meio")
    h = "à esquerda" if cx < 1 / 3 else ("à direita" if cx >= 2 / 3 else "ao centro")
    return "no centro da imagem" if (v, h) == ("no meio", "ao centro") else f"{v}, {h}"


def zonas(G: np.ndarray) -> list[float]:
    """Fração do total em cada zona A1…D6 (4 linhas × 6 colunas)."""
    tot = float(G.sum()) or 1.0
    out = []
    for zl in range(ZONAS_L):
        for zc in range(ZONAS_C):
            l0, l1 = zl * LINHAS // ZONAS_L, (zl + 1) * LINHAS // ZONAS_L
            c0, c1 = zc * COLUNAS // ZONAS_C, (zc + 1) * COLUNAS // ZONAS_C
            out.append(round(float(G[l0:l1, c0:c1].sum()) / tot, 4))
    return out


def focos(G: np.ndarray, n: int = 5, minimo: float = 0.03) -> list[dict]:
    """Áreas mais quentes: máximos locais do mapa suavizado, cada um com a
    fração do total que cai no seu entorno. Entorno já contado por um foco
    maior não volta a contar (não há dois focos na mesma mancha)."""
    tot = float(G.sum())
    if tot <= 0:
        return []
    S = suavizar(G, 1.8)
    usado = np.zeros_like(G, dtype=bool)
    out = []
    raio = 4
    yy, xx = np.mgrid[0:LINHAS, 0:COLUNAS]
    for _ in range(n * 4):
        S2 = np.where(usado, -1, S)
        i = int(np.argmax(S2))
        if S2.flat[i] <= 0:
            break
        ly, lx = divmod(i, COLUNAS)
        m = ((yy - ly) ** 2 + (xx - lx) ** 2 <= raio * raio) & ~usado
        frac = float(G[m].sum()) / tot
        usado |= (yy - ly) ** 2 + (xx - lx) ** 2 <= (raio + 2) ** 2
        if frac < minimo:
            continue
        cx, cy = (lx + .5) / COLUNAS, (ly + .5) / LINHAS
        out.append({"x": round(cx, 4), "y": round(cy, 4), "raio": round((raio + .5) / COLUNAS, 4),
                    "fracao": round(frac, 4), "regiao": regiao(cx, cy)})
        if len(out) >= n:
            break
    return out


async def coletar(db, cam, inicio: datetime, fim: datetime, grao: str) -> dict:
    """Os números de um período: grades, série por balde e semana × hora."""
    from sqlalchemy import select

    from app.models import Deteccao, OcupacaoHora, TipoDeteccao

    horas = (await db.execute(select(OcupacaoHora).where(
        OcupacaoHora.camera_id == cam.id,
        OcupacaoHora.inicio >= inicio - timedelta(hours=1), OcupacaoHora.inicio < fim))).scalars().all()
    horas = [h for h in horas if _local(h.inicio) + timedelta(hours=1) > _local(inicio)]
    passagens = (await db.execute(select(Deteccao.bbox, Deteccao.ocorrido_em).where(
        Deteccao.camera_id == cam.id, Deteccao.tipo == TipoDeteccao.OBJETO, Deteccao.classe == "pessoa",
        Deteccao.ocorrido_em >= inicio, Deteccao.ocorrido_em < fim))).all()

    lista = baldes(inicio, fim, grao)
    pos = {b: i for i, b in enumerate(lista)}
    serie = [{"inicio": b.isoformat(), "passagens": 0, "presenca_s": 0.0, "observado_s": 0.0,
              "pico": 0, "grupo": 0, "distintas": 0} for b in lista]
    Gp, Gg, Gv, Gpass = (np.zeros((LINHAS, COLUNAS), np.float64) for _ in range(4))
    semana = [[0.0] * 24 for _ in range(7)]
    semana_pass = [[0] * 24 for _ in range(7)]
    k = {"observado_s": 0.0, "presenca_s": 0.0, "veiculos_s": 0.0, "grupo_s": 0.0,
         "pico": 0, "pico_em": None, "maior_grupo": 0, "maior_grupo_em": None}
    por_hora_grupo = []
    for h in horas:
        _grade_densa(h.grade_pessoas, Gp); _grade_densa(h.grade_grupos, Gg); _grade_densa(h.grade_veiculos, Gv)
        for c in ("observado_s", "presenca_s", "veiculos_s", "grupo_s"):
            k[c] += float(getattr(h, c) or 0)
        if (h.pico or 0) > k["pico"]:
            k["pico"], k["pico_em"] = h.pico, h.pico_em
        if (h.maior_grupo or 0) > k["maior_grupo"]:
            k["maior_grupo"], k["maior_grupo_em"] = h.maior_grupo, h.maior_grupo_em
        if h.maior_grupo:
            por_hora_grupo.append((h.maior_grupo, h.maior_grupo_em or h.inicio, float(h.grupo_s or 0)))
        loc = _local(h.inicio)
        semana[loc.weekday()][loc.hour] += float(h.presenca_s or 0)
        i = pos.get(_balde(h.inicio, grao))
        if i is not None:
            s = serie[i]
            s["presenca_s"] += float(h.presenca_s or 0); s["observado_s"] += float(h.observado_s or 0)
            s["pico"] = max(s["pico"], h.pico or 0); s["grupo"] = max(s["grupo"], h.maior_grupo or 0)
            s["distintas"] += len(h.trilhas or [])
    sem_posicao = 0
    for bbox, quando in passagens:
        loc = _local(quando)
        semana_pass[loc.weekday()][loc.hour] += 1
        i = pos.get(_balde(quando, grao))
        if i is not None:
            serie[i]["passagens"] += 1
        cel = _celula_passagem(bbox)
        if cel is None:
            sem_posicao += 1
        else:
            Gpass.flat[cel] += 1
    for s in serie:
        s["presenca_s"] = round(s["presenca_s"], 1); s["observado_s"] = round(s["observado_s"], 1)
    k["passagens"] = len(passagens)
    k["sem_posicao"] = sem_posicao
    k["ocupacao_media"] = round(k["presenca_s"] / k["observado_s"], 2) if k["observado_s"] else None
    k["cobertura"] = round(min(1.0, k["observado_s"] / max(1.0, (fim - inicio).total_seconds())), 4)
    k["horas_com_dados"] = len(horas)
    return {"kpis": k, "serie": serie, "semana": semana, "semana_passagens": semana_pass,
            "G": {"ocupacao": Gp, "grupos": Gg, "veiculos": Gv, "passagens": Gpass},
            "grupos_hora": sorted(por_hora_grupo, key=lambda t: -t[0])}


def compactar(G: np.ndarray) -> list:
    """Grade em linhas de inteiros 0–1000 (normalizada pelo máximo): é só o
    que a tela precisa para pintar, e cabe em poucos KB."""
    m = float(G.max())
    if m <= 0:
        return []
    return np.rint(G / m * 1000).astype(int).tolist()
