"""Câmera que não entrega imagem espera antes de nova tentativa.

O OpenCV abre uma câmera por vez no processo; uma câmera quebrada tentando
sem parar segurava a abertura das outras 23.
"""
import time

import pytest

pytest.importorskip("cv2")

from app.services import video  # noqa: E402


class _CapFechado:
    def isOpened(self):
        return False

    def release(self):
        pass


@pytest.fixture(autouse=True)
def _limpo():
    video._pausa_ate.clear()
    video._falhas_seguidas.clear()
    video._leitores.pop("CAM-TESTE", None)
    yield
    video._pausa_ate.clear()
    video._falhas_seguidas.clear()
    video._leitores.pop("CAM-TESTE", None)


def _esperar(leitor):
    leitor.pronto.wait(5)
    leitor._thread.join(5)


def test_falha_na_abertura_pausa_e_nao_abre_outra(monkeypatch):
    aberturas = []
    monkeypatch.setattr(video, "_abrir", lambda cv2, url, ms: (aberturas.append(url) if url == "rtsp://pausa" else None) or _CapFechado())
    primeiro = video.obter("CAM-TESTE", "rtsp://pausa")
    _esperar(primeiro)
    assert primeiro.erro and video.em_pausa("CAM-TESTE")
    # Durante a pausa: o mesmo leitor morto, sem nova tentativa na fila.
    segundo = video.obter("CAM-TESTE", "rtsp://pausa")
    assert segundo is primeiro and len(aberturas) == 1


def test_pausa_cresce_a_cada_falha_e_tem_teto():
    pausas = [video._registrar_falha("CAM-TESTE") for _ in range(6)]
    assert pausas[:3] == [60.0, 120.0, 240.0]
    assert max(pausas) == video.PAUSA_FALHA_MAX_S


def test_passada_a_pausa_tenta_de_novo(monkeypatch):
    aberturas = []
    monkeypatch.setattr(video, "_abrir", lambda cv2, url, ms: (aberturas.append(url) if url == "rtsp://pausa" else None) or _CapFechado())
    primeiro = video.obter("CAM-TESTE", "rtsp://pausa")
    _esperar(primeiro)
    video._pausa_ate["CAM-TESTE"] = time.monotonic() - 1
    segundo = video.obter("CAM-TESTE", "rtsp://pausa")
    _esperar(segundo)
    assert segundo is not primeiro and len(aberturas) == 2
