"""Copia o banco SQLite (data/seguranca.db) para o PostgreSQL.

    py -3.13 -m scripts.migrar_para_postgres --destino postgresql+asyncpg://seguranca:SENHA@127.0.0.1:5432/seguranca

* Lê de uma cópia consistente do arquivo (API de backup do SQLite), então pode
  rodar com o servidor ligado. Para a troca definitiva, rode com o servidor
  desligado, senão o que for gravado depois da cópia fica para trás.
* Recria as tabelas no destino a partir dos modelos (`--recriar` apaga antes).
* Cada valor passa pelos tipos do próprio sistema (GUID, DataHora, Vetor,
  JSONPortatil), que já sabem converter entre os dois bancos.
* No fim acerta os contadores de id, aplica sql/01_postgres.sql e confere a
  contagem de cada tabela. O arquivo SQLite não é alterado.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

LOTE = 2000


def fotografar(origem: Path) -> Path:
    destino = Path(tempfile.mkdtemp(prefix="migrar_pg_")) / "copia.db"
    with sqlite3.connect(f"file:{origem}?mode=ro", uri=True) as src, sqlite3.connect(destino) as dst:
        src.backup(dst)
    return destino


async def migrar(origem: Path, destino_url: str, recriar: bool) -> int:
    from sqlalchemy import Integer, BigInteger, func, select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models import Base

    copia = fotografar(origem)
    print(f"Cópia consistente de {origem.name}: {copia}")
    src = create_async_engine(f"sqlite+aiosqlite:///{copia}")
    dst = create_async_engine(destino_url)

    async with dst.begin() as c:
        await c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        if recriar:
            await c.execute(text("DROP VIEW IF EXISTS vw_esquema_identificadores"))
            await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)

    async with src.connect() as c:
        tabelas_origem = {
            r[0] for r in (await c.execute(text("select name from sqlite_master where type='table'")))
        }

    problemas = 0
    contagens: dict[str, int] = {}
    pulos: dict[str, int] = {}
    for tabela in Base.metadata.sorted_tables:
        if tabela.name not in tabelas_origem:
            print(f"  {tabela.name:<28} (não existe no SQLite; fica vazia)")
            continue
        async with src.connect() as c:
            cols_origem = {r[1] for r in await c.execute(text(f'PRAGMA table_info("{tabela.name}")'))}
        colunas = [col for col in tabela.columns if col.name in cols_origem]
        async with dst.connect() as c:
            if (await c.execute(select(func.count()).select_from(tabela))).scalar_one():
                print(f"  {tabela.name}: destino já tem dados. Use --recriar.")
                return 1

        # Referências órfãs (o SQLite não as barrou): o vínculo fica vazio, como
        # num ON DELETE SET NULL; se a coluna não aceita vazio, a linha fica de fora.
        refs = {}
        for col in colunas:
            for fk in col.foreign_keys:
                alvo = fk.column
                async with dst.connect() as c:
                    refs[col.name] = (col.nullable, {r[0] for r in await c.execute(select(alvo))})
        orfas: dict[str, int] = {}
        puladas = 0

        t0, total = time.time(), 0
        async with src.connect() as cs:
            res = await cs.stream(select(*colunas))
            async for parte in res.partitions(LOTE):
                linhas = []
                for r in parte:
                    d = dict(zip([col.name for col in colunas], r))
                    ok = True
                    for nome, (anulavel, validos) in refs.items():
                        if d[nome] is not None and d[nome] not in validos:
                            orfas[nome] = orfas.get(nome, 0) + 1
                            if anulavel:
                                d[nome] = None
                            else:
                                ok = False
                    if ok:
                        linhas.append(d)
                    else:
                        puladas += 1
                if not linhas:
                    continue
                async with dst.begin() as cd:
                    await cd.execute(tabela.insert(), linhas)
                total += len(linhas)
        contagens[tabela.name] = total + puladas
        nota = "".join(f"; {n} vínculo(s) órfão(s) em {c}" for c, n in orfas.items())
        nota += f"; {puladas} linha(s) sem o registro pai, não copiadas" if puladas else ""
        print(f"  {tabela.name:<28} {total:>7} linhas  ({time.time() - t0:.1f} s{nota})")
        pulos[tabela.name] = puladas

    async with dst.begin() as c:
        for tabela in Base.metadata.sorted_tables:
            pk = list(tabela.primary_key.columns)
            if len(pk) == 1 and isinstance(pk[0].type, (Integer, BigInteger)) and pk[0].autoincrement is not False:
                seq = (await c.execute(text("select pg_get_serial_sequence(:t, :c)"),
                                       {"t": tabela.name, "c": pk[0].name})).scalar()
                if seq:
                    await c.execute(text(
                        f'select setval(:s, coalesce((select max("{pk[0].name}") from "{tabela.name}"), 0) + 1, false)'
                    ), {"s": seq})

    extras = (RAIZ / "sql" / "01_postgres.sql").read_text(encoding="utf-8-sig")
    import asyncpg
    url = destino_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        await conn.execute(extras)
    finally:
        await conn.close()
    print("  sql/01_postgres.sql aplicado (índices HNSW, auditoria imutável, formato do ID)")

    print("\nConferência (SQLite x PostgreSQL):")
    async with src.connect() as cs, dst.connect() as cd:
        for nome, copiadas in contagens.items():
            tabela = Base.metadata.tables[nome]
            a = (await cs.execute(select(func.count()).select_from(tabela))).scalar_one()
            b = (await cd.execute(select(func.count()).select_from(tabela))).scalar_one()
            ok = a == b + pulos.get(nome, 0)
            problemas += not ok
            extra = f"  ({pulos[nome]} sem o registro pai)" if pulos.get(nome) else ""
            print(f"  {'ok ' if ok else 'ERRO'} {nome:<28} {a:>7} {b:>7}{extra}")
    await src.dispose()
    await dst.dispose()
    print("\nTudo conferido." if not problemas else f"\n{problemas} tabela(s) com contagem diferente.")
    return 1 if problemas else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origem", default=str(RAIZ / "data" / "seguranca.db"))
    ap.add_argument("--destino", required=True, help="postgresql+asyncpg://usuario:senha@host:porta/banco")
    ap.add_argument("--recriar", action="store_true", help="apaga as tabelas do destino antes")
    a = ap.parse_args()
    sys.exit(asyncio.run(migrar(Path(a.origem), a.destino, a.recriar)))


if __name__ == "__main__":
    main()
