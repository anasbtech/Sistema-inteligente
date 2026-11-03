"""Leitura de placa veicular sobre o recorte do veículo.

Duas etapas, dois modelos ONNX pequenos, ambos sobre o `onnxruntime` que o
reconhecimento facial já usa: um localiza a placa dentro do recorte, o outro
lê os caracteres.

O que decide se a leitura entra no registro não é o modelo ter respondido —
ele sempre responde. É a combinação de dois sinais, medidos nas capturas
reais desta instalação:

* **confiança por caractere.** Nas leituras corretas o mínimo ficou em 0,69,
  0,77, 0,84 e 1,00. Nas erradas, entre 0,09 e 0,25. Não há zona cinzenta
  entre os dois grupos.
* **formato brasileiro.** `TVMBI52` e `TVM8152` são o mesmo carro lido duas
  vezes; só o segundo é placa possível, e é o certo. O padrão desempata o
  que a confiança sozinha não desempata.

Placa errada gravada é pior que placa nenhuma: manda quem consulta atrás de
um veículo que não esteve ali.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.placa")

_detector = None
_ocr = None
_indisponivel = False

# Antiga `AAA1234` e Mercosul `AAA1A23`. Placa de moto e de carro têm o mesmo
# desenho de caracteres, então um padrão serve aos dois.
PADRAO_PLACA = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")


def disponivel() -> bool:
    if _indisponivel:
        return False
    try:
        import fast_plate_ocr  # noqa: F401
        import open_image_models  # noqa: F401
        return True
    except ImportError:
        return False


def _carregar() -> bool:
    """Carrega os dois modelos na primeira leitura. Falhou, não tenta de novo.

    Sem isto, uma instalação sem os pacotes tentaria importar a cada veículo
    que passa — e são dezenas por hora na portaria.
    """
    global _detector, _ocr, _indisponivel
    if _indisponivel:
        return False
    if _detector is not None and _ocr is not None:
        return True
    try:
        from fast_plate_ocr import LicensePlateRecognizer
        from open_image_models import create_detector

        _detector = create_detector(settings.PLACA_MODELO_DETECTOR)
        _ocr = LicensePlateRecognizer(settings.PLACA_MODELO_OCR)
        log.info("Leitura de placa ativa (%s + %s)",
                 settings.PLACA_MODELO_DETECTOR, settings.PLACA_MODELO_OCR)
        return True
    except Exception as exc:  # noqa: BLE001
        _indisponivel = True
        log.warning("Leitura de placa indisponível: %s", exc)
        return False


def normalizar(texto: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (texto or "").upper())


def ler(recorte_veiculo: np.ndarray) -> Optional[Tuple[str, float]]:
    """Placa legível dentro do recorte, ou `None`.

    `None` quando não há placa, quando ela é pequena demais para ter
    caracteres, quando a confiança não alcança o piso ou quando o texto não é
    placa brasileira possível.
    """
    if recorte_veiculo is None or recorte_veiculo.size == 0:
        return None
    if not settings.PLACA_LEITURA or not _carregar():
        return None

    import cv2

    try:
        caixas = _detector.predict(recorte_veiculo)
    except Exception as exc:  # noqa: BLE001
        log.warning("Detector de placa falhou: %s", exc)
        return None

    melhor: Optional[Tuple[str, float]] = None
    for caixa in caixas:
        b = caixa.bounding_box
        largura, altura = b.x2 - b.x1, b.y2 - b.y1
        # Placa menor que isto não tem pixel por caractere; o modelo devolve
        # texto mesmo assim, e foi de onde saiu `OO006` num recorte de 13x4.
        if largura < settings.PLACA_MIN_LARGURA_PX or altura < settings.PLACA_MIN_ALTURA_PX:
            continue

        recorte = recorte_veiculo[max(0, b.y1):b.y2, max(0, b.x1):b.x2]
        if recorte.size == 0:
            continue
        try:
            pred = _ocr.run(cv2.cvtColor(recorte, cv2.COLOR_BGR2RGB),
                            return_confidence=True)[0]
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR de placa falhou: %s", exc)
            continue

        texto = normalizar(pred.plate)
        if not PADRAO_PLACA.match(texto):
            continue
        confianca = float(min(pred.char_probs)) if pred.char_probs is not None else 0.0
        if confianca < settings.PLACA_CONFIANCA_MIN:
            continue
        # Mais de uma placa no recorte acontece quando o carro de trás entra
        # no enquadramento; fica a mais confiante, que é a do veículo em foco.
        if melhor is None or confianca > melhor[1]:
            melhor = (texto, confianca)

    return melhor
