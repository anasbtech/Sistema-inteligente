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
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;8000000"
)

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

FPS_SAIDA = 10
OCIOSO_S = 30.0
FALHAS_ATE_DESISTIR = 60


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
        self.largura_nativa = 0
        self.altura_nativa = 0
        self.erro: str = ""
        self.pronto = threading.Event()
        self.pronto_detalhe = threading.Event()
        self._ultimo_uso = time.monotonic()
        # Zero, e não `monotonic()`: ninguém pediu detalhe ainda.
        self._ultimo_uso_detalhe = 0.0
        self._parar = threading.Event()
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

    def tocar_detalhe(self) -> None:
        """Marca que alguém está assistindo em resolução nativa."""
        agora = time.monotonic()
        self._ultimo_uso = agora
        self._ultimo_uso_detalhe = agora

    @property
    def detalhe_ativo(self) -> bool:
        return (time.monotonic() - self._ultimo_uso_detalhe) <= OCIOSO_DETALHE_S

    def parar(self) -> None:
        self._parar.set()

    def _rodar(self) -> None:
        import cv2

        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)

        if not cap.isOpened():
            self.erro = "não foi possível abrir o fluxo RTSP"
            log.warning("[%s] %s", self.codigo, self.erro)
            self.pronto.set()
            cap.release()
            _descartar(self)
            return

        log.info("[%s] fluxo RTSP aberto", self.codigo)
        intervalo = 1.0 / FPS_SAIDA
        proximo = 0.0
        falhas = 0

        while not self._parar.is_set():
            if time.monotonic() - self._ultimo_uso > OCIOSO_S:
                log.info("[%s] sem espectadores, encerrando leitor", self.codigo)
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
                # permite acompanhar os 30fps da câmera gastando CPU só nos 10
                # que realmente saem — decodificar todos saturava a máquina.
                continue
            proximo = agora + intervalo

            ok, quadro = cap.retrieve()
            if not ok or quadro is None:
                continue

            altura, largura = quadro.shape[:2]
            self.largura_nativa, self.altura_nativa = largura, altura

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
                _ultimos[self.codigo] = (time.monotonic(), self.quadro)
                self.pronto.set()

        cap.release()
        self.pronto.set()
        self.pronto_detalhe.set()   # não deixa espectador de detalhe pendurado
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
        if leitor is None or not leitor.vivo:
            leitor = Leitor(codigo, url)
            _leitores[codigo] = leitor
        leitor.tocar()
        return leitor


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

