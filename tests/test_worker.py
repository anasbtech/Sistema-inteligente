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
    cena. O que o modelo erra não é caminhão, é *hesitar* em furgão, e a
    hesitação aparece na confiança.

    Caminhão e ônibus voltaram a ser classes próprias, mas só acima de
    `OBJECT_PORTE_MIN`. Abaixo dele a resposta é "carro", que é verdadeira
    para qualquer um dos três. Este teste guarda o piso: baixá-lo traz a van
    de volta como ônibus.
    """
    from app.config import settings
    from app.services.vision import _classe_de

    assert settings.OBJECT_PORTE_MIN >= 0.90, "piso baixo demais devolve a van como ônibus"

    # As confianças medidas naquele furgão, nos dois pesos.
    assert _classe_de("bus", 0.49) == "carro"
    assert _classe_de("truck", 0.77) == "carro"
    assert _classe_de("bus", 0.731) == "carro", "a ficha que dizia Ônibus a 73,1%"

    # Os dois erros da Portaria, que derrubaram o piso anterior de 0,80. A
    # versão antiga deste teste afirmava `_classe_de("bus", 0.88) == "onibus"`
    # — e era essa crença que deixava passar o caminhão de baú abaixo.
    assert _classe_de("bus", 0.877) == "carro", "caminhão de baú registrado como Ônibus"
    assert _classe_de("truck", 0.885) == "carro", "van registrada como Caminhão"

    # Caminhão de verdade, que o modelo afirma sem hesitar, continua sendo
    # caminhão. O piso subiu, então a afirmação precisa ser mais firme.
    assert _classe_de("truck", 0.95) == "caminhao"
    assert _classe_de("bus", 0.94) == "onibus"
    # Carro não tem ressalva de porte: é o rótulo que não afirma tamanho.
    assert _classe_de("car", 0.30) == "carro"


def test_duas_classes_no_mesmo_veiculo_viram_carro():
    """O NMS do YOLO é por classe: veículo ambíguo rende duas caixas certas.

    Medido na Portaria: o mesmo corpo saiu `bus` a 0,88 e `truck` a 0,60. As
    duas passavam pela fusão — que também é por classe, de propósito, para
    não apagar a pessoa em pé ao lado do carro — e o registro ficava com uma
    ficha dizendo "Ônibus" e outra dizendo "carro", do mesmo veículo.

    Duas classes de porte sobre o mesmo corpo não são duas leituras: são o
    detector dizendo que não sabe o porte. Vale mais que o limiar, porque não
    depende de adivinhar onde fica a fronteira — usa o desacordo do próprio
    modelo como evidência.
    """
    from app.services.vision import (ObjetoDetectado, desfazer_hesitacao_de_porte,
                                     fundir_sobrepostas)

    mesma_van = [
        ObjetoDetectado("onibus", 0.95, (100, 100, 300, 250)),
        ObjetoDetectado("caminhao", 0.93, (105, 102, 298, 248)),
    ]
    resolvidos = desfazer_hesitacao_de_porte(mesma_van)
    assert {o.classe for o in resolvidos} == {"carro"}, "porte em disputa não se afirma"
    assert len(fundir_sobrepostas(resolvidos)) == 1, "é um veículo, não dois"

    # Dois veículos de verdade, em lugares diferentes, continuam dois.
    no_patio = [
        ObjetoDetectado("onibus", 0.95, (100, 100, 300, 250)),
        ObjetoDetectado("caminhao", 0.94, (600, 100, 800, 250)),
    ]
    assert {o.classe for o in desfazer_hesitacao_de_porte(no_patio)} == {"onibus", "caminhao"}

    # Pessoa dentro da caixa do carro é pessoa ao lado do carro, não hesitação
    # de porte: ela não entra na disputa e não pode ser rebaixada.
    com_pessoa = [
        ObjetoDetectado("carro", 0.95, (100, 100, 400, 300)),
        ObjetoDetectado("pessoa", 0.90, (150, 150, 200, 290)),
    ]
    assert {o.classe for o in desfazer_hesitacao_de_porte(com_pessoa)} == {"carro", "pessoa"}


def test_pessoa_nao_pode_ser_registrada_como_animal():
    """O caso real: a ficha de duas pessoas dizia "Animal" a 73,4% e 71,6%.

    O COCO não tem rótulo para pessoa agachada ou de costas, e o YOLO escolhe
    o animal de silhueta mais próxima. Em seis semanas deste parque — todo
    interno, sem via pública — as 10 detecções de animal que chegaram ao
    registro eram pessoas, e todas foram descartadas na revisão.

    O que torna o rótulo errado é o enquadramento, não o modelo: num pátio,
    animal solto é ocorrência de verdade. Por isso a classe existe, mas nasce
    barrada, e só a câmera que a peça recebe. Este teste guarda o padrão —
    ligá-la para o parque inteiro devolve as 10 pessoas rotuladas como bicho.
    """
    from app.services.inferencia import _classes_barradas
    from app.services.vision import CLASSES_ANIMAL

    class CamPadrao:
        detecta_veiculo = False
        detecta_bagagem = False
        detecta_animal = False

    assert CLASSES_ANIMAL <= _classes_barradas(CamPadrao()),         "animal tem de nascer barrado — é o padrão que protege o parque interno"

    class CamPatio(CamPadrao):
        detecta_animal = True

    assert not (CLASSES_ANIMAL & _classes_barradas(CamPatio())),         "a câmera que pede animal tem de recebê-lo"


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


# ----------------------------------------------------------------------
# Placa veicular
# ----------------------------------------------------------------------
def test_placa_so_aceita_formato_brasileiro():
    """O OCR sempre devolve texto; o formato é quem separa leitura de ruído.

    Medido nas capturas desta instalação: `TVMBI52` e `TVM8152` são o mesmo
    carro lido duas vezes, e só o segundo é placa possível — era o certo. O
    resto saiu de placas pequenas demais para ter caractere.
    """
    from app.services.placa import PADRAO_PLACA, normalizar

    for boa in ("SZQ3B16", "TVM8152", "ABC1234", "ABC1D23"):
        assert PADRAO_PLACA.match(boa), boa
    for ruim in ("TVMBI52", "OO006", "52288", "7WEK225", "C11402L", "1158BB4", ""):
        assert not PADRAO_PLACA.match(ruim), ruim

    # O que vem do modelo traz hífen e espaço conforme o desenho da placa.
    assert normalizar("szq-3b16") == "SZQ3B16"
    assert normalizar(" tvm 8152 ") == "TVM8152"


def test_leitura_de_placa_desligada_nao_chama_modelo(monkeypatch):
    """Com a leitura desligada, nem o carregamento dos modelos acontece."""
    import numpy as np

    from app.config import settings
    from app.services import placa

    monkeypatch.setattr(settings, "PLACA_LEITURA", False)
    monkeypatch.setattr(placa, "_carregar",
                        lambda: (_ for _ in ()).throw(AssertionError("não deve carregar")))
    assert placa.ler(np.zeros((80, 120, 3), dtype="uint8")) is None


def test_recorte_vazio_nao_vira_leitura():
    import numpy as np

    from app.services import placa
    assert placa.ler(None) is None
    assert placa.ler(np.zeros((0, 0, 3), dtype="uint8")) is None


# ----------------------------------------------------------------------
# Classes por lugar
# ----------------------------------------------------------------------
def test_bagagem_nasce_barrada_e_so_a_camera_que_pede_recebe():
    """O caso real: um saco de lixo preto no hall saiu rotulado "MOCHILA".

    `backpack`, `handbag` e `suitcase` do COCO descrevem bagagem fotografada
    de perto. Numa câmera de teto, o que tem essa silhueta é saco de lixo,
    caixa e capa de cadeira: em seis semanas vieram 15 registros e nenhum era
    bagagem.

    Numa revista de entrada ou num guarda-volumes, a mesma classe é o assunto.
    Então ela existe no mapa e nasce barrada, como animal.
    """
    from app.services.inferencia import _classes_barradas
    from app.services.vision import CLASSES_BAGAGEM

    class CamPadrao:
        detecta_veiculo = False
        detecta_bagagem = False
        detecta_animal = False

    assert CLASSES_BAGAGEM <= _classes_barradas(CamPadrao())

    class CamRevista(CamPadrao):
        detecta_bagagem = True

    assert not (CLASSES_BAGAGEM & _classes_barradas(CamRevista()))
    # Uma não arrasta a outra: quem revista bagagem não passa a ver cachorro.
    from app.services.vision import CLASSES_ANIMAL
    assert CLASSES_ANIMAL <= _classes_barradas(CamRevista())


def test_camera_sem_patio_nao_ve_veiculo():
    """Carro no hall do 4º andar é carro de verdade — do lado de fora.

    O modelo acerta ao apontá-lo; errado é registrá-lo como se estivesse na
    sala. Nem limiar nem tamanho separam os dois: medido, o carro da janela
    vinha com 66x101 px e 0,266, maior e mais confiante que várias pessoas
    reais da mesma cena, que começam em 16 px. Quem decide é o lugar.
    """
    from app.services.vision import CLASSES_VIA, ObjetoDetectado

    assert CLASSES_VIA == {"carro", "caminhao", "onibus",
                           "motocicleta", "bicicleta"}, (
        "caminhão e ônibus também só podem vir de fora num hall de andar")
    assert "pessoa" not in CLASSES_VIA, "pessoa nunca é recusada pelo lugar"

    # O filtro é o mesmo conjunto que a inferência passa adiante.
    cena = [ObjetoDetectado("pessoa", 0.30, (10, 10, 60, 160), None),
            ObjetoDetectado("carro", 0.27, (400, 50, 466, 151), None)]
    sobrou = [o for o in cena if o.classe not in CLASSES_VIA]
    assert [o.classe for o in sobrou] == ["pessoa"]
    assert len(suprimir_sobrepostas(sobrou)) == 1
