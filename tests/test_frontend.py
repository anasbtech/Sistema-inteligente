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
