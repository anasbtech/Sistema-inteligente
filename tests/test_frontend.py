"""Guardas estáticas da interface.

    pytest -q tests/test_frontend.py

A interface é JavaScript sem build e sem suíte própria, então dois tipos de
erro atravessam toda a revisão e só aparecem no navegador:

* um botão ligado a uma função que não existe — o clique lança
  `ReferenceError` e a tela não faz nada, sem erro visível. Foi o caso de
  "Nova câmera" e do lápis de editar, que chamavam `editarCamera` antes de ela
  ter sido escrita;
* um arquivo que não compila — um backtick perdido dentro de um template
  literal derruba o `.js` inteiro e a tela fica em branco.

Estes testes não substituem teste de interface. Cobrem o que o Python não
alcança e que, sem eles, só o usuário descobre.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
JS = RAIZ / "frontend" / "static" / "js"

# Globais do navegador e construtores usados como função nos handlers.
GLOBAIS = {
    "alert", "atob", "btoa", "clearInterval", "clearTimeout", "confirm",
    "decodeURIComponent", "encodeURIComponent", "fetch", "isNaN", "parseFloat",
    "parseInt", "prompt", "queueMicrotask", "requestAnimationFrame",
    "setInterval", "setTimeout", "structuredClone",
    "Array", "Blob", "Boolean", "CustomEvent", "Date", "Error", "Event",
    "FormData", "Image", "JSON", "Map", "Math", "Number", "Object", "Promise",
    "Set", "String", "URL", "WebSocket",
    "console", "document", "localStorage", "sessionStorage", "window",
}

# Palavras que o regex captura sem serem chamada de função.
NAO_SAO_FUNCOES = {"null", "undefined", "true", "false"}


def _arquivos() -> list[Path]:
    arquivos = sorted(JS.glob("*.js"))
    assert arquivos, f"nenhum .js encontrado em {JS}"
    return arquivos


def _fontes() -> str:
    return "\n".join(f.read_text(encoding="utf-8") for f in _arquivos())


def _declarados(fonte: str) -> set[str]:
    """Tudo que o pacote define: função, const/let/var e método de objeto."""
    nomes = set(re.findall(r"^\s*(?:async\s+)?function\s+(\w+)", fonte, re.M))
    nomes |= set(re.findall(r"^\s*(?:const|let|var)\s+(\w+)\s*=", fonte, re.M))
    nomes |= set(re.findall(r"^\s*(\w+)\s*\([^)]*\)\s*\{", fonte, re.M))
    # Parâmetros de arrow function de um argumento: `cam => …`
    nomes |= set(re.findall(r"(\w+)\s*=>", fonte))
    return nomes


def test_handler_nao_chama_funcao_inexistente():
    """Toda função chamada de um handler tem de existir no pacote."""
    fonte = _fontes()
    declarados = _declarados(fonte)

    chamados: set[str] = set()
    chamados |= set(re.findall(r"=>\s*(\w+)\s*\(", fonte))              # () => nome(...)
    chamados |= set(re.findall(r"on\w+\s*=\s*(\w+)\s*[;,)\}]", fonte))  # onclick = nome

    ausentes = sorted(
        n for n in chamados
        if n not in declarados and n not in GLOBAIS and n not in NAO_SAO_FUNCOES
    )
    assert not ausentes, (
        "handler chama função que não existe em frontend/static/js: "
        + ", ".join(ausentes)
    )


def test_javascript_compila():
    """Cada .js passa pelo parser antes de chegar ao navegador.

    As outras guardas daqui leem o texto, não a gramática, então um arquivo
    quebrado passaria por elas. Pula quando não há Node: a suíte não exige a
    ferramenta, apenas a usa quando existe.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node não encontrado; verificação de sintaxe do JS ignorada")

    falhas = []
    for arquivo in _arquivos():
        r = subprocess.run(
            [node, "--check", str(arquivo)], capture_output=True, text=True
        )
        if r.returncode != 0:
            saida = (r.stderr or "").strip().splitlines()
            detalhe = next(
                (l.strip() for l in saida if "Error" in l),
                saida[0].strip() if saida else "erro sem detalhe",
            )
            falhas.append(f"{arquivo.name}: {detalhe}")

    assert not falhas, "JavaScript com erro de sintaxe:\n" + "\n".join(falhas)


def _corpo_apos(fonte: str, inicio: int) -> str:
    """Do ponto dado até o fim do handler, aproximado pelo fecho `\n  };`.

    Para também no primeiro `abrirModal(`: ele repovoa a caixa, e o que vem
    depois pertence a uma tela nova, onde os elementos existem de novo.
    """
    trecho = fonte[inicio:]
    for marca in ("\n  };", "abrirModal("):
        fim = trecho.find(marca)
        if fim > 0:
            trecho = trecho[:fim]
    return trecho[:2000]


def test_nada_toca_o_modal_depois_de_confirmar():
    """`confirmar` esvazia a caixa do modal ao resolver.

    Ela chama `fecharModal`, que faz `innerHTML = ''` — e a caixa é a mesma
    que a tela por baixo ocupa. Quem perguntar e depois mexer num elemento
    daquela tela recebe `null`.

    Custou caro: o editor de zonas fazia `$('#zonaSalvar').disabled = true`
    logo depois do aviso. Lançava `TypeError` antes do pedido ao servidor,
    então o desenho não era gravado, nenhum erro aparecia na tela e nenhuma
    linha entrava no log. Da interface, parecia que o botão não fazia nada.
    """
    suspeitas = []
    for arquivo in _arquivos():
        fonte = arquivo.read_text(encoding="utf-8")
        for m in re.finditer(r"await confirmar\(", fonte):
            corpo = _corpo_apos(fonte, m.end())
            # Desreferência direta: `$('#id').algo`, sem passar por variável
            # que possa ser conferida antes do uso.
            for d in re.finditer(r"\$\(\s*'#[\w-]+'\s*\)\s*\.", corpo):
                linha = fonte[:m.end()].count("\n") + corpo[:d.start()].count("\n") + 1
                suspeitas.append(f"{arquivo.name}:{linha} — {d.group(0).strip()}")

    assert not suspeitas, (
        "elemento acessado sem guarda depois de `await confirmar` "
        "(a caixa do modal já foi esvaziada):\n  " + "\n  ".join(suspeitas)
    )


def test_salvar_zonas_tolera_o_botao_que_sumiu():
    """A guarda específica do editor de zonas, pelo nome.

    O teste acima pega a desreferência direta; este exige que a variável
    continue sendo conferida, porque é a forma que o editor usa.
    """
    fonte = (JS / "views_op.js").read_text(encoding="utf-8")
    assert re.search(r"const bt = \$\('#zonaSalvar'\);\s*\n\s*if\(bt\)\{", fonte), (
        "o salvamento de zonas voltou a mexer no botão sem conferir se ele "
        "ainda existe — depois de um aviso, não existe"
    )


def test_botoes_de_camera_estao_ligados():
    """Os dois caminhos de cadastro de câmera, explicitamente.

    Uma asserção genérica passaria se alguém apagasse o botão junto com a
    função; estas exigem que o par continue existindo.
    """
    fonte = _fontes()
    assert re.search(r"async function editarCamera\s*\(", fonte), \
        "editarCamera não está definida"
    assert 'id="camNova"' in fonte, "o botão Nova câmera desapareceu"
    assert "editarCamera(null)" in fonte, \
        "Nova câmera não abre o formulário em modo de cadastro"
    assert "data-edit=" in fonte, "o botão de editar câmera desapareceu"
    assert re.search(r"editarCamera\(b\.dataset\.edit\)", fonte), \
        "o lápis de editar não chama editarCamera com o código da câmera"


# ----------------------------------------------------------------------
# O tile que ficava preto para sempre
# ----------------------------------------------------------------------
# Este roda o JavaScript de verdade, no Node, porque o que precisa ser
# verificado é comportamento e não texto: o tile se recupera, ou não.
_ENSAIO_TILE = r"""
const fs = require('fs');
// Globais que o arquivo toca ao ser carregado ou durante o ensaio.
global.window = {devicePixelRatio: 1};
global.document = {hidden: true};
global.performance = {now: () => 0};
global.requestAnimationFrame = () => {};
class ImagemFalsa {
  set src(v) { this._src = v; if (ImagemFalsa.falhar && this.onerror) this.onerror(); }
  get src() { return this._src; }
}
ImagemFalsa.falhar = false;
global.Image = ImagemFalsa;

const fonte = fs.readFileSync(process.argv[2], 'utf8');
eval(fonte + '\nglobal.FEED = FEED;');

FEED.fonteAoVivo = cam => `/stream/${cam.codigo}/mjpeg`;
FEED.fonteSnapshot = cam => `/stream/${cam.codigo}/snapshot`;

const canvas = {width: 0, height: 0, isConnected: true,
                getContext: () => ({}),
                getBoundingClientRect: () => ({width: 320, height: 180})};

// A câmera está muda no instante em que o tile abre — o caso do Elevador.
ImagemFalsa.falhar = true;
FEED.register(canvas, {codigo: 'CAM-018', status: 'ONLINE'}, {});
const t = FEED.tiles[0];
console.log(JSON.stringify({modo: t.modo, temImagem: !!t.img,
                            rodizio: !!FEED._rodizio}));
// O rodízio é um `setInterval`, e o Node não encerra com um pendente.
process.exit(0);
"""


def test_tile_que_perde_o_fluxo_cai_no_rodizio(tmp_path):
    """O tile do Elevador, preto para sempre.

    O fluxo contínuo tinha `img.onerror = () => { t.img = null }` e nada mais
    — o comentário no código dizia literalmente "câmera muda: o tile fica
    preto". Com uma câmera só isso bastava para a tela mentir: a CAM-018
    Elevador leva minutos negociando o RTSP e, enquanto não negocia, o
    servidor recusa o fluxo. O `<img>` recebia o erro uma vez e nada tentava
    de novo, então o tile continuava preto depois de a câmera voltar — medido
    em 29/10/2026, ela falhou 4x entre 10:05:57 e 10:11:08 e abriu em
    seguida, com o tile preto o tempo todo.

    No 4×4 o Elevador é o primeiro tile da página 2, que é onde o fluxo
    contínuo é servido — e por isso era ali que aparecia.

    A recuperação é cair no rodízio de snapshots, que já se cura sozinho.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node não encontrado")

    ensaio = tmp_path / "ensaio.js"
    ensaio.write_text(_ENSAIO_TILE, encoding="utf-8")
    # Com prazo: um temporizador que o ensaio deixe pendente prenderia o Node
    # para sempre, e a suíte inteira ficaria pendurada sem dizer por quê.
    try:
        r = subprocess.run(
            [node, str(ensaio), str(JS / "feeds.js")],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("o ensaio não encerrou: temporizador pendente no feeds.js")
    assert r.returncode == 0, f"o ensaio não rodou:\n{r.stderr}"

    import json
    estado = json.loads(r.stdout.strip().splitlines()[-1])
    assert estado["modo"] == "poll", (
        "o tile que perdeu o fluxo precisa cair no rodízio, que se recupera; "
        f"ficou em {estado['modo']!r} e nunca mais tenta")
    assert estado["rodizio"], "o rodízio tem de estar rodando para o tile voltar"


# ----------------------------------------------------------------------
# Nenhum segredo pode chegar ao log
# ----------------------------------------------------------------------
def test_nenhum_log_escreve_a_url_com_credencial():
    """As 26 câmeras têm a senha de administrador embutida no endereço RTSP.

    O worker escrevia a URL inteira a cada reconexão, e isso não é hipótese:
    `logs/.worker.err.log` tem 4 linhas de setembro com usuário e senha em
    claro. Um log é copiado, anexado a chamado e mandado por e-mail sem
    ninguém pensar duas vezes — é a saída de segredo mais fácil que existe.

    A guarda é textual porque o defeito é textual: passar a URL crua a um
    `log.*`. Quem precisar logar o endereço usa a versão mascarada, que ainda
    diz host, porta e caminho.
    """
    import re
    from pathlib import Path

    suspeitas = []
    for arquivo in sorted((RAIZ / "app").rglob("*.py")) + \
            sorted((RAIZ / "worker").rglob("*.py")):
        if "__pycache__" in str(arquivo):
            continue
        for n, linha in enumerate(
                arquivo.read_text(encoding="utf-8").splitlines(), 1):
            if not re.search(r"\blog\.(debug|info|warning|error|exception|critical)\b",
                             linha):
                continue
            # `url_log` e `mascarar_credencial` são justamente as saídas
            # seguras; `self.url` cru, não.
            if re.search(r"(?<!_)\b(self\.url|rtsp_url|\.rtsp_url)\b", linha):
                suspeitas.append(f"{arquivo.relative_to(RAIZ)}:{n}: {linha.strip()}")

    assert not suspeitas, (
        "endereço RTSP com credencial indo para o log:\n" + "\n".join(suspeitas))


def test_a_mascara_tira_a_credencial_e_preserva_o_diagnostico():
    """Mascarar não pode cegar quem diagnostica: host, porta e caminho são o
    que identifica qual câmera caiu, e nenhum deles é segredo."""
    from app.security import CREDENCIAL_OCULTA, mascarar_credencial

    url = "rtsp://admin:S3nh@Forte@192.168.3.25:554/cam/realmonitor?channel=1"
    saida = mascarar_credencial(url)

    assert "admin" not in saida and "S3nh@Forte" not in saida
    assert CREDENCIAL_OCULTA in saida
    assert "192.168.3.25:554" in saida, "sem host não dá para saber qual câmera é"
    assert "/cam/realmonitor?channel=1" in saida, "o caminho identifica o canal"
    # Endereço sem credencial passa intacto: não há o que esconder.
    limpa = "rtsp://192.168.3.25:554/cam"
    assert mascarar_credencial(limpa) == limpa


def test_run_nao_sobrescreve_o_host_do_env():
    """`HOST` no `.env` era configuração morta que parecia viva.

    `run.py` tinha `--host` com padrão `127.0.0.1` e gravava esse palpite em
    `os.environ` antes de `app.config` carregar — e variável de ambiente tem
    prioridade sobre o arquivo `.env`. Então `HOST=0.0.0.0` escrito no `.env`
    era lido pela configuração e ignorado na prática: o servidor seguia
    escutando só em 127.0.0.1, e nenhuma outra máquina da rede o alcançava.

    Custava o link de redefinição de senha: ele podia apontar para o endereço
    certo e ainda assim não abrir, porque não havia nada escutando ali.
    """
    import re

    fonte = (RAIZ / "run.py").read_text(encoding="utf-8")

    assert re.search(r'add_argument\("--host",\s*default=None\)', fonte), (
        "`--host` voltou a ter padrão próprio; sem `default=None` não há como "
        "distinguir o que a pessoa pediu do que ela não disse")
    assert not re.search(r'os\.environ\.setdefault\(\s*"HOST"', fonte), (
        "`run.py` voltou a impor HOST ao ambiente, anulando o `.env`")
    # E o uvicorn precisa subir com o que a configuração resolveu, não com o
    # argumento cru — é esse elo que faz o `.env` valer.
    assert "host, porta = settings.HOST, settings.PORT" in fonte, (
        "o endereço de escuta deixou de vir da configuração")
    assert not re.search(r"host=args\.host", fonte), (
        "o uvicorn voltou a subir com o argumento cru")
