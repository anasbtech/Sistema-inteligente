"""Detecção de objetos (YOLO), rastreamento e classificação de ações.

As ações aqui são derivadas de *trajetórias* — velocidade, permanência,
densidade, cruzamento de linha — e não de julgamento sobre pessoas. São
indícios para triagem humana; o sistema não conclui intenção.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("vigia.vision")

_yolo = None

# COCO -> classes operacionais do VIGIA
MAPA_CLASSES: Dict[str, str] = {
    "person": "pessoa",
    # Carro, caminhão e ônibus separados: para quem lê o registro depois, "um
    # caminhão às 3h" e "um carro às 3h" não são a mesma ocorrência.
    "car": "carro",
    "truck": "caminhao",
    "bus": "onibus",
    "motorcycle": "motocicleta",
    "bicycle": "bicicleta",
    "backpack": "mochila",
    "handbag": "mochila",
    "suitcase": "volume",
    "knife": "arma",
    # Animais que o COCO distingue. Solto em via pública é ocorrência
    # operacional, e a espécie não muda a conduta — por isso uma classe só.
    "dog": "animal", "cat": "animal", "horse": "animal", "cow": "animal",
    "sheep": "animal", "bird": "animal", "bear": "animal",
    "elephant": "animal", "zebra": "animal", "giraffe": "animal",
}

SEVERIDADE_OBJETO: Dict[str, int] = {
    "pessoa": 1, "carro": 1, "caminhao": 1, "onibus": 1, "bicicleta": 1,
    "motocicleta": 2, "animal": 2, "mochila": 2, "capacete": 2, "placa": 2,
    "volume": 4, "arma": 4,
}

SEVERIDADE_ACAO: Dict[str, int] = {
    "corrida": 2, "permanencia": 2, "contrafluxo": 2,
    "aglomeracao": 3, "abandono": 3, "vandalismo": 3,
    "queda": 4, "briga": 4, "invasao": 4,
}

ROTULO_ACAO: Dict[str, str] = {
    "corrida": "Corrida súbita",
    "queda": "Queda de pessoa",
    "aglomeracao": "Aglomeração",
    "briga": "Agressão física",
    "permanencia": "Permanência prolongada",
    "invasao": "Invasão de perímetro",
    "contrafluxo": "Deslocamento em contrafluxo",
    "abandono": "Abandono de objeto",
    "vandalismo": "Dano ao patrimônio",
}


@dataclass
class ObjetoDetectado:
    classe: str
    confianca: float
    bbox: Tuple[int, int, int, int]
    track_id: Optional[int] = None

    def centro(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def bbox_normalizado(self, largura: int, altura: int) -> List[float]:
        x1, y1, x2, y2 = self.bbox
        return [x1 / largura, y1 / altura, (x2 - x1) / largura, (y2 - y1) / altura]


@dataclass
class EventoAcao:
    classe: str
    rotulo: str
    confianca: float
    severidade: int
    duracao_s: int
    envolvidos: int
    bbox: Optional[Tuple[int, int, int, int]] = None
    track_id: Optional[int] = None


# ----------------------------------------------------------------------
# YOLO
# ----------------------------------------------------------------------
class ModelosIndisponiveis(RuntimeError):
    """Ultralytics não instalado — só o caminho com vídeo real é afetado."""


def yolo_disponivel() -> bool:
    try:
        import ultralytics  # noqa: F401
        return True
    except ImportError:
        return False


def obter_yolo():
    global _yolo
    if _yolo is not None:
        return _yolo
    try:
        from ultralytics import YOLO  # import tardio
    except ImportError as exc:
        raise ModelosIndisponiveis(
            "Ultralytics não está instalado. Para analisar vídeo real:\n"
            "    pip install ultralytics\n"
            "O restante do sistema funciona sem ele."
        ) from exc

    _yolo = YOLO(settings.OBJECT_MODEL)
    if settings.DEVICE == "cuda":
        _yolo.to("cuda")
    log.info("YOLO '%s' carregado (%s)", settings.OBJECT_MODEL, settings.DEVICE)
    return _yolo


def detectar_objetos(quadro: np.ndarray, limiar: Optional[float] = None,
                     rastrear: bool = True) -> List[ObjetoDetectado]:
    """Roda YOLO no quadro. Com `rastrear=True` usa ByteTrack e mantém track_id
    estável entre quadros — é o que permite medir velocidade e permanência."""
    lim = settings.OBJECT_THRESHOLD if limiar is None else limiar
    modelo = obter_yolo()

    if rastrear:
        resultados = modelo.track(quadro, persist=True, conf=lim, verbose=False,
                                  tracker="bytetrack.yaml")
    else:
        resultados = modelo(quadro, conf=lim, verbose=False)

    saida: List[ObjetoDetectado] = []
    for r in resultados:
        nomes = r.names
        if r.boxes is None:
            continue
        for box in r.boxes:
            nome_coco = nomes[int(box.cls[0])]
            classe = MAPA_CLASSES.get(nome_coco)
            if classe is None:
                continue
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
            tid = int(box.id[0]) if getattr(box, "id", None) is not None else None
            saida.append(ObjetoDetectado(classe, float(box.conf[0]), (x1, y1, x2, y2), tid))
    return saida


# ----------------------------------------------------------------------
# Geometria de zonas
# ----------------------------------------------------------------------
def ponto_em_poligono(ponto: Tuple[float, float], poligono: Sequence[Sequence[float]]) -> bool:
    """Ray casting. `poligono` em coordenadas normalizadas 0..1."""
    x, y = ponto
    dentro = False
    n = len(poligono)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poligono[i]
        xj, yj = poligono[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            dentro = not dentro
        j = i
    return dentro


def _lado(p: Tuple[float, float], a: Sequence[float], b: Sequence[float]) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def cruzou_linha(anterior: Tuple[float, float], atual: Tuple[float, float],
                 linha: Sequence[Sequence[float]]) -> Optional[str]:
    """Devolve 'entrada'/'saida' conforme o lado de onde veio, ou None."""
    if len(linha) < 2:
        return None
    a, b = linha[0], linha[1]
    s1, s2 = _lado(anterior, a, b), _lado(atual, a, b)
    if s1 == 0 or s2 == 0 or (s1 > 0) == (s2 > 0):
        return None
    return "entrada" if s1 > 0 else "saida"


# ----------------------------------------------------------------------
# Análise de ações
# ----------------------------------------------------------------------
@dataclass
class Trilha:
    track_id: int
    classe: str
    pontos: Deque[Tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=90))
    visto_em: float = field(default_factory=time.time)
    primeiro_em: float = field(default_factory=time.time)
    parado_desde: Optional[float] = None
    alertou: Dict[str, float] = field(default_factory=dict)

    def registrar(self, x: float, y: float, t: float) -> None:
        self.pontos.append((x, y, t))
        self.visto_em = t

    def velocidade(self) -> float:
        """Unidades normalizadas por segundo, na janela recente."""
        if len(self.pontos) < 5:
            return 0.0
        x0, y0, t0 = self.pontos[-5]
        x1, y1, t1 = self.pontos[-1]
        dt = max(t1 - t0, 1e-3)
        return float(np.hypot(x1 - x0, y1 - y0) / dt)

    def direcao(self) -> Optional[Tuple[float, float]]:
        if len(self.pontos) < 6:
            return None
        x0, y0, _ = self.pontos[-6]
        x1, y1, _ = self.pontos[-1]
        v = np.array([x1 - x0, y1 - y0])
        n = float(np.linalg.norm(v))
        return tuple(v / n) if n > 1e-4 else None

    def queda_vertical(self) -> float:
        """Razão de achatamento recente — proxy simples para queda."""
        if len(self.pontos) < 8:
            return 0.0
        _, y0, t0 = self.pontos[-8]
        _, y1, t1 = self.pontos[-1]
        dt = max(t1 - t0, 1e-3)
        return float(max(0.0, (y1 - y0) / dt))


class AnalisadorAcoes:
    """Mantém trilhas por câmera e emite eventos comportamentais.

    Parâmetros pensados para cena de rua a 25 fps; ajuste por câmera em produção.
    """

    def __init__(
        self,
        vel_corrida: float = 0.22,
        seg_permanencia: float = 90.0,
        n_aglomeracao: int = 8,
        raio_aglomeracao: float = 0.18,
        seg_abandono: float = 45.0,
        cooldown_s: float = 30.0,
    ) -> None:
        self.vel_corrida = vel_corrida
        self.seg_permanencia = seg_permanencia
        self.n_aglomeracao = n_aglomeracao
        self.raio_aglomeracao = raio_aglomeracao
        self.seg_abandono = seg_abandono
        self.cooldown_s = cooldown_s
        self.trilhas: Dict[int, Trilha] = {}
        self._aglomeracao_em: Optional[float] = None

    # -- utilidades internas ------------------------------------------
    def _pode_alertar(self, trilha: Trilha, classe: str, agora: float) -> bool:
        ultimo = trilha.alertou.get(classe, 0.0)
        if agora - ultimo < self.cooldown_s:
            return False
        trilha.alertou[classe] = agora
        return True

    def _limpar(self, agora: float, ttl: float = 8.0) -> None:
        mortos = [tid for tid, t in self.trilhas.items() if agora - t.visto_em > ttl]
        for tid in mortos:
            del self.trilhas[tid]

    # -- API ----------------------------------------------------------
    def atualizar(
        self,
        objetos: Sequence[ObjetoDetectado],
        largura: int,
        altura: int,
        zonas: Optional[Sequence[dict]] = None,
        agora: Optional[float] = None,
    ) -> List[EventoAcao]:
        t = time.time() if agora is None else agora
        eventos: List[EventoAcao] = []
        zonas = zonas or []

        pessoas_xy: List[Tuple[float, float]] = []

        for obj in objetos:
            if obj.track_id is None:
                continue
            cx, cy = obj.centro()
            nx, ny = cx / largura, cy / altura

            trilha = self.trilhas.get(obj.track_id)
            if trilha is None:
                trilha = Trilha(obj.track_id, obj.classe)
                self.trilhas[obj.track_id] = trilha
            anterior = (trilha.pontos[-1][0], trilha.pontos[-1][1]) if trilha.pontos else None
            trilha.registrar(nx, ny, t)

            if obj.classe == "pessoa":
                pessoas_xy.append((nx, ny))

            vel = trilha.velocidade()

            # --- corrida ---
            if obj.classe == "pessoa" and vel > self.vel_corrida and self._pode_alertar(trilha, "corrida", t):
                eventos.append(EventoAcao(
                    "corrida", ROTULO_ACAO["corrida"],
                    confianca=float(min(0.6 + vel, 0.97)),
                    severidade=SEVERIDADE_ACAO["corrida"],
                    duracao_s=int(t - trilha.primeiro_em),
                    envolvidos=1, bbox=obj.bbox, track_id=obj.track_id,
                ))

            # --- queda ---
            if obj.classe == "pessoa":
                x1, y1, x2, y2 = obj.bbox
                razao = (x2 - x1) / max(y2 - y1, 1)
                if razao > 1.35 and trilha.queda_vertical() > 0.05 and self._pode_alertar(trilha, "queda", t):
                    eventos.append(EventoAcao(
                        "queda", ROTULO_ACAO["queda"], confianca=0.82,
                        severidade=SEVERIDADE_ACAO["queda"],
                        duracao_s=1, envolvidos=1, bbox=obj.bbox, track_id=obj.track_id,
                    ))

            # --- permanência prolongada ---
            if vel < 0.012:
                trilha.parado_desde = trilha.parado_desde or t
                parado_ha = t - trilha.parado_desde
                if obj.classe == "pessoa" and parado_ha > self.seg_permanencia \
                        and self._pode_alertar(trilha, "permanencia", t):
                    eventos.append(EventoAcao(
                        "permanencia", ROTULO_ACAO["permanencia"], confianca=0.88,
                        severidade=SEVERIDADE_ACAO["permanencia"],
                        duracao_s=int(parado_ha), envolvidos=1,
                        bbox=obj.bbox, track_id=obj.track_id,
                    ))
                if obj.classe in ("volume", "mochila") and parado_ha > self.seg_abandono \
                        and self._pode_alertar(trilha, "abandono", t):
                    eventos.append(EventoAcao(
                        "abandono", ROTULO_ACAO["abandono"], confianca=0.90,
                        severidade=SEVERIDADE_ACAO["abandono"],
                        duracao_s=int(parado_ha), envolvidos=1,
                        bbox=obj.bbox, track_id=obj.track_id,
                    ))
            else:
                trilha.parado_desde = None

            # --- zonas: invasão e contrafluxo ---
            if anterior is not None:
                for zona in zonas:
                    pontos = zona.get("pontos") or []
                    regras = zona.get("regras") or {}
                    if zona.get("tipo") == "LINHA" and regras.get("invasao", True):
                        if cruzou_linha(anterior, (nx, ny), pontos) == "entrada" \
                                and self._pode_alertar(trilha, "invasao", t):
                            eventos.append(EventoAcao(
                                "invasao", ROTULO_ACAO["invasao"], confianca=0.93,
                                severidade=SEVERIDADE_ACAO["invasao"],
                                duracao_s=1, envolvidos=1,
                                bbox=obj.bbox, track_id=obj.track_id,
                            ))
                    elif zona.get("tipo") == "POLIGONO" and regras.get("contrafluxo"):
                        esperado = regras.get("direcao")  # ex.: [1, 0]
                        atual_dir = trilha.direcao()
                        if esperado and atual_dir and ponto_em_poligono((nx, ny), pontos):
                            if float(np.dot(atual_dir, esperado)) < -0.6 \
                                    and self._pode_alertar(trilha, "contrafluxo", t):
                                eventos.append(EventoAcao(
                                    "contrafluxo", ROTULO_ACAO["contrafluxo"], confianca=0.79,
                                    severidade=SEVERIDADE_ACAO["contrafluxo"],
                                    duracao_s=int(t - trilha.primeiro_em), envolvidos=1,
                                    bbox=obj.bbox, track_id=obj.track_id,
                                ))

        # --- aglomeração (nível de cena, não de indivíduo) ---
        if len(pessoas_xy) >= self.n_aglomeracao:
            maior = self._maior_agrupamento(pessoas_xy)
            if maior >= self.n_aglomeracao:
                if self._aglomeracao_em is None:
                    self._aglomeracao_em = t
                elif t - self._aglomeracao_em > 10.0:
                    eventos.append(EventoAcao(
                        "aglomeracao", ROTULO_ACAO["aglomeracao"],
                        confianca=float(min(0.6 + maior / 40.0, 0.96)),
                        severidade=SEVERIDADE_ACAO["aglomeracao"],
                        duracao_s=int(t - self._aglomeracao_em), envolvidos=maior,
                    ))
                    self._aglomeracao_em = t + self.cooldown_s
        else:
            self._aglomeracao_em = None

        self._limpar(t)
        return eventos

    def _maior_agrupamento(self, pontos: Sequence[Tuple[float, float]]) -> int:
        """Maior componente conexa sob o raio de proximidade (união-busca leve)."""
        n = len(pontos)
        pai = list(range(n))

        def achar(i: int) -> int:
            while pai[i] != i:
                pai[i] = pai[pai[i]]
                i = pai[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                if np.hypot(pontos[i][0] - pontos[j][0], pontos[i][1] - pontos[j][1]) <= self.raio_aglomeracao:
                    ri, rj = achar(i), achar(j)
                    if ri != rj:
                        pai[ri] = rj

        tamanhos: Dict[int, int] = defaultdict(int)
        for i in range(n):
            tamanhos[achar(i)] += 1
        return max(tamanhos.values()) if tamanhos else 0


def severidade_objeto(classe: str) -> int:
    return SEVERIDADE_OBJETO.get(classe, 1)
