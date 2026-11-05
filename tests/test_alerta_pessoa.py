"""Alerta de ação só com pessoa de verdade, e a foto com o corpo inteiro."""
from types import SimpleNamespace

from app.services import video
from app.services.inferencia import ACOES_DE_PESSOA, Inferencia


def _pessoa(conf, bbox, tid=1):
    return SimpleNamespace(classe="pessoa", confianca=conf, bbox=bbox, track_id=tid)


def test_pessoa_com_confianca_baixa_nao_alerta():
    """Área restrita saía com 'pessoa' a 0,22 — planta, cadeira, legenda."""
    assert not Inferencia._pessoa_real("area_restrita", _pessoa(0.22, (100, 100, 140, 260)))
    assert Inferencia._pessoa_real("area_restrita", _pessoa(0.85, (100, 100, 140, 260)))


def test_sem_pessoa_no_quadro_nao_alerta():
    assert not Inferencia._pessoa_real("invasao", None)


def test_caixa_pequena_demais_nao_alerta():
    assert not Inferencia._pessoa_real("area_restrita", _pessoa(0.9, (100, 100, 110, 130)))


def test_queda_tem_piso_menor():
    deitada = _pessoa(0.5, (100, 200, 260, 280))
    assert Inferencia._pessoa_real("queda", deitada)
    assert not Inferencia._pessoa_real("area_restrita", deitada)


def test_acoes_sem_pessoa_ficam_de_fora():
    for codigo in ("aglomeracao", "fogo", "fumaca", "arma_detectada",
                   "veiculo_proibido", "abandono"):
        assert codigo not in ACOES_DE_PESSOA


def test_evento_acha_a_pessoa_pelo_rastreio_ou_pela_caixa():
    p = _pessoa(0.9, (100, 100, 150, 250), tid=7)
    por_id = SimpleNamespace(track_id=7, bbox=None)
    por_caixa = SimpleNamespace(track_id=None, bbox=(102, 98, 152, 248))
    assert Inferencia._pessoa_do_evento(por_id, [p]) is p
    assert Inferencia._pessoa_do_evento(por_caixa, [p]) is p


def test_so_as_pernas_no_alto_do_quadro_viram_corpo_inteiro():
    """Caixa só das pernas, encostada no alto: a foto cresce para caber um corpo."""
    larg, alt = 1280, 720
    x1, y1, x2, y2 = video.area_de_pessoa((600, 0, 660, 120), larg, alt)
    assert y1 == 0                              # não passa da borda
    assert (y2 - y1) >= 60 * 2.6                # altura de corpo inteiro
    assert 0 <= x1 < 600 and x2 > 660 <= larg   # folga dos lados
    assert abs((x2 - x1) / (y2 - y1) - 0.75) < 0.05   # retrato 3:4


def test_pessoa_inteira_ganha_folga_e_cabe_no_quadro():
    larg, alt = 1280, 720
    x1, y1, x2, y2 = video.area_de_pessoa((1200, 300, 1270, 560), larg, alt)
    assert x2 <= larg and y2 <= alt and x1 >= 0 and y1 >= 0
    assert x1 <= 1200 and y1 <= 300 and y2 >= 560


def test_miniatura_do_aviso_e_pequena_e_valida():
    """A foto vai no próprio aviso: data URL de JPEG, reduzida."""
    import base64

    import cv2
    import numpy as np

    from app.routers.deteccoes import _miniatura
    ok, jpg = cv2.imencode(".jpg", np.random.default_rng(1).integers(0, 255, (1080, 1920, 3), np.uint8))
    url = _miniatura(jpg.tobytes())
    assert url.startswith("data:image/jpeg;base64,")
    img = cv2.imdecode(np.frombuffer(base64.b64decode(url.split(",", 1)[1]), np.uint8), 1)
    assert max(img.shape[:2]) <= 440
    assert _miniatura(None) == "" and _miniatura(b"nao e jpeg") == ""
