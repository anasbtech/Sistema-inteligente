"""Tipos de coluna portáteis entre SQLite e PostgreSQL.

O sistema roda em dois modos:

* **SQLite** (padrão) — zero infraestrutura, `python run.py` e pronto. Vetores
  viram JSON e a busca 1:N é força bruta em numpy.
* **PostgreSQL + pgvector** — produção. Vetores viram `vector(512)` e a busca
  usa índice HNSW com distância cosseno.

O resto do código não sabe em qual modo está: pergunta a `dialeto_suporta_vetor()`
quando precisa.
"""
from __future__ import annotations

import json
import uuid
from datetime import timezone
from typing import Any, Optional, Sequence

from sqlalchemy import CHAR, JSON, DateTime, Text, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID


class GUID(TypeDecorator):
    """UUID nativo no PostgreSQL, CHAR(36) no SQLite."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return value if dialect.name == "postgresql" else str(value)

    def process_result_value(self, value: Any, dialect) -> Optional[uuid.UUID]:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class Vetor(TypeDecorator):
    """`vector(N)` do pgvector no PostgreSQL; texto JSON no SQLite."""

    impl = Text
    cache_ok = True

    def __init__(self, dimensoes: int) -> None:
        self.dimensoes = dimensoes
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector
                return dialect.type_descriptor(Vector(self.dimensoes))
            except ImportError:  # pragma: no cover
                pass
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        lista = [float(x) for x in value]
        if len(lista) != self.dimensoes:
            raise ValueError(
                f"Embedding com {len(lista)} dimensões; esperado {self.dimensoes}."
            )
        if dialect.name == "postgresql":
            return lista
        return json.dumps(lista)

    def process_result_value(self, value: Any, dialect) -> Optional[list]:
        if value is None:
            return None
        if isinstance(value, str):
            return json.loads(value)
        return list(value)


class JSONPortatil(TypeDecorator):
    """JSONB no PostgreSQL, JSON comum no SQLite."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class DataHora(TypeDecorator):
    """Instante sempre em UTC e sempre com fuso declarado.

    O SQLite não guarda fuso: grava o texto e devolve naive. Serializado
    assim, `2026-09-21T12:08:25` chega ao navegador sem o `Z` e é lido como
    hora local — o carimbo aparece 3h adiantado em Brasília. Como a trilha
    de auditoria depende do instante exato, a conversão fica aqui, na
    fronteira do banco, e não espalhada pelas rotas.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        # O SQLite ignora tzinfo ao formatar; gravamos o relógio UTC já nu.
        return value if dialect.name == "postgresql" else value.replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def dialeto_suporta_vetor(bind) -> bool:
    """True quando dá para usar o operador `<=>` do pgvector."""
    try:
        nome = bind.dialect.name if hasattr(bind, "dialect") else bind.bind.dialect.name
    except Exception:  # noqa: BLE001
        return False
    if nome != "postgresql":
        return False
    try:
        import pgvector.sqlalchemy  # noqa: F401
        return True
    except ImportError:
        return False


def como_utc(valor):
    """Garante datetime com fuso.

    O PostgreSQL devolve `timestamptz` com tzinfo; o SQLite devolve naive.
    Comparar os dois levanta TypeError, então normalizamos na leitura.
    """
    if valor is None:
        return None
    if valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor


def travar_linha(consulta, bind):
    """`SELECT … FOR UPDATE` onde houver suporte.

    O SQLite serializa escritas por arquivo, então a trava explícita é
    desnecessária lá — e emiti-la geraria erro de sintaxe.
    """
    try:
        nome = bind.dialect.name if hasattr(bind, "dialect") else bind.bind.dialect.name
    except Exception:  # noqa: BLE001
        nome = ""
    return consulta.with_for_update() if nome == "postgresql" else consulta
