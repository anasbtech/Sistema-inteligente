"""Ponte RTSP → MJPEG para o mosaico.

O navegador não reproduz RTSP. Em produção o caminho é o gateway WebRTC que
`/stream/{codigo}` anuncia, com passagem direta do H.264 e latência abaixo de
um segundo. Esta ponte cobre o desenvolvimento, quando não há gateway no ar.

Ela reencoda cada quadro em JPEG, então custa CPU e não escala: por isso há um
único leitor por câmera, compartilhado entre os espectadores, que descarta
quadros para manter a taxa de saída baixa e o buffer do RTSP vazio. Um leitor
sem espectadores se encerra sozinho.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional

log = logging.getLogger("seguranca.video")

# Sem isto o OpenCV negocia UDP e perde pacote em rede com switch gerenciado.
#
# `stimeout;8000000` saiu daqui: o FFmpeg o renomeou para `timeout` e passou a
# ignorá-lo, e a medição de 29/10/2026 confirma que ele não valia nada — uma
# abertura contra endereço mudo gastou 30,1s com ele presente, não os 8s que
# ele dizia. Quem controla isso agora é `_abrir`, por câmera e de fato.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp"
)

# Quanto esperar pela negociação RTSP antes de desistir, na primeira vez.
#
# Tentei 25s para todo mundo, porque o Elevador leva de 12 a 30 para negociar.
# Medido, custou caro: a volta pelo parque passou de ~130s para 181,8s, porque
# *qualquer* câmera que falhe passa a bloquear por 25 em vez de 8. Revertido —
# e o erro ali foi o escopo, não o número: 23 das 24 câmeras abrem de primeira
# e nunca precisaram de um segundo a mais.
ABERTURA_MS = 8000
# Até onde a espera pode crescer na câmera que falha. Medido em 29/10/2026: a
# CAM-018 Elevador foi a única do parque a falhar, e levou 4 tentativas e 5
# minutos entre 10:05:57 e 10:11:08 para abrir — cada tentativa desistindo aos
# 8s de uma negociação que precisava de mais. As outras 23 tiveram zero falhas.
ABERTURA_MAX_MS = 40000
# Tempo limite de cada leitura, depois do fluxo aberto. O detector de inanição
# (`SEM_IMAGEM_S`) é quem decide reconectar; este só evita que uma leitura
# isolada pendure a thread.
LEITURA_MS = 8000

# Quanto cada câmera espera de fato, aprendido com o que já aconteceu com ela.
#
# O tempo limite é um teto, não uma espera: quem negocia em meio segundo abre
# em meio segundo, com teto de 8s ou de 40s. Só paga mais quem de fato falha —
# e é por isso que isto pode subir sem tocar no custo das outras.
#
# Não desce de novo. Uma câmera que precisou de 32s uma vez não fica mais
# lenta por manter o teto alto, e baixá-lo devolveria a primeira falha da
# próxima vez — que é a falha que custa os cinco minutos de tile preto.
_abertura_ms: Dict[str, int] = {}

# Mosaico: dezesseis tiles pequenos ao mesmo tempo. Aqui o limite é a CPU do
# servidor, e 640px já cobre o tamanho em que cada tile é exibido.
LARGURA_SAIDA = 640
QUALIDADE_JPEG = 70

# Detalhe: um espectador só, com o quadro grande na tela para conferir foco,
# enquadramento e zona. Reduzir aqui não economiza nada que importe e destrói
# justamente o que se foi olhar — uma câmera 1080p chegava ao operador como
# 640x360 recomprimido a 70, três vezes menor em cada lado que o sensor.
# `0` significa não redimensionar: sai o que a lente entregou.
LARGURA_DETALHE = 0
QUALIDADE_DETALHE = 92
# O encode extra só acontece enquanto alguém está de fato olhando o detalhe.
# Sem pedido novo dentro desta janela, o leitor volta a gastar só com o
# mosaico.
OCIOSO_DETALHE_S = 6.0

# Cada quadro que sai custa um decode de H.264/H.265, e o parque inteiro
# decodifica ao mesmo tempo, na mesma máquina que roda os modelos. A 10, com
# 26 câmeras, a CPU ficava em 100% e a análise de uma câmera passava de 0,2s
# para mais de 7s — a volta completa chegou a dois minutos, e a caixa só
# aparecia quando o worker enfim voltava àquela câmera. Seis é fluido o
# bastante para vigilância e devolve o tempo que a inferência precisa.
FPS_SAIDA = 6
# Ritmo de quem não tem ninguém assistindo. A inferência consome um quadro por
# volta, não seis por segundo: manter o parque inteiro em FPS_SAIDA gastava a
# CPU dos modelos para produzir quadros que ninguém olhava. O leitor sobe para
# FPS_SAIDA assim que uma tela pede, e volta a este ritmo quando ela fecha.
FPS_OCIOSO = 1
# Quanto tempo, sem pedido de tela, o leitor continua no ritmo cheio. Cobre a
# troca de página do mosaico sem fazer o vídeo engasgar a cada navegação.
EXIBINDO_S = 8.0
# Precisa ser maior que uma volta completa da inferência, porque o worker é um
# espectador como qualquer outro: ele toca a câmera uma vez por ciclo. A 30s,
# com o parque em 26 câmeras, o leitor expirava antes da volta seguinte e o
# log virava um vaivém de "encerrando leitor" / "fluxo RTSP aberto" — e cada
# reabertura custa segundos negociando RTSP, que saem do tempo de análise.
OCIOSO_S = 180.0
FALHAS_ATE_DESISTIR = 60
# Quanto tempo um leitor pode ficar sem produzir imagem antes de ser dado
# por perdido e reconectado.
#
# Contar falhas não basta, e o Elevador provou: um `grab` que estoura o
# tempo limite gasta oito segundos por tentativa, então sessenta delas são
# oito minutos de leitor *vivo, sem erro e sem quadro*. E um `retrieve` que
# falha nem contava — o laço voltava ao topo sem registrar nada, girando
# para sempre. Medido na CAM-018: leitor saudável por toda medida que o
# sistema tinha, com o último quadro de 28,6s.
#
# Abaixo dos 15s que o snapshot aceita, para a reconexão começar antes de o
# tile apagar. Reabrir custa segundos; ficar faminto custa para sempre.
SEM_IMAGEM_S = 12.0

# Quanto o leitor espera de fato antes de desistir. `OCIOSO_S` é só o piso: o
# worker ajusta isto a cada volta, porque é ele quem sabe de quanto em quanto
# tempo volta a cada câmera — e o número cresce com o parque.
#
# Deixá-lo fixo já custou caro duas vezes. A 30s o leitor morria entre duas
# visitas e o log virava vaivém de abrir e fechar. Subiu para 180, e a volta
# chegou a 168,7s medidos: onze segundos de margem. Quando ela passa de 180,
# toda visita reabre o RTSP — e numa câmera lenta a reabertura estoura o
# limite de abertura e o tile fica preto, que foi o caso do Elevador.
_ocioso_s = OCIOSO_S


def definir_ocioso(segundos: float) -> None:
    """O worker informa de quanto em quanto tempo volta a cada câmera."""
    global _ocioso_s
    _ocioso_s = max(OCIOSO_S, float(segundos))


def _abrir(cv2, url: str, espera_ms: int):
    """Abre o fluxo RTSP com o tempo limite valendo de verdade.

    O jeito antigo era construir e depois ajustar::

        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ABERTURA_MS)

    e isso **não tem efeito nenhum**: o construtor já tenta abrir, então o
    ajuste chega depois da abertura que ele deveria limitar. Medido em
    29/10/2026 contra um endereço que não responde, no OpenCV 5.0.0:

        pedido  2,0s -> gastou 30,1s
        pedido 12,0s -> gastou 30,1s     (pelo construtor + set)
        pedido  2,0s -> gastou  2,1s
        pedido 12,0s -> gastou 12,0s     (pelos parâmetros)

    Ou seja, `ABERTURA_MS` nunca valeu, e o que mandava era o padrão de 30s
    do FFmpeg. Isso explica as duas pontas do problema: a câmera morta
    pendurava o leitor por 30s em vez dos 8s que o código dizia, e o
    Elevador — que negocia entre 12 e 30 — ficava no limite, às vezes
    entrando e às vezes não.

    Pelos parâmetros do construtor o número é respeitado. O `try` cobre
    build de OpenCV sem essa sobrecarga: ali se volta ao comportamento
    anterior, que funciona, só não deixa escolher o tempo.
    """
    try:
        return cv2.VideoCapture(url, cv2.CAP_FFMPEG, [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, int(espera_ms),
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, int(LEITURA_MS),
        ])
    except Exception:                                        # noqa: BLE001
        cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, LEITURA_MS)
        return cap


# Câmera que não entrega imagem espera antes de tentar de novo, e cada vez
# mais. O OpenCV abre **uma câmera por vez** no processo (medido: em threads,
# a segunda só começa quando a primeira termina), e cada tentativa ocupa essa
# fila pelo tempo limite inteiro. A CAM-018 — conecta e não manda imagem —
# reabriu 18 vezes e falhou 6 em quatro horas, e a cada vez as outras 23
# esperavam: na partida, o mosaico levava até 60 s para mostrar todas.
PAUSA_FALHA_S = 60.0
PAUSA_FALHA_MAX_S = 300.0
_pausa_ate: Dict[str, float] = {}
_falhas_seguidas: Dict[str, int] = {}


def em_pausa(codigo: str) -> bool:
    return time.monotonic() < _pausa_ate.get(codigo, 0.0)


def _registrar_falha(codigo: str) -> float:
    n = _falhas_seguidas.get(codigo, 0) + 1
    _falhas_seguidas[codigo] = n
    pausa = min(PAUSA_FALHA_MAX_S, PAUSA_FALHA_S * 2 ** (n - 1))
    _pausa_ate[codigo] = time.monotonic() + pausa
    return pausa


# Senha de abertura, na ordem em que os leitores foram criados. A trava do
# OpenCV não é uma fila: com 24 câmeras esperando, ela entregava a vez a
# qualquer uma, e a primeira página do mosaico — pré-aberta primeiro — ficava
# pronta por último tão frequentemente quanto primeiro.
_fila_abertura = threading.Condition()
_senha_proxima = 0
_senha_atendida = 0


def _tirar_senha() -> int:
    global _senha_proxima
    with _fila_abertura:
        senha = _senha_proxima
        _senha_proxima += 1
        return senha


def _abrir_na_vez(cv2, url: str, espera_ms: int, senha: int):
    global _senha_atendida
    with _fila_abertura:
        # O teto é só uma garantia: se um leitor sumir sem abrir, a fila não
        # para por causa dele.
        _fila_abertura.wait_for(lambda: _senha_atendida >= senha, timeout=espera_ms / 1000 + 30)
    try:
        return _abrir(cv2, url, espera_ms)
    finally:
        with _fila_abertura:
            _senha_atendida = max(_senha_atendida, senha + 1)
            _fila_abertura.notify_all()


class LeitorIndisponivel(RuntimeError):
    """OpenCV ausente — a ponte não pode funcionar."""


class Leitor:
    """Mantém sempre o quadro mais recente de uma câmera, em JPEG."""

    def __init__(self, codigo: str, url: str) -> None:
        self.codigo = codigo
        self.url = url
        self.quadro: Optional[bytes] = None
        # Quadro em resolução nativa, produzido só enquanto há quem assista.
        self.quadro_detalhe: Optional[bytes] = None
        # Matriz BGR nativa do último quadro, sem encode — para os modelos.
        self.bgr_nativo = None
        self.largura_nativa = 0
        self.altura_nativa = 0
        self.erro: str = ""
        self.pronto = threading.Event()
        self.pronto_detalhe = threading.Event()
        self._ultimo_uso = time.monotonic()
        # Zero, e não `monotonic()`: ninguém pediu detalhe ainda.
        self._ultimo_uso_detalhe = 0.0
        # Idem para a exibição: o worker tocando a câmera não conta como tela
        # aberta, e é essa diferença que decide o ritmo de decodificação.
        self._ultimo_uso_exibicao = 0.0
        self._parar = threading.Event()
        # Se este leitor chegou a entregar imagem: é o que separa a câmera
        # que caiu por um instante (reconecta já) da que nunca funcionou.
        self.entregou = False
        self._senha = _tirar_senha()
        self._thread = threading.Thread(
            target=self._rodar, daemon=True, name=f"rtsp-{codigo}"
        )
        self._thread.start()

    @property
    def vivo(self) -> bool:
        return self._thread.is_alive()

    def tocar(self) -> None:
        """Marca que alguém ainda está assistindo."""
        self._ultimo_uso = time.monotonic()

    def tocar_exibicao(self) -> None:
        """Marca que há uma tela aberta — não apenas o worker analisando."""
        agora = time.monotonic()
        self._ultimo_uso = agora
        self._ultimo_uso_exibicao = agora

    def tocar_detalhe(self) -> None:
        """Marca que alguém está assistindo em resolução nativa."""
        agora = time.monotonic()
        self._ultimo_uso = agora
        self._ultimo_uso_exibicao = agora
        self._ultimo_uso_detalhe = agora

    @property
    def detalhe_ativo(self) -> bool:
        return (time.monotonic() - self._ultimo_uso_detalhe) <= OCIOSO_DETALHE_S

    @property
    def exibindo(self) -> bool:
        return (time.monotonic() - self._ultimo_uso_exibicao) <= EXIBINDO_S

    def parar(self) -> None:
        self._parar.set()

    def _rodar(self) -> None:
        import cv2

        espera = _abertura_ms.get(self.codigo, ABERTURA_MS)
        cap = _abrir_na_vez(cv2, self.url, espera, self._senha)

        if not cap.isOpened():
            # Da próxima vez, esta câmera — e só ela — ganha o dobro. Falhar
            # com o teto apertado não encurta nada: o leitor é descartado, o
            # worker cria outro na visita seguinte e a conta recomeça. Era
            # assim que o Elevador gastava minutos em tentativas curtas
            # enquanto o tile ficava preto.
            _abertura_ms[self.codigo] = min(espera * 2, ABERTURA_MAX_MS)
            self.erro = "não foi possível abrir o fluxo RTSP"
            log.warning("[%s] %s em %.0fs; a próxima tentativa espera %.0fs",
                        self.codigo, self.erro, espera / 1000,
                        _abertura_ms[self.codigo] / 1000)
            self.pronto.set()
            cap.release()
            pausa = _registrar_falha(self.codigo)
            log.warning("[%s] nova tentativa em %.0fs", self.codigo, pausa)
            # Fica registrado, morto e com o erro: quem pedir durante a pausa
            # recebe a falha na hora, sem abrir outra tentativa na fila.
            return

        log.info("[%s] fluxo RTSP aberto", self.codigo)
        proximo = 0.0
        falhas = 0
        # Quando saiu o último quadro de verdade. É o sinal de saúde que
        # importa: `vivo` só diz que a thread existe.
        ultima_imagem = time.monotonic()

        while not self._parar.is_set():
            if time.monotonic() - self._ultimo_uso > _ocioso_s:
                log.info("[%s] sem espectadores, encerrando leitor", self.codigo)
                break

            if time.monotonic() - ultima_imagem > SEM_IMAGEM_S:
                self.erro = "sem imagem"
                log.warning("[%s] %s há %.0fs, reconectando",
                            self.codigo, self.erro,
                            time.monotonic() - ultima_imagem)
                break

            if not cap.grab():
                falhas += 1
                if falhas > FALHAS_ATE_DESISTIR:
                    self.erro = "fluxo interrompido"
                    log.warning("[%s] %s", self.codigo, self.erro)
                    break
                time.sleep(0.05)
                continue
            falhas = 0

            agora = time.monotonic()
            if agora < proximo:
                # `grab` tira o quadro do buffer sem decodificar. É isso que
                # permite acompanhar os 30fps da câmera gastando CPU só nos
                # que realmente saem — decodificar todos saturava a máquina.
                # Mas `grab` também não é de graça: ele ainda lê e separa o
                # stream, e girar aqui sem pausa em 24 câmeras custava CPU que
                # fazia falta aos modelos. Com a tela aberta o giro continua
                # apertado, para o vídeo não atrasar; sem ninguém olhando, uma
                # pausa curta derruba a leitura a um quinto do ritmo, e o
                # atraso que ela acumula não incomoda quem não está vendo.
                if not self.exibindo:
                    time.sleep(0.2)
                continue
            proximo = agora + 1.0 / (FPS_SAIDA if self.exibindo else FPS_OCIOSO)

            ok, quadro = cap.retrieve()
            if not ok or quadro is None:
                # `grab` separa o pacote; `retrieve` o decodifica. Só o
                # segundo produz imagem, e voltar ao topo sem registrar nada
                # era o que deixava o leitor girando sem entregar.
                time.sleep(0.05)
                continue

            altura, largura = quadro.shape[:2]
            self.largura_nativa, self.altura_nativa = largura, altura

            # Quadro nativo cru, para quem vai analisá-lo. Guardar a matriz
            # custa uma referência; o caminho do detalhe custaria um encode
            # JPEG de 1080p por quadro lido, e com três câmeras isso satura a
            # CPU e derruba a própria leitura do RTSP.
            self.bgr_nativo = quadro
            if not self.entregou:
                self.entregou = True
                _falhas_seguidas.pop(self.codigo, None)
                _pausa_ate.pop(self.codigo, None)

            # O detalhe sai do quadro intacto, antes de qualquer redução — e
            # só quando há espectador, porque é um encode a mais por quadro.
            if self.detalhe_ativo:
                bruto = quadro
                if LARGURA_DETALHE and largura > LARGURA_DETALHE:
                    nova = int(altura * LARGURA_DETALHE / largura)
                    bruto = cv2.resize(
                        quadro, (LARGURA_DETALHE, nova), interpolation=cv2.INTER_AREA
                    )
                ok_det, buf_det = cv2.imencode(
                    ".jpg", bruto, [int(cv2.IMWRITE_JPEG_QUALITY), QUALIDADE_DETALHE]
                )
                if ok_det:
                    self.quadro_detalhe = buf_det.tobytes()
                    self.pronto_detalhe.set()
            elif self.quadro_detalhe is not None:
                # Ninguém olhando: solta o buffer grande e rearma a espera,
                # senão o próximo espectador receberia um quadro velho.
                self.quadro_detalhe = None
                self.pronto_detalhe.clear()

            if largura > LARGURA_SAIDA:
                nova_altura = int(altura * LARGURA_SAIDA / largura)
                quadro = cv2.resize(
                    quadro, (LARGURA_SAIDA, nova_altura), interpolation=cv2.INTER_AREA
                )

            codificou, buffer = cv2.imencode(
                ".jpg", quadro, [int(cv2.IMWRITE_JPEG_QUALITY), QUALIDADE_JPEG]
            )
            if codificou:
                self.quadro = buffer.tobytes()
                ultima_imagem = time.monotonic()
                _ultimos[self.codigo] = (ultima_imagem, self.quadro)
                self.pronto.set()

        cap.release()
        self.pronto.set()
        self.pronto_detalhe.set()   # não deixa espectador de detalhe pendurado
        if self.erro and not self.entregou:
            # Conectou e nunca mandou imagem: mesma pausa de quem não abriu.
            pausa = _registrar_falha(self.codigo)
            log.warning("[%s] nova tentativa em %.0fs", self.codigo, pausa)
            return
        _descartar(self)


_leitores: Dict[str, Leitor] = {}
_trava = threading.Lock()

# Último quadro de cada câmera, que sobrevive ao fim do leitor. Miniatura de
# lista não justifica manter um fluxo RTSP aberto — e abrir um a cada visita
# custava segundos. Atribuição de dict é atômica sob o GIL: dispensa trava.
_ultimos: Dict[str, tuple] = {}


def ultimo_quadro(codigo: str, idade_maxima_s: float) -> Optional[bytes]:
    """Quadro já capturado, se ainda for recente o bastante."""
    registro = _ultimos.get(codigo)
    if registro is None:
        return None
    instante, quadro = registro
    return quadro if (time.monotonic() - instante) <= idade_maxima_s else None


def _descartar(leitor: Leitor) -> None:
    """Remove o leitor do registro — só se ainda for ele, nunca um sucessor."""
    with _trava:
        if _leitores.get(leitor.codigo) is leitor:
            del _leitores[leitor.codigo]


def obter(codigo: str, url: str) -> Leitor:
    """Leitor da câmera, criando ou reaproveitando o que já existe."""
    try:
        import cv2  # noqa: F401
    except ImportError as exc:
        raise LeitorIndisponivel(
            "OpenCV não está instalado: pip install opencv-python-headless"
        ) from exc

    with _trava:
        leitor = _leitores.get(codigo)
        # Morto e em pausa: devolve o mesmo, com o erro, sem nova tentativa.
        if leitor is None or (not leitor.vivo and not em_pausa(codigo)):
            leitor = Leitor(codigo, url)
            _leitores[codigo] = leitor
        leitor.tocar()
        return leitor


def quadro_bgr(codigo: str, url: str, detalhe: bool = False):
    """Quadro corrente da câmera como matriz BGR, para alimentar os modelos.

    Com ``detalhe``, pede a resolução nativa. Importa para quem analisa: o
    fluxo do mosaico sai reduzido a 640px de largura, e num quadro de 1920
    isso encolhe tudo a um terço — um rosto de 54 pixels chega ao modelo com
    18, abaixo de qualquer piso de identificação. O que a tela pode perder
    sem prejuízo, o modelo não pode.

    ``None`` enquanto a câmera não entregou nada — a negociação do RTSP leva
    segundos, e quem chama deve simplesmente tentar de novo no ciclo seguinte.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    try:
        leitor = obter(codigo, url)
    except LeitorIndisponivel:
        return None

    if detalhe:
        # Direto da matriz que o leitor já tem, sem passar por JPEG. Pedir o
        # "detalhe" aqui ligaria um encode de 1080p a cada quadro lido, e não
        # a cada análise: com três câmeras isso consome a CPU que o RTSP
        # precisa e as imagens param de chegar.
        nativo = leitor.bgr_nativo
        if nativo is not None:
            return nativo
    jpeg = leitor.quadro_detalhe if leitor.detalhe_ativo else leitor.quadro
    if not jpeg:
        return None
    try:
        return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    except Exception:  # noqa: BLE001
        return None


def recortar_bgr(quadro, bbox_px, margem: float = 0.25) -> Optional[bytes]:
    """Recorta a região da caixa (em pixels) e devolve JPEG."""
    try:
        import cv2
    except ImportError:
        return None
    try:
        alt, larg = quadro.shape[:2]
        x1, y1, x2, y2 = (int(v) for v in bbox_px)
        mx, my = int((x2 - x1) * margem), int((y2 - y1) * margem)
        x1, y1 = max(0, x1 - mx), max(0, y1 - my)
        x2, y2 = min(larg, x2 + mx), min(alt, y2 + my)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None
        ok, buf = cv2.imencode(
            ".jpg", quadro[y1:y2, x1:x2], [int(cv2.IMWRITE_JPEG_QUALITY), 85]
        )
        return buf.tobytes() if ok else None
    except Exception:  # noqa: BLE001
        return None


def recorte(codigo: str, url: str, bbox: Optional[list], margem: float = 0.35) -> Optional[bytes]:
    """Recorte do quadro corrente da câmera, na região da caixa.

    Devolve ``None`` quando não há quadro ainda — a primeira leitura de uma
    câmera leva segundos para negociar o RTSP, e travar a ingestão à espera
    dela pararia o registro inteiro por uma imagem.

    A margem abre o enquadramento em volta da caixa: um recorte colado no
    contorno não deixa a revisão humana ver onde a pessoa estava.
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    try:
        leitor = obter(codigo, url)
    except LeitorIndisponivel:
        return None

    jpeg = leitor.quadro
    if not jpeg:
        return None
    if not bbox or len(bbox) < 4:
        return jpeg

    try:
        quadro = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if quadro is None:
            return jpeg
        alt, larg = quadro.shape[:2]

        x, y, w, h = (float(v) for v in bbox[:4])
        mx, my = w * margem, h * margem
        x1 = max(0, int((x - mx) * larg))
        y1 = max(0, int((y - my) * alt))
        x2 = min(larg, int((x + w + mx) * larg))
        y2 = min(alt, int((y + h + my) * alt))
        if x2 - x1 < 8 or y2 - y1 < 8:
            return jpeg

        ok, buf = cv2.imencode(
            ".jpg", quadro[y1:y2, x1:x2], [int(cv2.IMWRITE_JPEG_QUALITY), 82]
        )
        return buf.tobytes() if ok else jpeg
    except Exception as exc:  # noqa: BLE001
        log.debug("Falha ao recortar quadro de %s: %s", codigo, exc)
        return jpeg


def encerrar_todos() -> None:
    with _trava:
        leitores = list(_leitores.values())
    for leitor in leitores:
        leitor.parar()



def jpeg_ampliado(recorte, largura_min: int = 360) -> Optional[bytes]:
    """JPEG de um recorte pequeno, ampliado até uma largura legível.

    Para a placa: no quadro ela tem 60 a 120 px de largura, e a ficha a mostra
    a 300. Ampliar aqui, com interpolação cúbica, sai mais nítido que deixar o
    navegador esticar um JPEG minúsculo. Nunca reduz.
    """
    import cv2

    if recorte is None or getattr(recorte, "size", 0) == 0:
        return None
    alt, larg = recorte.shape[:2]
    if larg < largura_min:
        fator = largura_min / max(larg, 1)
        recorte = cv2.resize(recorte, (largura_min, max(1, int(alt * fator))),
                             interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".jpg", recorte, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return buf.tobytes() if ok else None
