"""Identificação do veículo: cor pela imagem, marca e modelo pela placa."""
import json

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.config import settings  # noqa: E402
from app.services import veiculo  # noqa: E402


def _veiculo(cor_bgr, fundo=(110, 110, 110), alt=200, larg=320):
    """Recorte justo de um veículo: lataria, vidro escuro, rodas, chão embaixo."""
    img = np.full((alt, larg, 3), fundo, np.uint8)
    cv2.rectangle(img, (10, 40), (larg - 10, alt - 40), cor_bgr, -1)       # lataria
    cv2.rectangle(img, (60, 50), (larg - 60, 85), (25, 25, 25), -1)        # vidro
    for x in (70, larg - 70):
        cv2.circle(img, (x, alt - 40), 28, (15, 15, 15), -1)               # rodas
    return img


@pytest.mark.parametrize("bgr,esperado", [
    ((235, 235, 235), "branco"),
    ((20, 20, 20), "preto"),
    ((40, 40, 200), "vermelho"),
    ((200, 90, 30), "azul"),
])
def test_cor_predominante(bgr, esperado):
    cor, conf = veiculo.cor_predominante(_veiculo(bgr))
    assert cor == esperado and 0 < conf <= 1


def test_recorte_minusculo_nao_tem_cor():
    assert veiculo.cor_predominante(np.zeros((8, 8, 3), np.uint8)) == ("", 0.0)


def test_resposta_estilo_sinesp_separa_marca_e_modelo():
    r = veiculo._interpretar({"marca": "VW/GOL 1.0", "anoModelo": "2014", "cor": "Branca"})
    assert r == {"marca": "VW", "modelo": "GOL 1.0", "ano": "2014", "cor": "branca"}


def test_resposta_aninhada_e_sem_marca():
    assert veiculo._interpretar({"dados": {"marca": "Fiat", "modelo": "Strada"}})["modelo"] == "STRADA"
    assert veiculo._interpretar({"modelo": "Onix"}) is None


def test_consulta_desligada_nao_vai_a_rede(monkeypatch):
    monkeypatch.setattr(settings, "VEICULO_CONSULTA", "desligado")
    chamou = []
    monkeypatch.setattr(veiculo.urllib.request, "urlopen", lambda *a, **k: chamou.append(1))
    assert veiculo.consultar_placa("ABC1D23") is None and not chamou


def test_consulta_http_usa_placa_e_token_e_guarda_em_cache(monkeypatch):
    monkeypatch.setattr(settings, "VEICULO_CONSULTA", "http")
    monkeypatch.setattr(settings, "VEICULO_CONSULTA_URL", "https://base.oficial/placa/{placa}")
    monkeypatch.setattr(settings, "VEICULO_CONSULTA_TOKEN", "segredo")
    veiculo._cache.clear()
    pedidos = []

    class Resp:
        def __init__(self, corpo): self.corpo = corpo
        def read(self): return self.corpo
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def falso(req, timeout):
        pedidos.append((req.full_url, req.get_header("Authorization")))
        return Resp(json.dumps({"marca": "CHEVROLET/ONIX", "ano": "2020"}).encode())

    monkeypatch.setattr(veiculo.urllib.request, "urlopen", falso)
    r = veiculo.consultar_placa("RIO2A18")
    assert r["marca"] == "CHEVROLET" and r["modelo"] == "ONIX"
    assert pedidos == [("https://base.oficial/placa/RIO2A18", "Bearer segredo")]
    veiculo.consultar_placa("RIO2A18")
    assert len(pedidos) == 1, "a segunda passagem do mesmo carro sai do cache"
