"""Detecção de fogo e fumaça por assinatura de imagem.

Fogo e fumaça não têm classe no COCO, e não há aqui um modelo treinado para
eles. Mas, diferente de "arma" ou "capacete", estes dois **têm assinatura
física mensurável**, e é sobre ela que este módulo trabalha — a abordagem
clássica, anterior às redes, que continua valendo porque a física não mudou:

* **Fogo** é laranja saturado *que oscila*. A cor sozinha não serve: luminária
  quente, cone de sinalização, camisa vermelha e reflexo de pôr do sol passam
  no mesmo filtro. O que separa chama de objeto laranja parado é a
  **cintilação** — a borda da chama muda de lugar várias vezes por segundo, e
  um cone não muda nunca.

* **Fumaça** é uma região que *perde cor e perde borda*. Cinza de baixa
  saturação, cobrindo algo que antes tinha textura: o fundo continua lá, mas
  o detalhe some. Medir só o cinza acusaria parede e piso; o que acusa fumaça
  é o detalhe **desaparecendo** onde antes existia.

**O que isto não é.** Não é um classificador treinado, e não vai reconhecer
fogo por trás de vidro, chama azul de gás, nem fumaça branca contra parede
branca. Os limiares foram escolhidos para errar para menos: num presídio, um
alarme de incêndio falso esvazia uma galeria. Quem precisar de cobertura maior
que esta troca por um modelo dedicado em `extras.py` — o encaixe é o mesmo.

A cintilação exige quadros próximos no tempo, e a visita do rodízio entrega
um só. Por isso o fluxo é em duas etapas: a varredura de cor é barata e roda
sempre; os quadros extras só são pedidos quando há candidato, e é aí que se
decide.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.chama")


@dataclass(frozen=True)
class Candidato:
    """Região suspeita, em pixels do quadro analisado."""
    classe: str                       # "fogo" | "fumaca"
    bbox: Tuple[int, int, int, int]
    area: int
    confianca: float


def _cv2():
    try:
        import cv2
        return cv2
    except ImportError:
        return None


def _na_moldura(caixa, largura: int, altura: int) -> bool:
    """A caixa cabe inteira na faixa de borda do quadro?

    O caso real que motivou este filtro: a câmera dos Analistas acusou fumaça
    a 71% sobre uma parede cinza no canto superior direito. O que mudava ali
    não era a cena — era o **carimbo de data e hora** que a própria câmera
    sobrepõe ao vídeo. Os dígitos trocam a cada segundo, a textura local muda
    junto, e "textura que some" é exatamente o sinal que acusa fumaça.

    Toda câmera deste parque carimba numa borda. Fumaça que importa não vive
    num cantinho de 8%: ela cresce e toma o meio do quadro.
    """
    x1, y1, x2, y2 = caixa
    m = settings.CHAMA_MARGEM_BORDA
    return (x2 <= largura * m or x1 >= largura * (1 - m)
            or y2 <= altura * m or y1 >= altura * (1 - m))


def _regioes(mascara, area_min: int) -> List[Tuple[Tuple[int, int, int, int], int]]:
    """Componentes conexas acima da área mínima, como caixas."""
    cv2 = _cv2()
    if cv2 is None:
        return []
    alt, larg = mascara.shape[:2]
    n, _, stats, _ = cv2.connectedComponentsWithStats(mascara, connectivity=8)
    saida = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < area_min:
            continue
        caixa = (int(x), int(y), int(x + w), int(y + h))
        if _na_moldura(caixa, larg, alt):
            continue
        saida.append((caixa, int(area)))
    return saida


def mascara_fogo(quadro_bgr: np.ndarray) -> Optional[np.ndarray]:
    """Pixels com cor compatível com chama.

    Dois critérios somados, e os dois são necessários:

    * **ordem dos canais** — numa chama o vermelho domina o verde, e o verde
      domina o azul. É o que derruba a luz fria e o céu;
    * **saturação e brilho altos em HSV**, com matiz na faixa do vermelho ao
      amarelo. É o que derruba o marrom de madeira e o bege de parede.

    Isto sozinho **não** é fogo: é o filtro barato que decide se vale pedir os
    quadros da cintilação.
    """
    cv2 = _cv2()
    if cv2 is None or quadro_bgr is None or quadro_bgr.size == 0:
        return None
    b, g, r = (c.astype(np.int16) for c in cv2.split(quadro_bgr))
    ordem = (r > g) & (g > b) & (r > settings.FOGO_R_MIN)

    hsv = cv2.cvtColor(quadro_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = cv2.split(hsv)
    # Matiz do OpenCV vai a 179: 0..35 cobre vermelho, laranja e amarelo.
    cor = ((h <= 35) & (s >= settings.FOGO_S_MIN) & (v >= settings.FOGO_V_MIN))

    return ((ordem & cor).astype(np.uint8)) * 255


def mascara_fumaca(quadro_bgr: np.ndarray) -> Optional[np.ndarray]:
    """Pixels acinzentados de baixa saturação, no meio da faixa de brilho.

    Fumaça não tem cor própria: o que a caracteriza é a ausência dela. Preto e
    branco puros ficam de fora — sombra e papel não são fumaça.
    """
    cv2 = _cv2()
    if cv2 is None or quadro_bgr is None or quadro_bgr.size == 0:
        return None
    hsv = cv2.cvtColor(quadro_bgr, cv2.COLOR_BGR2HSV)
    _, s, v = cv2.split(hsv)
    cinza = ((s <= settings.FUMACA_S_MAX)
             & (v >= settings.FUMACA_V_MIN) & (v <= settings.FUMACA_V_MAX))
    return (cinza.astype(np.uint8)) * 255


def ha_candidato(quadro_bgr: np.ndarray) -> bool:
    """Vale a pena pedir os quadros extras?

    Roda em toda visita de câmera que vigia fogo ou fumaça, então é só
    operação de matriz — sem componentes conexas e sem alocação grande.
    """
    m = mascara_fogo(quadro_bgr)
    if m is not None and int(np.count_nonzero(m)) >= settings.FOGO_AREA_MIN:
        return True

    m = mascara_fumaca(quadro_bgr)
    if m is None or int(np.count_nonzero(m)) < settings.FUMACA_AREA_MIN:
        return False

    # Cinza de baixa saturação é metade de qualquer cena interna — parede,
    # piso, porta. Medido: uma parede uniforme preenche os 307.200 pixels do
    # quadro e passaria a cada visita, anulando a economia que esta triagem
    # existe para fazer.
    #
    # O que separa cena de parede é a textura: a fumaça é detectada por
    # *perda* de detalhe, e onde não há detalhe não há o que perder. A mesma
    # parede dá variância 0,0 contra 1.741 de uma cena com mobília.
    cv2 = _cv2()
    cinza = cv2.cvtColor(quadro_bgr, cv2.COLOR_BGR2GRAY)
    textura = float(np.var(cv2.Laplacian(cinza, cv2.CV_32F)))
    return textura >= settings.FUMACA_TEXTURA_MIN


def analisar(quadros: List[np.ndarray], pessoas=()) -> List[Candidato]:
    """Decide sobre uma sequência curta de quadros da mesma câmera.

    Precisa de pelo menos três: com dois não há como distinguir cintilação de
    uma mudança única — uma porta abrindo muda a cena uma vez, uma chama muda
    a cada quadro.

    `pessoas` são as caixas que o detector de objetos já achou neste quadro.
    Servem para descartar o que cai sobre gente: mão em movimento borra e
    perde textura exatamente como fumaça.
    """
    cv2 = _cv2()
    if cv2 is None or len(quadros) < 3:
        return []

    achados: List[Candidato] = []
    mascaras = [mascara_fogo(q) for q in quadros]
    if all(m is not None for m in mascaras):
        achados += _decidir_fogo(cv2, quadros, mascaras, pessoas)
    achados += _decidir_fumaca(cv2, quadros, pessoas)
    return achados


def _decidir_fogo(cv2, quadros, mascaras, pessoas=()) -> List[Candidato]:
    """Cor presente em todos os quadros, mas com borda inquieta.

    A região precisa persistir — chama que aparece num quadro só é ruído de
    compressão — e precisa *variar*: é a diferença entre a máscara de um
    quadro e a do seguinte, dentro da mesma região, que separa fogo de um
    objeto laranja parado. Um cone de sinalização dá variação perto de zero.
    """
    persistente = mascaras[0].copy()
    for m in mascaras[1:]:
        persistente = cv2.bitwise_and(persistente, m)
    persistente = cv2.morphologyEx(
        persistente, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    saida = []
    for caixa, area in _regioes(persistente, settings.FOGO_AREA_MIN):
        # Roupa laranja e pele em movimento oscilam como chama. O detector de
        # objetos já sabe onde está a gente; o que cai sobre ela não é fogo.
        if _sobrepoe_pessoa(caixa, pessoas):
            continue
        x1, y1, x2, y2 = caixa
        mudancas = []
        for a, b in zip(mascaras, mascaras[1:]):
            ra, rb = a[y1:y2, x1:x2], b[y1:y2, x1:x2]
            if ra.size == 0:
                continue
            mudancas.append(float(np.count_nonzero(cv2.bitwise_xor(ra, rb))) / ra.size)
        if not mudancas:
            continue
        cintilacao = float(np.mean(mudancas))
        if cintilacao < settings.FOGO_CINTILACAO_MIN:
            continue
        # A confiança cresce com a cintilação, e satura: acima do dobro do
        # piso não há mais o que distinguir — já é chama sem dúvida.
        conf = min(0.95, 0.55 + cintilacao / (settings.FOGO_CINTILACAO_MIN * 4))
        saida.append(Candidato("fogo", caixa, area, round(conf, 3)))
    return saida


def _sobrepoe_pessoa(caixa, pessoas) -> bool:
    """A região cai sobre alguém que o detector de objetos já marcou?

    O falso positivo que motivou isto: duas vezes na Produção, "fumaça" a 68% e
    72% sobre **uma mão passando por cima de papel**. A mão em movimento borra,
    a região perde textura, e textura que some é exatamente o sinal da fumaça.

    O YOLO já roda no mesmo quadro e já sabe onde estão as pessoas. Descartar o
    que cai sobre elas custa uma comparação de retângulos e elimina a família
    inteira desse erro — mão, braço, cabeça e roupa clara em movimento.

    O preço, assumido: fumaça que saia exatamente de trás de alguém é perdida.
    Numa escolha entre perder essa e alarmar incêndio toda vez que alguém mexe
    a mão sobre a mesa, perder essa é o lado certo de errar.
    """
    x1, y1, x2, y2 = caixa
    area = max(1, (x2 - x1) * (y2 - y1))
    for px1, py1, px2, py2 in pessoas:
        ix = max(0, min(x2, px2) - max(x1, px1))
        iy = max(0, min(y2, py2) - max(y1, py1))
        if ix * iy / area >= settings.FUMACA_SOBREPOSICAO_MAX:
            return True
    return False


def _mapa_textura(cv2, quadro, lado: int = 31) -> np.ndarray:
    """Quanto detalhe existe em torno de cada pixel.

    Variância local da Laplaciana, calculada por filtro de caixa: média do
    quadrado menos quadrado da média. É barato e dá um mapa do mesmo tamanho
    do quadro, que é o que permite perguntar *onde* o detalhe sumiu em vez de
    perguntar se ele sumiu em média.
    """
    g = cv2.cvtColor(quadro, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lap = cv2.Laplacian(g, cv2.CV_32F)
    m1 = cv2.boxFilter(lap, -1, (lado, lado))
    m2 = cv2.boxFilter(lap * lap, -1, (lado, lado))
    return np.maximum(m2 - m1 * m1, 0.0)


def _decidir_fumaca(cv2, quadros, pessoas=()) -> List[Candidato]:
    """Onde o detalhe sumiu — e sumiu de vez.

    A versão anterior pegava **toda** região cinza e media a perda de textura
    na média dela. Numa cena clara isso quebra: medido numa mesa de escritório,
    a máscara de cinza cobriu 100% do quadro, a região virou o quadro inteiro,
    e uma fumaça ocupando 20% da cena diluiu para 7,9% de perda — abaixo de
    qualquer limiar. O detector só funcionaria onde a fumaça fosse a única
    coisa clara.

    Aqui a ordem se inverte: primeiro acha-se **onde** a textura caiu, pixel a
    pixel, e só então se pergunta se aquilo é cinza. A fumaça é a região que
    perdeu detalhe, não a região que por acaso é cinza.

    As quatro defesas, cada uma nascida de um erro observado:

    1. **perda localizada** — o mapa acima, em vez da média de toda área cinza;
    2. **fora de pessoa** — mão em movimento borra igual (veja
       `_sobrepoe_pessoa`), e foi assim que a Produção alarmou duas vezes;
    3. **área grande** — fumaça que justifica alarme toma a cena, não os 0,7%
       que mediam aqueles falsos positivos;
    4. **persistência** — a perda tem de valer em cada passo. A mão borra num
       quadro e a cena volta no seguinte; fumaça não volta.
    """
    cinza = mascara_fumaca(quadros[-1])
    if cinza is None:
        return []

    t0 = _mapa_textura(cv2, quadros[0])
    t1 = _mapa_textura(cv2, quadros[-1])

    # Onde havia detalhe para perder. Parede lisa fica de fora: ali não há o
    # que sumir, e sem este piso ela entraria como fumaça em qualquer limiar.
    tinha = t0 >= settings.FUMACA_TEXTURA_LOCAL_MIN
    perdeu = (t0 - t1) >= (t0 * settings.FUMACA_PERDA_MIN)

    m = ((tinha & perdeu & (cinza > 0)).astype(np.uint8)) * 255
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))

    alt, larg = m.shape[:2]
    quadro_area = larg * alt

    # Mapa de cada quadro, para exigir que a perda seja contínua em vez de um
    # solavanco só.
    mapas = [_mapa_textura(cv2, q) for q in quadros]

    saida = []
    for caixa, area in _regioes(m, settings.FUMACA_AREA_MIN):
        # O tamanho que conta é o da caixa, não o dos pixels soltos. A fumaça
        # só deixa rastro onde antes havia detalhe, então a máscara de perda
        # sai esburacada: medida numa cena de mesa, ela cobriu 3,2% do quadro
        # para uma fumaça que ocupava 20%. Pelos pixels, a fumaça legítima era
        # recusada; pela extensão que ela toma, não.
        x1, y1, x2, y2 = caixa
        if (x2 - x1) * (y2 - y1) < quadro_area * settings.FUMACA_AREA_FRACAO_MIN:
            continue
        if _sobrepoe_pessoa(caixa, pessoas):
            continue
        serie = [float(np.mean(mp[y1:y2, x1:x2])) for mp in mapas]
        if serie[0] < settings.FUMACA_TEXTURA_LOCAL_MIN:
            continue
        perda = 1.0 - (serie[-1] / serie[0])
        if perda < settings.FUMACA_PERDA_MIN:
            continue
        if any(b > a * settings.FUMACA_FOLGA_PASSO for a, b in zip(serie, serie[1:])):
            continue
        conf = min(0.9, 0.5 + perda / 2.0)
        saida.append(Candidato("fumaca", caixa, area, round(conf, 3)))
    return saida
