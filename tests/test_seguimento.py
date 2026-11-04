"""Seguimento das caixas entre duas análises do detector."""
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from app.services.seguimento import Seguidor  # noqa: E402

LARG, ALT = 1280, 720


def _cena(x_pessoa: int) -> np.ndarray:
    """Fundo com textura fixa e um "corpo" texturizado que anda em x."""
    rng = np.random.default_rng(7)
    fundo = rng.integers(0, 60, (ALT, LARG), dtype=np.uint8)
    fundo = cv2.GaussianBlur(fundo, (0, 0), 3)
    corpo = np.random.default_rng(3).integers(80, 255, (300, 120), dtype=np.uint8)
    img = fundo.copy()
    visivel = max(0, min(120, LARG - x_pessoa))
    if visivel:
        img[200:500, x_pessoa:x_pessoa + visivel] = corpo[:, :visivel]
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _caixa(x_pessoa: int) -> dict:
    return {"classe": "pessoa", "confianca": 0.9,
            "bbox": [x_pessoa / LARG, 200 / ALT, 120 / LARG, 300 / ALT]}


def test_caixa_anda_com_a_pessoa():
    s = Seguidor()
    s.quadro(_cena(200), 0.0)
    s.semear(_cena(200), 0.0, [_caixa(200)])
    for i in range(1, 11):
        caixas = s.quadro(_cena(200 + i * 12), i * 0.16)
    x = caixas[0]["bbox"][0] * LARG
    assert abs(x - 320) < 6


def test_semear_reproduz_ate_agora():
    """A análise descreve um quadro antigo; a caixa sai na posição de agora."""
    s = Seguidor()
    for i in range(8):
        s.quadro(_cena(200 + i * 12), i * 0.16)
    caixas = s.semear(_cena(200), 0.0, [_caixa(200)])
    x = caixas[0]["bbox"][0] * LARG
    assert abs(x - (200 + 7 * 12)) < 6


def test_identidade_se_mantem_entre_analises():
    s = Seguidor()
    s.quadro(_cena(200), 0.0)
    id1 = s.semear(_cena(200), 0.0, [_caixa(200)])[0]["track_id"]
    for i in range(1, 6):
        s.quadro(_cena(200 + i * 12), i * 0.16)
    id2 = s.semear(_cena(260), 0.8, [_caixa(260)])[0]["track_id"]
    assert id1 == id2


def test_quem_o_detector_nao_achou_some():
    s = Seguidor()
    s.quadro(_cena(200), 0.0)
    s.semear(_cena(200), 0.0, [_caixa(200)])
    assert s.semear(_cena(200), 0.2, []) == []


def test_caixa_que_sai_do_quadro_some():
    """Quem sai andando pela borda leva a caixa junto, sem esperar a análise."""
    s = Seguidor()
    x0 = LARG - 300
    s.quadro(_cena(x0), 0.0)
    s.semear(_cena(x0), 0.0, [_caixa(x0)])
    vistas = []
    for i in range(1, 16):
        vistas.append(s.quadro(_cena(x0 + i * 20), i * 0.16))
    assert vistas[2] and vistas[2][0]["bbox"][0] * LARG > x0 + 40
    assert vistas[-1] == []


def test_quadro_corrompido_nao_apaga_a_cena():
    """Um quadro borrado derruba o fluxo óptico; a caixa espera e volta a seguir."""
    s = Seguidor()
    s.quadro(_cena(200), 0.0)
    s.semear(_cena(200), 0.0, [_caixa(200)])
    borrado = cv2.GaussianBlur(_cena(212), (0, 0), 25)
    assert len(s.quadro(borrado, 0.16)) == 1
    s.quadro(_cena(224), 0.32)
    caixas = s.quadro(_cena(236), 0.48)
    assert len(caixas) == 1


def test_fraca_so_fica_sobre_quem_ja_era_seguido():
    """Histerese: 0,4 mantém a pessoa confirmada, mas não cria caixa nova."""
    s = Seguidor()
    s.quadro(_cena(200), 0.0)
    fraca = dict(_caixa(200), confianca=0.4)
    assert s.semear(_cena(200), 0.0, [], [fraca]) == []
    id1 = s.semear(_cena(200), 0.1, [_caixa(200)])[0]["track_id"]
    mantida = s.semear(_cena(200), 0.2, [], [fraca])
    assert len(mantida) == 1 and mantida[0]["track_id"] == id1
    assert mantida[0]["confianca"] == 0.4


def test_fraca_aceita_onde_houve_pessoa_confirmada():
    """Quem a volta completa confirmou é aceito com confiança menor depois."""
    s = Seguidor()
    s.confirmar([_caixa(200)], 0.0)
    s.quadro(_cena(200), 10.0)
    fraca = dict(_caixa(200), confianca=0.42)
    assert len(s.semear(_cena(200), 10.0, [], [fraca])) == 1
    # Em outro lugar, não.
    longe = dict(_caixa(800), confianca=0.42)
    assert s.semear(_cena(200), 11.0, [], [longe]) == []


def test_historico_diz_se_cobre_a_analise():
    s = Seguidor()
    for i in range(5):
        s.quadro(_cena(200), 10.0 + i)
    assert s.cobre(11.5)
    assert not s.cobre(5.0)
