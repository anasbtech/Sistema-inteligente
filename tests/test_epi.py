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


@pytest.fixture(autouse=True)
def _sem_detector_de_rosto(monkeypatch):
    monkeypatch.setattr(epi, "_conferir_mascara", lambda q, b: None)


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
    monkeypatch.setattr(epi, "_carregar", lambda: _modelo([(0, .8, [50, 0, 80, 20]), (1, .4, [50, 0, 80, 20])]))
    assert epi.analisar_pessoa(QUADRO, PESSOA, ["capacete"])["itens"]["capacete"]["estado"] == "detectado"


def test_caixa_do_item_volta_para_coordenadas_do_quadro(monkeypatch):
    monkeypatch.setattr(epi, "_carregar", lambda: _modelo([(0, .8, [10, 5, 50, 30])]))
    r = epi.analisar_pessoa(QUADRO, PESSOA, ["capacete"])
    rec, (ox, oy) = epi.recorte_da_pessoa(QUADRO, PESSOA)
    assert r["itens"]["capacete"]["bbox"] == [10 + ox, 5 + oy, 50 + ox, 30 + oy]


def _modelo_luva_so_ampliada():
    """Luva que só aparece nos pedaços ampliados — e só no primeiro (canto de cima, à esquerda)."""
    vistos = []

    class M:
        names = {4: "Gloves"}

        def __call__(self, img, **kw):
            ampliado = max(img.shape[:2]) == 640
            if ampliado:
                vistos.append(img.shape)
            achados = [(4, .82, [0, 0, 64, 64])] if ampliado and len(vistos) == 1 else []
            c = _Caixas(achados)
            w = SimpleNamespace(cls=SimpleNamespace(tolist=lambda: c.cls),
                                conf=SimpleNamespace(tolist=lambda: c.conf),
                                xyxy=SimpleNamespace(tolist=lambda: c.xyxy))
            return [SimpleNamespace(boxes=w)]
    return M(), vistos


def test_luva_pequena_achada_na_passada_ampliada(monkeypatch):
    m, vistos = _modelo_luva_so_ampliada()
    monkeypatch.setattr(epi, "_carregar", lambda: m)
    r = epi.analisar_pessoa(QUADRO, PESSOA, ["luvas"])
    luva = r["itens"]["luvas"]
    assert luva["estado"] == "detectado" and r["resultado"] == "conforme"
    # Caixa de volta no quadro: o 1º pedaço começa no canto da área ampliada.
    x1, y1, x2, y2 = PESSOA
    X1, Y1 = int(x1 - (x2 - x1) * .45), int(y1 - (y2 - y1) * .30)
    assert luva["bbox"][0] == pytest.approx(X1, abs=1) and luva["bbox"][1] == pytest.approx(max(0, Y1), abs=1)
    assert len(vistos) == 6 and len(luva["caixas"]) == 1


def test_sem_passada_ampliada_quando_luva_nao_e_exigida(monkeypatch):
    m, vistos = _modelo_luva_so_ampliada()
    monkeypatch.setattr(epi, "_carregar", lambda: m)
    epi.analisar_pessoa(QUADRO, PESSOA, ["capacete"])
    epi.analisar_quadro(QUADRO, [(PESSOA, .9, 1)], ["luvas"], detalhar_ate=0)
    assert vistos == []


def test_mascara_vem_do_rosto_e_cor_do_capacete(monkeypatch):
    monkeypatch.setattr(epi, "_carregar", lambda: _modelo([(0, .8, [10, 5, 50, 30])]))
    monkeypatch.setattr(epi, "_conferir_mascara",
                        lambda q, b: {"estado": "detectado", "confianca": .7, "bbox": [1, 2, 3, 4]})
    q = QUADRO.copy()
    q[:, :] = (0, 200, 255)                       # BGR: amarelo
    r = epi.analisar_pessoa(q, PESSOA, ["capacete", "mascara"])
    assert r["itens"]["mascara"]["estado"] == "detectado" and r["resultado"] == "conforme"
    assert r["itens"]["capacete"]["cor"] == "#EBB800"


def test_pele_separa_rosto_de_mascara_branca():
    pele = np.full((20, 20, 3), (110, 140, 200), np.uint8)    # BGR de pele
    branca = np.full((20, 20, 3), (235, 235, 235), np.uint8)
    assert epi._pele(pele) > .9 and epi._pele(branca) < .1


def test_capacete_do_vizinho_nao_vale(monkeypatch):
    # Recorte começa em x=82; a pessoa vai de 100 a 200 (eixo em 150).
    # O capacete mais confiante está no vizinho (x≈210), o próprio no eixo.
    monkeypatch.setattr(epi, "_carregar", lambda: _modelo([(0, .9, [115, 5, 140, 30]),
                                                          (0, .6, [55, 5, 80, 30])]))
    r = epi.analisar_pessoa(QUADRO, PESSOA, ["capacete"])
    assert r["itens"]["capacete"]["confianca"] == .6
