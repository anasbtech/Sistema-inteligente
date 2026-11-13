"""MediaPipe Hands sobre o recorte de cada pessoa.

A mão inteira de alguém a cinco metros da câmera cabe em 20 pixels; o
detector da MediaPipe espera a mão grande no quadro. Por isso ele roda no
recorte da metade de cima de cada pessoa, ampliado — e só em pessoa com
altura suficiente para a mão poder ter os `gesto.MAO_MIN_PX`.

Custo medido em 10/11/2026: 40–190 ms por pessoa na CPU. O serviço limita
quantas pessoas por quadro e quantos quadros por segundo.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.guardian")

_det = None
_falhou = False
_trava = threading.Lock()


def disponivel() -> bool:
    return _carregar() is not None


def _carregar():
    global _det, _falhou
    if _det is not None or _falhou:
        return _det
    with _trava:
        if _det is not None or _falhou:
            return _det
        modelo = Path(settings.GUARDIAN_MODELO_MAOS)
        try:
            from mediapipe.tasks.python import BaseOptions, vision
            if not modelo.exists():
                raise FileNotFoundError(modelo)
            _det = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(modelo)),
                num_hands=2, min_hand_detection_confidence=0.45,
                min_hand_presence_confidence=0.45, running_mode=vision.RunningMode.IMAGE))
            log.info("Guardian: MediaPipe Hands carregado (%s)", modelo.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("Guardian sem detector de mãos (%s): o gesto não será avaliado.", exc)
            _falhou = True
    return _det


def maos_da_pessoa(quadro: np.ndarray, bbox: Tuple[float, float, float, float]
                   ) -> List[Tuple[list, float]]:
    """Mãos na metade de cima da pessoa: [(21 pontos em px do quadro, confiança)]."""
    det = _carregar()
    if det is None:
        return []
    import cv2
    import mediapipe as mp
    alt, larg = quadro.shape[:2]
    x1, y1, x2, y2 = bbox
    w, h = x2 - x1, y2 - y1
    # Metade de cima e uma folga lateral: o gesto se faz na altura do peito
    # ou do rosto, às vezes com o braço afastado do corpo.
    xa, xb = int(max(0, x1 - 0.25 * w)), int(min(larg, x2 + 0.25 * w))
    ya, yb = int(max(0, y1 - 0.05 * h)), int(min(alt, y1 + 0.70 * h))
    rec = quadro[ya:yb, xa:xb]
    if rec.size == 0:
        return []
    esc = max(1.0, 320.0 / max(rec.shape[:2]))
    if esc > 1.0:
        rec = cv2.resize(rec, None, fx=esc, fy=esc, interpolation=cv2.INTER_CUBIC)
    with _trava:
        res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                  data=cv2.cvtColor(rec, cv2.COLOR_BGR2RGB)))
    saida = []
    for i, lm in enumerate(res.hand_landmarks):
        pts = [[xa + p.x * rec.shape[1] / esc, ya + p.y * rec.shape[0] / esc] for p in lm]
        conf = 0.8
        try:
            conf = float(res.handedness[i][0].score)
        except (IndexError, AttributeError):
            pass
        saida.append((pts, conf))
    return saida
