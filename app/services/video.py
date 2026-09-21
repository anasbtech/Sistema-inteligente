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

log = logging.getLogger("vigia.video")

# Sem isto o OpenCV negocia UDP e perde pacote em rede com switch gerenciado.
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;8000000"
)

LARGURA_SAIDA = 640
QUALIDADE_JPEG = 70
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
        self.erro: str = ""
        self.pronto = threading.Event()
        self._ultimo_uso = time.monotonic()
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


def encerrar_todos() -> None:
    with _trava:
        leitores = list(_leitores.values())
    for leitor in leitores:
        leitor.parar()
