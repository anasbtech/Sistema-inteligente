"""Armazenamento dos recortes de captura.

O caminho é derivado do código da detecção, **nunca** do que o worker sugere.
A chave chega pela rede, e usá-la como caminho abriria travessia de diretório
— um worker comprometido poderia escrever em qualquer lugar do disco.

Guardar o recorte é o que permite a revisão humana olhar o rosto antes de
confirmar uma identificação. Por isso o expurgo apaga o arquivo junto com o
registro: imagem que sobrevive ao seu prazo de retenção é vazamento.

Em produção, troque por S3/MinIO com política de ciclo de vida.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from app.config import settings

log = logging.getLogger("seguranca.evidencias")

RAIZ = (Path(__file__).resolve().parents[2] / settings.SNAPSHOT_DIR).resolve()


def chave_de(codigo: str, quando: datetime) -> str:
    """Chave estável e sem caractere de caminho vindo de fora."""
    seguro = "".join(c for c in codigo if c.isalnum() or c in "-_")
    return f"{quando:%Y/%m/%d}/{seguro}.jpg"


def _caminho(chave: str) -> Optional[Path]:
    alvo = (RAIZ / chave).resolve()
    # Mesmo derivando a chave aqui, conferimos: um registro antigo pode
    # carregar chave gravada por outra versão do sistema.
    if not alvo.is_relative_to(RAIZ):
        log.warning("Chave de evidência fora da raiz, recusada: %r", chave)
        return None
    return alvo


def gravar(codigo: str, quando: datetime, jpeg: bytes) -> str:
    chave = chave_de(codigo, quando)
    alvo = _caminho(chave)
    if alvo is None:
        return ""
    alvo.parent.mkdir(parents=True, exist_ok=True)
    alvo.write_bytes(jpeg)
    return chave


def ler(chave: str) -> Optional[bytes]:
    if not chave:
        return None
    alvo = _caminho(chave)
    if alvo is None or not alvo.is_file():
        return None
    return alvo.read_bytes()


def apagar(chave: str) -> bool:
    if not chave:
        return False
    alvo = _caminho(chave)
    if alvo is None or not alvo.is_file():
        return False
    alvo.unlink()
    return True

