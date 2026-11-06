"""Conformidade de EPI: o que cada pessoa em cena está usando.

O modelo do parque (COCO) não conhece capacete, colete, luva nem óculos. Este
serviço roda um detector próprio de EPI — SafetyVision v2 (YOLOv8s, 13
classes, 80 mil imagens, mAP@0,5 de teste 0,75; AGPL-3.0, como o Ultralytics
que o sistema já usa) — no **recorte de cada pessoa**, ampliado: na cena
inteira, luva e óculos têm poucos pixels.

O modelo vem em ONNX de propósito: só pesos, sem código executável ao abrir.

Cada item sai em um de três estados, e o terceiro é o que torna a tela
honesta:

* **detectado** — o modelo viu o item;
* **ausente** — o modelo viu a *falta* (ele tem classes "sem capacete",
  "sem colete"...);
* **nao_verificado** — nem um nem outro: rosto de costas, item fora do
  quadro, ou item que o modelo não cobre (botas).

"Em conformidade" só quando todo item exigido foi *detectado*; qualquer
ausente é "fora de conformidade"; o resto é "inconclusivo". Medido nas
imagens de referência: colete e capacete saem bem, óculos razoável, luvas
fracas — e botas não existem no modelo.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.epi")

# Itens que o sistema conhece, na ordem do corpo (cabeça aos pés).
ITENS: Dict[str, str] = {
    "capacete": "Capacete",
    "oculos": "Óculos de proteção",
    "mascara": "Máscara",
    "colete": "Colete refletivo",
    "luvas": "Luvas",
    "botas": "Botas de segurança",
}
# Classe do modelo -> (item, estado).
MAPA = {
    "Hardhat": ("capacete", "detectado"), "NO-Hardhat": ("capacete", "ausente"),
    "Goggles": ("oculos", "detectado"), "NO-Goggles": ("oculos", "ausente"),
    "Mask": ("mascara", "detectado"), "NO-Mask": ("mascara", "ausente"),
    "Safety Vest": ("colete", "detectado"), "NO-Safety Vest": ("colete", "ausente"),
    "Gloves": ("luvas", "detectado"), "NO-Gloves": ("luvas", "ausente"),
}
# O modelo não tem classe de bota: o item existe na tela, sempre "não verificado".
SEM_DETECTOR = {"botas"}

_modelo = None
_trava = threading.Lock()
_indisponivel = False


def disponivel() -> bool:
    return not _indisponivel and Path(settings.EPI_MODEL).exists()


def _carregar():
    global _modelo, _indisponivel
    if _modelo is not None or _indisponivel:
        return _modelo
    with _trava:
        if _modelo is None and not _indisponivel:
            try:
                from ultralytics import YOLO
                _modelo = YOLO(settings.EPI_MODEL, task="detect")
                log.info("Detector de EPI carregado (%s)", Path(settings.EPI_MODEL).name)
            except Exception as exc:  # noqa: BLE001
                _indisponivel = True
                log.warning("Detector de EPI indisponível: %s", exc)
    return _modelo


def recorte_da_pessoa(quadro: np.ndarray, bbox) -> Tuple[np.ndarray, Tuple[int, int]]:
    """A pessoa com folga: capacete passa da caixa por cima, luva pelos lados."""
    alt, larg = quadro.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    w, h = x2 - x1, y2 - y1
    X1, Y1 = max(0, int(x1 - w * .18)), max(0, int(y1 - h * .10))
    X2, Y2 = min(larg, int(x2 + w * .18)), min(alt, int(y2 + h * .04))
    return quadro[Y1:Y2, X1:X2], (X1, Y1)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def _procurar_luvas(m, quadro: np.ndarray, bbox) -> List[tuple]:
    """Segunda passada só para luvas: a área da pessoa em 6 pedaços ampliados.

    Na passada normal a luva tem poucos pixels e o modelo não a vê — medido
    numa foto com as duas mãos enluvadas e à vista: nada, nem com limiar 5%.
    Ampliando a região da mão, 81% e 84%. Como a mão pode estar levantada,
    abaixada ou aberta para o lado, a área ganha folga larga para os lados e
    para cima e é varrida em pedaços, em vez de adivinhar onde a mão está.
    Devolve (estado, confiança, caixa) por mão, sem repetir a mesma mão.
    """
    import cv2

    alt, larg = quadro.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    w, h = x2 - x1, y2 - y1
    X1, Y1 = max(0, int(x1 - w * .45)), max(0, int(y1 - h * .30))
    X2, Y2 = min(larg, int(x2 + w * .45)), min(alt, int(y2 + h * .05))
    area = quadro[Y1:Y2, X1:X2]
    ah, aw = area.shape[:2]
    if ah < 16 or aw < 16:
        return []
    achados = []
    for cx in range(3):
        for cy in range(2):
            tw, th = aw / 3 * 1.35, ah / 2 * 1.35
            tx1 = max(0, int(min(aw - tw, cx * aw / 3 - (tw - aw / 3) / 2)))
            ty1 = max(0, int(min(ah - th, cy * ah / 2 - (th - ah / 2) / 2)))
            peca = area[ty1:int(ty1 + th), tx1:int(tx1 + tw)]
            if peca.size == 0:
                continue
            f = 640 / max(peca.shape[:2])
            r = m(cv2.resize(peca, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC),
                  conf=settings.EPI_CONF_MIN, imgsz=640, verbose=False)[0]
            for c, p, b in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist(), r.boxes.xyxy.tolist()):
                par = MAPA.get(m.names[int(c)])
                if not par or par[0] != "luvas":
                    continue
                achados.append((par[1], float(p), [X1 + tx1 + b[0] / f, Y1 + ty1 + b[1] / f,
                                                   X1 + tx1 + b[2] / f, Y1 + ty1 + b[3] / f]))
    achados.sort(key=lambda a: -a[1])
    maos: List[tuple] = []
    for a in achados:                       # uma caixa por mão
        if all(_iou(a[2], o[2]) < 0.3 for o in maos):
            maos.append(a)
    return maos[:2]


def analisar_pessoa(quadro: np.ndarray, bbox, exigidos: List[str],
                    procurar_luvas: bool = True) -> dict:
    """EPI de uma pessoa: estado de cada item, caixas e o resultado."""
    m = _carregar()
    itens = {k: {"estado": "nao_verificado", "confianca": None, "bbox": None} for k in ITENS}
    if m is not None:
        rec, (ox, oy) = recorte_da_pessoa(quadro, bbox)
        if rec.size:
            r = m(rec, conf=settings.EPI_CONF_MIN, imgsz=640, verbose=False)[0]
            for cls, conf, xyxy in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist(),
                                       r.boxes.xyxy.tolist()):
                par = MAPA.get(m.names[int(cls)])
                if par is None:
                    continue
                item, estado = par
                atual = itens[item]
                # "Ausente" só vence "detectado" se for mais confiante.
                if atual["confianca"] is None or conf > atual["confianca"]:
                    itens[item] = {"estado": estado, "confianca": round(float(conf), 3),
                                   "bbox": [xyxy[0] + ox, xyxy[1] + oy, xyxy[2] + ox, xyxy[3] + oy]}
        # Luva exigida e não vista: a passada ampliada (custa ~1 s por pedaço
        # com a máquina ocupada, por isso só quando faz diferença).
        if procurar_luvas and "luvas" in exigidos and itens["luvas"]["estado"] != "detectado":
            maos = _procurar_luvas(m, quadro, bbox)
            if maos:
                melhor = maos[0]
                itens["luvas"] = {"estado": melhor[0], "confianca": round(melhor[1], 3),
                                  "bbox": [round(v, 1) for v in melhor[2]],
                                  "caixas": [[round(v, 1) for v in x[2]] for x in maos]}
    exig = [e for e in exigidos if e in ITENS]
    estados = [itens[e]["estado"] for e in exig]
    if not exig:
        resultado = "sem_exigencia"
    elif "ausente" in estados:
        resultado = "fora"
    elif all(s == "detectado" for s in estados):
        resultado = "conforme"
    else:
        resultado = "inconclusivo"
    return {"itens": itens, "exigidos": exig, "resultado": resultado,
            "faltando": [e for e in exig if itens[e]["estado"] == "ausente"]}


def analisar_quadro(quadro: np.ndarray, pessoas: List[tuple], exigidos: List[str],
                    luvas_ate: int = 2) -> List[dict]:
    """`pessoas`: lista de (bbox, confiança, track_id), as mais próximas primeiro.

    A passada ampliada das luvas só nas `luvas_ate` primeiras: quem está ao
    fundo não tem mão com pixels para isso, e o custo cresce por pessoa.
    """
    saida = []
    for n, (bbox, conf, tid) in enumerate(pessoas):
        r = analisar_pessoa(quadro, bbox, exigidos, procurar_luvas=n < luvas_ate)
        r.update({"pessoa_bbox": [float(v) for v in bbox], "pessoa_conf": round(float(conf), 3),
                  "track_id": tid})
        saida.append(r)
    return saida
