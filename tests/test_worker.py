"""Testes da consolidação de passagens no worker.

    pytest -q tests/test_worker.py

A regra que estes testes protegem é uma só: **uma passagem diante da câmera
vira um registro, o do melhor instante**. É lógica sutil — depende de casar
detecções entre análises sem os sinais serem confiáveis — e cada afrouxamento
volta como lista de detecções cheia da mesma pessoa ou do mesmo carro parado.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from app.services.vision import MAPA_CLASSES, ObjetoDetectado
from worker.pipeline import (
    SeletorMelhorObjeto, SeletorMelhorQuadro, suprimir_sobrepostas,
)

AGORA = datetime.now(timezone.utc)
CAIXA = [0.2, 0.3, 0.25, 0.2]


def captura(marca: str):
    return lambda: (f"jpeg-{marca}", f"hash-{marca}")


def escoar(seletor) -> list:
    """Deixa as passagens abertas fecharem, sem novas detecções.

    Os ciclos vêm do próprio seletor: afrouxar a tolerância à oscilação do
    detector não deve quebrar teste nenhum aqui.
    """
    saindo = []
    for _ in range(seletor.ciclos_de_espera + 1):
        saindo += seletor.prontos()
    return saindo


# ----------------------------------------------------------------------
# Supressão de caixas sobrepostas
# ----------------------------------------------------------------------
def test_mesma_caixa_com_duas_leituras_vira_uma_so():
    """O YOLO pontua as classes à parte: o mesmo furgão pontua na cabeça de
    `car` e na de `truck`. As duas entram como `carro`, mas continuam sendo
    duas caixas — sem supressão, dois registros do mesmo veículo."""
    mesma = (100, 50, 400, 300)
    saida = suprimir_sobrepostas([
        ObjetoDetectado("carro", 0.683, mesma, 7),
        ObjetoDetectado("carro", 0.661, mesma, 8),
    ])
    assert [o.confianca for o in saida] == [0.683], "fica a leitura mais confiante"


def test_caixa_encaixada_de_outra_coisa_sobrevive():
    """A mochila fica dentro da caixa da pessoa, e some junto se a supressão
    olhar só para sobreposição. Some com ela o `volume` e a `arma`, que são as
    classes que mais importam."""
    pessoa = ObjetoDetectado("pessoa", 0.90, (100, 50, 200, 350), 1)
    mochila = ObjetoDetectado("mochila", 0.72, (110, 90, 165, 190), 2)
    assert len(suprimir_sobrepostas([pessoa, mochila])) == 2


# ----------------------------------------------------------------------
# Objetos
# ----------------------------------------------------------------------
def test_objeto_registra_uma_vez_com_a_leitura_mais_confiante():
    sel = SeletorMelhorObjeto()
    for conf, marca in [(0.60, "entrando"), (0.88, "centro"), (0.71, "saindo")]:
        sel.adicionar(ObjetoDetectado("carro", conf, (100, 50, 400, 300), 5),
                      CAIXA, "", captura(marca), AGORA)
        assert sel.prontos() == [], "nada sai enquanto o objeto está em cena"

    saiu = escoar(sel)
    assert len(saiu) == 1, "uma passagem, um registro"
    assert saiu[0].melhor.confianca == 0.88
    assert saiu[0].recorte == "jpeg-centro", "o recorte é o do melhor instante"
    assert saiu[0].amostras == 3


def test_objeto_parado_nao_rende_um_registro_por_analise():
    sel = SeletorMelhorObjeto()
    emitidos = []
    for _ in range(20):
        sel.adicionar(ObjetoDetectado("carro", 0.7, (100, 50, 400, 300), 9),
                      CAIXA, "", captura("p"), AGORA)
        emitidos += sel.prontos()
    assert emitidos == [], "quem continua ali já foi contado"


def test_furgao_nao_vira_onibus_nem_caminhao():
    """O caso real: a ficha de uma van dizia "Ônibus" a 73,1%.

    O COCO não tem `van`, e o mesmo furgão pontuou `bus` no yolov8n e `truck`
    no yolov8l — o rótulo dependia do peso carregado, não do que estava na
    cena. As três classes de veículo entram como `carro` para que o registro
    não afirme um tipo que o modelo não sabe distinguir.
    """
    assert MAPA_CLASSES["car"] == "carro"
    assert MAPA_CLASSES["truck"] == "carro"
    assert MAPA_CLASSES["bus"] == "carro"
    assert "onibus" not in MAPA_CLASSES.values()
    assert "caminhao" not in MAPA_CLASSES.values()


def test_objeto_parado_que_pisca_no_detector_nao_reregistra():
    """O caso real: uma moto estacionada, confiança entre 0,65 e 0,71 contra
    um limiar de 0,65. O detector a perde, a passagem fecha e emite; ela
    volta, e tudo recomeça. Rendeu dez registros da mesma moto em vinte e
    cinco minutos.
    """
    sel = SeletorMelhorObjeto()
    parada = [0.863, 0.224, 0.128, 0.254]
    emitidos = []

    for rodada in range(6):
        # Aparece por duas análises...
        for _ in range(2):
            sel.adicionar(
                ObjetoDetectado("motocicleta", 0.67, (550, 120, 640, 260),
                                # A trilha se perde a cada sumiço: id novo.
                                10 + rodada),
                parada, "", captura("m"), AGORA)
            emitidos += sel.prontos()
        # ... e some, porque a confiança caiu abaixo do limiar.
        for _ in range(sel.ciclos_de_espera + 1):
            emitidos += sel.prontos()

    assert len(emitidos) == 1, (
        f"a mesma moto parada virou {len(emitidos)} registros")


def test_objeto_novo_no_lugar_de_um_ja_esquecido_volta_a_registrar():
    """A lembrança não é eterna: passada a memória sem ninguém ali, quem
    chegar àquele ponto é outra passagem."""
    sel = SeletorMelhorObjeto(memoria_s=0.0)   # esquece na hora
    caixa = [0.5, 0.5, 0.1, 0.1]
    emitidos = []
    for _ in range(2):
        sel.adicionar(ObjetoDetectado("carro", 0.7, (300, 300, 380, 380), 1),
                      caixa, "", captura("c"), AGORA)
        emitidos += escoar(sel)
    assert len(emitidos) == 2


def test_objetos_separados_na_cena_sao_passagens_distintas():
    sel = SeletorMelhorObjeto()
    sel.adicionar(ObjetoDetectado("pessoa", 0.9, (50, 50, 150, 250), 1),
                  [0.1, 0.3, 0.15, 0.2], "", captura("a"), AGORA)
    sel.adicionar(ObjetoDetectado("pessoa", 0.9, (500, 50, 600, 250), 2),
                  [0.7, 0.3, 0.15, 0.2], "", captura("b"), AGORA)
    assert len(escoar(sel)) == 2


# ----------------------------------------------------------------------
# Rostos
# ----------------------------------------------------------------------
@dataclass
class _FaceFalsa:
    embedding: np.ndarray
    bbox: tuple
    qualidade: float


def vetor(semente: int) -> np.ndarray:
    v = np.random.default_rng(semente).normal(size=512).astype(np.float32)
    return v / np.linalg.norm(v)


QUADRO = np.zeros((480, 640, 3), dtype=np.uint8)


def test_rosto_sozinho_rende_um_registro_do_melhor_enquadramento():
    sel = SeletorMelhorQuadro()
    caixas = [(100, 100, 160, 180), (200, 100, 260, 180),
              (300, 100, 360, 180), (400, 100, 460, 180)]
    for i, caixa in enumerate(caixas):
        sel.adicionar_quadro([_FaceFalsa(vetor(400 + i), caixa, 0.4 + i * 0.15)],
                             QUADRO, AGORA)
    saiu = escoar(sel)
    assert len(saiu) == 1
    assert round(saiu[0].melhor.qualidade, 2) == 0.85, "guarda o melhor ângulo"
    assert saiu[0].amostras == 4


def test_rosto_a_mais_num_quadro_nao_multiplica_presencas():
    """O detector às vezes acha um rosto a mais — um reflexo, um cartaz. Com
    dois candidatos abertos, o quadro seguinte com uma pessoa só não pode
    inventar uma terceira presença: nesta resolução o embedding não separa
    ninguém, e a caixa se afasta assim que a pessoa anda.
    """
    sel = SeletorMelhorQuadro()
    sel.adicionar_quadro([_FaceFalsa(vetor(1), (100, 100, 160, 180), 0.50),
                          _FaceFalsa(vetor(2), (300, 100, 360, 180), 0.45)],
                         QUADRO, AGORA)

    for i, caixa in enumerate([(150, 105, 210, 185), (220, 110, 280, 190),
                               (290, 115, 350, 195)]):
        sel.adicionar_quadro([_FaceFalsa(vetor(100 + i), caixa, 0.6 + i * 0.1)],
                             QUADRO, AGORA)

    assert len(escoar(sel)) == 2, "no máximo as presenças já abertas"


def test_dois_rostos_ao_mesmo_tempo_seguem_separados():
    """Simultâneos são pessoas diferentes — disso o detector dá conta."""
    sel = SeletorMelhorQuadro()
    for i in range(3):
        sel.adicionar_quadro([_FaceFalsa(vetor(200 + i), (100, 100, 160, 180), 0.5),
                              _FaceFalsa(vetor(300 + i), (400, 100, 460, 180), 0.5)],
                             QUADRO, AGORA)
    assert len(escoar(sel)) == 2


def test_sequencial_lido_do_codigo():
    """De onde o contador parte quando ainda não existe: o maior código já
    gravado. Ler errado aqui reemitiria código em uso."""
    from app.routers.deteccoes import _sequencial_do_codigo
    assert _sequencial_do_codigo("OD-2026000151", 6) == 151
    assert _sequencial_do_codigo("FR-2026000001", 6) == 1
    assert _sequencial_do_codigo("ALR-00042", 5) == 42
    # Sem código algém ou ilegível, a série começa do zero em vez de estourar.
    assert _sequencial_do_codigo("", 6) == 0
    assert _sequencial_do_codigo("lixo", 6) == 0
