"""Conformidade de EPI: a regra de decisão, com um modelo de mentira."""
from types import SimpleNamespace

import numpy as np
import pytest

from app.services import epi


class _Caixas:
    def __init__(self, achados):
        self.cls = [c for c, _, _ in achados]
        self.conf = [p for _, p, _ in achados]
        self.xyxy = [b for _, _, b in achados]


def _modelo(achados):
    nomes = {0: "Hardhat", 1: "NO-Hardhat", 2: "Safety Vest", 3: "NO-Safety Vest", 4: "Gloves"}

    class M:
        names = nomes

        def __call__(self, img, **kw):
            caixas = _Caixas([(c, p, b) for c, p, b in achados])

            class Wrap:
                pass
            w = Wrap()
            w.cls = SimpleNamespace(tolist=lambda: caixas.cls)
            w.conf = SimpleNamespace(tolist=lambda: caixas.conf)
            w.xyxy = SimpleNamespace(tolist=lambda: caixas.xyxy)
            return [SimpleNamespace(boxes=w)]
    return M()


QUADRO = np.zeros((400, 400, 3), np.uint8)
PESSOA = (100, 50, 200, 380)


@pytest.mark.parametrize("achados,exigidos,resultado,faltando", [
    ([(0, .8, [10, 0, 50, 30]), (2, .9, [10, 40, 90, 150])], ["capacete", "colete"], "conforme", []),
    ([(1, .7, [10, 0, 50, 30]), (2, .9, [10, 40, 90, 150])], ["capacete", "colete"], "fora", ["capacete"]),
    ([(2, .9, [10, 40, 90, 150])], ["capacete", "colete"], "inconclusivo", []),
    ([(0, .8, [10, 0, 50, 30])], ["capacete", "botas"], "inconclusivo", []),   # botas: sem detector
    ([(0, .8, [10, 0, 50, 30])], [], "sem_exigencia", []),
])
def test_regra_de_conformidade(monkeypatch, achados, exigidos, resultado, faltando):
    monkeypatch.setattr(epi, "_carregar", lambda: _modelo(achados))
    r = epi.analisar_pessoa(QUADRO, PESSOA, exigidos)
    assert r["resultado"] == resultado and r["faltando"] == faltando
    assert r["itens"]["botas"]["estado"] == "nao_verificado"


def test_ausente_so_vence_detectado_se_mais_confiante(monkeypatch):
    monkeypatch.setattr(epi, "_carregar", lambda: _modelo([(0, .8, [0, 0, 9, 9]), (1, .4, [0, 0, 9, 9])]))
    assert epi.analisar_pessoa(QUADRO, PESSOA, ["capacete"])["itens"]["capacete"]["estado"] == "detectado"


def test_caixa_do_item_volta_para_coordenadas_do_quadro(monkeypatch):
    monkeypatch.setattr(epi, "_carregar", lambda: _modelo([(0, .8, [10, 5, 50, 30])]))
    r = epi.analisar_pessoa(QUADRO, PESSOA, ["capacete"])
    rec, (ox, oy) = epi.recorte_da_pessoa(QUADRO, PESSOA)
    assert r["itens"]["capacete"]["bbox"] == [10 + ox, 5 + oy, 50 + ox, 30 + oy]
