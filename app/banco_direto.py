"""Conexão síncrona e direta ao banco, para os scripts de linha de comando.

Os scripts (triagem, treino e analisador de veículos, revisão de
identificações) escrevem SQL à mão e rodam fora do servidor. Este módulo
entrega a mesma interface do `sqlite3` nos dois bancos:

* SQLite: a própria conexão `sqlite3`, com espera de 30 s se o arquivo estiver
  ocupado.
* PostgreSQL: `?` vira `%s`, e os valores lidos chegam como chegariam do
  SQLite (UUID e datas como texto, JSON e vetores como texto JSON), para que o
  código que já faz `json.loads` continue igual. Em modo autocommit: um laço
  longo não segura transação aberta, e um comando que falha não trava os
  seguintes.

O SQL dos scripts precisa ser comum aos dois: `where ativo` em vez de
`ativo=1`, nada de PRAGMA fora de `usa_sqlite`.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

from app.config import settings

ERROS_OCUPADO: tuple = (sqlite3.OperationalError,)
try:  # pragma: no cover - depende do driver instalado
    import psycopg

    ERROS_OCUPADO += (psycopg.OperationalError,)
except ImportError:  # pragma: no cover
    psycopg = None


def _como_sqlite(v: Any) -> Any:
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, datetime):
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return v


class _Cursor:
    def __init__(self, cur) -> None:
        self._cur = cur

    def _linha(self, r):
        return None if r is None else tuple(_como_sqlite(v) for v in r)

    def fetchone(self):
        return self._linha(self._cur.fetchone()) if self._cur.description else None

    def fetchall(self):
        return [self._linha(r) for r in self._cur.fetchall()] if self._cur.description else []

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


class _ConexaoPG:
    def __init__(self, url: str) -> None:
        self._c = psycopg.connect(url, autocommit=True, connect_timeout=30)

    def execute(self, sql: str, params: Iterable | None = None) -> _Cursor:
        cur = self._c.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql.replace("%", "%%").replace("?", "%s"), tuple(params))
        return _Cursor(cur)

    def commit(self) -> None:  # autocommit: cada comando já está gravado
        pass

    def close(self) -> None:
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def conectar(timeout: float = 30):
    """Conexão com o banco configurado em DATABASE_URL."""
    if settings.usa_sqlite:
        c = sqlite3.connect(settings.DATABASE_URL.split(":///")[-1], timeout=timeout)
        c.execute(f"pragma busy_timeout = {int(timeout * 1000)}")
        return c
    if psycopg is None:
        raise RuntimeError("PostgreSQL configurado, mas falta o driver: pip install psycopg[binary]")
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    return _ConexaoPG(url)
