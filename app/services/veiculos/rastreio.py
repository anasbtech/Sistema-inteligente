"""Rastreio de veículos com ByteTrack — um rastreador por câmera.

O laço geral desistiu do ByteTrack (inferencia.py, `_processar`) porque um
único rastreador atendia as 24 câmeras em rodízio: cada quadro era um corte
de cena e as trilhas morriam. Aqui cada câmera tem o seu, e recebe quadros
seguidos (VEICULO_FPS), que é a condição em que o ByteTrack funciona.

`track_buffer` é contado em quadros; ele é derivado do tempo que um veículo
pode sumir atrás de outro sem perder a identidade (VEICULO_SUMIU_S).
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import List

import numpy as np

from app.config import settings
from app.services.veiculos import deteccao


@dataclass
class Trilha:
    id: int
    bbox: tuple          # x1, y1, x2, y2 em pixels
    confianca: float
    classe: str


class RastreadorCamera:
    def __init__(self, fps: float | None = None) -> None:
        from ultralytics.trackers.byte_tracker import BYTETracker

        fps = fps or settings.VEICULO_FPS
        args = SimpleNamespace(
            tracker_type="bytetrack",
            track_high_thresh=settings.VEICULO_CONF_MIN,
            track_low_thresh=0.10,
            new_track_thresh=settings.VEICULO_CONF_MIN,
            track_buffer=max(3, int(round(settings.VEICULO_SUMIU_S * fps))),
            # Entre dois quadros a 3 fps o carro anda bem mais que a 30 fps:
            # o casamento por sobreposição precisa ser mais tolerante.
            match_thresh=0.85,
            fuse_score=True,
        )
        self._bt = BYTETracker(args)

    def atualizar(self, dets: np.ndarray, forma: tuple) -> List[Trilha]:
        """`dets`: N x 6 (x1, y1, x2, y2, conf, classe). Devolve as trilhas ativas
        neste quadro, com o id estável enquanto o veículo estiver em cena."""
        from ultralytics.engine.results import Boxes

        caixas = Boxes(dets if len(dets) else np.zeros((0, 6), np.float32), forma[:2])
        saida = self._bt.update(caixas)
        trilhas = []
        for linha in saida:
            x1, y1, x2, y2, tid, conf, cls = linha[:7]
            # A caixa que o detector viu neste quadro, não a prevista pelo
            # rastreador: a previsão (Kalman) se deforma quando o carro acelera
            # ou é encoberto — medido na Portaria, uma sombrinha na frente de
            # um carro esticou a caixa até a largura da rua.
            if len(linha) >= 8 and 0 <= int(linha[7]) < len(dets):
                x1, y1, x2, y2 = dets[int(linha[7])][:4]
            trilhas.append(Trilha(int(tid), (float(x1), float(y1), float(x2), float(y2)),
                                  float(conf), deteccao.nome(cls)))
        return trilhas
