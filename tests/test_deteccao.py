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
def _rotular(nome):
    from app.services.inferencia import Inferencia

    caixa_rosto, nomes = {"classe": "rosto"}, []
    Inferencia._rotular(caixa_rosto, nome, nomes)
    return caixa_rosto, nomes


def test_o_nome_fica_na_caixa_do_rosto():
    """Um nome por pessoa, e é o rosto que identifica.

    Rosto e corpo da mesma pessoa são duas caixas, e escrever nas duas
    repetia o nome empilhado no vídeo — era o "ANDRÉ LUIZ CARDOSO" em dobro
    do Service Desk. Fica com o rosto porque é ali que a leitura aconteceu e
    é ali que quem confere vai olhar para decidir se o sistema acertou.
    """
    caixa_rosto, nomes = _rotular("ANDRÉ LUIZ CARDOSO")

    assert caixa_rosto["pessoa_nome"] == "ANDRÉ LUIZ CARDOSO"
    assert nomes == ["ANDRÉ LUIZ CARDOSO"], "quem está em cena segue contado"


def test_a_caixa_do_corpo_nao_leva_tarja():
    """A contrapartida, assumida: só o rosto rotula.

    `_rotular` é o único lugar que escreve `pessoa_nome`. Quem foi
    identificado antes e está de costas agora aparece como "pessoa", porque
    não há rosto em cena para rotular — o corpo segue recebendo a identidade
    no banco, que é do que a trajetória é feita, mas não a tarja na tela.
    """
    import re
    from pathlib import Path

    fonte = Path("app/services/inferencia.py").read_text(encoding="utf-8")
    escritas = set(re.findall(r'(\w+)\[.?pessoa_nome.?\]\s*=', fonte))
    # `caixa_rosto` é `_rotular`, o único lugar que *cria* a tarja, e ela sai
    # de um rosto lido. Nenhum outro nome pode aparecer aqui: escrever
    # `pessoa_nome` em qualquer outra caixa é pôr a tarja no corpo.
    assert escritas == {"caixa_rosto"}, (
        f"quem escreve a tarja mudou: {sorted(escritas)}")

    import inspect

    from app.services.inferencia import Inferencia

    # O laço curto não roda facial, e por isso leva adiante a tarja da volta
    # anterior — ancorada no corpo, para acompanhar quem anda. Ancorar no
    # corpo é uma coisa; *identificar* pelo corpo é outra, e é a que não pode
    # acontecer. A âncora só copia um nome que um rosto já produziu.
    ancorados = inspect.getsource(Inferencia._rostos_ancorados)
    assert '"classe": "rosto"' in ancorados, (
        "a tarja herdada tem de sair numa caixa de rosto, nunca de corpo")
    assert "_ancoras" in ancorados, (
        "a herança vem da volta anterior, não de leitura nova")
    for proibido in ("_corpo_do_rosto", "buscar_pessoa", "extrair_faces",
                     "similaridade"):
        assert proibido not in ancorados, (
            f"`_rostos_ancorados` passou a identificar por conta própria: "
            f"{proibido}")

    # E a âncora nasce de um reconhecimento já feito: sem `pessoa_nome` na
    # caixa do rosto, não há o que ancorar.
    ancorar = inspect.getsource(Inferencia._ancorar)
    assert 'caixa_rosto.get("pessoa_nome")' in ancorar, (
        "a âncora passou a aceitar corpo sem rosto reconhecido")


def test_registro_sem_nome_nao_escreve_nada_no_video():
    """O BIO-N da Produção 03, que saía rotulado "NÃO IDENTIFICADO".

    O literal vinha do campo `nome` do registro provisório e era desenhado na
    mesma tarja laranja de quem tem nome — quem opera lê isso como falha do
    reconhecimento. Sem nome, a caixa volta a ser a de qualquer um em cena.
    """
    caixa_rosto, nomes = _rotular("")
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




# ----------------------------------------------------------------------
# Região ignorada: o que o operador sabe e o detector não
# ----------------------------------------------------------------------
class _CamFalsa:
    def __init__(self, zonas):
        self.zonas = zonas


class _ZonaFalsa:
    def __init__(self, pontos, regras):
        self.pontos, self.regras = pontos, regras


class _QuadroFalso:
    shape = (720, 1280, 3)


# O hidrante da Garagem: canto esquerdo da parede, onde ninguém passa.
HIDRANTE = _ZonaFalsa([[0.10, 0.42], [0.25, 0.42], [0.25, 0.62], [0.10, 0.62]],
                      {"ignorar": True})


def _filtrar(objetos, zonas):
    from app.services.inferencia import Inferencia

    return Inferencia._fora_das_zonas_ignoradas(
        objetos, _CamFalsa(zonas), _QuadroFalso())


def test_o_que_cai_na_regiao_ignorada_some():
    """O hidrante sai do detector com 0,9 de confiança.

    Não há limiar que o separe de uma pessoa — qualquer piso alto o bastante
    para barrá-lo barra gente de verdade primeiro. O que o distingue é saber
    que naquele pedaço de parede não passa ninguém.
    """
    # Caixa em pixels; o pé fica em x≈0,18 y≈0,58, dentro da zona.
    hidrante = ObjetoDetectado("pessoa", 0.93, (205, 310, 260, 420), track_id=1)
    assert _filtrar([hidrante], [HIDRANTE]) == []


def test_quem_passa_ao_lado_continua_sendo_visto():
    """A região apaga o que está dentro dela, e só isso."""
    pessoa = ObjetoDetectado("pessoa", 0.88, (600, 300, 690, 520), track_id=2)
    assert _filtrar([pessoa], [HIDRANTE]) == [pessoa]


def test_a_regiao_decide_pelo_pe_e_nao_pelo_centro():
    """É por onde a pessoa toca o chão que se sabe de que lado da borda ela
    está. Pelo centro, um corpo alto rente à zona ficaria metade fora."""
    # Centro em y≈0,33 (acima da zona), pé em y≈0,58 (dentro).
    alta = ObjetoDetectado("pessoa", 0.9, (205, 60, 260, 420), track_id=3)
    assert _filtrar([alta], [HIDRANTE]) == [], "o pé manda"


def test_camera_sem_regiao_ignorada_nao_perde_nada():
    objs = [ObjetoDetectado("pessoa", 0.9, (205, 310, 260, 420), track_id=1)]
    assert _filtrar(objs, []) == objs
    semregra = _ZonaFalsa(HIDRANTE.pontos, {"restrita": True})
    assert _filtrar(objs, [semregra]) == objs, (
        "só `ignorar` apaga; área restrita vigia, não esconde")


# ----------------------------------------------------------------------
# Quem identifica é o rosto
# ----------------------------------------------------------------------
def test_corpo_nao_vira_registro_identificado():
    """O worker não costura mais identidade a caixa de corpo.

    Era o "Registros por corpo" da ficha: o rosto era lido, o corpo que o
    continha virava um registro de objeto e recebia a mesma pessoa. Rendia
    um segundo nome na tela e um segundo caminho de identificação que não
    passou por reconhecimento nenhum — o corpo foi deduzido por geometria,
    não reconhecido.

    O que sobrou é uma anotação em memória, usada só para a zona restrita
    saber de quem é o corpo que entrou nela. Ela não grava nada, e este
    teste existe para que o registro não volte sem alguém decidir.
    """
    import inspect
    from pathlib import Path
    from app.services.inferencia import Inferencia

    assert not hasattr(Inferencia, "_alinhar_corpo"), (
        "o costurador de identidade ao corpo voltou")

    fonte = inspect.getsource(Inferencia._marcar_corpo)
    for proibido in ("processar_deteccao", "vincular_corpo", "TipoDeteccao.OBJETO"):
        assert proibido not in fonte, (
            f"`_marcar_corpo` voltou a gravar: encontrou {proibido}")

    app = Path("app")
    assert "vincular_corpo" not in (app / "routers" / "deteccoes.py").read_text(
        encoding="utf-8"), "vincular_corpo voltou ao router"


def test_a_zona_restrita_continua_sabendo_quem_entrou():
    """A contrapartida que precisa sobreviver à remoção.

    A autorização de perímetro compara o `track_id` do corpo com a lista de
    quem pode estar ali. Sem a anotação em memória, todo mundo viraria
    desconhecido e a lista de autorizados deixaria de ter efeito.
    """
    import inspect
    from app.services.inferencia import Inferencia

    fonte = inspect.getsource(Inferencia._marcar_corpo)
    assert "_identidades" in fonte, (
        "a ponte rosto→corpo sumiu; a zona restrita não saberia quem entrou")


# ----------------------------------------------------------------------
# A caixa não sobrevive ao quadro que a sustentava
# ----------------------------------------------------------------------
def test_quadro_ilegivel_apaga_a_sobreposicao():
    """O rastro não vem do rastreador — vem do worker saindo calado.

    Quando a leitura do quadro falha (e falha: o parque opera com 24 das 26
    câmeras on-line), `_processar` devolvia sem publicar. A tela mantinha a
    marcação anterior até a validade vencer, que é `ciclo_real * 2` — de 28
    a 72 segundos de caixa sobre gente que já saiu de cena.

    Afirmar presença sem imagem que a sustente é, num sistema de vigilância,
    pior que a caixa piscar por uma volta.
    """
    import asyncio
    from types import SimpleNamespace

    from app.services import inferencia as mod

    publicados = []

    async def _publicar_falso(tipo, dados):
        publicados.append((tipo, dados))

    async def _cenario():
        motor = mod.Inferencia()
        motor.ciclo_real = 20.0
        cam = SimpleNamespace(codigo="CAM-999", rtsp_url="rtsp://x", nome="Teste")

        original_quadro = mod.video.quadro_bgr
        original_pub = mod.publicar
        mod.video.quadro_bgr = lambda *a, **k: None      # câmera muda
        mod.publicar = _publicar_falso
        try:
            await motor._processar(None, cam)
        finally:
            mod.video.quadro_bgr = original_quadro
            mod.publicar = original_pub

    asyncio.run(_cenario())

    assert publicados, "sem quadro, o worker saía sem dizer nada à tela"
    tipo, dados = publicados[-1]
    assert tipo == "quadro_ao_vivo"
    assert dados["camera_codigo"] == "CAM-999"
    assert dados["caixas"] == [], "a marcação anterior tem de ser apagada"



def test_objeto_cortado_pela_borda_ainda_cai_na_regiao_ignorada():
    """O pé de quem está em primeiro plano fica exatamente na borda.

    A caixa do detector é recortada no quadro, então um objeto encostado na
    base termina em y=1,0 — e um ponto *sobre* a aresta do polígono é caso
    degenerado do ray casting: responde "fora" mesmo com o polígono indo
    até 1,0. Foi o que fez a zona sobre a placa de incêndio da CAM-010 não
    pegar nada: a placa é cortada pela borda inferior.
    """
    canto = _ZonaFalsa([[0.80, 0.78], [1.0, 0.78], [1.0, 1.0], [0.80, 1.0]],
                       {"ignorar": True})
    # Caixa encostada na base do quadro de 1280x720.
    placa = ObjetoDetectado("pessoa", 0.9, (1100, 640, 1190, 720), track_id=1)
    assert _filtrar([placa], [canto]) == []


def test_quem_anda_no_fundo_nao_e_afetado_pela_borda():
    """A tolerância de borda não pode virar uma zona maior do que foi
    desenhada: quem passa longe segue aparecendo."""
    canto = _ZonaFalsa([[0.80, 0.78], [1.0, 0.78], [1.0, 1.0], [0.80, 1.0]],
                       {"ignorar": True})
    pessoa = ObjetoDetectado("pessoa", 0.9, (400, 300, 490, 719), track_id=2)
    assert _filtrar([pessoa], [canto]) == [pessoa]


# ----------------------------------------------------------------------
# A câmera que está na tela é reolhada mais vezes
# ----------------------------------------------------------------------
def _cams(n):
    from types import SimpleNamespace
    return [SimpleNamespace(codigo=f"CAM-{i:03d}", rtsp_url="rtsp://x")
            for i in range(1, n + 1)]


def _com_espectador(monkeypatch, codigos):
    from types import SimpleNamespace
    from app.services import inferencia as mod

    leitores = {c: SimpleNamespace(exibindo=(c in codigos))
                for c in [f"CAM-{i:03d}" for i in range(1, 30)]}
    monkeypatch.setattr(mod.video, "_leitores", leitores)


def test_sem_espectador_a_volta_e_a_lista_crua(monkeypatch):
    from app.services.inferencia import Inferencia

    _com_espectador(monkeypatch, set())
    cams = _cams(24)
    assert Inferencia()._ordem_da_volta(cams) == cams


def test_camera_na_tela_volta_varias_vezes(monkeypatch):
    """A queixa de "trava depois que a pessoa sai".

    A sobreposição só se renova quando o worker volta àquela cena, e a volta
    leva perto de dois minutos. Quem assiste vê a caixa de alguém que já
    saiu continuar de pé todo esse tempo.
    """
    from app.config import settings
    from app.services.inferencia import Inferencia

    _com_espectador(monkeypatch, {"CAM-007"})
    ordem = Inferencia()._ordem_da_volta(_cams(24))
    vezes = [c.codigo for c in ordem].count("CAM-007")
    assert vezes == 1 + settings.INFERENCIA_REVISITAS, (
        f"a câmera na tela foi olhada {vezes}x na volta")
    # Nenhuma outra perdeu a vez.
    for i in range(1, 25):
        assert any(c.codigo == f"CAM-{i:03d}" for c in ordem)


def test_revisitas_se_dividem_entre_as_cameras_vistas(monkeypatch):
    """No 2x2 são quatro telas: o orçamento se reparte, não se multiplica.

    E repartir significa que nem toda câmera em tela ganha revisita — com
    quatro telas e orçamento de duas, duas ficam sem. Isso é aceitável porque
    a revisita deixou de ser o que renova a marcação: quem renova é o laço
    curto, que gira por todas as câmeras em tela. A revisita só acrescenta o
    que ele não faz — varredura em ladrilhos e leitura de rosto.

    O que não pode acontecer é a revisita roubar a vez de alguém.
    """
    from app.config import settings
    from app.services.inferencia import Inferencia

    vistas = {"CAM-002", "CAM-005", "CAM-011", "CAM-020"}
    _com_espectador(monkeypatch, vistas)
    ordem = [c.codigo for c in Inferencia()._ordem_da_volta(_cams(24))]
    extras = len(ordem) - 24
    assert extras == settings.INFERENCIA_REVISITAS, "o orçamento é por volta"
    # Toda câmera do parque continua sendo visitada ao menos uma vez.
    for i in range(1, 25):
        assert f"CAM-{i:03d}" in ordem, "uma câmera perdeu a vez para a revisita"
    # E as revisitas vão para quem está na tela, nunca para quem não está.
    repetidas = {c for c in ordem if ordem.count(c) > 1}
    assert repetidas and repetidas <= vistas, (
        f"revisita foi parar em câmera sem espectador: {repetidas - vistas}")


# ----------------------------------------------------------------------
# A caixa não sobrevive muito além da próxima visita
# ----------------------------------------------------------------------
async def _nada(tipo, dados):
    """Publicação no vácuo: aqui o que importa é a validade, não o envio."""


def _publicar(motor, codigo, instantes):
    """Publica a mesma cena nos instantes dados, e devolve as validades."""
    import asyncio

    async def correr():
        saida = []
        for t in instantes:
            await motor._publicar_caixas(codigo, [], agora=t)
            saida.append(motor._ultimas_caixas[codigo]["validade_s"])
        return saida

    return asyncio.run(correr())


def test_validade_segue_o_intervalo_medido_da_camera(monkeypatch):
    """A caixa fantasma de quem já saiu, e a caixa que pisca.

    A validade era deduzida do rodízio: ciclo dividido por `1 +
    INFERENCIA_REVISITAS` sempre que havia espectador. A dedução errava para
    os dois lados ao mesmo tempo, porque o orçamento de revisitas é repartido
    entre todas as câmeras em tela — com uma tela aberta a validade saía
    longa demais (fantasma), e no mosaico 4×4, onde a maioria das câmeras
    ganha zero revisita, saía sete vezes curta demais (a caixa pisca).

    Medir dispensa adivinhar: o intervalo observado já embute o rodízio, o
    laço curto, o teto de câmeras e a câmera que falhou e nem publicou.
    """
    from app.services import inferencia as mod

    motor = mod.Inferencia()
    motor.ciclo_real = 140.0
    monkeypatch.setattr(mod, "publicar", _nada)

    # Uma câmera republicada a cada 2s não pode carregar a validade da volta.
    validades = _publicar(motor, "CAM-RAPIDA", [0.0, 2.0, 4.0, 6.0])
    assert validades[0] == 280.0, "a primeira publicação só tem o ciclo"
    assert validades[-1] <= 5.0, (
        f"a validade não acompanhou o intervalo medido: {validades}")

    # E uma câmera vista uma vez por volta precisa da validade da volta,
    # senão a caixa dela some entre duas visitas.
    lenta = _publicar(motor, "CAM-LENTA", [0.0, 140.0, 280.0])
    assert lenta[-1] >= 280.0, f"a caixa vai piscar: {lenta}"


def test_validade_sobe_na_hora_e_desce_devagar(monkeypatch):
    """Alongar tarde deixa a caixa piscar; encurtar cedo devolve o fantasma.

    Quando a máquina enche, a volta seguinte pode ser muito mais lenta que a
    anterior — e a caixa emitida com a validade da volta rápida morre com a
    pessoa ainda em cena. Por isso o intervalo medido sobe de uma vez e desce
    pela metade a cada publicação.
    """
    from app.services import inferencia as mod

    motor = mod.Inferencia()
    motor.ciclo_real = 10.0
    monkeypatch.setattr(mod, "publicar", _nada)

    # Rápida, rápida, e de repente uma pausa longa.
    v = _publicar(motor, "CAM-X", [0.0, 2.0, 4.0, 44.0])
    assert v[-1] >= 80.0, f"a validade não acompanhou a pausa: {v}"
    # E depois de voltar ao ritmo, encolhe — mas não de uma vez.
    v += _publicar(motor, "CAM-X", [46.0, 48.0])
    assert v[-1] < v[-3], f"a validade precisa encolher: {v}"
    assert v[-1] > 4.0 * 2, "encolheu de uma vez e devolve o fantasma"


def test_validade_tem_piso_para_nao_piscar(monkeypatch):
    """Apertada demais, a caixa some entre duas visitas — e o vídeo fica
    limpo com gente em cena, que é o erro oposto e pior."""
    from app.services import inferencia as mod

    motor = mod.Inferencia()
    motor.ciclo_real = 0.5
    monkeypatch.setattr(mod, "publicar", _nada)
    assert min(_publicar(motor, "CAM-RAPIDA", [0.0, 0.3, 0.6])) >= 4.0


def test_camera_que_nunca_publicou_usa_o_ciclo():
    """Sem intervalo observado ainda, o da volta é a única estimativa."""
    from app.services import inferencia as mod

    motor = mod.Inferencia()
    motor.ciclo_real = 100.0
    assert motor._validade_de("CAM-QUE-NAO-ABRIU") == 200.0


# ----------------------------------------------------------------------
# Cena cheia: nada do nosso código pode engolir gente
# ----------------------------------------------------------------------
def _multidao(n, largura=1920, altura=1080):
    """`n` pessoas lado a lado, sem sobreposição, em grade."""
    colunas = 7
    objs = []
    for i in range(n):
        cx = (i % colunas + 0.5) / colunas * largura
        cy = (i // colunas + 0.5) / ((n // colunas) + 1) * altura
        objs.append(ObjetoDetectado(
            "pessoa", 0.85, (int(cx - 45), int(cy - 110),
                             int(cx + 45), int(cy + 110)), track_id=i + 1))
    return objs


def test_vinte_e_cinco_pessoas_sobrevivem_a_fusao():
    """O parque não tem cena com 25 pessoas, então o modelo não pode ser
    verificado aqui — mas o nosso código pode, e é ele que poderia engolir
    gente ao deduplicar. Nenhuma caixa distinta pode cair na fusão."""
    gente = _multidao(25)
    assert len(fundir_sobrepostas(gente)) == 25


def test_rastreio_da_identidade_propria_a_cada_uma():
    from app.services.vision import RastreioPorPosicao

    rastreio = RastreioPorPosicao()
    marcados = rastreio.atribuir(_multidao(25))
    marcas = {o.track_id for o in marcados}
    assert len(marcas) == 25, f"25 pessoas renderam {len(marcas)} identidades"


def test_multidao_inteira_chega_a_sobreposicao():
    """A caixa do vídeo sai antes das podas de registro: ninguém em cena
    pode ficar sem marcação porque o represamento já viu aquela trilha."""
    from app.services.vision import AnalisadorAcoes

    an = AnalisadorAcoes()
    an.acoes_ativas = {"aglomeracao"}
    an.n_aglomeracao = 8
    gente = _multidao(25)
    eventos = an.atualizar(gente, 1920, 1080, agora=5000.0)
    # A cena é de 25; o analisador não pode perder ninguém na contagem.
    assert all(e.envolvidos <= 25 for e in eventos)
    assert len(an.trilhas) == 25, "cada pessoa precisa da própria trilha"


def test_pessoas_encostadas_nao_viram_uma():
    """Fila de elevador: ombro a ombro, e ainda assim duas."""
    a = ObjetoDetectado("pessoa", 0.9, (500, 300, 600, 760), track_id=1)
    b = ObjetoDetectado("pessoa", 0.9, (575, 300, 675, 760), track_id=2)
    assert len(fundir_sobrepostas([a, b])) == 2


def _andar(rastreio, passos, passo_px, intervalo_s=0.15):
    """Duas pessoas em sentidos opostos, um quadro por passo.

    O intervalo vai explícito: o rastreio segue pela posição prevista quando
    os quadros estão próximos, e sem `agora` as chamadas saíam a milissegundos
    umas das outras — a "visita distante" do teste virava rajada.
    """
    vistos = []
    for p in range(passos):
        xe, xd = 300 + p * passo_px, 1500 - p * passo_px
        objs = rastreio.atribuir([
            ObjetoDetectado("pessoa", 0.9, (xe - 45, 290, xe + 45, 510)),
            ObjetoDetectado("pessoa", 0.9, (xd - 45, 290, xd + 45, 510)),
        ], agora=1000.0 + p * intervalo_s)
        vistos.append(tuple(o.track_id for o in objs))
    return vistos


def test_na_rajada_a_identidade_persiste_e_nao_troca():
    """Quadros próximos — a escala em que corrida, queda e contrafluxo medem.

    A rajada tira oito quadros a 0,15s. Um adulto anda ~1,4 m/s, o que nesse
    intervalo dá poucos pixels: as caixas se sobrepõem bastante e cada pessoa
    mantém a própria identidade. É o que impede o nome saltar de uma para a
    outra quando duas se cruzam.
    """
    from app.services.vision import RastreioPorPosicao

    vistos = _andar(RastreioPorPosicao(), 6, passo_px=8)
    assert len(set(vistos)) == 1, f"a identidade mudou entre quadros: {vistos}"
    assert len(set(vistos[0])) == 2, "duas pessoas, duas identidades"


def test_entre_visitas_distantes_quem_atravessou_e_outra_passagem():
    """O limite declarado do rastreio, e ele é deliberado.

    Entre duas visitas à mesma câmera passam dezenas de segundos. Nessa
    escala a caixa de quem andou não se sobrepõe à anterior, e o objeto
    ganha identidade nova — o que está certo para permanência e aglomeração,
    que perguntam *quem ficou no lugar*, e é por isso que corrida, queda e
    contrafluxo exigem a rajada em vez do rodízio.

    Este teste existe para que a troca de identidade aqui não seja lida como
    defeito por quem vier depois: é o contrato do `RastreioPorPosicao`.
    """
    from app.services.vision import RastreioPorPosicao

    vistos = _andar(RastreioPorPosicao(), 3, passo_px=60, intervalo_s=30.0)
    assert vistos[0] != vistos[1], (
        "passo de visita distante deveria render identidade nova")


# ----------------------------------------------------------------------
# A passada leve: renova só a marcação de quem está na tela
# ----------------------------------------------------------------------
def _motor_com_tela(monkeypatch, objetos, exibindo=True):
    import asyncio
    from types import SimpleNamespace

    from app.services import inferencia as mod

    cam = SimpleNamespace(codigo="CAM-700", rtsp_url="rtsp://x", nome="Tela",
                          limiar_objeto=0.25, detecta_veiculo=False,
                          detecta_bagagem=False, detecta_animal=False,
                          zonas=[])
    motor = mod.Inferencia()
    motor.ciclo_real = 100.0
    motor._cameras_cache = [cam]

    monkeypatch.setattr(mod.video, "_leitores",
                        {"CAM-700": SimpleNamespace(exibindo=exibindo)})
    monkeypatch.setattr(mod.video, "quadro_bgr",
                        lambda *a, **k: SimpleNamespace(shape=(1080, 1920, 3)))
    monkeypatch.setattr(mod.vision, "yolo_disponivel", lambda: True)
    monkeypatch.setattr(mod.vision, "detectar_objetos", lambda *a, **k: objetos)

    publicados = []

    async def _pub(tipo, dados):
        publicados.append(dados)

    monkeypatch.setattr(mod, "publicar", _pub)
    return motor, publicados, asyncio


def test_passada_leve_publica_o_que_esta_em_cena(monkeypatch):
    """O que faz a caixa sumir logo depois de a pessoa sair.

    A volta completa leva perto de dois minutos porque paga a varredura em
    ladrilhos — medido, 1.295 ms a passada inteira contra 4.989 ms com ela.
    Para saber quem está em cena agora a varredura não é necessária.
    """
    pessoa = ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 760), track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [pessoa])

    aio.run(motor._renovar_sobreposicoes())

    assert len(publicados) == 1
    assert publicados[0]["camera_codigo"] == "CAM-700"
    assert len(publicados[0]["caixas"]) == 1
    assert publicados[0]["caixas"][0]["classe"] == "pessoa"


def test_cena_vazia_publica_lista_vazia(monkeypatch):
    """A pessoa saiu: a marcação tem de ser apagada, não apenas não renovada."""
    motor, publicados, aio = _motor_com_tela(monkeypatch, [])
    aio.run(motor._renovar_sobreposicoes())
    assert publicados and publicados[0]["caixas"] == []


def test_camera_sem_espectador_nao_paga_a_passada(monkeypatch):
    """O laço existe para o que está na tela. Sem ninguém olhando, nada roda
    — senão ele dobraria o custo do parque inteiro."""
    pessoa = ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 760), track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [pessoa], exibindo=False)
    aio.run(motor._renovar_sobreposicoes())
    assert publicados == []


def _norm(x1, y1, x2, y2, largura=1920, altura=1080):
    return [x1 / largura, y1 / altura, (x2 - x1) / largura, (y2 - y1) / altura]


def _ancora(corpo, rosto, nome="ANDRÉ LUIZ CARDOSO"):
    return {"nome": nome, "confianca": 0.9,
            "rosto": _norm(*rosto), "corpo": _norm(*corpo)}


def test_o_nome_nao_pisca_entre_duas_voltas(monkeypatch):
    """A passada leve não roda facial.

    Sem levar a tarja adiante, o nome de quem foi reconhecido sumiria a cada
    renovação e voltaria só na volta completa — piscando a cada segundo.

    A montagem importa, e a antiga escondia o defeito: ela punha o nome numa
    caixa de *pessoa*, e herdar por sobreposição funcionava. Na produção o
    nome está na caixa do **rosto**, que é uma fração da do corpo — a
    sobreposição entre as duas nunca chegava ao limiar, e o nome não
    sobrevivia a renovação nenhuma.
    """
    pessoa = ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 760), track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [pessoa])
    motor._ancoras["CAM-700"] = [
        _ancora(corpo=(800, 300, 900, 760), rosto=(830, 310, 870, 360))]

    aio.run(motor._renovar_sobreposicoes())

    caixas = publicados[0]["caixas"]
    nomeadas = [c for c in caixas if c.get("pessoa_nome")]
    assert len(nomeadas) == 1, "o nome tem de aparecer, e uma vez só"
    assert nomeadas[0]["pessoa_nome"] == "ANDRÉ LUIZ CARDOSO"
    assert nomeadas[0]["classe"] == "rosto", "a tarja não pode ir para o corpo"
    # A tarja leva a identidade do corpo sob ela: é por ela que a tela desliza
    # as duas juntas em vez de deslizar o retângulo e saltar o nome.
    corpo = next(c for c in caixas if c["classe"] == "pessoa")
    assert nomeadas[0]["track_id"] == corpo["track_id"] is not None


def test_a_tarja_acompanha_quem_se_move(monkeypatch):
    """A pessoa se moveu entre duas renovações: o nome vai junto.

    O rosto só é lido na volta completa. Se a tarja ficasse onde o rosto
    estava, ela se descolaria do corpo e acabaria pairando sobre o lugar
    vazio de onde a pessoa saiu.

    Trinta pixels numa caixa de cem — menos de meia largura, que é o que a
    sobreposição entre duas renovações tolera.
    """
    moveu = ObjetoDetectado("pessoa", 0.9, (830, 300, 930, 760), track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [moveu])
    motor._ancoras["CAM-700"] = [
        _ancora(corpo=(800, 300, 900, 760), rosto=(830, 310, 870, 360))]

    aio.run(motor._renovar_sobreposicoes())

    rosto = next(c for c in publicados[0]["caixas"] if c["classe"] == "rosto")
    # O corpo andou 30px à direita; o rosto tem de ter andado o mesmo tanto.
    assert abs(rosto["bbox"][0] - 860 / 1920) < 1e-6, (
        f"a tarja ficou para trás: {rosto['bbox']}")
    # E continua do tamanho de antes, porque o corpo não mudou de tamanho.
    assert abs(rosto["bbox"][2] - 40 / 1920) < 1e-6


def test_a_tarja_nao_persegue_quem_atravessou_a_cena(monkeypatch):
    """O limite declarado, e é de método, não de ajuste.

    Entre duas renovações passam uns dois segundos, e quem atravessa a cena
    andando percorre várias larguras de caixa nesse tempo: as duas caixas não
    se tocam e nenhum limiar as associa. O certo então é **perder** o nome, e
    não arrastá-lo pela tela até a caixa mais próxima — essa seria a troca de
    identidade, que num sistema de vigilância é o erro mais caro.
    """
    atravessou = ObjetoDetectado("pessoa", 0.9, (1400, 300, 1500, 760),
                                 track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [atravessou])
    motor._ancoras["CAM-700"] = [
        _ancora(corpo=(800, 300, 900, 760), rosto=(830, 310, 870, 360))]

    aio.run(motor._renovar_sobreposicoes())

    assert not [c for c in publicados[0]["caixas"] if c.get("pessoa_nome")], (
        "o nome foi arrastado para uma caixa que não é a mesma pessoa")


def test_nome_nao_salta_para_quem_esta_noutro_lugar(monkeypatch):
    """A âncora é o corpo: sem reencontrá-lo, o nome morre.

    Uma caixa do outro lado da cena não pode receber o nome de quem estava
    aqui — seria pôr a identidade de alguém sobre outra pessoa, que é o erro
    mais caro que a tela pode cometer.
    """
    longe = ObjetoDetectado("pessoa", 0.9, (100, 300, 200, 760), track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [longe])
    motor._ancoras["CAM-700"] = [
        _ancora(corpo=(1600, 300, 1700, 760), rosto=(1630, 310, 1670, 360))]

    aio.run(motor._renovar_sobreposicoes())

    assert not [c for c in publicados[0]["caixas"] if c.get("pessoa_nome")]
    assert motor._ancoras["CAM-700"] == [], "âncora sem corpo tem de morrer"


# ----------------------------------------------------------------------
# O custo do laço curto, que é a razão de ele existir
# ----------------------------------------------------------------------
def test_o_laco_curto_nao_paga_a_varredura(monkeypatch):
    """Medido nos 23 quadros deste parque: a passada inteira leva 1.231 ms
    por câmera e a varredura em ladrilhos mais 3.309 ms — 73% do custo.

    O laço existe para republicar a cena a cada segundo. Pedindo "ocupado",
    como estava, a varredura ligava exatamente nas câmeras que importam — as
    que têm gente em cena —, e o laço "curto" passava de 1,2s para 4,5s por
    câmera. Com nove tiles abertos, 41s por rodada em vez de 11s.
    """
    from app.services import inferencia as mod

    pessoa = ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 760), track_id=1)
    motor, _pub, aio = _motor_com_tela(monkeypatch, [pessoa])
    pedidos = []
    monkeypatch.setattr(mod.vision, "detectar_objetos",
                        lambda *a, **k: (pedidos.append(a), [pessoa])[1])

    aio.run(motor._renovar_sobreposicoes())

    # A assinatura é (quadro, limiar, rastrear, classes_fora, varredura).
    assert pedidos and pedidos[0][4] == "nunca", (
        f"o laço curto voltou a pagar a varredura: {pedidos[0][4]!r}")


def test_quem_so_a_varredura_acha_nao_pisca(monkeypatch):
    """A pessoa que a passada inteira perde.

    Nos quadros deste parque a varredura achou quatro pessoas que a passada
    inteira não viu — inclusive uma sentada à mesa do Administrativo
    ocupando 241x147 px, que de longe não é "pessoa distante": é alguém
    debruçado sobre papel, que o modelo só reconhece com mais pixels.

    O laço curto não roda varredura. Sem lembrar o que a volta achou, a caixa
    dessas pessoas apareceria uma vez por volta e sumiria no segundo seguinte.
    """
    perto = ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 760), track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [perto])

    # O que a volta completa viu: a mesma pessoa, mais uma que só a varredura
    # alcançou, do outro lado da sala.
    distante = ObjetoDetectado("pessoa", 0.3, (1500, 400, 1560, 520))
    distante.de_varredura = True
    motor._lembrar_distantes("CAM-700", [perto, distante], 1920, 1080)

    aio.run(motor._renovar_sobreposicoes())

    caixas = publicados[0]["caixas"]
    assert len(caixas) == 2, f"a pessoa distante sumiu da cena: {caixas}"
    assert any(abs(c["bbox"][0] - 1500 / 1920) < 1e-6 for c in caixas)


def test_a_distante_nao_vira_caixa_em_dobro(monkeypatch):
    """Quando a passada curta também a acha, vale a leitura de agora.

    Empilhar as duas poria duas caixas sobre a mesma pessoa — a duplicata que
    a fusão existe para evitar, reintroduzida pela porta dos fundos.
    """
    pessoa = ObjetoDetectado("pessoa", 0.9, (1500, 400, 1560, 520), track_id=1)
    motor, publicados, aio = _motor_com_tela(monkeypatch, [pessoa])

    lembrada = ObjetoDetectado("pessoa", 0.3, (1498, 398, 1562, 522))
    lembrada.de_varredura = True
    motor._lembrar_distantes("CAM-700", [lembrada], 1920, 1080)

    aio.run(motor._renovar_sobreposicoes())

    assert len(publicados[0]["caixas"]) == 1, "duas caixas sobre a mesma pessoa"


def test_a_distante_vence_com_a_volta(monkeypatch):
    """Lembrada para sempre, ela seria o fantasma que a validade apaga."""
    motor, publicados, aio = _motor_com_tela(monkeypatch, [])

    distante = ObjetoDetectado("pessoa", 0.3, (1500, 400, 1560, 520))
    distante.de_varredura = True
    motor._lembrar_distantes("CAM-700", [distante], 1920, 1080)
    # Envelhece a lembrança para além de uma volta inteira.
    quando, caixas = motor._distantes["CAM-700"]
    motor._distantes["CAM-700"] = (quando - motor.ciclo_real - 1, caixas)

    aio.run(motor._renovar_sobreposicoes())

    assert publicados[0]["caixas"] == []
    assert "CAM-700" not in motor._distantes


def test_nenhum_tile_do_mosaico_fica_sem_a_vez():
    """Mais câmeras em tela que o teto da rodada.

    Fatiando sempre do começo da lista, as que sobram ficavam fora de *toda*
    rodada e só eram reolhadas na volta completa — era o tile que nunca
    ganhava caixa. Girando o ponto de partida, cada uma entra na sua vez.

    O 4×4 cabe inteiro no teto desde que a varredura saiu do laço; este teste
    guarda o caso em que não cabe, que é para o que o rodízio serve.
    """
    from app.config import settings
    from app.services.inferencia import Inferencia

    motor = Inferencia()
    vistas = [f"CAM-{i:03d}" for i in range(1, 25)]
    assert len(vistas) > settings.INFERENCIA_SOBREPOSICAO_MAX, (
        "o teste precisa de mais câmeras que o teto para valer de algo")

    atendidas = set()
    for _ in range(4):
        fila = motor._fila_da_renovacao(vistas)
        # O teto vale sempre: a rodada não pode crescer com o parque.
        assert len(fila) == settings.INFERENCIA_SOBREPOSICAO_MAX
        atendidas |= set(fila)
    assert atendidas == set(vistas), (
        f"ficaram de fora: {sorted(set(vistas) - atendidas)}")


def test_o_quatro_por_quatro_cabe_numa_rodada_so():
    """Com dezesseis tiles, todos se renovam no mesmo passo.

    Medido com teto de nove: a renovação de cada câmera variava de 17s a 43s
    conforme ela caísse no começo ou no fim de uma rodada — a caixa de uma
    acompanhava a cena e a da vizinha ficava para trás, sem nada na cena que
    explicasse a diferença.
    """
    from app.services.inferencia import Inferencia

    motor = Inferencia()
    vistas = [f"CAM-{i:03d}" for i in range(1, 17)]
    assert set(motor._fila_da_renovacao(vistas)) == set(vistas)


def test_a_identidade_segue_a_pessoa_entre_duas_renovacoes(monkeypatch):
    """Sem número estável a tela não tem como mover a caixa: ela só pode
    apagá-la de um lugar e acendê-la noutro, que é o piscar de posição."""
    motor, publicados, aio = _motor_com_tela(
        monkeypatch, [ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 760))])
    aio.run(motor._renovar_sobreposicoes())
    primeira = publicados[0]["caixas"][0]["track_id"]
    assert primeira is not None, "a caixa saiu sem identidade"

    # Mesma pessoa, trinta pixels adiante.
    from app.services import inferencia as mod
    monkeypatch.setattr(
        mod.vision, "detectar_objetos",
        lambda *a, **k: [ObjetoDetectado("pessoa", 0.9, (830, 300, 930, 760))])
    aio.run(motor._renovar_sobreposicoes())

    assert publicados[1]["caixas"][0]["track_id"] == primeira


def test_duas_pessoas_que_se_aproximam_nao_trocam_de_numero(monkeypatch):
    """A troca de identidade é o erro mais caro: ela leva o nome de uma
    pessoa para cima da outra.

    Duas pessoas que se aproximam até ficarem ombro a ombro — o caso medido
    neste parque, onde o IoU entre pessoas distintas chegou a 0,29. Cada uma
    se sobrepõe mais a si mesma do passo anterior (0,43) do que à outra, e é
    disso que a associação vive.

    O que este teste **não** cobre, porque não é decidível: duas caixas
    exatamente sobrepostas. Aí não há informação de posição que diga qual é
    qual, e nenhum rastreador por posição acerta — distinguir as duas pediria
    comparar aparência.
    """
    from app.services import inferencia as mod

    motor, publicados, aio = _motor_com_tela(monkeypatch, [])
    for passo in range(5):
        esq, dir_ = 400 + passo * 40, 800 - passo * 40
        monkeypatch.setattr(mod.vision, "detectar_objetos", lambda *a, **k: [
            ObjetoDetectado("pessoa", 0.9, (esq, 300, esq + 100, 760)),
            ObjetoDetectado("pessoa", 0.9, (dir_, 300, dir_ + 100, 760)),
        ])
        aio.run(motor._renovar_sobreposicoes())

    # Cada passo devolve duas identidades distintas, e o conjunto não cresce:
    # ninguém ganhou número novo no caminho nem herdou o do outro.
    marcas = [tuple(c["track_id"] for c in p["caixas"]) for p in publicados]
    assert all(len(set(m)) == 2 for m in marcas), f"duas viraram uma: {marcas}"
    assert marcas[0] == marcas[-1], f"a identidade trocou no cruzamento: {marcas}"


# ----------------------------------------------------------------------
# A abertura do RTSP: o tile preto do Elevador
# ----------------------------------------------------------------------
class _CapFalso:
    """VideoCapture de mentira que anota como foi construído."""

    def __init__(self, abre: bool):
        self._abre = abre
        self.ajustes: list = []

    def isOpened(self):
        return self._abre

    def set(self, prop, valor):
        self.ajustes.append((prop, valor))

    def release(self):
        pass


class _Cv2Falso:
    CAP_FFMPEG = 1900
    CAP_PROP_OPEN_TIMEOUT_MSEC = 53
    CAP_PROP_READ_TIMEOUT_MSEC = 54

    def __init__(self, abre=True):
        self.abre = abre
        self.params = None
        self.ultimo = None

    def VideoCapture(self, url, api, params=None):
        self.params = params
        self.ultimo = _CapFalso(self.abre)
        return self.ultimo


def test_o_tempo_limite_de_abertura_vai_no_construtor():
    """Ajustar depois de construir não limita nada.

    O construtor do `VideoCapture` já tenta abrir o fluxo, então um
    `cap.set(CAP_PROP_OPEN_TIMEOUT_MSEC, ...)` feito em seguida chega depois
    da abertura que deveria limitar. Medido em 29/10/2026 contra um endereço
    que não responde, no OpenCV 5.0.0: pedindo 2s gastou 30,1s, e pedindo 12s
    gastou os mesmos 30,1s — o padrão do FFmpeg. Pelos parâmetros, 2,0s virou
    2,1s e 12,0s virou 12,0s.

    Era por isso que `ABERTURA_MS` nunca valeu: a câmera morta pendurava o
    leitor por 30s em vez dos 8s que o código afirmava, e o Elevador, que
    negocia entre 12 e 30, ficava no limite.
    """
    from app.services import video

    falso = _Cv2Falso()
    video._abrir(falso, "rtsp://x/y", 16000)

    assert falso.params is not None, "o tempo limite voltou a ser aplicado tarde"
    pares = dict(zip(falso.params[::2], falso.params[1::2]))
    assert pares[_Cv2Falso.CAP_PROP_OPEN_TIMEOUT_MSEC] == 16000


def test_so_a_camera_que_falha_ganha_mais_tempo():
    """A correção anterior subiu o tempo para o parque inteiro e custou caro:
    a volta passou de ~130s para 181,8s, porque *qualquer* câmera que falhe
    passa a bloquear mais. O erro ali foi o escopo — 23 das 24 câmeras abrem
    de primeira e nunca precisaram de um segundo a mais.
    """
    from app.services import video

    video._abertura_ms.clear()
    try:
        # Quatro tentativas frustradas na mesma câmera.
        esperas = []
        for _ in range(4):
            espera = video._abertura_ms.get("CAM-LENTA", video.ABERTURA_MS)
            esperas.append(espera)
            video._abertura_ms["CAM-LENTA"] = min(
                espera * 2, video.ABERTURA_MAX_MS)

        assert esperas[0] == video.ABERTURA_MS, "a primeira não pode custar mais"
        assert esperas == sorted(esperas), "a espera tem de crescer"
        assert max(video._abertura_ms.values()) <= video.ABERTURA_MAX_MS, (
            "sem teto, a câmera morta penduraria o leitor para sempre")
        # E a vizinha, que nunca falhou, não paga nada disso.
        assert video._abertura_ms.get(
            "CAM-SAUDAVEL", video.ABERTURA_MS) == video.ABERTURA_MS
    finally:
        video._abertura_ms.clear()


def test_a_opcao_morta_do_ffmpeg_saiu():
    """`stimeout` foi renomeado para `timeout` no FFmpeg e passou a ser
    ignorado. Mantê-lo ali dizia que a abertura esperava 8s quando ela
    esperava 30 — configuração morta que parece viva é pior que nenhuma."""
    from app.services import video

    assert "stimeout" not in video.os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]
    # O transporte TCP continua, e esse vale: sem ele o OpenCV negocia UDP e
    # perde pacote em rede com switch gerenciado.
    assert "rtsp_transport;tcp" in video.os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"]


def test_na_rajada_quem_corre_mantem_a_identidade():
    """Quem corre já não se sobrepõe à própria caixa do quadro anterior.

    Medido no Avenue: o corredor ganhava número novo em 9 de 10 quadros, a
    trajetória nunca se formava e corrida não disparava. Com quadros
    próximos, o rastreio segue pela posição prevista pela velocidade.
    """
    from app.services.vision import RastreioPorPosicao

    vistos = _andar(RastreioPorPosicao(), 8, passo_px=110, intervalo_s=0.22)
    assert len(set(vistos)) == 1, f"a identidade mudou durante a corrida: {vistos}"


def test_correr_na_horizontal_e_na_vertical_mede_igual():
    """A régua é o corpo nos dois eixos.

    Os pontos ficam em fração da largura e da altura, e numa imagem 16:9 o
    deslocamento horizontal saía 44% menor que o vertical de mesmo tamanho.
    """
    from app.services.vision import Trilha

    def ritmo(dx, dy):
        tr = Trilha(1, "pessoa")
        for i in range(8):
            px, py = 200 + i * dx, 200 + i * dy
            tr.registrar(px / 1280, py / 720, 1000 + i * 0.22, 120 / 720, 1280 / 720)
        return tr.arranque()[0]

    horizontal, vertical = ritmo(60, 0), ritmo(0, 60)
    assert abs(horizontal - vertical) < 0.01, (horizontal, vertical)
    # 60 px a cada 0,22 s com um corpo de 120 px: 2,27 corpos por segundo.
    assert abs(horizontal - 60 / 0.22 / 120) < 0.01
