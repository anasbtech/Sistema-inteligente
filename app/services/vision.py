"""Detecção de objetos (YOLO), rastreamento e classificação de ações.

As ações aqui são derivadas de *trajetórias* — velocidade, permanência,
densidade, cruzamento de linha — e não de julgamento sobre pessoas. São
indícios para triagem humana; o sistema não conclui intenção.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.vision")

_yolo = None
_idx_varredura: Optional[List[int]] = None

# COCO -> classes operacionais do SEGURANÇA
#
# Todo rótulo que o modelo sabe emitir está aqui. Quais deles cada câmera pode
# registrar é decisão da câmera, em `classes_fora` — e não deste mapa: tirar a
# classe daqui a apagava do parque inteiro, inclusive de onde ela fazia
# sentido. Veja `CLASSES_OPCIONAIS`.
MAPA_CLASSES: Dict[str, str] = {
    "person": "pessoa",
    "car": "carro",
    # Caminhão e ônibus voltaram a ser classes próprias, mas só acima de
    # `OBJECT_PORTE_MIN` — veja `_classe_de`. O que motivou fundi-los em
    # "carro" não foi o modelo errar caminhão, foi ele hesitar em furgão: o
    # mesmo furgão desta instalação saiu `bus` a 0,49 e `truck` a 0,77, e a
    # ficha de uma van dizia "Ônibus a 73,1%". Abaixo do limiar de porte a
    # hesitação continua virando "carro", que é a resposta que não mente.
    "truck": "caminhao",
    "bus": "onibus",
    "motorcycle": "motocicleta",
    "bicycle": "bicicleta",
    # Faca de mesa, que é o que o COCO chama de `knife`. Arma de fogo não
    # existe neste modelo e vem do detector dedicado — veja `armamento.py`.
    "knife": "arma",
    # --- opcionais, desligadas por padrão (veja CLASSES_OPCIONAIS) ---
    #
    # `backpack`, `handbag` e `suitcase` descrevem bagagem fotografada de
    # perto. Numa câmera de teto, o que tem essa silhueta é saco de lixo,
    # caixa de papelão, capa de cadeira: em seis semanas vieram 15 registros e
    # nenhum era bagagem. Numa esteira, num guarda-volumes ou numa revista de
    # entrada, a mesma classe é justamente o que se quer ver.
    "backpack": "mochila",
    "handbag": "mochila",
    "suitcase": "volume",
    # O COCO não tem rótulo para pessoa agachada, de costas ou parcialmente
    # enquadrada, e o YOLO resolve a dúvida escolhendo o animal mais próximo
    # em silhueta. Das 10 detecções de animal que chegaram ao registro deste
    # parque — 26 câmeras, todas internas — as 10 eram pessoas. Numa câmera de
    # pátio ou de via pública, animal solto é ocorrência de verdade.
    "dog": "animal", "cat": "animal", "horse": "animal", "cow": "animal",
    "sheep": "animal", "bird": "animal", "bear": "animal",
    "elephant": "animal", "zebra": "animal", "giraffe": "animal",
}

# Classes que não entram sozinhas: a câmera precisa pedir.
#
# Ficam desligadas porque o erro que produzem é do *enquadramento*, não do
# modelo — pessoa agachada vista do teto é cachorro para qualquer YOLO, e saco
# de lixo é mochila. Onde o enquadramento muda, o acerto muda junto, e aí quem
# decide é quem conhece a câmera.
CLASSES_ANIMAL: set[str] = {"animal"}
CLASSES_BAGAGEM: set[str] = {"mochila", "volume"}
CLASSES_OPCIONAIS: set[str] = CLASSES_ANIMAL | CLASSES_BAGAGEM

# Classes que o modelo troca entre si conforme o ângulo contam como uma
# família só para decidir se um objeto já passou por ali — senão cada
# oscilação de rótulo era lida como uma passagem nova, e um veículo parado
# rendia uma linha por ciclo.
# Está vazio porque a oscilação que este mapa existia para absorver era
# justamente carro/caminhão/ônibus, e as três já entram como `carro` em
# MAPA_CLASSES — ela não chega mais até aqui. `familia_de` fica como a
# costura para o dia em que duas classes distintas voltarem a se confundir.
# Moto e bicicleta continuam de fora de propósito: têm silhueta própria, e
# agrupá-las esconderia uma moto passando ao lado de um carro parado.
FAMILIA_CLASSE: Dict[str, str] = {}


def familia_de(classe: str) -> str:
    """Grupo de classes indistinguíveis para fins de contagem de passagem."""
    return FAMILIA_CLASSE.get(classe, classe)


# Classes que carregam placa — a leitura só roda sobre elas. Bicicleta fica
# de fora porque não tem.
VEICULOS: set[str] = {"carro", "caminhao", "onibus", "motocicleta"}

# Tudo que anda em via, e que numa câmera de andar só pode vir de fora.
#
# O hall do 4º andar tem janela para a rua, e o carro estacionado lá embaixo
# chega ao modelo com 66x101 px e 0,266 de confiança — maior e mais confiante
# que várias pessoas reais da mesma cena, que começam em 16 px. Nem limiar nem
# tamanho separam os dois: o que torna o carro errado é ele estar num hall.
#
# Por isso a decisão é por câmera, em `Camera.detecta_veiculo`, e não por um
# número. No registro isto já se via: das 568 detecções de veículo do
# histórico, todas as 568 vieram da Portaria e da Garagem.
CLASSES_VIA: set[str] = {"carro", "caminhao", "onibus", "motocicleta", "bicicleta"}


# `caminhao`, `onibus` e `animal` seguem aqui pelos registros gravados antes
# de saírem do mapa: o detector não emite mais essas classes, mas a ficha
# antiga ainda as consulta.
SEVERIDADE_OBJETO: Dict[str, int] = {
    "pessoa": 1, "carro": 1, "caminhao": 1, "onibus": 1, "bicicleta": 1,
    "motocicleta": 2, "animal": 2, "mochila": 2, "capacete": 2, "placa": 2,
    "volume": 4, "arma": 4,
}


@dataclass(frozen=True)
class FichaAcao:
    """O que o sistema sabe sobre uma ação, incluindo o que ele *não* faz.

    A interface oferece dez ações desde sempre, e nem todas existem por trás.
    Enquanto isso ficava só no código, ligar "Fogo" numa câmera era um botão
    que não acendia nada e ninguém tinha como saber. Esta ficha é o que
    permite a tela dizer a verdade sobre cada uma.
    """
    codigo: str
    rotulo: str
    severidade: int
    # Por que esta ação não pode disparar hoje. `None` quando ela funciona.
    indisponivel: Optional[str] = None
    # Mede movimento entre quadros próximos. No rodízio pelo parque, dois
    # quadros da mesma câmera estão a dezenas de segundos de distância, e
    # nessa escala não há movimento para medir.
    #
    # Desde 15/10 estas ações funcionam: a câmera recebe uma *rajada* de
    # quadros seguidos quando há pessoa e movimento em cena
    # (`Inferencia._rajada_continua`). Mas a rajada tem teto por volta —
    # análise contínua em 24 câmeras não cabe em 8 núcleos —, então o que
    # se entrega é amostragem, não vigilância ininterrupta. A ficha diz isso
    # na tela, e é por isso que o campo continua existindo.
    exige_continua: bool = False
    # Precisa de uma zona desenhada na câmera para ter sentido.
    exige_zona: Optional[str] = None
    # Só enxerga objeto que a câmera tenha liberado em `detecta_bagagem`:
    # mochila, bolsa e mala. Numa câmera que não pede bagagem, o detector
    # existe e nunca recebe o que procura.
    exige_bagagem: bool = False

    @property
    def disponivel(self) -> bool:
        return self.indisponivel is None


SEM_DETECTOR = "Não há detector implementado para esta ação"

CATALOGO_ACOES: Tuple[FichaAcao, ...] = (
    FichaAcao("corrida", "Corrida súbita", 2, exige_continua=True),
    FichaAcao("queda", "Queda de pessoa", 4, exige_continua=True),
    FichaAcao("aglomeracao", "Aglomeração", 3),
    # Heurística sobre trajetórias: duas pessoas coladas, ambas rápidas e
    # ambas com movimento errático, por alguns segundos. Nenhum dos três
    # sinais significa briga sozinho — é a coincidência que vira indício.
    # Depende da rajada: sem trajetória densa, velocidade e agitação são
    # zero. Veja `AnalisadorAcoes._avaliar_briga`.
    FichaAcao("briga", "Agressão física", 4, exige_continua=True),
    FichaAcao("permanencia", "Permanência prolongada", 2),
    FichaAcao("invasao", "Invasão de perímetro", 4, exige_zona="LINHA"),
    FichaAcao("contrafluxo", "Deslocamento em contrafluxo", 2,
              exige_continua=True, exige_zona="POLIGONO"),
    # Depende das classes `volume` e `mochila`, que voltaram ao mapa em
    # 15/10/2026 — barradas por padrão e liberadas por câmera, em
    # `detecta_bagagem`. O detector recebe o objeto parado que procura assim
    # que a câmera pedir bagagem; sem isso ele continua mudo, e é por isso que
    # a ficha avisa em vez de prometer.
    FichaAcao("abandono", "Abandono de objeto", 3, exige_bagagem=True),
    # A única das quinze que segue sem detector, e por falta de sinal, não de
    # código. As outras catorze se decidem por trajetória (onde o corpo está,
    # quão rápido, por quanto tempo) ou por assinatura de imagem (cor que
    # oscila, textura que some). Dano ao patrimônio não deixa nenhum dos dois:
    # quem picha uma parede fica parado, e quem arranca um equipamento se move
    # como quem o carrega.
    #
    # O que distinguiria é o gesto — braço repetindo um golpe —, e gesto exige
    # estimativa de pose, que é outro modelo rodando sobre cada pessoa em cena.
    # A alternativa sem pose seria comparar a cena antes e depois, e isso
    # acusaria toda cadeira arrastada e toda luz acesa.
    #
    # Uma heurística fraca aqui seria pior que a ausência: num presídio, alarme
    # que erra sempre é alarme que se aprende a ignorar.
    FichaAcao("vandalismo", "Dano ao patrimônio", 3,
              indisponivel="Exige estimativa de pose — o gesto é o único sinal "
                           "que distingue dano de uso normal"),
    # Detector próprio, por assinatura de imagem — veja `services/chama.py`.
    # Não é modelo treinado: é cor de chama somada à cintilação, que é o
    # que separa fogo de cone de sinalização. Erra para menos de propósito.
    FichaAcao("fogo", "Fogo", 4),

    # --- acrescentadas em 2026-10-08 ---------------------------------
    # Pessoa dentro de um polígono marcado como restrito. Diferente de
    # `invasao`, que é cruzamento de linha e precisa de duas posições: esta
    # se decide num quadro só, então funciona no rodízio pelo parque.
    FichaAcao("area_restrita", "Pessoa em área restrita", 4, exige_zona="POLIGONO"),
    # Pessoa vista fora da janela de horário permitido. A única das cinco que
    # não depende de zona nem de modelo novo: a hora o sistema já sabe, e
    # pessoa ele já detecta.
    FichaAcao("fora_de_horario", "Entrada em horário não permitido", 3),
    # Carro, moto ou bicicleta dentro de um polígono proibido. Só vale onde a
    # câmera alcança via, pátio ou garagem — nas outras, veículo é reflexo de
    # janela e já é descartado antes de chegar aqui.
    FichaAcao("veiculo_proibido", "Veículo em área proibida", 3, exige_zona="POLIGONO"),
    # O detector emite a classe `arma`, vinda de `knife` do COCO — então o
    # caminho existe de ponta a ponta. O que não existe é confiabilidade:
    # `knife` foi treinada com faca de cozinha fotografada de perto, e em
    # 10.481 detecções deste parque ela não apareceu uma única vez. Fica
    # disponível porque ligar não custa nada e um acerto vale muito; não fica
    # prometida, porque a estatística desta instalação diz que ela cala.
    FichaAcao("arma_detectada", "Detecção de arma", 4),
    # Desligada em 16/10/2026, depois de errar duas vezes em vídeo real.
    #
    # O detector existe e está escrito em `services/chama.py`. O problema não é
    # de ajuste: é que os sinais da fumaça — cinza de baixa saturação e perda
    # de textura — são os mesmos que **uma mão em movimento** deixa. Ela borra
    # ao se mover, a região fica lisa, e isso é indistinguível de fumaça
    # encobrindo o fundo.
    #
    # Medido ao calibrar: numa cena de mesa, a mão passando rendeu uma região
    # de 5,7% do quadro e a fumaça de verdade 1,7%. Qualquer limiar de tamanho
    # que deixasse a fumaça passar deixaria a mão passar antes. Só a caixa de
    # pessoa separava os dois, e ela falha assim que o YOLO perde a pessoa.
    #
    # Fogo segue ligado porque ali a base é outra: cor muito específica somada
    # à cintilação da borda. Testado contra cone laranja parado, camisa laranja
    # andando e lâmpada quente variando — nenhum dos três passa.
    #
    # Para religar fumaça é preciso um modelo treinado, pelo encaixe de
    # `extras.py`. Até lá, alarme de incêndio que erra é alarme que se aprende
    # a ignorar — e aí ele não serve nem quando acerta.
    FichaAcao("fumaca", "Fumaça", 4,
              indisponivel="Sem detector confiável — mão em movimento é "
                           "indistinguível de fumaça por assinatura de imagem"),
)

FICHA_ACAO: Dict[str, FichaAcao] = {f.codigo: f for f in CATALOGO_ACOES}

# Mantidos como dicionários porque meia dúzia de lugares já os consultam
# assim; derivados do catálogo para não divergirem dele.
SEVERIDADE_ACAO: Dict[str, int] = {f.codigo: f.severidade for f in CATALOGO_ACOES}
ROTULO_ACAO: Dict[str, str] = {f.codigo: f.rotulo for f in CATALOGO_ACOES}

# O que uma câmera nova liga sozinha: só o que pode disparar. Oferecer o
# resto ligado por padrão seria repetir o problema que a ficha existe para
# resolver — prometer vigilância que não acontece.
ACOES_PADRAO: Tuple[str, ...] = tuple(f.codigo for f in CATALOGO_ACOES if f.disponivel)

# As que medem movimento entre quadros próximos. A câmera que vigia uma delas
# recebe uma rajada de quadros na visita — veja `Inferencia._rajada_continua`.
ACOES_CONTINUAS: frozenset = frozenset(
    f.codigo for f in CATALOGO_ACOES if f.exige_continua)


def _duracao(segundos: float) -> str:
    if segundos >= 120:
        return f"{int(round(segundos / 60))} min"
    return f"{int(round(segundos))} s"


def detalhe_acao(codigo: str) -> str:
    """Em que condição esta ação dispara, com os números que estão valendo.

    A tela precisa dizer isto *antes* de alguém ligar a ação. "Permanência
    prolongada" soa inofensivo até se descobrir que, numa sala de trabalho,
    ele acusa todo mundo que senta — medido nos quadros reais da Produção 03,
    são doze ocorrências de uma vez. Ligada na Garagem, onde ninguém deveria
    ficar parado, a mesma regra é exatamente o que se quer.

    Sai da configuração e não de texto fixo: mudar o parâmetro sem mudar a
    frase deixaria a tela mentindo sobre o próprio sistema.
    """
    if codigo == "permanencia":
        return f"Dispara com alguém parado no mesmo lugar por {_duracao(settings.ACAO_PERMANENCIA_S)}"
    if codigo == "aglomeracao":
        return (f"Dispara com {settings.ACAO_AGLOMERACAO_N} pessoas ou mais agrupadas "
                f"no mesmo canto do quadro")
    if codigo == "abandono":
        return f"Dispararia com um volume parado por {_duracao(settings.ACAO_ABANDONO_S)}"
    if codigo == "invasao":
        return "Dispara quando alguém cruza a linha desenhada na câmera"
    if codigo == "contrafluxo":
        return "Dispara com deslocamento contra a direção definida na zona"
    if codigo == "area_restrita":
        return "Dispara com qualquer pessoa dentro da zona marcada como restrita"
    if codigo == "fora_de_horario":
        ini, fim = janela_horario()
        return (f"Dispara com pessoa vista fora da janela definida na câmera — "
                f"sem janela própria, vale a do sistema, {ini}–{fim}")
    if codigo == "veiculo_proibido":
        return "Dispara com carro, moto ou bicicleta dentro da zona proibida"
    if codigo == "arma_detectada":
        return "Dispara com objeto classificado como arma — nunca ocorreu neste parque"
    if codigo == "corrida":
        return ("Dispara quando a pessoa acelera de andar para correr e "
                "sustenta o ritmo — não basta estar rápido")
    if codigo == "queda":
        return "Dispara quando a caixa da pessoa se achata de repente"
    return ""


def acoes_validas(codigos: Sequence[str]) -> List[str]:
    """Filtra para as ações que existem, preservando a ordem do catálogo."""
    pedidas = set(codigos or ())
    return [f.codigo for f in CATALOGO_ACOES if f.codigo in pedidas]


@dataclass
class ObjetoDetectado:
    classe: str
    confianca: float
    bbox: Tuple[int, int, int, int]
    track_id: Optional[int] = None

    def centro(self) -> Tuple[float, float]:
        x1, y1, x2, y2 = self.bbox
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def bbox_normalizado(self, largura: int, altura: int) -> List[float]:
        x1, y1, x2, y2 = self.bbox
        return [x1 / largura, y1 / altura, (x2 - x1) / largura, (y2 - y1) / altura]


@dataclass
class EventoAcao:
    classe: str
    rotulo: str
    confianca: float
    severidade: int
    duracao_s: int
    envolvidos: int
    bbox: Optional[Tuple[int, int, int, int]] = None
    track_id: Optional[int] = None
    # Qual área disparou. Sem isto o alerta de área restrita diz que alguém
    # entrou em algum lugar proibido, mas não em qual.
    zona_nome: str = ""


# ----------------------------------------------------------------------
# YOLO
# ----------------------------------------------------------------------
class ModelosIndisponiveis(RuntimeError):
    """Ultralytics não instalado — só o caminho com vídeo real é afetado."""


def yolo_disponivel() -> bool:
    try:
        import ultralytics  # noqa: F401
        return True
    except ImportError:
        return False


def obter_yolo():
    global _yolo
    if _yolo is not None:
        return _yolo
    try:
        from ultralytics import YOLO  # import tardio
    except ImportError as exc:
        raise ModelosIndisponiveis(
            "Ultralytics não está instalado. Para analisar vídeo real:\n"
            "    pip install ultralytics\n"
            "O restante do sistema funciona sem ele."
        ) from exc

    _yolo = YOLO(settings.OBJECT_MODEL)
    if settings.DEVICE == "cuda":
        _yolo.to("cuda")
    log.info("YOLO '%s' carregado (%s)", settings.OBJECT_MODEL, settings.DEVICE)
    return _yolo


def _area(b: Tuple[int, int, int, int]) -> float:
    return max(0.0, float((b[2] - b[0]) * (b[3] - b[1])))


def _intersecao(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    return float((x2 - x1) * (y2 - y1)) if x2 > x1 and y2 > y1 else 0.0


def iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """Interseção sobre união — o quanto duas caixas são a mesma caixa."""
    i = _intersecao(a, b)
    if not i:
        return 0.0
    uniao = _area(a) + _area(b) - i
    return i / uniao if uniao else 0.0


def ios(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """Interseção sobre a *menor* das duas — o quanto uma está dentro da outra.

    É o que o IoU não enxerga. O tronco marcado dentro do corpo inteiro rende
    IoU 0,27, passa pelo NMS e vira a segunda caixa sobre a mesma pessoa;
    aqui rende 1,00 e cai.
    """
    i = _intersecao(a, b)
    if not i:
        return 0.0
    menor = min(_area(a), _area(b))
    return i / menor if menor else 0.0


def razao_area(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    """Quanto a menor das duas caixas representa da maior, de 0 a 1."""
    aa, ab = _area(a), _area(b)
    maior = max(aa, ab)
    return min(aa, ab) / maior if maior else 0.0


def mesma_pessoa(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    """Duas caixas que descrevem o mesmo corpo.

    Um limiar só não dá conta, e isto foi medido, não suposto. Rotulando à
    mão todos os pares sobrepostos de duas capturas ao vivo do parque:

        ios   iou   razão área   o que era
        0,42  0,11     0,30      duas pessoas
        0,41  0,08     0,21      duas pessoas
        0,44  0,22     0,70      duas pessoas
        0,58  0,29     0,70      duas pessoas (o pior caso do parque)
        0,52  0,33     0,90      UMA pessoa, duas caixas
        0,69  0,48     0,89      UMA pessoa, duas caixas
        0,63  0,09     0,15      um pedaço (mão/fone) dentro de alguém

    A contenção sozinha já não separa — duplicata desce a 0,52 e pessoas
    distintas sobem a 0,58. O que separa é a contenção *junto com* o tamanho
    relativo, e cada linha abaixo diz uma coisa diferente sobre a cena:

    1. caixas quase idênticas (IoU alto) — o detector marcou duas vezes;
    2. uma inteiramente dentro da outra — tronco dentro do corpo;
    3. mesmo tamanho no mesmo lugar — a mesma pessoa vista por duas passadas;
    4. coisa pequena funda dentro de alguém — é parte dessa pessoa.

    O caso que fica de fora de propósito: duas caixas de tamanho parecido com
    sobreposição média são duas pessoas encostadas, e ficam as duas. Errar
    para o lado de manter custa uma caixa a mais; errar para o outro apaga
    gente do vídeo.
    """
    s = ios(a, b)
    if iou(a, b) >= settings.OBJECT_FUSAO_IOU:
        return True
    if s >= settings.OBJECT_FUSAO_IOS:
        return True
    if s < settings.OBJECT_FUSAO_IOS_MIN:
        return False
    razao = razao_area(a, b)
    if razao >= settings.OBJECT_FUSAO_RAZAO_IGUAL:
        return True
    return (s >= settings.OBJECT_FUSAO_IOS_PARTE
            and razao <= settings.OBJECT_FUSAO_RAZAO_PARTE)


# Classes que o modelo troca entre si sobre o mesmo veículo. "carro" entra
# porque a hesitação é de porte: a mesma van sai `truck` numa leitura e `car`
# noutra, e o que está em disputa é o tamanho, não o que é o objeto.
PORTE_CONCORRENTE: set[str] = {"carro", "caminhao", "onibus"}


def desfazer_hesitacao_de_porte(
        objetos: Sequence[ObjetoDetectado]) -> List[ObjetoDetectado]:
    """Duas classes de veículo sobre o mesmo corpo significam porte incerto.

    O NMS do YOLO é **por classe**: um veículo ambíguo não rende uma caixa
    com dúvida, rende duas caixas com certezas diferentes — `bus` a 0,88
    sobre `truck` a 0,60, no mesmo lugar. A fusão adiante também é por
    classe, de propósito (pessoa dentro de um carro não é releitura), então
    as duas sobrevivem: uma vira "Ônibus" na ficha e a outra "carro". Foi
    assim que um caminhão de baú da Portaria virou ônibus.

    Aqui a sobreposição é lida pelo que ela é: o detector não sabe o porte.
    As duas caem para "carro", a fusão as reduz a uma, e o registro deixa de
    afirmar o que não se sabe.

    Mais honesto que subir o limiar sozinho, porque não depende de adivinhar
    onde fica a fronteira entre acerto e erro — usa o desacordo do próprio
    modelo como evidência. O limiar continua valendo para o outro caso, o do
    rótulo único e confiante, em que não há desacordo para observar.
    """
    grandes = [o for o in objetos if o.classe in PORTE_CONCORRENTE]
    em_duvida: set[int] = set()
    for i, a in enumerate(grandes):
        for b in grandes[i + 1:]:
            if a.classe == b.classe:
                continue
            if ios(a.bbox, b.bbox) >= settings.OBJECT_PORTE_CONFLITO:
                em_duvida.add(id(a))
                em_duvida.add(id(b))
    for o in objetos:
        if id(o) in em_duvida and o.classe in PORTE_VEICULO:
            o.classe = PORTE_VEICULO[o.classe]
    return list(objetos)


def fundir_sobrepostas(objetos: Sequence[ObjetoDetectado]) -> List[ObjetoDetectado]:
    """Uma caixa por objeto.

    O NMS do YOLO já tira a caixa quase idêntica, mas não a caixa *contida*
    nem a mesma pessoa relida por outra passada. Quem sobrevive aqui é a
    leitura mais confiante; as que ela engole são releituras do mesmo corpo,
    pelo critério de `mesma_pessoa`.
    """
    ficam: List[ObjetoDetectado] = []
    for obj in sorted(objetos, key=lambda o: -o.confianca):
        engolida: Optional[ObjetoDetectado] = None
        for mantida in ficam:
            # Só contra a mesma classe. Pessoa dentro da caixa de um carro é
            # pessoa em pé ao lado do carro, não leitura repetida — comparar
            # entre classes apagaria justamente quem o operador precisa ver.
            if mantida.classe != obj.classe:
                continue
            if mesma_pessoa(obj.bbox, mantida.bbox):
                engolida = mantida
                break
        if engolida is None:
            ficam.append(obj)
        elif engolida.track_id is None and obj.track_id is not None:
            # A trilha vale mais que a caixa que a trouxe. A varredura não
            # rastreia; sem herdar daqui, uma caixa dela cobrindo a do
            # rastreador tiraria do objeto a identidade entre quadros — e com
            # ela a análise de ação, que ignora objeto sem trilha.
            engolida.track_id = obj.track_id
    return ficam


def _ladrilhos(largura: int, altura: int, colunas: int, linhas: int,
               sobreposicao: float) -> List[Tuple[int, int, int, int]]:
    """Recortes que cobrem o quadro inteiro, emendando um no outro.

    A emenda não é enfeite: sem ela, quem cai na divisa sai partido nos dois
    ladrilhos e não é reconhecido por nenhum.
    """
    colunas, linhas = max(1, colunas), max(1, linhas)
    sob = min(max(sobreposicao, 0.0), 0.9)
    lw = int(largura / (colunas - (colunas - 1) * sob))
    lh = int(altura / (linhas - (linhas - 1) * sob))
    dx, dy = max(1, int(lw * (1.0 - sob))), max(1, int(lh * (1.0 - sob)))

    recortes: List[Tuple[int, int, int, int]] = []
    for iy in range(linhas):
        for ix in range(colunas):
            # O último de cada fileira é ancorado na borda, não no passo: o
            # passo é inteiro e a sobra da divisão deixaria uma faixa do
            # quadro fora de todos os ladrilhos.
            x0 = largura - lw if ix == colunas - 1 else ix * dx
            y0 = altura - lh if iy == linhas - 1 else iy * dy
            x0, y0 = max(0, x0), max(0, y0)
            recortes.append((x0, y0, min(largura, x0 + lw), min(altura, y0 + lh)))
    return recortes


def _indices_varredura(modelo) -> List[int]:
    """Índices COCO das classes que a varredura procura.

    Lidos do modelo, não fixados: `person` é 0 hoje, mas trocar os pesos por
    um treinado em outro conjunto mudaria o número sem avisar.
    """
    global _idx_varredura
    if _idx_varredura is None:
        alvo = set(settings.OBJECT_VARREDURA_CLASSES)
        _idx_varredura = [i for i, nome in modelo.names.items()
                          if MAPA_CLASSES.get(nome) in alvo]
    return _idx_varredura


# Porte de veículo é a única distinção deste mapa que o modelo faz mal quando
# hesita, e hesitar é o caso comum do furgão. Acima deste limiar o rótulo é
# afirmação; abaixo, vira "carro" — que é verdadeiro para qualquer um dos três.
PORTE_VEICULO: Dict[str, str] = {"caminhao": "carro", "onibus": "carro"}


def _classe_de(rotulo_coco: str, confianca: float) -> Optional[str]:
    """Rótulo do COCO para classe operacional, já com a ressalva de porte."""
    classe = MAPA_CLASSES.get(rotulo_coco)
    if classe is None:
        return None
    if classe in PORTE_VEICULO and confianca < settings.OBJECT_PORTE_MIN:
        return PORTE_VEICULO[classe]
    return classe


def _colher(resultados, classes_fora: Optional[set],
            deslocamento: Tuple[int, int] = (0, 0)) -> List[ObjetoDetectado]:
    """Traduz a saída do YOLO para as classes operacionais do SEGURANÇA.

    `deslocamento` devolve a caixa do ladrilho às coordenadas do quadro
    inteiro — é o que faz a varredura e a passada inteira falarem do mesmo
    lugar, sem o que a fusão não teria como saber que são o mesmo corpo.
    """
    dx, dy = deslocamento
    saida: List[ObjetoDetectado] = []
    for r in resultados:
        nomes = r.names
        if r.boxes is None:
            continue
        for box in r.boxes:
            classe = _classe_de(nomes[int(box.cls[0])], float(box.conf[0]))
            if classe is None:
                continue
            # Recusado pelo lugar, não pela confiança: carro visto da janela
            # de um hall é carro de verdade, e o modelo acerta ao apontá-lo.
            # Errado é registrá-lo como se estivesse ali dentro.
            if classes_fora and classe in classes_fora:
                continue
            x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
            tid = int(box.id[0]) if getattr(box, "id", None) is not None else None
            saida.append(ObjetoDetectado(
                classe, float(box.conf[0]),
                (x1 + dx, y1 + dy, x2 + dx, y2 + dy), tid))
    return saida


def _na_emenda(caixa: Tuple[int, int, int, int],
               ladrilho: Tuple[int, int, int, int],
               largura: int, altura: int, folga: int = 2) -> bool:
    """A caixa encosta numa borda do ladrilho que não é borda do quadro.

    Caixa assim é corpo cortado pelo recorte, não leitura nova: o YOLO marca
    o pedaço que coube e devolve uma caixa que termina na divisa. Foi daí que
    vieram as três duplicatas que sobraram na primeira medição — todas com a
    base no mesmo y da emenda, empilhadas sobre quem a passada inteira já
    tinha marcado inteiro.

    Descartá-las não custa ninguém, porque a emenda existe justamente para
    que quem cai nela apareça inteiro no ladrilho vizinho. Quem é grande
    demais para caber em qualquer ladrilho é grande na cena — e esse a
    passada do quadro inteiro já vê.
    """
    x1, y1, x2, y2 = caixa
    lx1, ly1, lx2, ly2 = ladrilho
    return ((x1 <= lx1 + folga and lx1 > 0)
            or (y1 <= ly1 + folga and ly1 > 0)
            or (x2 >= lx2 - folga and lx2 < largura)
            or (y2 >= ly2 - folga and ly2 < altura))


def _varrer_ladrilhos(modelo, quadro: np.ndarray,
                      classes_fora: Optional[set]) -> List[ObjetoDetectado]:
    """Segunda passada, em pedaços ampliados do quadro.

    Quem senta ao fundo da sala chega ao modelo com menos de 30 px de altura,
    e nessa escala não há limiar que resolva — baixá-lo só empilha caixa sobre
    quem já estava detectado. O que resolve é dar mais pixels ao mesmo corpo.
    """
    indices = _indices_varredura(modelo)
    if not indices:
        return []
    altura, largura = quadro.shape[:2]
    achados: List[ObjetoDetectado] = []
    for ladrilho in _ladrilhos(largura, altura,
                               settings.OBJECT_VARREDURA_COLUNAS,
                               settings.OBJECT_VARREDURA_LINHAS,
                               settings.OBJECT_VARREDURA_SOBREPOSICAO):
        x0, y0, x1, y1 = ladrilho
        pedaco = quadro[y0:y1, x0:x1]
        if pedaco.size == 0:
            continue
        # Sem rastreio, sempre: o ByteTrack guarda estado entre chamadas, e
        # cada ladrilho chega a ele como um corte de cena. A trilha de quem
        # também saiu na passada inteira é herdada na fusão.
        resultados = modelo(pedaco, conf=settings.OBJECT_VARREDURA_LIMIAR,
                            imgsz=settings.OBJECT_VARREDURA_IMGSZ,
                            iou=settings.OBJECT_NMS_IOU,
                            classes=indices, verbose=False)
        achados.extend(
            obj for obj in _colher(resultados, classes_fora, (x0, y0))
            if not _na_emenda(obj.bbox, ladrilho, largura, altura)
        )
    return achados


def detectar_objetos(quadro: np.ndarray, limiar: Optional[float] = None,
                     rastrear: bool = True,
                     classes_fora: Optional[set] = None,
                     varredura: Optional[str] = None,
                     imgsz: Optional[int] = None) -> List[ObjetoDetectado]:
    """Roda YOLO no quadro e devolve uma caixa por objeto.

    Com `rastrear=True` usa ByteTrack e mantém track_id estável entre quadros
    — é o que permite medir velocidade e permanência.

    Duas passadas, não uma: a do quadro inteiro vê a cena, e a varredura em
    ladrilhos volta para buscar quem é pequeno demais para ela. As duas listas
    passam juntas pela fusão, que é o que impede a mesma pessoa de sair
    marcada duas vezes — uma por passada.
    """
    lim = settings.OBJECT_THRESHOLD if limiar is None else limiar
    modelo = obter_yolo()

    # `imgsz` manda mais que o limiar em quem está longe. O YOLO reduz o quadro
    # a este lado antes de olhar: a 640, uma pessoa sentada ao fundo da sala
    # chega ao modelo com poucos pixels de altura e sai com confiança baixa
    # demais para qualquer limiar razoável — 6 pessoas contra 11 no mesmo
    # quadro. Baixar o limiar não substitui isto: a 0,10 as caixas extras
    # caíram todas empilhadas sobre quem já estava detectado, sem achar
    # ninguém novo. Quem está longe demais até para este lado é assunto da
    # varredura, logo abaixo.
    # Quem mede *movimento* pede um lado menor: a posição do corpo basta, e a
    # rajada de quadros seguidos sairia a oito segundos por câmera no lado
    # cheio contra dois no reduzido.
    tam = settings.OBJECT_IMGSZ if imgsz is None else imgsz
    if rastrear:
        resultados = modelo.track(quadro, persist=True, conf=lim, imgsz=tam,
                                  iou=settings.OBJECT_NMS_IOU,
                                  verbose=False, tracker="bytetrack.yaml")
    else:
        resultados = modelo(quadro, conf=lim, imgsz=tam,
                            iou=settings.OBJECT_NMS_IOU, verbose=False)

    saida = _colher(resultados, classes_fora)

    # "ocupado" varre só o quadro que já tem algo: corredor, garagem e
    # datacenter vazios não pagam a segunda passada, e são a maior parte do
    # parque. O preço desse atalho é a pessoa distante que não aparece nem na
    # passada inteira — para ela existe "sempre".
    modo = (settings.OBJECT_VARREDURA if varredura is None else varredura) or ""
    modo = modo.lower()
    if modo == "sempre" or (modo == "ocupado" and saida):
        saida.extend(_varrer_ladrilhos(modelo, quadro, classes_fora))

    # Antes da fusão: ela só junta a mesma classe, e é justamente o
    # desacordo de classe que precisa ser resolvido para as duas leituras do
    # mesmo veículo virarem uma.
    return fundir_sobrepostas(desfazer_hesitacao_de_porte(saida))


# ----------------------------------------------------------------------
# Geometria de zonas
# ----------------------------------------------------------------------
def ponto_em_poligono(ponto: Tuple[float, float], poligono: Sequence[Sequence[float]]) -> bool:
    """Ray casting. `poligono` em coordenadas normalizadas 0..1."""
    x, y = ponto
    dentro = False
    n = len(poligono)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poligono[i]
        xj, yj = poligono[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            dentro = not dentro
        j = i
    return dentro


def _lado(p: Tuple[float, float], a: Sequence[float], b: Sequence[float]) -> float:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def cruzou_linha(anterior: Tuple[float, float], atual: Tuple[float, float],
                 linha: Sequence[Sequence[float]]) -> Optional[str]:
    """Devolve 'entrada'/'saida' conforme o lado de onde veio, ou None."""
    if len(linha) < 2:
        return None
    a, b = linha[0], linha[1]
    s1, s2 = _lado(anterior, a, b), _lado(atual, a, b)
    if s1 == 0 or s2 == 0 or (s1 > 0) == (s2 > 0):
        return None
    return "entrada" if s1 > 0 else "saida"


# ----------------------------------------------------------------------
# Análise de ações
# ----------------------------------------------------------------------
@dataclass
class Trilha:
    track_id: int
    classe: str
    pontos: Deque[Tuple[float, float, float]] = field(default_factory=lambda: deque(maxlen=90))
    # Altura aparente do corpo em cada ponto, na mesma ordem de `pontos`. É a
    # régua que torna a velocidade comparável entre quem passa rente à lente e
    # quem atravessa o fundo da sala.
    alturas: Deque[float] = field(default_factory=lambda: deque(maxlen=90))
    visto_em: float = field(default_factory=time.time)
    primeiro_em: float = field(default_factory=time.time)
    parado_desde: Optional[float] = None
    # Desde quando esta pessoa reúne as condições de corrida. Enquanto durar
    # pouco não vira alerta: um salto de posição isolado é quase sempre o
    # rastreador reatribuindo a trilha, não alguém disparando a correr.
    correndo_desde: Optional[float] = None
    alertou: Dict[str, float] = field(default_factory=dict)

    def registrar(self, x: float, y: float, t: float, altura: float = 0.0) -> None:
        self.pontos.append((x, y, t))
        self.alturas.append(float(altura))
        self.visto_em = t

    def velocidade(self) -> float:
        """Unidades normalizadas por segundo, na janela recente."""
        if len(self.pontos) < 5:
            return 0.0
        x0, y0, t0 = self.pontos[-5]
        x1, y1, t1 = self.pontos[-1]
        dt = max(t1 - t0, 1e-3)
        return float(np.hypot(x1 - x0, y1 - y0) / dt)

    def direcao(self) -> Optional[Tuple[float, float]]:
        if len(self.pontos) < 6:
            return None
        x0, y0, _ = self.pontos[-6]
        x1, y1, _ = self.pontos[-1]
        v = np.array([x1 - x0, y1 - y0])
        n = float(np.linalg.norm(v))
        return tuple(v / n) if n > 1e-4 else None

    def caminho(self) -> float:
        """Distância *percorrida* por segundo, somando passo a passo.

        Diferente de `velocidade`, que mede o deslocamento líquido entre dois
        instantes. A distinção é o que torna briga detectável: quem luta vai e
        volta no mesmo lugar, percorre muito caminho e termina onde começou —
        medido pela velocidade, dá zero.

        Corrida é o caso oposto: caminho e deslocamento quase iguais, porque
        o movimento é dirigido.
        """
        if len(self.pontos) < 5:
            return 0.0
        soma = 0.0
        pts = list(self.pontos)[-9:]
        for (x0, y0, _), (x1, y1, _) in zip(pts, pts[1:]):
            soma += float(np.hypot(x1 - x0, y1 - y0))
        dt = max(pts[-1][2] - pts[0][2], 1e-3)
        return soma / dt

    def agitacao(self) -> float:
        """Quanto a direção do movimento muda entre passos consecutivos.

        Quem atravessa um corredor anda em linha: os vetores de um passo para
        o seguinte apontam quase para o mesmo lado, e a média fica perto de
        zero. Quem está em luta corporal oscila — avança, recua, gira —, e os
        vetores consecutivos divergem.

        É o sinal que separa briga de corrida: as duas têm velocidade alta, só
        que a corrida é dirigida. Devolve de 0 (reto) a 1 (invertendo a cada
        passo).
        """
        if len(self.pontos) < 6:
            return 0.0
        passos = []
        for (x0, y0, _), (x1, y1, _) in zip(self.pontos, list(self.pontos)[1:]):
            v = np.array([x1 - x0, y1 - y0])
            n = float(np.linalg.norm(v))
            if n > 1e-4:
                passos.append(v / n)
        if len(passos) < 4:
            return 0.0
        # Cosseno entre passos vizinhos: 1 é seguir reto, -1 é voltar.
        cossenos = [float(np.dot(a, b)) for a, b in zip(passos, passos[1:])]
        return float(np.clip((1.0 - np.mean(cossenos)) / 2.0, 0.0, 1.0))

    def escala(self) -> float:
        """Altura aparente do corpo, em fração da altura do quadro.

        Existe para dar unidade à velocidade. Em fração de quadro, a mesma
        corrida rende muito mais deslocamento rente à lente que no fundo da
        sala — um piso só vira, na prática, dois pisos diferentes na mesma
        câmera: alto demais para o fundo, baixo demais para a frente. Medido
        em alturas de corpo por segundo, correr é correr nos dois lugares.

        Mediana das recentes, não a última: uma caixa mal cortada num quadro
        mudaria a régua inteira.
        """
        recentes = [h for h in list(self.alturas)[-9:] if h > 1e-3]
        return float(np.median(recentes)) if recentes else 0.0

    def arranque(self, salto_max_s: float = 1.0) -> Tuple[float, float]:
        """Ritmo recente e ritmo logo antes, em alturas de corpo por segundo.

        É o par que decide se a corrida foi *súbita*. Comparar a pessoa com
        ela mesma de um segundo atrás, e não só com um limiar, é o que separa
        quem disparou a correr de quem apenas anda depressa — e quem sempre
        atravessa o pátio apressado deixa de virar alerta a cada volta.

        Só passos contíguos entram, e o buraco entre duas visitas reinicia a
        conta. A trilha sobrevive de uma visita do rodízio até a seguinte, e
        esse intervalo é de dezenas de segundos: tomado como passo, ele daria
        deslocamento grande sobre tempo grande e envenenaria as duas médias.
        """
        escala = self.escala()
        if escala <= 1e-3:
            return 0.0, 0.0
        passos: List[float] = []
        pts = list(self.pontos)
        for (x0, y0, t0), (x1, y1, t1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt > salto_max_s:
                passos.clear()          # outra visita: a comparação recomeça
                continue
            if dt <= 1e-3:
                continue
            passos.append(float(np.hypot(x1 - x0, y1 - y0) / dt / escala))
        if len(passos) < 5:
            return 0.0, 0.0
        return float(np.mean(passos[-3:])), float(np.mean(passos[-7:-3]))

    def queda_vertical(self) -> float:
        """Razão de achatamento recente — proxy simples para queda."""
        if len(self.pontos) < 8:
            return 0.0
        _, y0, t0 = self.pontos[-8]
        _, y1, t1 = self.pontos[-1]
        dt = max(t1 - t0, 1e-3)
        return float(max(0.0, (y1 - y0) / dt))


def _minutos(hhmm: str, padrao: int) -> int:
    try:
        h, m = hhmm.strip().split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return padrao


def janela_horario(inicio: Optional[str] = None,
                   fim: Optional[str] = None) -> Tuple[str, str]:
    """A janela que vale: a da câmera, ou a do sistema quando ela não tem.

    Existe para que a tela, o alerta e o detector leiam a mesma coisa. Quando
    os três montavam a frase por conta própria, bastava a câmera ter janela
    própria para a tela anunciar a do sistema.
    """
    return (inicio or settings.ACAO_HORARIO_INICIO,
            fim or settings.ACAO_HORARIO_FIM)


def fora_do_horario(quando: Optional[datetime] = None,
                    inicio: Optional[str] = None,
                    fim: Optional[str] = None) -> bool:
    """O instante está fora da janela em que circular é normal?

    Em hora **local**, porque é assim que quem configura pensa o expediente —
    o banco guarda UTC, mas "entrada fora de hora" se julga pelo relógio da
    parede. A janela atravessa a meia-noite quando o fim é menor que o
    início, que é o caso de quem quer vigiar a madrugada (22:00–06:00).

    `inicio`/`fim` são os da câmera; em branco, valem os do sistema.
    """
    agora = datetime.now() if quando is None else quando
    minuto = agora.hour * 60 + agora.minute
    ini_txt, fim_txt = janela_horario(inicio, fim)
    inicio_m = _minutos(ini_txt, 7 * 60)
    fim_m = _minutos(fim_txt, 19 * 60)
    if inicio_m == fim_m:
        return False                      # janela de 24h: nada é fora de hora
    if inicio_m < fim_m:
        return not (inicio_m <= minuto < fim_m)
    return fim_m <= minuto < inicio_m     # janela que cruza a meia-noite


class RastreioPorPosicao:
    """Dá identidade a um objeto entre duas visitas à mesma câmera.

    O ByteTrack do Ultralytics não serve no rodízio: ele guarda estado entre
    chamadas e o mesmo modelo atende as 26 câmeras em revezamento, então cada
    quadro chega como um corte de cena e ele descarta o que não associou —
    medido, a Produção 02 relatava ZERO pessoas numa sala com onze. Foi por
    isso que a inferência passou a rodar sem rastreio.

    Só que sem identidade a análise de ação não processa nada: ela ignora
    objeto sem `track_id`, e era daí que vinha o banco com zero ocorrência de
    ação desde sempre, inclusive aglomeração — a contagem de pessoas acontece
    depois desse descarte.

    Aqui a identidade vem da posição, não da aparência: a caixa desta visita
    herda o id da caixa mais sobreposta da visita anterior, da mesma classe.
    Entre duas visitas separadas por dezenas de segundos isso não é um
    rastreador de trajetória — é um teste de *permanência no lugar*, que é
    exatamente a pergunta que permanência e aglomeração fazem. Quem atravessou
    a sala não casa com nada e ganha id novo, e está certo: para o sistema é
    outra passagem.

    O que isto **não** entrega: corrida, queda e contrafluxo medem movimento
    entre quadros próximos, e duas visitas à mesma câmera estão a dezenas de
    segundos de distância. Nessa escala não há velocidade para medir, e o
    catálogo de ações marca as três como `exige_continua`.
    """

    def __init__(self, iou_minimo: Optional[float] = None,
                 ttl_s: float = 300.0) -> None:
        self.iou_minimo = (settings.ACAO_RASTREIO_IOU
                           if iou_minimo is None else iou_minimo)
        self.ttl_s = ttl_s
        # track_id -> (classe, caixa, visto_em)
        self._conhecidos: Dict[int, Tuple[str, Tuple[int, int, int, int], float]] = {}
        self._proximo = 1

    def _expirar(self, agora: float) -> None:
        mortos = [tid for tid, (_, _, visto) in self._conhecidos.items()
                  if agora - visto > self.ttl_s]
        for tid in mortos:
            del self._conhecidos[tid]

    def atribuir(self, objetos: Sequence[ObjetoDetectado],
                 agora: Optional[float] = None) -> List[ObjetoDetectado]:
        """Carimba `track_id` em cada objeto, reaproveitando o da visita anterior."""
        t = time.time() if agora is None else agora
        self._expirar(t)

        # Pares candidatos, do mais sobreposto ao menos. Guloso e não ótimo:
        # com uma dezena de objetos por quadro, a diferença para um pareamento
        # ótimo não paga a complexidade — e o empate que importaria é entre
        # duas pessoas encostadas, que a fusão de caixas já resolveu antes.
        candidatos = []
        for i, obj in enumerate(objetos):
            for tid, (classe, caixa, _) in self._conhecidos.items():
                if classe != obj.classe:
                    continue
                s = iou(obj.bbox, caixa)
                if s >= self.iou_minimo:
                    candidatos.append((s, i, tid))
        candidatos.sort(key=lambda c: -c[0])

        casados_obj: set = set()
        casados_tid: set = set()
        for _, i, tid in candidatos:
            if i in casados_obj or tid in casados_tid:
                continue
            objetos[i].track_id = tid
            casados_obj.add(i)
            casados_tid.add(tid)

        for i, obj in enumerate(objetos):
            if i not in casados_obj:
                obj.track_id = self._proximo
                self._proximo += 1

        for obj in objetos:
            self._conhecidos[obj.track_id] = (obj.classe, obj.bbox, t)
        return list(objetos)


class AnalisadorAcoes:
    """Mantém trilhas por câmera e emite eventos comportamentais.

    Os números vêm da configuração, não do código: os originais foram escritos
    para cena de rua a 25 fps, e este parque é de escritório visitado de
    tempos em tempos — permanência de 90 segundos acusaria todo mundo que
    senta para trabalhar. O que torna cada ação útil é o lugar, e é por isso
    que a escolha é por câmera.
    """

    def __init__(
        self,
        seg_permanencia: Optional[float] = None,
        n_aglomeracao: Optional[int] = None,
        raio_aglomeracao: Optional[float] = None,
        seg_abandono: Optional[float] = None,
        cooldown_s: Optional[float] = None,
    ) -> None:
        # Corrida súbita: ritmo, arranque, direção e quanto têm de durar.
        self.ritmo_corrida = settings.ACAO_CORRIDA_RITMO
        self.salto_corrida = settings.ACAO_CORRIDA_SALTO
        self.agitacao_corrida = settings.ACAO_CORRIDA_AGITACAO
        self.duracao_corrida = settings.ACAO_CORRIDA_DURACAO_S
        self.coerencia_camera = settings.ACAO_CORRIDA_COERENCIA
        # Briga: os três sinais e o tempo que precisam durar juntos.
        self.vel_briga = settings.ACAO_BRIGA_VEL
        self.agitacao_briga = settings.ACAO_BRIGA_AGITACAO
        self.proximidade_briga = settings.ACAO_BRIGA_PROXIMIDADE
        self.duracao_briga = settings.ACAO_BRIGA_DURACAO_S
        # Desde quando cada par está colado e agitado.
        self._briga_desde: Dict[Tuple[int, int], float] = {}
        self.seg_permanencia = (settings.ACAO_PERMANENCIA_S
                                if seg_permanencia is None else seg_permanencia)
        self.n_aglomeracao = (settings.ACAO_AGLOMERACAO_N
                              if n_aglomeracao is None else n_aglomeracao)
        self.raio_aglomeracao = (settings.ACAO_AGLOMERACAO_RAIO
                                 if raio_aglomeracao is None else raio_aglomeracao)
        self.seg_abandono = settings.ACAO_ABANDONO_S if seg_abandono is None else seg_abandono
        self.cooldown_s = settings.ACAO_COOLDOWN_S if cooldown_s is None else cooldown_s
        self.trilhas: Dict[int, Trilha] = {}
        self._aglomeracao_em: Optional[float] = None
        # Quanto uma trilha sobrevive sem ser vista. Oito segundos servem para
        # quem analisa a câmera quadro a quadro; quem a visita de tempos em
        # tempos precisa esticar isto até além do intervalo entre as visitas,
        # ou a trilha é descartada antes da próxima e nada acumula.
        self.ttl_trilha = 8.0
        # Quais ações esta câmera emite. `None` é "todas", para quem usa o
        # analisador sem passar por uma câmera cadastrada — os testes e o
        # worker avulso. A câmera do parque sempre diz a sua lista, porque
        # vigiar queda num datacenter e aglomeração num corredor são decisões
        # diferentes e quem as toma é quem conhece o lugar.
        self.acoes_ativas: Optional[set] = None
        # Janela de horário permitido desta câmera. `None` nas duas é "usa a
        # do sistema" — ver `Camera.horario_inicio`. Relida a cada volta pela
        # inferência, como `acoes_ativas`: mudar a janela na tela tem de valer
        # na volta seguinte, não no próximo reinício.
        self.horario_inicio: Optional[str] = None
        self.horario_fim: Optional[str] = None

    # -- utilidades internas ------------------------------------------
    def ativa(self, classe: str) -> bool:
        """Esta câmera foi configurada para vigiar esta ação?"""
        return self.acoes_ativas is None or classe in self.acoes_ativas

    def _firme(self, obj: ObjetoDetectado) -> bool:
        """A caixa sustenta um alerta sozinha?

        Mesmo piso que a inferência usa para decidir se vale **registrar** a
        detecção (`INFERENCIA_CONFIANCA_MIN` e `INFERENCIA_LADO_MIN_PX`). O
        detector entrega a partir do limiar da câmera, 0,25 no parque — e sem
        este piso uma mancha de 0,32 virava alerta de severidade 3 sozinha,
        enquanto a mesma mancha era fraca demais para entrar no registro como
        simples detecção. Alerta sustentado por prova que o próprio sistema
        descarta é pior que alerta nenhum.

        Vale para ação que acusa **uma** pessoa. Aglomeração não passa por
        aqui: ela se decide pelo conjunto, onde uma caixa fraca entre oito não
        muda o fato.
        """
        if obj.confianca < settings.INFERENCIA_CONFIANCA_MIN:
            return False
        x1, y1, x2, y2 = obj.bbox
        return min(x2 - x1, y2 - y1) >= settings.INFERENCIA_LADO_MIN_PX

    def _camera_moveu(self, objetos, largura: int, altura: int) -> bool:
        """Todo mundo em cena andou junto — foi a câmera, não as pessoas.

        Tremor, rajada de vento ou reposicionamento da PTZ deslocam a imagem
        inteira: cada trilha ganha de uma vez o mesmo passo, na mesma direção
        e do mesmo tamanho. Gente não faz isso — cada um anda para o seu lado,
        e numa sala de expediente a maior parte sequer sai da cadeira.

        Daí as duas exigências. A **coerência** mede o quanto os passos são o
        mesmo vetor; a de **tamanhos parecidos** é o que separa o tranco da
        imagem de um grupo que caminha junto no mesmo sentido, onde sempre há
        quem ande mais devagar — e, sobretudo, quem não ande.
        """
        passos = []
        for obj in objetos:
            if obj.track_id is None:
                continue
            trilha = self.trilhas.get(obj.track_id)
            if trilha is None or not trilha.pontos:
                continue
            cx, cy = obj.centro()
            x0, y0, _ = trilha.pontos[-1]
            passos.append((cx / largura - x0, cy / altura - y0))

        if len(passos) < 3:
            return False
        vetores = np.asarray(passos, dtype=float)
        tamanhos = np.hypot(vetores[:, 0], vetores[:, 1])
        menor, maior = float(tamanhos.min()), float(tamanhos.max())
        if menor < 1e-4:
            return False                  # alguém ficou parado: são pessoas
        if menor < 0.4 * maior:
            return False                  # passos de tamanhos muito diferentes
        coerencia = float(np.linalg.norm(vetores.sum(axis=0)) / tamanhos.sum())
        return coerencia >= self.coerencia_camera

    def _avaliar_corrida(self, trilha: Trilha, obj, t: float,
                         camera_moveu: bool, eventos: List[EventoAcao]) -> None:
        """Passou de andar a correr, e seguiu correndo.

        Quatro condições, e nenhuma basta sozinha:

        * **ritmo** acima do piso, em alturas de corpo por segundo — a
          unidade que vale igual rente à lente e no fundo da sala.
        * **arranque**: o ritmo subiu sobre o que esta mesma pessoa vinha
          tendo. É o "súbita" do rótulo, e é o que distingue disparar a
          correr de apenas andar depressa.
        * **direção**: correr é dirigido. Oscilar no mesmo lugar é briga, ou
          é a caixa trocando de dono entre duas pessoas próximas.
        * **duração**: tem de durar mais que um quadro.

        `camera_moveu` veta tudo: com a imagem andando, toda trilha arranca
        ao mesmo tempo e todas pareceriam correr de uma vez.
        """
        ritmo, antes = trilha.arranque()
        correndo = (not camera_moveu
                    and ritmo >= self.ritmo_corrida
                    and ritmo - antes >= self.salto_corrida
                    and trilha.agitacao() <= self.agitacao_corrida)
        if not correndo:
            trilha.correndo_desde = None
            return

        trilha.correndo_desde = trilha.correndo_desde or t
        if t - trilha.correndo_desde < self.duracao_corrida:
            return
        # Por último, porque marca a trilha: conferir o cooldown antes de a
        # corrida estar confirmada gastaria o alerta num indício que ainda
        # podia se desfazer no quadro seguinte.
        if not self._pode_alertar(trilha, "corrida", t):
            return

        eventos.append(EventoAcao(
            "corrida", ROTULO_ACAO["corrida"],
            confianca=float(np.clip(0.55 + (ritmo - self.ritmo_corrida) / 4.0,
                                    0.55, 0.97)),
            severidade=SEVERIDADE_ACAO["corrida"],
            duracao_s=max(1, int(t - trilha.correndo_desde)),
            envolvidos=1, bbox=obj.bbox, track_id=obj.track_id,
        ))

    def _pode_alertar(self, trilha: Trilha, classe: str, agora: float) -> bool:
        # A seleção da câmera vem antes do cooldown de propósito: ação
        # desligada não deve nem marcar a trilha, senão religá-la ficaria
        # mudo até o cooldown de um alerta que nunca foi emitido vencer.
        if not self.ativa(classe):
            return False
        ultimo = trilha.alertou.get(classe, 0.0)
        if agora - ultimo < self.cooldown_s:
            return False
        trilha.alertou[classe] = agora
        return True

    def _limpar(self, agora: float, ttl: Optional[float] = None) -> None:
        ttl = self.ttl_trilha if ttl is None else ttl
        mortos = [tid for tid, t in self.trilhas.items() if agora - t.visto_em > ttl]
        for tid in mortos:
            del self.trilhas[tid]

    # -- API ----------------------------------------------------------
    def atualizar(
        self,
        objetos: Sequence[ObjetoDetectado],
        largura: int,
        altura: int,
        zonas: Optional[Sequence[dict]] = None,
        agora: Optional[float] = None,
    ) -> List[EventoAcao]:
        t = time.time() if agora is None else agora
        eventos: List[EventoAcao] = []
        zonas = zonas or []
        # Uma leitura do relógio por volta: dentro do laço, duas pessoas no
        # mesmo quadro podiam cair em lados diferentes da virada do minuto.
        self._fora_de_horario = self.ativa("fora_de_horario") and fora_do_horario(
            inicio=self.horario_inicio, fim=self.horario_fim)

        pessoas_xy: List[Tuple[float, float]] = []

        # Antes de registrar qualquer ponto: a conta compara onde cada trilha
        # estava com onde o objeto está agora, e registrar primeiro apagaria
        # justamente o passo que ela precisa medir.
        camera_moveu = self._camera_moveu(objetos, largura, altura)

        for obj in objetos:
            if obj.track_id is None:
                continue
            cx, cy = obj.centro()
            nx, ny = cx / largura, cy / altura

            trilha = self.trilhas.get(obj.track_id)
            if trilha is None:
                trilha = Trilha(obj.track_id, obj.classe)
                self.trilhas[obj.track_id] = trilha
            anterior = (trilha.pontos[-1][0], trilha.pontos[-1][1]) if trilha.pontos else None
            _, topo, _, base = obj.bbox
            trilha.registrar(nx, ny, t, abs(base - topo) / max(altura, 1))

            if obj.classe == "pessoa":
                pessoas_xy.append((nx, ny))

            vel = trilha.velocidade()

            # --- corrida súbita ---
            if obj.classe == "pessoa" and self.ativa("corrida"):
                self._avaliar_corrida(trilha, obj, t, camera_moveu, eventos)

            # --- queda ---
            if obj.classe == "pessoa":
                x1, y1, x2, y2 = obj.bbox
                razao = (x2 - x1) / max(y2 - y1, 1)
                if razao > 1.35 and trilha.queda_vertical() > 0.05 and self._pode_alertar(trilha, "queda", t):
                    eventos.append(EventoAcao(
                        "queda", ROTULO_ACAO["queda"], confianca=0.82,
                        severidade=SEVERIDADE_ACAO["queda"],
                        duracao_s=1, envolvidos=1, bbox=obj.bbox, track_id=obj.track_id,
                    ))

            # --- permanência prolongada ---
            if vel < 0.012:
                trilha.parado_desde = trilha.parado_desde or t
                parado_ha = t - trilha.parado_desde
                if obj.classe == "pessoa" and parado_ha > self.seg_permanencia \
                        and self._pode_alertar(trilha, "permanencia", t):
                    eventos.append(EventoAcao(
                        "permanencia", ROTULO_ACAO["permanencia"], confianca=0.88,
                        severidade=SEVERIDADE_ACAO["permanencia"],
                        duracao_s=int(parado_ha), envolvidos=1,
                        bbox=obj.bbox, track_id=obj.track_id,
                    ))
                if obj.classe in ("volume", "mochila") and parado_ha > self.seg_abandono \
                        and self._pode_alertar(trilha, "abandono", t):
                    eventos.append(EventoAcao(
                        "abandono", ROTULO_ACAO["abandono"], confianca=0.90,
                        severidade=SEVERIDADE_ACAO["abandono"],
                        duracao_s=int(parado_ha), envolvidos=1,
                        bbox=obj.bbox, track_id=obj.track_id,
                    ))
            else:
                trilha.parado_desde = None

            # --- arma em cena ---
            # Não depende de zona, de horário nem de trajetória: o objeto
            # estar ali já é o fato. A classe vem de `knife` do COCO e, neste
            # parque, nunca apareceu — ligar custa nada e pode valer muito.
            if obj.classe == "arma" and self._pode_alertar(trilha, "arma_detectada", t):
                eventos.append(EventoAcao(
                    "arma_detectada", ROTULO_ACAO["arma_detectada"],
                    confianca=float(obj.confianca),
                    severidade=SEVERIDADE_ACAO["arma_detectada"],
                    duracao_s=1, envolvidos=1,
                    bbox=obj.bbox, track_id=obj.track_id,
                ))

            # --- entrada em horário não permitido ---
            # Avaliado por pessoa, e não uma vez por quadro, porque o alerta
            # carrega a caixa de quem foi visto — sem ela quem revisa recebe
            # "há alguém fora de hora" sem onde olhar.
            if (obj.classe == "pessoa" and self._fora_de_horario
                    and self._firme(obj)
                    and self._pode_alertar(trilha, "fora_de_horario", t)):
                eventos.append(EventoAcao(
                    "fora_de_horario", ROTULO_ACAO["fora_de_horario"],
                    confianca=float(obj.confianca),
                    severidade=SEVERIDADE_ACAO["fora_de_horario"],
                    duracao_s=1, envolvidos=1,
                    bbox=obj.bbox, track_id=obj.track_id,
                ))

            # --- áreas proibidas: pessoa e veículo ---
            # Ponto no polígono, decidido num quadro só — ao contrário de
            # `invasao`, que precisa de duas posições para saber de que lado a
            # pessoa veio. Por isso estas duas funcionam no rodízio.
            for zona in zonas:
                if zona.get("tipo") != "POLIGONO":
                    continue
                regras = zona.get("regras") or {}
                pontos = zona.get("pontos") or []
                if not ponto_em_poligono((nx, ny), pontos):
                    continue
                nome_zona = zona.get("nome") or ""
                if obj.classe == "pessoa" and regras.get("restrita") \
                        and self._pode_alertar(trilha, "area_restrita", t):
                    eventos.append(EventoAcao(
                        "area_restrita", ROTULO_ACAO["area_restrita"],
                        confianca=float(obj.confianca),
                        severidade=SEVERIDADE_ACAO["area_restrita"],
                        duracao_s=1, envolvidos=1, zona_nome=nome_zona,
                        bbox=obj.bbox, track_id=obj.track_id,
                    ))
                if obj.classe in CLASSES_VIA and regras.get("veiculo_proibido") \
                        and self._pode_alertar(trilha, "veiculo_proibido", t):
                    eventos.append(EventoAcao(
                        "veiculo_proibido", ROTULO_ACAO["veiculo_proibido"],
                        confianca=float(obj.confianca),
                        severidade=SEVERIDADE_ACAO["veiculo_proibido"],
                        duracao_s=1, envolvidos=1, zona_nome=nome_zona,
                        bbox=obj.bbox, track_id=obj.track_id,
                    ))

            # --- zonas: invasão e contrafluxo ---
            if anterior is not None:
                for zona in zonas:
                    pontos = zona.get("pontos") or []
                    regras = zona.get("regras") or {}
                    if zona.get("tipo") == "LINHA" and regras.get("invasao", True):
                        if cruzou_linha(anterior, (nx, ny), pontos) == "entrada" \
                                and self._pode_alertar(trilha, "invasao", t):
                            eventos.append(EventoAcao(
                                "invasao", ROTULO_ACAO["invasao"], confianca=0.93,
                                severidade=SEVERIDADE_ACAO["invasao"],
                                duracao_s=1, envolvidos=1,
                                bbox=obj.bbox, track_id=obj.track_id,
                            ))
                    elif zona.get("tipo") == "POLIGONO" and regras.get("contrafluxo"):
                        esperado = regras.get("direcao")  # ex.: [1, 0]
                        atual_dir = trilha.direcao()
                        if esperado and atual_dir and ponto_em_poligono((nx, ny), pontos):
                            if float(np.dot(atual_dir, esperado)) < -0.6 \
                                    and self._pode_alertar(trilha, "contrafluxo", t):
                                eventos.append(EventoAcao(
                                    "contrafluxo", ROTULO_ACAO["contrafluxo"], confianca=0.79,
                                    severidade=SEVERIDADE_ACAO["contrafluxo"],
                                    duracao_s=int(t - trilha.primeiro_em), envolvidos=1,
                                    bbox=obj.bbox, track_id=obj.track_id,
                                ))

        # --- aglomeração (nível de cena, não de indivíduo) ---
        # Checada aqui, e não em `_pode_alertar`: aglomeração é da cena
        # inteira e não tem trilha por onde passar o cooldown.
        if self.ativa("aglomeracao") and len(pessoas_xy) >= self.n_aglomeracao:
            maior = self._maior_agrupamento(pessoas_xy)
            if maior >= self.n_aglomeracao:
                if self._aglomeracao_em is None:
                    self._aglomeracao_em = t
                elif t - self._aglomeracao_em > 10.0:
                    eventos.append(EventoAcao(
                        "aglomeracao", ROTULO_ACAO["aglomeracao"],
                        confianca=float(min(0.6 + maior / 40.0, 0.96)),
                        severidade=SEVERIDADE_ACAO["aglomeracao"],
                        duracao_s=int(t - self._aglomeracao_em), envolvidos=maior,
                    ))
                    self._aglomeracao_em = t + self.cooldown_s
        else:
            self._aglomeracao_em = None

        if self.ativa("briga"):
            eventos += self._avaliar_briga(objetos, largura, altura, t)

        self._limpar(t)
        return eventos

    def _avaliar_briga(self, objetos, largura: int, altura: int,
                       t: float) -> List[EventoAcao]:
        """Duas pessoas coladas, ambas rápidas e ambas com movimento errático.

        Não há classe de "briga" em modelo nenhum deste sistema, e o sistema
        não conclui intenção a partir de vídeo — o que esta heurística aponta
        é um **indício para triagem humana**, pelos três sinais que uma luta
        corporal deixa na trajetória e que, juntos, nenhuma cena comum produz:

        * **proximidade** — as caixas se tocam ou quase. Duas pessoas
          conversando ficam próximas, não sobrepostas;
        * **velocidade** nas duas ao mesmo tempo. Uma pessoa correndo ao lado
          de outra parada não é briga;
        * **agitação** nas duas. É o que separa de corrida lado a lado:
          correr é dirigido, lutar oscila.

        Exigir os três no mesmo par, e por alguns segundos, é o que torna o
        indício utilizável. Ainda assim erra: abraço efusivo e brincadeira
        entram. Por isso a confiança não passa de 0,8 — o registro diz
        "parece", não "é".

        Depende da rajada de quadros: sem trajetória densa, `velocidade` e
        `agitacao` devolvem zero e nada dispara.
        """
        pessoas = [o for o in objetos
                   if o.classe == "pessoa" and o.track_id is not None]
        if len(pessoas) < 2:
            self._briga_desde.clear()
            return []

        eventos: List[EventoAcao] = []
        vistos = set()
        for i, a in enumerate(pessoas):
            ta = self.trilhas.get(a.track_id)
            # `caminho`, e não `velocidade`: quem luta volta ao mesmo lugar, e
            # o deslocamento líquido de uma luta corporal é perto de zero.
            if ta is None or ta.caminho() < self.vel_briga \
                    or ta.agitacao() < self.agitacao_briga:
                continue
            for b in pessoas[i + 1:]:
                tb = self.trilhas.get(b.track_id)
                if tb is None or tb.caminho() < self.vel_briga \
                        or tb.agitacao() < self.agitacao_briga:
                    continue
                if ios(a.bbox, b.bbox) < self.proximidade_briga:
                    continue

                par = tuple(sorted((a.track_id, b.track_id)))
                vistos.add(par)
                desde = self._briga_desde.setdefault(par, t)
                # Persistência derruba o encontrão de corredor: um esbarrão
                # dura um quadro, uma luta dura.
                if t - desde < self.duracao_briga:
                    continue
                if not self._pode_alertar(ta, "briga", t):
                    continue
                self._pode_alertar(tb, "briga", t)
                x1 = min(a.bbox[0], b.bbox[0]); y1 = min(a.bbox[1], b.bbox[1])
                x2 = max(a.bbox[2], b.bbox[2]); y2 = max(a.bbox[3], b.bbox[3])
                eventos.append(EventoAcao(
                    "briga", ROTULO_ACAO["briga"],
                    confianca=float(min(0.8, 0.5 + (ta.agitacao() + tb.agitacao()) / 4)),
                    severidade=SEVERIDADE_ACAO["briga"],
                    duracao_s=int(t - desde), envolvidos=2,
                    bbox=(x1, y1, x2, y2), track_id=a.track_id,
                ))
                self._briga_desde[par] = t + self.cooldown_s

        # Par que se separou perde o relógio: voltar a se aproximar recomeça
        # a contagem, em vez de somar com o encontrão de dois minutos atrás.
        for par in [p for p in self._briga_desde if p not in vistos]:
            self._briga_desde.pop(par, None)
        return eventos

    def _maior_agrupamento(self, pontos: Sequence[Tuple[float, float]]) -> int:
        """Maior componente conexa sob o raio de proximidade (união-busca leve)."""
        n = len(pontos)
        pai = list(range(n))

        def achar(i: int) -> int:
            while pai[i] != i:
                pai[i] = pai[pai[i]]
                i = pai[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                if np.hypot(pontos[i][0] - pontos[j][0], pontos[i][1] - pontos[j][1]) <= self.raio_aglomeracao:
                    ri, rj = achar(i), achar(j)
                    if ri != rj:
                        pai[ri] = rj

        tamanhos: Dict[int, int] = defaultdict(int)
        for i in range(n):
            tamanhos[achar(i)] += 1
        return max(tamanhos.values()) if tamanhos else 0


def severidade_objeto(classe: str) -> int:
    return SEVERIDADE_OBJETO.get(classe, 1)

