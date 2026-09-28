#!/usr/bin/env python3
"""VIGIA — ponto de entrada único.

    python run.py

Cria o banco na primeira execução, popula com dados de demonstração e sobe o
servidor com a interface e a API no mesmo endereço. Nada de Docker, Postgres
ou Redis para começar.

Opções:
    python run.py --porta 9000      outra porta
    python run.py --reset           apaga o banco e recomeça
    python run.py --sem-simulador   não gera detecções automáticas
    python run.py --sem-navegador   não abre o navegador
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

VERDE, AMARELO, AZUL, CINZA, NEGRITO, FIM = (
    "\033[32m", "\033[33m", "\033[36m", "\033[90m", "\033[1m", "\033[0m"
)
if os.name == "nt" or not sys.stdout.isatty():
    VERDE = AMARELO = AZUL = CINZA = NEGRITO = FIM = ""


def faixa(texto: str = "") -> None:
    print(f"{CINZA}{'─' * 64}{FIM}")
    if texto:
        print(f"  {NEGRITO}{texto}{FIM}")
        print(f"{CINZA}{'─' * 64}{FIM}")


def checar_dependencias() -> None:
    faltando = []
    for modulo, pacote in [
        ("fastapi", "fastapi"), ("uvicorn", "uvicorn[standard]"), ("websockets", "websockets"),
        ("sqlalchemy", "SQLAlchemy"), ("aiosqlite", "aiosqlite"),
        ("pydantic_settings", "pydantic-settings"), ("jwt", "PyJWT"),
        ("passlib", "passlib[argon2]"), ("numpy", "numpy"),
        ("email_validator", "email-validator"), ("multipart", "python-multipart"),
    ]:
        try:
            __import__(modulo)
        except ImportError:
            faltando.append(pacote)

    if faltando:
        print(f"\n{AMARELO}Faltam dependências:{FIM} {', '.join(faltando)}\n")
        print(f"  {NEGRITO}pip install -r config/requirements-min.txt{FIM}\n")
        print(f"{CINZA}  (config/requirements.txt traz também os modelos de visão, que são opcionais){FIM}\n")
        sys.exit(1)


async def preparar_banco(reset: bool) -> bool:
    """Cria o schema e semeia. Devolve True se semeou agora."""
    from sqlalchemy import func, select

    from app.config import settings
    from app.database import db_context, engine
    from app.models import Base, Usuario

    caminho = settings.DATABASE_URL.split("///")[-1] if settings.usa_sqlite else None
    if reset and caminho and Path(caminho).exists():
        for sufixo in ("", "-wal", "-shm"):
            arq = Path(caminho + sufixo)
            if arq.exists():
                arq.unlink()
        print(f"  {AMARELO}✓{FIM} Banco anterior removido")

    async with engine.begin() as conn:
        if not settings.usa_sqlite:
            from sqlalchemy import text
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    print(f"  {VERDE}✓{FIM} Schema pronto")

    async with db_context() as db:
        ja_tem = int((await db.execute(select(func.count()).select_from(Usuario))).scalar_one())

    if ja_tem:
        print(f"  {VERDE}✓{FIM} Banco já populado ({ja_tem} usuário(s))")
        return False

    print(f"  {AZUL}…{FIM} Populando dados de demonstração", end="", flush=True)
    from scripts.seed import semear
    await semear(silencioso=True)
    print(f"\r  {VERDE}✓{FIM} Dados de demonstração criados          ")
    return True


DEMONSTRACAO = [
    ("admin@pa.com.br", "Admin@2026", "Administrador"),
    ("supervisor@pm.gov.br", "Super@2026", "Supervisor"),
    ("operador@pm.gov.br", "Oper@2026", "Operador"),
    ("analista@pm.gov.br", "Anal@2026", "Analista"),
    ("auditoria@mp.gov.br", "Audi@2026", "Auditor"),
]


async def _contas_de_demonstracao() -> list[tuple[str, str, str]]:
    """Das contas de demonstração, as que de fato existem e estão ativas."""
    from sqlalchemy import select

    from app.database import db_context, engine
    from app.models import Usuario

    async with db_context() as db:
        vivos = set(
            (
                await db.execute(
                    select(Usuario.email).where(
                        Usuario.email.in_([e for e, _, _ in DEMONSTRACAO]),
                        Usuario.ativo.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    await engine.dispose()
    return [c for c in DEMONSTRACAO if c[0] in vivos]


def abrir_navegador(url: str, atraso: float = 2.0) -> None:
    def _abrir():
        time.sleep(atraso)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_abrir, daemon=True).start()


def main() -> None:
    p = argparse.ArgumentParser(
        description="VIGIA — Centro Integrado de Videomonitoramento",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--porta", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--reset", action="store_true", help="apaga o banco e recomeça")
    p.add_argument("--sem-simulador", action="store_true", help="não gera detecções automáticas")
    p.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    p.add_argument("--recarregar", action="store_true", help="recarrega ao editar o código")
    args = p.parse_args()

    checar_dependencias()

    # As variáveis precisam existir antes de `app.config` ser importado.
    os.environ.setdefault("HOST", args.host)
    os.environ.setdefault("PORT", str(args.porta))
    if args.sem_simulador:
        os.environ["SIMULADOR"] = "false"
    # Chave estável entre execuções, para a sessão do navegador sobreviver
    # a um restart. Em produção, defina SECRET_KEY no .env.
    if not os.environ.get("SECRET_KEY"):
        secrets_dir = RAIZ / ".secrets"
        secrets_dir.mkdir(exist_ok=True)
        arq = secrets_dir / ".secret_key"
        if not arq.exists():
            import secrets
            arq.write_text(secrets.token_urlsafe(48))
            try:
                arq.chmod(0o600)
            except Exception:
                pass
        os.environ["SECRET_KEY"] = arq.read_text().strip()

    print()
    faixa("VIGIA — Centro Integrado de Videomonitoramento")

    from app.config import settings
    print(f"  Modo: {settings.modo}")
    if settings.usa_sqlite:
        print(f"  {CINZA}Banco: {settings.DATABASE_URL.split('///')[-1]}{FIM}")
    print()

    semeou = asyncio.run(preparar_banco(args.reset))

    url = f"http://{args.host}:{args.porta}"
    print()
    faixa()
    print(f"  Interface     {NEGRITO}{AZUL}{url}{FIM}")
    print(f"  API (docs)    {CINZA}{url}/docs{FIM}")
    print()
    if semeou or settings.ENV == "development":
        # Filtrado pelo que existe: uma conta apagada não deve seguir aparecendo
        # aqui com a senha ao lado, num convite que falha no login.
        existentes = asyncio.run(_contas_de_demonstracao())
        if existentes:
            print(f"  {NEGRITO}Credenciais de demonstração{FIM}")
            for email, senha, perfil in existentes:
                print(f"    {email:<26} {VERDE}{senha:<12}{FIM} {CINZA}{perfil}{FIM}")
            print()
    if settings.SIMULADOR:
        print(f"  {CINZA}O simulador gera detecções continuamente. Desligue com --sem-simulador.{FIM}")
    print(f"  {CINZA}Ctrl+C para encerrar.{FIM}")
    faixa()
    print()

    if not args.sem_navegador:
        abrir_navegador(url)

    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=args.host, port=args.porta,
        reload=args.recarregar,
        log_level="warning",       # o ciclo de vida já registra o essencial
        access_log=False,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{CINZA}  Encerrado.{FIM}\n")

