"""Testes da caixa que o operador vê no vídeo.

    pytest -q tests/test_deteccao.py

Duas regras, e são opostas — é por isso que têm teste próprio. **Ninguém em
cena pode ficar sem caixa**, e **ninguém pode ganhar duas**. Afrouxar a
primeira devolve a sala com gente não marcada; apertar a segunda apaga uma
pessoa de verdade, que é o pior dos dois erros.

As coordenadas abaixo não são inventadas: saíram dos quadros reais da
Produção 02, da Produção 03 e do Service Desk desta instalação, medidos com
os pesos e o limiar que as câmeras têm em produção.
"""
from __future__ import annotations

from app.services.vision import (
    ObjetoDetectado, _ladrilhos, _na_emenda, fundir_sobrepostas, ios, iou,
    mesma_pessoa, razao_area,
)


def pessoa(bbox, conf, tid=None) -> ObjetoDetectado:
    return ObjetoDetectado("pessoa", conf, bbox, tid)


# ----------------------------------------------------------------------
# Uma caixa por pessoa
# ----------------------------------------------------------------------
def test_caixa_contida_na_outra_e_a_mesma_pessoa():
    """O caso do Service Desk: tronco marcado dentro do corpo inteiro.

    As duas caixas rendem IoU 0,33 — passam folgadas pelo NMS do YOLO, que só
    compara união. É a contenção que as denuncia: a menor está inteira dentro
    da maior.
    """
    corpo = pessoa((933, 350, 1204, 522), 0.788)
    tronco = pessoa((1108, 350, 1202, 515), 0.180)

    assert iou(corpo.bbox, tronco.bbox) < 0.55, "o NMS por IoU não pegaria"
    assert ios(corpo.bbox, tronco.bbox) >= 0.80

    ficam = fundir_sobrepostas([tronco, corpo])
    assert [o.bbox for o in ficam] == [corpo.bbox]


def test_duas_pessoas_encostadas_continuam_duas():
    """Produção 02: o homem em pé à frente da mulher sentada.

    É o par de pessoas *distintas* que mais se sobrepõe em todo o parque —
    0,59 de contenção. O limite da fusão existe para ficar acima disto.
    """
    em_pe = pessoa((748, 316, 920, 673), 0.85)
    sentada = pessoa((699, 450, 860, 717), 0.90)

    assert ios(em_pe.bbox, sentada.bbox) < 0.80
    assert len(fundir_sobrepostas([em_pe, sentada])) == 2


def test_pessoa_dentro_da_caixa_de_um_carro_nao_some():
    """Pessoa em pé ao lado do carro, na Portaria.

    A caixa do carro engole a dela por inteiro. Fundir entre classes apagaria
    justamente quem o operador precisa ver — e sobraria o carro.
    """
    carro = ObjetoDetectado("carro", 0.91, (0, 380, 300, 640))
    gente = pessoa((120, 400, 180, 600), 0.42)

    assert ios(carro.bbox, gente.bbox) == 1.0
    classes = sorted(o.classe for o in fundir_sobrepostas([carro, gente]))
    assert classes == ["carro", "pessoa"]


def test_trilha_sobrevive_a_caixa_que_a_trouxe():
    """A varredura não rastreia, e pode vencer a passada que rastreia.

    Sem herdar o track_id, o objeto perderia a identidade entre quadros — e
    com ela a análise de ação, que ignora objeto sem trilha.
    """
    rastreada = pessoa((100, 100, 200, 400), 0.40, tid=7)
    varrida = pessoa((98, 98, 204, 404), 0.80)

    ficam = fundir_sobrepostas([rastreada, varrida])
    assert len(ficam) == 1
    assert ficam[0].confianca == 0.80
    assert ficam[0].track_id == 7


# Os três casos abaixo têm as coordenadas que o detector devolveu em capturas
# ao vivo do parque. Nenhum deles cai pela contenção sozinha — foi medindo
# estes que a regra deixou de ser um limiar só. Ver `vision.mesma_pessoa`.
def test_mesma_pessoa_lida_por_duas_passadas():
    """Service Desk: a passada inteira e a varredura marcaram a mesma mulher.

    Contenção 0,69 — abaixo do limite de "uma dentro da outra" — mas as duas
    caixas têm 89% do mesmo tamanho no mesmo lugar. Duas pessoas encostadas
    não se parecem tanto assim.
    """
    a, b = (560, 590, 754, 718), (620, 590, 835, 720)
    assert ios(a, b) < 0.80 and razao_area(a, b) >= 0.80
    assert mesma_pessoa(a, b)


def test_mesma_pessoa_com_contencao_baixa():
    """Produção 01: contenção 0,52, mais baixa que a de duas pessoas de
    verdade (0,58) — só o tamanho idêntico (0,90) denuncia a duplicata."""
    a, b = (9, 546, 113, 671), (0, 494, 97, 615)
    assert ios(a, b) < 0.58, "mais baixa que o pior par de pessoas distintas"
    assert mesma_pessoa(a, b)


def test_pedaco_de_alguem_nao_e_uma_segunda_pessoa():
    """Entrada: uma mão sobre o fone, funda dentro da caixa de quem a usa.

    O IoU é quase nulo (0,09) porque a caixa é minúscula; o que a identifica
    é estar dentro de alguém *e* ter 15% do tamanho dela.
    """
    pessoa_, pedaco = (1068, 550, 1279, 710), (1044, 588, 1109, 664)
    assert iou(pessoa_, pedaco) < 0.10 and razao_area(pessoa_, pedaco) <= 0.25
    assert mesma_pessoa(pessoa_, pedaco)


def test_duas_pessoas_de_tamanho_parecido_ficam_as_duas():
    """O caso deixado de fora de propósito.

    Sobreposição média entre caixas de tamanho comparável são duas pessoas
    encostadas. Esta é a linha que separa marcar uma caixa a mais de apagar
    gente do vídeo — e ela fica do lado de manter.
    """
    a, b = (434, 274, 530, 479), (382, 365, 488, 534)
    assert 0.30 < ios(a, b) < 0.50
    assert not mesma_pessoa(a, b)


def test_fusao_nao_inventa_nem_perde_quem_nao_se_toca():
    distantes = [pessoa((0, 0, 50, 150), 0.9), pessoa((400, 300, 460, 500), 0.4),
                 pessoa((900, 100, 980, 390), 0.3)]
    assert len(fundir_sobrepostas(distantes)) == 3


# ----------------------------------------------------------------------
# Varredura em ladrilhos
# ----------------------------------------------------------------------
def test_ladrilhos_cobrem_o_quadro_inteiro():
    """Sobra de divisão inteira deixava uma faixa fora de todos os recortes."""
    recortes = _ladrilhos(1280, 720, 2, 2, 0.25)
    assert min(x1 for x1, _, _, _ in recortes) == 0
    assert min(y1 for _, y1, _, _ in recortes) == 0
    assert max(x2 for _, _, x2, _ in recortes) == 1280
    assert max(y2 for _, _, _, y2 in recortes) == 720


def test_ladrilhos_se_emendam():
    """Sem emenda, quem cai na divisa sai partido nos dois e some nos dois."""
    recortes = _ladrilhos(1280, 720, 2, 2, 0.25)
    esquerdo, direito = recortes[0], recortes[1]
    assert direito[0] < esquerdo[2], "os recortes precisam se sobrepor"


def test_corpo_cortado_pela_divisa_e_descartado():
    """A base na emenda é corpo cortado, não leitura nova.

    Eram as três duplicatas que sobravam na Produção 02 depois da fusão:
    todas com a base no mesmo y da divisa entre ladrilhos.
    """
    ladrilho = (0, 0, 731, 420)
    cortado = (124, 293, 252, 419)        # encosta na divisa de baixo
    assert _na_emenda(cortado, ladrilho, 1277, 736)


def test_corpo_na_borda_do_quadro_nao_e_descartado():
    """Quem encosta na borda do *quadro* está cortado pela câmera, não pelo
    recorte — e é a única leitura que haverá dele."""
    ladrilho = (546, 316, 1277, 736)
    encostado = (1100, 400, 1277, 735)    # borda direita e de baixo do quadro
    assert not _na_emenda(encostado, ladrilho, 1277, 736)


def test_ladrilho_unico_nao_tem_emenda():
    """Grade 1x1: todas as bordas são as do quadro."""
    assert _ladrilhos(1280, 720, 1, 1, 0.25) == [(0, 0, 1280, 720)]
    assert not _na_emenda((0, 0, 1280, 720), (0, 0, 1280, 720), 1280, 720)


# ----------------------------------------------------------------------
# Quando varrer: o quadro sem achado é onde a pessoa pequena se esconde
# ----------------------------------------------------------------------
class _CaixaFalsa:
    def __init__(self, indice, confianca, xyxy):
        self.cls, self.conf, self.xyxy = [indice], [confianca], [xyxy]
        self.id = None


class _SaidaFalsa:
    def __init__(self, nomes, caixas):
        self.names, self.boxes = nomes, caixas


class _ModeloFalso:
    """YOLO que só enxerga o corpo quando ele chega ampliado no ladrilho.

    É o caso que a varredura existe para resolver, e aqui ele é explícito: a
    passada do quadro inteiro devolve lista vazia, e o mesmo corpo aparece
    quando o pedaço é ampliado. Sem isso, o teste mediria a sorte do modelo.
    """

    names = {0: "person"}

    def __init__(self):
        self.inteiras = self.ladrilhos = 0

    def __call__(self, imagem, conf=0.0, imgsz=None, iou=None,
                 classes=None, verbose=False):
        # A varredura é a única passada que restringe as classes.
        if classes is None:
            self.inteiras += 1
            return [_SaidaFalsa(self.names, [])]
        self.ladrilhos += 1
        corpo = [_CaixaFalsa(0, 0.31, (300, 150, 340, 260))]
        return [_SaidaFalsa(self.names, corpo if self.ladrilhos == 1 else [])]


def _parque_de_mentira(monkeypatch, modo):
    import numpy as np

    from app.services import vision

    monkeypatch.setattr(vision.settings, "OBJECT_VARREDURA", modo)
    monkeypatch.setattr(vision, "_idx_varredura", None)   # cache por processo
    modelo = _ModeloFalso()
    monkeypatch.setattr(vision, "obter_yolo", lambda: modelo)
    quadro = np.zeros((720, 1280, 3), dtype=np.uint8)
    achados = vision.detectar_objetos(quadro, 0.25, rastrear=False)
    return modelo, achados


def test_quadro_sem_achado_na_passada_inteira_ainda_e_varrido(monkeypatch):
    """A câmera cujo único ocupante é pequeno demais para a passada inteira.

    Era o que aparecia como "tem câmera que não mostra bounding box": sem
    achado na passada inteira a varredura não rodava, e sem varredura o corpo
    distante não era achado — a sala com gente ficava sem caixa nenhuma.
    """
    modelo, achados = _parque_de_mentira(monkeypatch, "sempre")

    assert modelo.ladrilhos == 4, "o quadro vazio também precisa ser varrido"
    assert [o.classe for o in achados] == ["pessoa"]
    assert achados[0].bbox == (300, 150, 340, 260)


def test_varredura_so_do_quadro_ocupado_perde_quem_esta_sozinho(monkeypatch):
    """O atalho que o modo "ocupado" faz, e por que ele não serve para pessoa.

    Decidir *se vale procurar mais* pelo que a passada inteira achou é usar a
    ausência de achado como prova de que não há ninguém — e o quadro sem
    achado é exatamente onde o corpo pequeno se esconde. Fica como teste para
    que a troca de volta apareça como uma pessoa perdida, não como economia.
    """
    modelo, achados = _parque_de_mentira(monkeypatch, "ocupado")

    assert modelo.ladrilhos == 0
    assert achados == [], "é a pessoa que o modo 'sempre' recupera"


def test_parque_varre_sempre():
    """A escolha de 07/10, fixada: para pessoa, cobertura vence velocidade."""
    from app.config import settings

    assert settings.OBJECT_VARREDURA == "sempre"


# ----------------------------------------------------------------------
# Um nome por pessoa, e só onde há nome
# ----------------------------------------------------------------------
def _rotular(nome, no_corpo):
    from app.services.inferencia import Inferencia

    caixa_rosto, nomes = {"classe": "rosto"}, []
    Inferencia._rotular(caixa_rosto, nome, no_corpo, nomes)
    return caixa_rosto, nomes


def test_nome_fica_na_caixa_do_corpo_e_nao_tambem_na_do_rosto():
    """O nome repetido do Service Desk: duas tarjas iguais, empilhadas.

    Rosto e corpo da mesma pessoa são duas caixas, e cada uma escrevia o
    nome. A do corpo é a que se lê no mosaico — a do rosto tem uns sessenta
    pixels —, então é ela que fica com a tarja.
    """
    caixa_rosto, nomes = _rotular("ANDRÉ LUIZ CARDOSO", no_corpo=True)

    assert "pessoa_nome" not in caixa_rosto, "a do corpo já leva o nome"
    assert nomes == ["ANDRÉ LUIZ CARDOSO"], "quem está em cena segue contado"


def test_rosto_sem_corpo_achado_fica_com_o_nome():
    """Quando o detector de objeto não achou o corpo, o rosto é tudo o que há
    dela em cena — sem a tarja aqui, a identificação sumiria da tela."""
    caixa_rosto, nomes = _rotular("VITOR GABRIEL FARIAS", no_corpo=False)

    assert caixa_rosto["pessoa_nome"] == "VITOR GABRIEL FARIAS"
    assert nomes == ["VITOR GABRIEL FARIAS"]


def test_registro_sem_nome_nao_escreve_nada_no_video():
    """O BIO-N da Produção 03, que saía rotulado "NÃO IDENTIFICADO".

    O literal vinha do campo `nome` do registro provisório e era desenhado na
    mesma tarja laranja de quem tem nome — quem opera lê isso como falha do
    reconhecimento. Sem nome, a caixa volta a ser a de qualquer um em cena.
    """
    for no_corpo in (True, False):
        caixa_rosto, nomes = _rotular("", no_corpo=no_corpo)
        assert "pessoa_nome" not in caixa_rosto
        assert nomes == [], "não há nome para o alerta de sala anunciar"


# ----------------------------------------------------------------------
# Classes de objeto: o que o filtro oferece tem de existir de verdade
# ----------------------------------------------------------------------
def test_toda_classe_do_filtro_tem_origem_declarada():
    """O caso real: o filtro oferecia 12 classes e o detector emitia 4.

    Quem escolhia "Capacete" ou "Animal" recebia lista vazia para sempre, sem
    nada dizer que aquilo nunca ia aparecer. Cada classe oferecida precisa
    sair de algum lugar: do mapa do COCO, de um detector extra, ou de um campo
    da própria detecção — e este teste é o que impede uma opção órfã de voltar
    à tela.
    """
    from app.services.vision import MAPA_CLASSES, SEVERIDADE_OBJETO

    do_modelo = set(MAPA_CLASSES.values())
    de_detector_extra = {"capacete", "arma"}
    de_campo = {"placa"}                       # lida sobre o veículo detectado

    oferecidas = set(SEVERIDADE_OBJETO) - {"veiculo"}   # 'veiculo' é histórico
    orfas = oferecidas - do_modelo - de_detector_extra - de_campo
    assert not orfas, f"classe sem origem: {sorted(orfas)}"


def test_placa_e_campo_da_deteccao_e_nao_classe():
    """Placa não é objeto detectado: é leitura feita sobre o veículo.

    Comparada contra `Deteccao.classe`, a opção do filtro nunca devolvia nada
    — mesmo havendo placas lidas e gravadas no banco.
    """
    from app.services.vision import MAPA_CLASSES
    assert "placa" not in MAPA_CLASSES.values(), (
        "se virar classe do modelo, o filtro passa a ter dois significados")


def test_detector_extra_ausente_nao_derruba_a_inferencia():
    """Sem modelo configurado o sistema não pode quebrar nem travar.

    Capacete e arma de fogo dependem de um modelo que não vem embarcado. A
    ausência dele é o caso normal, não a exceção.
    """
    import numpy as np
    from app.services import extras

    assert extras.configurado("capacete") is False
    assert extras.configurado("arma") is False
    assert set(extras.disponiveis()) == {"capacete", "arma"}

    quadro = np.zeros((120, 160, 3), dtype=np.uint8)
    assert extras.detectar(quadro, ["capacete", "arma"]) == []
    # E de novo: a segunda chamada não pode tentar carregar outra vez.
    assert extras.detectar(quadro, ["capacete", "arma"]) == []


def test_camera_so_recebe_as_classes_que_pediu():
    """A lista do que o modelo vê é do parque; o que se registra é da câmera."""
    from app.services.inferencia import _classes_barradas
    from app.services.vision import CLASSES_ANIMAL, CLASSES_BAGAGEM, CLASSES_VIA

    class Cam:
        detecta_veiculo = False
        detecta_bagagem = False
        detecta_animal = False

    barradas = _classes_barradas(Cam())
    assert CLASSES_VIA <= barradas
    assert CLASSES_BAGAGEM <= barradas
    assert CLASSES_ANIMAL <= barradas
    assert "pessoa" not in barradas, "pessoa nunca é barrada pelo lugar"

    class Portaria(Cam):
        detecta_veiculo = True

    livres = _classes_barradas(Portaria())
    assert not (CLASSES_VIA & livres)
    # Liberar veículo não libera bagagem nem animal.
    assert CLASSES_BAGAGEM <= livres and CLASSES_ANIMAL <= livres


# ----------------------------------------------------------------------
# Fogo e fumaça por assinatura de imagem
# ----------------------------------------------------------------------
def _cena_com_textura(w=640, h=480):
    """Fundo com detalhe, como uma parede real — fumaça precisa ter o que
    encobrir, e sem textura o teste passaria por engano."""
    import cv2
    import numpy as np
    rng = np.random.default_rng(7)
    q = rng.integers(60, 110, (h, w, 3), dtype=np.uint8)
    for i in range(0, w, 40):
        cv2.line(q, (i, 0), (i, h), (90, 95, 100), 2)
    return q


def _mancha_irregular(base, cx, cy, raio, semente):
    """Chama: laranja de borda irregular, e a borda muda com a semente."""
    import cv2
    import numpy as np
    q = base.copy()
    r = np.random.default_rng(semente)
    pts = [[int(cx + raio * (0.65 + 0.5 * r.random()) * np.cos(2 * np.pi * k / 14)),
            int(cy + raio * (0.65 + 0.5 * r.random()) * np.sin(2 * np.pi * k / 14))]
           for k in range(14)]
    cv2.fillPoly(q, [np.array(pts, np.int32)], (40, 150, 245))
    return q


def test_chama_que_oscila_e_detectada():
    from app.services import chama
    seq = [_mancha_irregular(_cena_com_textura(), 320, 240, 44, s) for s in (1, 2, 3, 4)]
    achados = [c.classe for c in chama.analisar(seq)]
    assert "fogo" in achados


def test_objeto_laranja_parado_nao_e_fogo():
    """A armadilha que derruba todo detector por cor.

    Cone de sinalização, luminária quente e camisa vermelha passam em qualquer
    filtro de matiz. O que separa chama de objeto laranja é a cintilação: a
    borda da chama muda várias vezes por segundo, a do cone não muda nunca.
    Sem este teste, o primeiro cone do pátio viraria alarme de incêndio.
    """
    import cv2
    from app.services import chama

    base = _cena_com_textura()
    cv2.circle(base, (320, 240), 44, (40, 150, 245), -1)
    parado = [base.copy() for _ in range(4)]
    assert [c.classe for c in chama.analisar(parado)] == []


def test_mao_em_movimento_e_indistinguivel_de_fumaca():
    """A medição que desligou a detecção de fumaça, em 16/10/2026.

    Dois falsos positivos em vídeo real, na Produção: "fumaça" a 68% e 72%
    sobre **uma mão passando por cima de papel**. A mão borra ao se mover, a
    região perde textura, e perda de textura é o sinal que acusa fumaça.

    Este teste não conserta nada — ele guarda o número que mostra por que não
    há conserto por limiar: a mão rende uma região *maior* que a fumaça de
    verdade. Qualquer piso de tamanho que deixasse a fumaça passar deixaria a
    mão passar antes.

    Se algum dia entrar um modelo treinado por `extras.py`, este teste diz
    contra o que ele precisa ser medido.
    """
    import cv2
    import numpy as np
    from app.config import settings
    from app.services import chama

    W, H = 1280, 720
    base = np.full((H, W, 3), 190, np.uint8)
    base[:int(H * 0.35)] = (120, 110, 105)
    for i in range(14):                       # papéis sobre a mesa
        x = int(W * 0.25) + i * 42
        cv2.rectangle(base, (x, int(H * 0.55)), (x + 30, int(H * 0.95)), (205, 205, 200), -1)
        cv2.rectangle(base, (x, int(H * 0.55)), (x + 30, int(H * 0.95)), (150, 150, 148), 2)

    def regiao(seq):
        """Maior caixa que a máscara de perda de textura produz, em % do quadro."""
        cinza = chama.mascara_fumaca(seq[-1])
        t0 = chama._mapa_textura(cv2, seq[0])
        t1 = chama._mapa_textura(cv2, seq[-1])
        m = (((t0 >= settings.FUMACA_TEXTURA_LOCAL_MIN)
              & ((t0 - t1) >= (t0 * settings.FUMACA_PERDA_MIN))
              & (cinza > 0)).astype(np.uint8)) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        regs = chama._regioes(m, settings.FUMACA_AREA_MIN)
        maior = max(((x2 - x1) * (y2 - y1) for (x1, y1, x2, y2), _ in regs), default=0)
        return maior / (W * H)

    mao = [base.copy()]
    for k in (1, 2, 3):
        q = base.copy()
        cx, cy = int(W * 0.52) + k * 34, int(H * 0.80)
        cv2.ellipse(q, (cx, cy), (95, 52), 18, 0, 360, (150, 150, 152), -1)
        a, b, c, d = max(0, cx - 140), max(0, cy - 90), min(W, cx + 140), min(H, cy + 90)
        q[b:d, a:c] = cv2.GaussianBlur(q[b:d, a:c], (0, 0), 2.5 * k)
        mao.append(q)

    fumaca = [base.copy()]
    reg = (slice(int(H * 0.10), int(H * 0.62)), slice(int(W * 0.04), int(W * 0.42)))
    for k in (1, 2, 3):
        f = base.copy()
        alpha = 0.3 * k
        f[reg] = (f[reg] * (1 - alpha) + np.full_like(f[reg], 150) * alpha).astype(np.uint8)
        f[reg] = cv2.GaussianBlur(f[reg], (0, 0), 5 * k)
        fumaca.append(f)

    assert regiao(mao) > regiao(fumaca), (
        "a mão em movimento rende região maior que a fumaça — é por isso que "
        "nenhum limiar de tamanho separa os dois")


def test_fumaca_esta_declarada_indisponivel():
    """A tela não pode oferecer o que não se sustenta.

    O detector segue no código para quando houver modelo treinado; o que não
    pode é a câmera ligar fumaça e o operador contar com ela.
    """
    from app.services.vision import FICHA_ACAO

    ficha = FICHA_ACAO["fumaca"]
    assert not ficha.disponivel
    assert "mão em movimento" in ficha.indisponivel
    # Fogo tem base própria e continua valendo.
    assert FICHA_ACAO["fogo"].disponivel


def test_parede_cinza_lisa_nao_e_fumaca():
    """Metade de qualquer cena interna é cinza de baixa saturação. Se o cinza
    bastasse, toda câmera de corredor alarmaria incêndio no primeiro quadro."""
    import numpy as np
    from app.services import chama

    parede = np.full((480, 640, 3), 150, np.uint8)
    assert [c.classe for c in chama.analisar([parede.copy() for _ in range(4)])] == []


def test_sequencia_curta_demais_nao_decide():
    """Com dois quadros, uma porta abrindo é indistinguível de uma chama."""
    from app.services import chama
    seq = [_mancha_irregular(_cena_com_textura(), 320, 240, 44, s) for s in (1, 2)]
    assert chama.analisar(seq) == []


def test_varredura_barata_filtra_antes_de_pedir_quadros():
    """A decisão custa quatro leituras de câmera; a triagem custa uma matriz.

    Sem a triagem, toda câmera que vigia fogo pagaria as quatro leituras por
    visita para quase sempre concluir que não há nada laranja em cena.
    """
    import numpy as np
    from app.services import chama

    assert chama.ha_candidato(_mancha_irregular(_cena_com_textura(), 320, 240, 60, 1))
    assert not chama.ha_candidato(np.full((480, 640, 3), 90, np.uint8))


def test_carimbo_de_data_e_hora_nao_e_fumaca():
    """O falso positivo real: AR-2026000708, "fumaça" a 71% na sala de Analistas.

    O recorte era uma parede cinza no canto superior direito — e dentro dele, o
    carimbo de data e hora que a própria câmera sobrepõe ao vídeo. Os dígitos
    trocam a cada segundo, a textura local muda junto, e "textura que some" é
    exatamente o sinal que acusa fumaça.

    A caixa daquele registro ia de x 0,95 a 1,00 e y 0,05 a 0,18: cabe inteira
    na faixa de borda. Fumaça que importa não vive num cantinho — ela cresce e
    toma o meio do quadro.
    """
    import cv2
    import numpy as np
    from app.services import chama

    base = _cena_com_textura(640, 480)
    seq = [base.copy()]
    # Canto superior direito, como o carimbo: cinza que perde detalhe.
    reg = (slice(20, 90), slice(590, 638))
    for k in (1, 2, 3):
        f = base.copy()
        alpha = 0.3 * k
        f[reg] = (f[reg] * (1 - alpha) + np.full_like(f[reg], 150) * alpha).astype(np.uint8)
        f[reg] = cv2.GaussianBlur(f[reg], (0, 0), 4 * k)
        seq.append(f)

    assert [c.classe for c in chama.analisar(seq)] == [], (
        "região colada na borda é carimbo da câmera, não fumaça")


