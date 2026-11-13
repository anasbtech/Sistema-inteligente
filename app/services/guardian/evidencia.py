"""Vídeo de evidência: os segundos antes e depois de um sinal.

Cada câmera do Guardian guarda os últimos `GUARDIAN_CLIPE_ANTES_S` segundos
em memória (quadros reduzidos, em JPEG). Quando um sinal fecha, o serviço
junta esse passado aos `GUARDIAN_CLIPE_DEPOIS_S` seguintes e grava um único
arquivo, **criptografado** (Fernet: AES-128-CBC + HMAC-SHA256) com a chave de
`.secrets/guardian.key` — que não vai para o git nem para o backup das
evidências. Quem copiar a pasta `evidencias/` não vê o vídeo.

Formato: JPEGs concatenados (MJPEG cru). A tela o toca como
`multipart/x-mixed-replace`, que todo navegador exibe numa <img>, sem
depender de codec de vídeo instalado no servidor.
"""
from __future__ import annotations

import collections
import logging
import time
from pathlib import Path
from typing import Deque, Iterator, List, Optional, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.guardian")

LARGURA = 960
QUALIDADE = 80


def _chave() -> bytes:
    from cryptography.fernet import Fernet
    arq = Path(settings.GUARDIAN_CHAVE)
    if not arq.exists():
        arq.parent.mkdir(parents=True, exist_ok=True)
        arq.write_bytes(Fernet.generate_key())
        log.info("Guardian: chave de criptografia criada em %s", arq)
    return arq.read_bytes().strip()


def cifrar(dados: bytes) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(_chave()).encrypt(dados)


def decifrar(dados: bytes) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(_chave()).decrypt(dados)


def jpeg(quadro: np.ndarray) -> Optional[bytes]:
    import cv2
    alt, larg = quadro.shape[:2]
    if larg > LARGURA:
        quadro = cv2.resize(quadro, (LARGURA, int(alt * LARGURA / larg)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", quadro, [int(cv2.IMWRITE_JPEG_QUALITY), QUALIDADE])
    return buf.tobytes() if ok else None


class Memoria:
    """Os últimos segundos de uma câmera, em JPEG."""

    def __init__(self, segundos: float) -> None:
        self.segundos = segundos
        self.quadros: Deque[Tuple[float, bytes]] = collections.deque()

    def guardar(self, quadro: np.ndarray, agora: Optional[float] = None) -> None:
        agora = time.monotonic() if agora is None else agora
        j = jpeg(quadro)
        if j:
            self.quadros.append((agora, j))
        while self.quadros and agora - self.quadros[0][0] > self.segundos:
            self.quadros.popleft()

    def desde(self, inicio: float) -> List[Tuple[float, bytes]]:
        return [q for q in self.quadros if q[0] >= inicio]


def gravar_clipe(codigo: str, quando, quadros: List[Tuple[float, bytes]]) -> str:
    """Grava o clipe cifrado e devolve a chave em `evidencias/`."""
    from app.services import evidencias
    if not quadros:
        return ""
    # Cabeçalho "segundo:tamanho" de cada quadro: a tela toca no ritmo
    # original, e a leitura corta os JPEGs sem procurar marcadores.
    t0 = quadros[0][0]
    cabecalho = ",".join(f"{t - t0:.3f}:{len(j)}" for t, j in quadros).encode() + b"|"
    bruto = cabecalho + b"".join(j for _, j in quadros)
    return evidencias.gravar(f"{codigo}-clipe", quando, cifrar(bruto))


def ler_clipe(chave: str) -> List[Tuple[float, bytes]]:
    """[(segundo relativo, jpeg)] do clipe gravado; [] se não houver."""
    from app.services import evidencias
    dados = evidencias.ler(chave) if chave else None
    if not dados:
        return []
    bruto = decifrar(dados)
    cab, _, corpo = bruto.partition(b"|")
    saida, i = [], 0
    for item in cab.decode().split(","):
        if not item:
            continue
        t, n = item.split(":")
        saida.append((float(t), corpo[i:i + int(n)]))
        i += int(n)
    return saida


def tocar(quadros: List[Tuple[float, bytes]]) -> Iterator[bytes]:
    """Partes de um multipart/x-mixed-replace, no ritmo em que foram gravados."""
    anterior = None
    for t, j in quadros:
        if anterior is not None:
            time.sleep(max(0.0, min(1.0, t - anterior)))
        anterior = t
        yield (b"--quadro\r\nContent-Type: image/jpeg\r\nContent-Length: "
               + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n")
