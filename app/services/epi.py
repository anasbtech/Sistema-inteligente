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


def analisar_pessoa(quadro: np.ndarray, bbox, exigidos: List[str]) -> dict:
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


def analisar_quadro(quadro: np.ndarray, pessoas: List[tuple], exigidos: List[str]) -> List[dict]:
    """`pessoas`: lista de (bbox, confiança, track_id)."""
    saida = []
    for bbox, conf, tid in pessoas:
        r = analisar_pessoa(quadro, bbox, exigidos)
        r.update({"pessoa_bbox": [float(v) for v in bbox], "pessoa_conf": round(float(conf), 3),
                  "track_id": tid})
        saida.append(r)
    return saida
