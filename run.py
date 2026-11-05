from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
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


# Colunas acrescentadas ao modelo depois que este banco já existia.
#
# `create_all` cria tabela que falta, mas não coluna que falta: num banco já
# povoado ele passa batido e o sistema sobe para quebrar na primeira consulta.
# Como aqui não há Alembic, a costura é esta — idempotente, uma linha por
# coluna, com o DEFAULT que o banco antigo deve assumir.
#
# Manter em ordem cronológica, e nunca remover uma linha: um parque que ficou
# parado dois releases precisa passar por todas elas.
def _colunas_novas() -> list[tuple[str, str, str, str | None]]:
    """tabela, coluna, DDL, e o UPDATE que arruma as linhas que já existiam."""
    import json

    from app.services.vision import ACOES_PADRAO

    # A câmera que já tinha análise de ação ligada vigiava tudo que o sistema
    # sabe detectar — era o que o interruptor significava antes de existir
    # seleção. Herdar essa lista mantém o parque fazendo exatamente o que
    # fazia ontem; deixar em branco apagaria vigilância sem ninguém pedir.
    padrao = json.dumps(list(ACOES_PADRAO))
    return [
        ("cameras", "acoes_ativas", "JSON DEFAULT '[]'",
         f"UPDATE cameras SET acoes_ativas = '{padrao}'"),
        # Desligado em todas: ligar é decisão de quem conhece a sala.
        ("cameras", "alerta_presenca", "BOOLEAN DEFAULT 0", None),
        ("alertas", "snapshot_key", "VARCHAR(200) DEFAULT ''", None),
        # Nulas de propósito: câmera sem janela própria segue a do sistema, e
        # preencher todas com o padrão de hoje congelaria esse padrão nelas.
        ("cameras", "horario_inicio", "VARCHAR(5)", None),
        ("cameras", "horario_fim", "VARCHAR(5)", None),
        # Classes que a câmera precisa pedir. Todas nascem desligadas, mesmo
        # em parque já instalado: ligá-las sozinhas devolveria ao registro o
        # ruído que motivou tirá-las, e em 26 câmeras ninguém notaria a tempo.
        ("cameras", "detecta_bagagem", "BOOLEAN DEFAULT 0", None),
        ("cameras", "detecta_animal", "BOOLEAN DEFAULT 0", None),
        ("cameras", "detecta_celular", "BOOLEAN DEFAULT 0", None),
        ("deteccoes", "placa_snapshot_key", "VARCHAR(300) DEFAULT ''", None),
        ("cameras", "detecta_capacete", "BOOLEAN DEFAULT 0", None),
        ("cameras", "detecta_arma", "BOOLEAN DEFAULT 0", None),
        # Nulas nas que já existiam, e é a resposta certa: não há como saber
        # de que câmera veio uma amostra cadastrada antes desta coluna, e
        # chutar uma falsearia a cobertura que a cota vai consultar.
        ("amostras_faciais", "camera_id", "CHAR(36)", None),
        ("deteccoes", "pessoa_sugerida_id", "CHAR(36)", None),
        # Nula pelo mesmo motivo do horário: câmera sem limite próprio segue
        # o do sistema, e preencher todas com o padrão de hoje o congelaria
        # nelas — mudá-lo depois deixaria de valer justamente para quem nunca
        # quis decidir.
        ("cameras", "limite_aglomeracao", "INTEGER", None),
    ]


async def acrescentar_colunas(conn) -> int:
    """Põe no banco as colunas que o modelo ganhou depois dele. Idempotente."""
    from sqlalchemy import inspect, text

    def existentes(sync_conn, tabela: str) -> set[str]:
        inspetor = inspect(sync_conn)
        if tabela not in inspetor.get_table_names():
            return set()
        return {c["name"] for c in inspetor.get_columns(tabela)}

    acrescentadas = 0
    for tabela, coluna, ddl, preenchimento in _colunas_novas():
        colunas = await conn.run_sync(existentes, tabela)
        # Tabela inexistente não é caso de ALTER: o `create_all` logo acima já
        # a teria criado com a coluna. Coluna presente, idem — é o caminho de
        # todo arranque depois do primeiro.
        if not colunas or coluna in colunas:
            continue
        await conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {ddl}"))
        if preenchimento:
            await conn.execute(text(preenchimento))
        acrescentadas += 1
    return acrescentadas


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
        novas = await acrescentar_colunas(conn)
    print(f"  {VERDE}✓{FIM} Schema pronto" + (f" ({novas} coluna(s) acrescentada(s))" if novas else ""))

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


def sockets_do_laco_local(host: str, porta: int):
    """Escuta nos **dois** laços locais: `::1` e `127.0.0.1`.

    Medido nesta máquina: `localhost` resolve para `::1` antes de `127.0.0.1`,
    e com o servidor só em IPv4 cada conexão nova pagava **2,057 s** esperando
    o IPv6 ser recusado antes de tentar o IPv4. O servidor respondia em 20 ms;
    os dois segundos eram inteiros de handshake.

    O estrago aparecia longe da causa: a ficha biométrica dispara duas
    chamadas em paralelo e só desenha quando as duas voltam, então ela ficava
    no "carregando" para sempre; e salvar uma câmera estourava a paciência do
    navegador antes de a requisição sair. Os dois chegaram como "não abre" e
    "não salva", que não se parecem com um problema de rede.

    Com host de laço local, continua restrito a ele — nada é exposto na rede.

    Com `0.0.0.0` (aberto para a rede, para o link do e-mail abrir em outra
    máquina) o mesmo atraso volta: `0.0.0.0` é só IPv4, e `localhost` nesta
    máquina tenta `::1` primeiro. Por isso esse caso escuta também em `::`.
    Outro host qualquer devolve `None`, e aí quem resolve é o uvicorn.
    """
    import socket

    if host in ("127.0.0.1", "localhost", "::1"):
        enderecos = ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1"))
    elif host in ("0.0.0.0", "::"):
        enderecos = ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0"))
    else:
        return None

    socks = []
    for familia, endereco in enderecos:
        try:
            s = socket.socket(familia, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if familia == socket.AF_INET6:
                # Um soquete por família: sem isto, no Linux o soquete IPv6
                # aceitaria IPv4 mapeado e o bind seguinte falharia.
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((endereco, porta))
            s.listen(2048)
            s.set_inheritable(True)
            socks.append(s)
        except OSError:
            # Máquina sem IPv6, ou aquele laço já ocupado. Seguir com o que
            # deu certo é melhor que não subir: o pior caso é o de hoje.
            continue

    if not socks:
        return None
    return socks


def abrir_navegador(url: str, atraso: float = 2.0) -> None:
    def _abrir():
        time.sleep(atraso)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_abrir, daemon=True).start()


# Chrome e Edge, onde o instalador os deixa no Windows, mais o nome solto
# para quando estiverem no PATH (Linux inclusive).
NAVEGADORES_QUIOSQUE = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "chrome", "google-chrome", "chromium", "msedge",
)


def achar_navegador_quiosque() -> str | None:
    import shutil
    for cand in NAVEGADORES_QUIOSQUE:
        if os.path.isfile(cand):
            return cand
        achado = shutil.which(cand)
        if achado:
            return achado
    return None


def abrir_quiosque(url: str, atraso: float = 2.0) -> bool:
    """Abre o mosaico em tela cheia pelo próprio navegador.

    A tela cheia pedida pela página dispara o aviso de "prima Esc para sair",
    que o navegador não deixa suprimir — e num videowall ligado o dia inteiro
    ele reaparece a cada vez que alguém expande. Entrando por --kiosk a tela
    cheia é do navegador, não da página, e o aviso não existe.
    """
    exe = achar_navegador_quiosque()
    if not exe:
        return False

    # Perfil próprio: com o perfil padrão, se já houver uma janela do Chrome
    # aberta, a nova invocação vira apenas mais uma aba nela e o --kiosk é
    # ignorado. O diretório também guarda a sessão entre reinícios.
    perfil = RAIZ / "data" / "navegador-quiosque"
    perfil.mkdir(parents=True, exist_ok=True)

    def _abrir():
        time.sleep(atraso)
        try:
            subprocess.Popen([
                exe, "--kiosk", f"--app={url}",
                f"--user-data-dir={perfil}",
                "--no-first-run", "--no-default-browser-check",
                # Numa parede de vídeo não há quem responda a um diálogo.
                "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
                "--autoplay-policy=no-user-gesture-required",
            ])
        except Exception:
            webbrowser.open(url)

    threading.Thread(target=_abrir, daemon=True).start()
    return True


def ceder_a_tela() -> None:
    """Põe o servidor abaixo do navegador na fila da CPU (Windows).

    A IA analisa as câmeras sem parar e mantém os 8 núcleos em 100%. Na mesma
    prioridade, o navegador disputava a CPU com ela: o login que o servidor
    responde em 0,25 s levava 2,6 s para aparecer na tela, e o mosaico
    engasgava. Abaixo do normal, a IA continua com toda a CPU que sobra — que
    é quase toda —, mas a tela, o vídeo e o mouse passam na frente.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ABAIXO_DO_NORMAL = 0x4000
        k = ctypes.windll.kernel32
        # O identificador do processo é um ponteiro de 64 bits; sem declarar
        # os tipos, o ctypes o corta para 32 e a chamada falha calada.
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), ABAIXO_DO_NORMAL)
    except Exception:  # noqa: BLE001 — sem isso o sistema funciona igual
        pass


def main() -> None:
    p = argparse.ArgumentParser(
        description="SEGURANÇA — Centro Integrado de Videomonitoramento",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Sem valor padrão aqui, de propósito. Com `default="127.0.0.1"` não há
    # como distinguir "a pessoa pediu o laço local" de "a pessoa não disse
    # nada" — e o `setdefault` abaixo gravava esse palpite na variável de
    # ambiente, que tem prioridade sobre o `.env`. Resultado: `HOST=0.0.0.0`
    # escrito no `.env` era lido pela configuração e **ignorado na prática**,
    # com o servidor escutando só em 127.0.0.1. Configuração morta que parece
    # viva, e aqui ela custava o acesso pela rede.
    p.add_argument("--porta", type=int, default=None)
    p.add_argument("--host", default=None)
    p.add_argument("--reset", action="store_true", help="apaga o banco e recomeça")
    p.add_argument("--sem-simulador", action="store_true", help="não gera detecções automáticas")
    p.add_argument("--sem-navegador", action="store_true", help="não abre o navegador")
    p.add_argument("--quiosque", action="store_true",
                   help="abre em tela cheia pelo navegador, sem o aviso do Esc (videowall)")
    p.add_argument("--recarregar", action="store_true", help="recarrega ao editar o código")
    args = p.parse_args()

    checar_dependencias()
    ceder_a_tela()

    # As variáveis precisam existir antes de `app.config` ser importado. Só
    # o que a linha de comando pediu de fato: o que ela não disse fica para
    # o `.env` responder, que é a ordem de precedência esperada.
    if args.host:
        os.environ["HOST"] = args.host
    if args.porta:
        os.environ["PORT"] = str(args.porta)
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
    faixa("SEGURANÇA — Centro Integrado de Videomonitoramento")

    from app.config import settings
    print(f"  Modo: {settings.modo}")
    if settings.usa_sqlite:
        print(f"  {CINZA}Banco: {settings.DATABASE_URL.split('///')[-1]}{FIM}")
    print()

    semeou = asyncio.run(preparar_banco(args.reset))

    url = settings.url_local
    print()
    faixa()
    print(f"  Interface     {NEGRITO}{AZUL}{url}{FIM}")
    print(f"  API (docs)    {CINZA}{url}/docs{FIM}")
    # O endereço que as *outras* máquinas usam, e que vai no link de
    # redefinição de senha. Sai só quando difere do local: numa instalação
    # de máquina única repetir a mesma linha duas vezes só confunde.
    if settings.url_publica != url:
        print(f"  Na rede       {NEGRITO}{settings.url_publica}{FIM}  "
              f"{CINZA}(APP_URL — é este que vai no e-mail){FIM}")
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
        if args.quiosque and abrir_quiosque(url):
            print(f"  {CINZA}Modo quiosque: Alt+F4 fecha a parede de vídeo.{FIM}")
            print()
        else:
            if args.quiosque:
                print(f"  {CINZA}Chrome ou Edge não encontrado — abrindo em janela normal.{FIM}")
                print()
            abrir_navegador(url)

    import uvicorn

    # Resolvido pela configuração, não pelo argparse: a linha de comando tem
    # precedência (ela já gravou a variável de ambiente acima), o `.env` vem
    # em seguida, e o padrão do `Settings` fecha. Era este elo que faltava
    # para `HOST=0.0.0.0` no `.env` valer de fato.
    host, porta = settings.HOST, settings.PORT

    socks = sockets_do_laco_local(host, porta) if not args.recarregar else None
    if socks:
        config = uvicorn.Config(
            "app.main:app",
            log_level="warning",   # o ciclo de vida já registra o essencial
            access_log=False,
        )
        uvicorn.Server(config).run(sockets=socks)
    else:
        uvicorn.run(
            "app.main:app",
            host=host, port=porta,
            reload=args.recarregar,
            log_level="warning",
            access_log=False,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n{CINZA}  Encerrado.{FIM}\n")

