"""Signal for Help: a sequência da mão, quadro a quadro.

O gesto (Canadian Women's Foundation, 2020) tem dois tempos:

1. **palma aberta para a câmera com o polegar dobrado sobre ela**;
2. **os quatro dedos fecham sobre o polegar**, que fica preso dentro do punho.

Nenhum dos dois tempos sozinho é o gesto. Mão aberta com o polegar recolhido
aparece ao contar "quatro"; punho com o polegar dentro aparece em quem segura
algo. O que identifica é **a passagem de um para o outro**, em poucos segundos,
na mesma mão. Repetir a passagem reforça.

Entrada: os 21 pontos do MediaPipe Hands (0 = punho; 1–4 polegar; 5–8
indicador; 9–12 médio; 13–16 anelar; 17–20 mínimo), em pixels. As distâncias
são medidas na largura da palma (punho → base do médio), então o resultado
não depende de quão perto a mão está — mas a mão precisa ter pixels para os
dedos existirem: abaixo de `MAO_MIN_PX` nada é avaliado.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

ABERTA_POLEGAR = "aberta_polegar_dobrado"
PUNHO_POLEGAR = "punho_polegar_preso"
OUTRO = "outro"

DEDOS = ((5, 6, 8), (9, 10, 12), (13, 14, 16), (17, 18, 20))   # base, meio, ponta

# Abaixo disto os dedos se confundem: medido em 10/11/2026, nas câmeras de
# sala as mãos achadas tinham 18 e 32 px e os pontos dos dedos não se
# separavam. É o que decide em quais câmeras o gesto é legível.
MAO_MIN_PX = 48
JANELA_S = 3.0          # do tempo 1 ao tempo 2
QUADROS_MIN = 2         # cada tempo visto em pelo menos 2 quadros
INTERVALO_S = 20.0      # mesma mão, novo aviso só depois disto


def _d(a, b) -> float:
    return float(np.hypot(a[0] - b[0], a[1] - b[1]))


def tamanho(pontos: Sequence[Sequence[float]]) -> float:
    p = np.asarray(pontos, float)
    return float(max(np.ptp(p[:, 0]), np.ptp(p[:, 1])))


def classificar(pontos: Sequence[Sequence[float]]) -> Tuple[str, float]:
    """(estado, firmeza 0..1) de uma mão. `OUTRO` quando não é nenhum tempo."""
    p = np.asarray(pontos, float)
    if p.shape != (21, 2) or tamanho(p) < MAO_MIN_PX:
        return OUTRO, 0.0
    palma = _d(p[0], p[9])
    if palma < 1e-3:
        return OUTRO, 0.0
    punho = p[0]
    # Dedo esticado: a ponta está bem mais longe do punho que a articulação
    # do meio; dobrado: a ponta voltou para perto da base do dedo.
    estic = [_d(p[t], punho) > 1.12 * _d(p[m], punho) for _, m, t in DEDOS]
    dobra = [_d(p[t], punho) < 0.98 * _d(p[m], punho) or _d(p[t], p[b]) < 0.55 * palma
             for b, m, t in DEDOS]
    centro = p[[0, 5, 9, 13, 17]].mean(axis=0)
    ponta_pol = p[4]
    # Polegar recolhido: a ponta foi para o meio da palma (entre o indicador e
    # o anelar), não ficou para fora, ao lado da mão.
    polegar_dentro = (_d(ponta_pol, centro) < 0.62 * palma
                      or min(_d(ponta_pol, p[9]), _d(ponta_pol, p[13])) < 0.48 * palma)
    # Polegar preso: dentro do punho, sob os dedos dobrados.
    dedos_dobrados = p[[6, 10, 14, 18]].mean(axis=0)
    polegar_preso = (_d(ponta_pol, dedos_dobrados) < 0.55 * palma
                     or _d(ponta_pol, centro) < 0.55 * palma)
    n_estic, n_dobra = sum(estic), sum(dobra)
    if n_estic >= 3 and polegar_dentro:
        return ABERTA_POLEGAR, min(1.0, 0.55 + 0.15 * (n_estic - 3) + 0.3 * (1 - _d(ponta_pol, centro) / palma))
    if n_dobra >= 3 and polegar_preso:
        return PUNHO_POLEGAR, min(1.0, 0.55 + 0.15 * (n_dobra - 3) + 0.3 * (1 - _d(ponta_pol, dedos_dobrados) / palma))
    return OUTRO, 0.0


@dataclass
class Sinal:
    trilha: int
    em: float
    confianca: float
    repeticoes: int
    tamanho_px: float
    centro: Tuple[float, float]


@dataclass
class _Historico:
    centro: Tuple[float, float]
    visto: float
    estados: List[Tuple[float, str, float]] = field(default_factory=list)
    ultimo_aviso: float = -1e9
    repeticoes: int = 0


class SequenciaGesto:
    """Acompanha as mãos de uma câmera e avisa quando a sequência fecha.

    A mão é seguida pela posição (a mais próxima do quadro anterior, dentro
    de meia largura de mão), sem rastreio de pessoa: o gesto dura segundos e a
    mão quase não sai do lugar enquanto ele acontece.
    """

    def __init__(self) -> None:
        self._maos: Dict[int, _Historico] = {}
        self._prox = 1

    def _associar(self, centro, tam, agora) -> int:
        melhor, dist = None, 1e9
        for tid, h in self._maos.items():
            if agora - h.visto > 2.0:
                continue
            d = _d(centro, h.centro)
            if d < dist:
                melhor, dist = tid, d
        if melhor is not None and dist < 0.8 * max(tam, 1.0):
            return melhor
        tid = self._prox
        self._prox += 1
        self._maos[tid] = _Historico(centro, agora)
        return tid

    def observar(self, maos: Sequence[Tuple[Sequence[Sequence[float]], float]], agora: float) -> List[Sinal]:
        """`maos`: [(21 pontos em px, confiança da detecção)]. Devolve os sinais fechados."""
        sinais = []
        for pontos, conf_det in maos:
            estado, firmeza = classificar(pontos)
            p = np.asarray(pontos, float)
            tam = tamanho(p)
            centro = (float(p[:, 0].mean()), float(p[:, 1].mean()))
            tid = self._associar(centro, tam, agora)
            h = self._maos[tid]
            h.centro, h.visto = centro, agora
            if estado == OUTRO:
                continue
            h.estados.append((agora, estado, firmeza * float(conf_det)))
            h.estados = [e for e in h.estados if agora - e[0] <= JANELA_S * 2]
            s = self._fechou(h, agora)
            if s is not None and agora - h.ultimo_aviso >= INTERVALO_S:
                h.repeticoes += 1
                h.ultimo_aviso = agora
                sinais.append(Sinal(tid, agora, s, h.repeticoes, tam, centro))
        # Esquece mãos que saíram de cena.
        self._maos = {k: v for k, v in self._maos.items() if agora - v.visto <= 10.0}
        return sinais

    @staticmethod
    def _fechou(h: _Historico, agora: float) -> Optional[float]:
        """Tempo 1 visto ≥ QUADROS_MIN vezes, seguido do tempo 2 ≥ QUADROS_MIN
        vezes, tudo dentro de JANELA_S. Devolve a confiança, ou None."""
        abertos = [e for e in h.estados if e[1] == ABERTA_POLEGAR]
        punhos = [e for e in h.estados if e[1] == PUNHO_POLEGAR]
        if len(abertos) < QUADROS_MIN or len(punhos) < QUADROS_MIN:
            return None
        inicio_abertos = abertos[QUADROS_MIN - 1][0]
        depois = [e for e in punhos if e[0] > abertos[0][0]]
        if len(depois) < QUADROS_MIN:
            return None
        fim = depois[QUADROS_MIN - 1][0]
        if not (0 < fim - inicio_abertos <= JANELA_S) or agora - fim > 1.0:
            return None
        usados = abertos[:4] + depois[:4]
        return round(float(np.mean([e[2] for e in usados])), 3)
