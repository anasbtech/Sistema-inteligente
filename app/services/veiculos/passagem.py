"""Passagem: tudo o que se viu de um veículo, da entrada em cena até a saída.

O registro nasce **uma vez por passagem**, não por quadro: o que o rastreio
vê em vinte quadros vira um veículo, com a melhor imagem que houve dele. A
melhor é a maior, mais nítida e inteira dentro do quadro — é nela que cor,
marca/modelo e placa têm mais chance, e é ela que vai como evidência.

Carro estacionado não fica aberto para sempre: depois de
VEICULO_PASSAGEM_MAX_S ele é registrado e segue rastreado sem gerar outro
registro enquanto a trilha durar.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from app.config import settings

# Quantos recortes guardar para a leitura de placa (os maiores).
RECORTES_PLACA = 4


def _nitidez(recorte: np.ndarray) -> float:
    import cv2
    cinza = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
    if cinza.shape[1] > 320:
        cinza = cv2.resize(cinza, (320, int(320 * cinza.shape[0] / cinza.shape[1])))
    return float(cv2.Laplacian(cinza, cv2.CV_64F).var())


def recortar(quadro: np.ndarray, bbox, margem: float = 0.06) -> np.ndarray:
    alt, larg = quadro.shape[:2]
    x1, y1, x2, y2 = bbox
    mx, my = (x2 - x1) * margem, (y2 - y1) * margem
    x1, y1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
    x2, y2 = min(larg, int(x2 + mx)), min(alt, int(y2 + my))
    return quadro[y1:y2, x1:x2].copy()


@dataclass
class Passagem:
    camera: str
    trilha: int
    inicio: datetime
    visto_mono: float
    aberto_mono: float
    quadros: int = 0
    votos: Dict[str, float] = field(default_factory=dict)
    n_votos: Dict[str, int] = field(default_factory=dict)
    primeira_bbox: Optional[tuple] = None
    ultima_bbox: Optional[tuple] = None
    conf_max: float = 0.0
    melhor_nota: float = -1.0
    melhor_quadro: Optional[np.ndarray] = None
    melhor_bbox: Optional[tuple] = None
    melhor_conf: float = 0.0
    recortes_placa: List[tuple] = field(default_factory=list)   # (área, recorte)
    fim: Optional[datetime] = None
    registrada: bool = False
    # Tipo já visto pela sonda (prévia ou identificação), para a tarja ao vivo.
    tipo: Optional[tuple] = None            # (tipo, confiança)
    previa_pedida: bool = False

    @property
    def categoria(self) -> str:
        if not self.votos:
            return "veiculo"
        cat = max(self.votos, key=self.votos.get)
        # Mesma ressalva do detector geral (vision.PORTE_VEICULO): caminhão e
        # ônibus só com convicção. Hesitar entre os três é o caso do furgão —
        # a Kombi da Portaria saía "ônibus" a 0,5. Na dúvida, "veículo": não
        # é falso nem para a Kombi nem para um caminhão de verdade. Quem
        # separa van, picape e os portes de caminhão é a sonda de tipo.
        if cat in ("caminhao", "onibus"):
            n = self.n_votos.get(cat, 1)
            if self.votos[cat] / n < settings.OBJECT_PORTE_MIN:
                return "veiculo"
        return cat

    @property
    def categoria_conf(self) -> float:
        total = sum(self.votos.values())
        if not total:
            return 0.0
        cat = max(self.votos, key=self.votos.get)
        return round(self.votos[cat] / total * self.conf_max, 3)

    def ver(self, quadro: np.ndarray, bbox: tuple, conf: float, classe: str,
            agora: datetime, mono: float) -> None:
        self.quadros += 1
        self.visto_mono = mono
        self.fim = agora
        self.votos[classe] = self.votos.get(classe, 0.0) + conf
        self.n_votos[classe] = self.n_votos.get(classe, 0) + 1
        if self.primeira_bbox is None:
            self.primeira_bbox = bbox
        self.ultima_bbox = bbox
        self.conf_max = max(self.conf_max, conf)
        alt, larg = quadro.shape[:2]
        x1, y1, x2, y2 = bbox
        lado = min(x2 - x1, y2 - y1)
        if lado < settings.VEICULO_LADO_MIN_PX:
            return
        # Cortado na borda do quadro esconde metade do carro (e às vezes a placa).
        inteiro = x1 > 4 and y1 > 4 and x2 < larg - 4 and y2 < alt - 4
        area = (x2 - x1) * (y2 - y1)
        rec = recortar(quadro, bbox)
        if rec.size == 0:
            return
        nota = area * conf * (1.0 if inteiro else 0.4) * min(1.0, _nitidez(rec) / 150.0 + 0.2)
        if nota > self.melhor_nota:
            self.melhor_nota, self.melhor_quadro = nota, quadro
            self.melhor_bbox, self.melhor_conf = bbox, conf
        self.recortes_placa.append((area, recortar(quadro, bbox, 0.02)))
        self.recortes_placa.sort(key=lambda t: -t[0])
        del self.recortes_placa[RECORTES_PLACA:]

    def parado(self) -> bool:
        """Não saiu do lugar: o centro andou menos que 15% da largura."""
        if self.primeira_bbox is None or self.ultima_bbox is None:
            return False
        a, b = self.primeira_bbox, self.ultima_bbox
        larg = max(1.0, a[2] - a[0])
        dx = ((a[0] + a[2]) - (b[0] + b[2])) / 2
        dy = ((a[1] + a[3]) - (b[1] + b[3])) / 2
        return (dx * dx + dy * dy) ** 0.5 < 0.15 * larg

    def vale_registro(self) -> bool:
        return (self.quadros >= 2 and self.melhor_quadro is not None
                and self.conf_max >= settings.VEICULO_REGISTRO_CONF_MIN)


def _iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    uniao = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / uniao if uniao > 0 else 0.0


class Passagens:
    """As passagens abertas de uma câmera.

    Lembra também os veículos **parados** já registrados. O rastreio perde e
    recria a trilha de um carro estacionado sempre que o detector oscila —
    medido na Portaria em 06/11/2026: a Kombi parada na borda virou 12
    passagens em 4 minutos. Trilha nova que nasce onde há um parado
    conhecido é o mesmo veículo: segue rastreada, sem novo registro.
    """

    def __init__(self, camera: str) -> None:
        self.camera = camera
        self.abertas: Dict[int, Passagem] = {}
        self.parados: List[list] = []      # [bbox, visto_mono]

    def _parado(self, bbox, mono: float) -> Optional[list]:
        """O parado conhecido neste lugar: [bbox, visto_mono, tipo]."""
        validade = settings.VEICULO_PARADO_MEMORIA_S
        self.parados = [p for p in self.parados if mono - p[1] < validade]
        for p in self.parados:
            if _iou(p[0], bbox) >= 0.5:
                p[0], p[1] = bbox, mono
                return p
        return None

    def _parado_conhecido(self, bbox, mono: float) -> bool:
        return self._parado(bbox, mono) is not None

    def _lembrar_se_parado(self, p: Passagem, mono: float) -> None:
        if p.parado() and p.ultima_bbox is not None:
            conhecido = self._parado(p.ultima_bbox, mono)
            if conhecido is None:
                self.parados.append([p.ultima_bbox, mono, p.tipo])
            elif p.tipo:
                conhecido[2] = p.tipo

    def atualizar(self, quadro: np.ndarray, trilhas, agora: Optional[datetime] = None,
                  mono: Optional[float] = None) -> List[Passagem]:
        """Registra o quadro e devolve as passagens prontas para identificar."""
        agora = agora or datetime.now(timezone.utc)
        mono = time.monotonic() if mono is None else mono
        for t in trilhas:
            p = self.abertas.get(t.id)
            if p is None:
                p = self.abertas[t.id] = Passagem(self.camera, t.id, agora, mono, mono)
                # Nasceu onde já há um parado registrado: é ele de novo — e
                # herda o tipo que já se sabia dele.
                conhecido = self._parado(t.bbox, mono)
                if conhecido is not None:
                    p.registrada = True
                    if conhecido[2]:
                        p.tipo, p.previa_pedida = conhecido[2], True
            p.ver(quadro, t.bbox, t.confianca, t.classe, agora, mono)
            if p.registrada and p.parado():
                conhecido = self._parado(t.bbox, mono)   # renova a memória
                if conhecido is not None and p.tipo and not conhecido[2]:
                    conhecido[2] = p.tipo

        prontas = []
        for tid, p in list(self.abertas.items()):
            sumiu = mono - p.visto_mono >= settings.VEICULO_SUMIU_S
            longa = mono - p.aberto_mono >= settings.VEICULO_PASSAGEM_MAX_S
            if sumiu:
                del self.abertas[tid]
                if not p.registrada and p.vale_registro():
                    prontas.append(p)
                self._lembrar_se_parado(p, mono)
            elif longa and not p.registrada and p.vale_registro():
                p.registrada = True          # segue rastreado, sem novo registro
                prontas.append(p)
                self._lembrar_se_parado(p, mono)
        return prontas

    def encerrar_todas(self) -> List[Passagem]:
        prontas = [p for p in self.abertas.values() if not p.registrada and p.vale_registro()]
        self.abertas.clear()
        return prontas
