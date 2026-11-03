"""Trilha de auditoria em PDF, para leitura, impressão e juntada a processo.

O CSV que havia aqui antes era dado para outro programa consumir. Este é um
documento para alguém ler e arquivar: tem capa com a identificação de quem
exportou e por quê, selo de integridade da cadeia, numeração de páginas e
cabeçalho repetido — o que falta num arquivo de planilha entregue a uma
corregedoria.

Uma perda a registrar: o hash de cada linha tem 64 caracteres e dois deles por
registro não cabem numa página legível, então na tabela eles saem abreviados.
Por isso o documento carrega, na capa, o resultado da verificação da cadeia no
instante da exportação e o hash terminal inteiro — é o que permite confrontar
o PDF com o banco depois. Quem precisar recalcular a cadeia linha a linha
precisa dos dados crus, não deste documento.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Iterable, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

from app.config import settings

# A paisagem é obrigação, não gosto: são dez colunas, e em retrato a descrição
# ficaria com quinze caracteres por linha.
PAGINA = landscape(A4)
MARGEM = 14 * mm

TINTA = colors.HexColor("#101418")
TINTA_FRACA = colors.HexColor("#5A6672")
LINHA = colors.HexColor("#C9D1D9")
ZEBRA = colors.HexColor("#F2F5F7")
CABECALHO = colors.HexColor("#1C2733")

COR_SEV = {
    "err": colors.HexColor("#B3261E"),
    "warn": colors.HexColor("#8A5A00"),
    "ok": colors.HexColor("#1B5E20"),
    "info": TINTA_FRACA,
}
ROTULO_SEV = {"err": "ERRO", "warn": "AVISO", "ok": "OK", "info": "INFO"}

# Soma 728pt; a página em paisagem oferece 842 menos as margens.
COLUNAS = [
    ("Nº",          30),
    ("Data e hora", 74),
    ("Ação",        76),
    ("Sev.",        36),
    ("Descrição",  142),
    ("Usuário",     92),
    ("Alvo",        76),
    ("Finalidade",  88),
    ("IP",          58),
    ("Hash",        56),
]

_normal = ParagraphStyle("n", fontName="Helvetica", fontSize=6.6, leading=8.2,
                         textColor=TINTA, alignment=TA_LEFT)
_titulo = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=15, leading=19,
                         textColor=TINTA)
_rotulo = ParagraphStyle("r", fontName="Helvetica", fontSize=7.4, leading=10,
                         textColor=TINTA_FRACA)
_valor = ParagraphStyle("v", fontName="Helvetica-Bold", fontSize=8.6, leading=11.5,
                        textColor=TINTA)
_nota = ParagraphStyle("o", fontName="Helvetica", fontSize=7, leading=9.6,
                       textColor=TINTA_FRACA)
_mono = ParagraphStyle("mono", fontName="Courier", fontSize=6.2, leading=8,
                       textColor=TINTA_FRACA)

# Um estilo por severidade, criado uma vez. Instanciar ParagraphStyle dentro do
# laço custava dois objetos por linha — dois mil e seiscentos numa trilha de mil
# e trezentas — e era o segundo peso da geração, depois da montagem dupla.
_ESTILO_SEV = {
    chave: ParagraphStyle(f"sev_{chave}", parent=_normal, fontName="Helvetica-Bold",
                          textColor=cor)
    for chave, cor in COR_SEV.items()
}
_CABECALHO_CEL = ParagraphStyle("cab", fontName="Helvetica-Bold", fontSize=6.8,
                                leading=8.4, textColor=colors.white)


VAZIO = "—"


def _escapar(texto) -> str:
    """O Paragraph lê marcação, e uma descrição com "<" abortaria a geração do
    documento inteiro."""
    s = "" if texto is None else str(texto)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _p(texto, estilo=_normal) -> Paragraph:
    """Texto de célula que precisa quebrar em várias linhas."""
    return Paragraph(_escapar(texto) or VAZIO, estilo)


def _linhas(partes, estilo=_normal) -> Paragraph:
    """Várias linhas numa célula.

    Cada parte é escapada antes de entrar, e só então o `<br/>` é posto entre
    elas — escapando o texto já montado, a própria quebra virava `&lt;br/&gt;`
    e o documento mostrava "Ana Caroline<br/>ana@exemplo" em todas as mil e
    trezentas linhas.
    """
    return Paragraph("<br/>".join(_escapar(p) for p in partes if p) or VAZIO, estilo)


def _cel(texto, largura: float, estilo=_normal):
    """Devolve string crua quando o texto cabe na coluna; Paragraph quando não.

    Paragraph é caro: o reportlab reposiciona cada um três vezes para calcular
    a altura da linha, e a quebra de linha respondia por dezoito dos vinte e
    sete segundos da geração. Mas trocar todos por string não serve — uma
    descrição longa, em string, transborda a coluna e invade a vizinha.

    Quem decide é a medida do texto na fonte, não um palpite sobre qual coluna
    costuma ser curta: campo livre é campo livre, e um dia vem o alvo de cem
    caracteres na coluna que sempre teve doze.
    """
    s = "" if texto is None else str(texto)
    if not s:
        # Travessão, e não branco: em documento oficial, célula vazia deixa a
        # dúvida entre "não havia valor" e "faltou imprimir".
        return VAZIO
    if stringWidth(s, estilo.fontName, estilo.fontSize) <= largura - 8:
        return s
    return _p(s, estilo)


class _CanvasNumerado(pdfcanvas.Canvas):
    """Carimba cabeçalho e rodapé sabendo o total de páginas.

    O caminho óbvio — montar o documento duas vezes, uma para contar e outra
    para escrever — custava quinze dos dezessete segundos de uma trilha de mil
    e trezentas linhas, porque o layout inteiro era refeito. Aqui as páginas
    são guardadas em memória durante a única passada e só recebem a tinta das
    margens no `save()`, quando o total já é conhecido.
    """

    def __init__(self, *args, titulo="", subtitulo="", **kw):
        super().__init__(*args, **kw)
        self._titulo = titulo
        self._subtitulo = subtitulo
        self._paginas = []

    def showPage(self):                                  # noqa: N802 (API do reportlab)
        self._paginas.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._paginas)
        for estado in self._paginas:
            self.__dict__.update(estado)
            self._carimbar(total)
            super().showPage()
        super().save()

    def _carimbar(self, total: int):
        largura, altura = PAGINA
        self.saveState()

        self.setFont("Helvetica-Bold", 8)
        self.setFillColor(TINTA)
        self.drawString(MARGEM, altura - MARGEM - 1, self._titulo)
        self.setFont("Helvetica", 7.4)
        self.setFillColor(TINTA_FRACA)
        self.drawRightString(largura - MARGEM, altura - MARGEM - 1, self._subtitulo)
        self.setStrokeColor(LINHA)
        self.setLineWidth(0.5)
        self.line(MARGEM, altura - MARGEM - 6, largura - MARGEM, altura - MARGEM - 6)

        self.line(MARGEM, MARGEM + 12, largura - MARGEM, MARGEM + 12)
        self.setFont("Helvetica", 7)
        self.drawString(MARGEM, MARGEM + 3,
                        f"{settings.APP_NAME} · documento gerado pelo sistema")
        self.drawRightString(largura - MARGEM, MARGEM + 3,
                             f"Página {self._pageNumber} de {total}")
        self.restoreState()


def _documento(buffer, titulo: str, subtitulo: str) -> BaseDocTemplate:
    doc = BaseDocTemplate(
        buffer, pagesize=PAGINA,
        leftMargin=MARGEM, rightMargin=MARGEM,
        topMargin=MARGEM + 9 * mm, bottomMargin=MARGEM + 6 * mm,
        title=titulo, author=settings.APP_NAME, subject=subtitulo,
    )
    quadro = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height,
                   id="corpo", leftPadding=0, rightPadding=0,
                   topPadding=0, bottomPadding=0)
    doc.addPageTemplates([PageTemplate(id="padrao", frames=[quadro])])
    return doc


def _capa(meta: dict) -> list:
    """Identificação do documento: o que um auditor confere antes de ler."""
    linhas = [
        ("Período dos registros", meta["periodo"]),
        ("Recorte aplicado", meta["recorte"]),
        ("Registros no documento", f"{meta['total']:,}".replace(",", ".")),
        ("Finalidade declarada", meta["finalidade"]),
        ("Exportado por", meta["operador"]),
        ("Data da exportação", meta["quando"]),
    ]
    celulas = [[_p(r, _rotulo) for r, _ in linhas], [_p(v, _valor) for _, v in linhas]]
    larguras = [(PAGINA[0] - 2 * MARGEM) / len(linhas)] * len(linhas)
    ficha = Table(celulas, colWidths=larguras)
    ficha.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 1), (-1, 1), 0.5, LINHA),
    ]))

    integra = meta["integra"]
    selo = Table([[
        _p("INTEGRIDADE DA CADEIA", _rotulo),
        _p("ÚLTIMO HASH DA CADEIA (SHA-256)", _rotulo),
    ], [
        Paragraph(
            "Cadeia íntegra na conferência" if integra
            else "CADEIA COMPROMETIDA — ver mensagem abaixo",
            ParagraphStyle("selo", parent=_valor,
                           textColor=COR_SEV["ok"] if integra else COR_SEV["err"])),
        Paragraph(meta["hash_final"] or "—",
                  ParagraphStyle("hashfim", fontName="Courier", fontSize=7.6,
                                 leading=10, textColor=TINTA)),
    ]], colWidths=[210, PAGINA[0] - 2 * MARGEM - 210])
    selo.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (-1, -1), ZEBRA),
        ("BOX", (0, 0), (-1, -1), 0.5, LINHA),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, 0), 6),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 6),
    ]))

    corpo = [
        Paragraph("Trilha de auditoria", _titulo),
        Spacer(1, 2),
        Paragraph(
            "Registros encadeados por hash e somente-inserção. Cada linha carrega o hash "
            "da anterior, de modo que alterar ou remover qualquer uma delas quebra a cadeia "
            "de todas as seguintes.", _nota),
        Spacer(1, 7),
        ficha,
        Spacer(1, 9),
        selo,
    ]
    if not integra:
        corpo += [Spacer(1, 5),
                  Paragraph(meta["mensagem"],
                            ParagraphStyle("msg", parent=_nota, textColor=COR_SEV["err"]))]
    corpo += [
        Spacer(1, 5),
        Paragraph(
            "Na tabela, a coluna <b>Hash</b> traz os dez primeiros caracteres do hash de "
            "cada registro, o bastante para localizar a linha na base. A conferência "
            "automática da cadeia, linha a linha, exige os dados crus — não este documento.",
            _nota),
        Spacer(1, 10),
    ]
    return corpo


def _tabela(linhas: Iterable, fuso) -> Table:
    larg = [w for _, w in COLUNAS]
    dados = [[Paragraph(t, _CABECALHO_CEL) for t, _ in COLUNAS]]
    estilo_linhas = []
    for n, a in enumerate(linhas, start=1):
        quando = a.ocorrido_em.astimezone(fuso) if a.ocorrido_em.tzinfo else a.ocorrido_em
        sev = (a.severidade or "info").lower()
        dados.append([
            str(a.id),
            f"{quando:%d/%m/%y} {quando:%H:%M:%S}",
            _cel(a.acao, larg[2]),
            ROTULO_SEV.get(sev, sev.upper()),
            _cel(a.descricao, larg[4]),
            # Nome e e-mail em linhas separadas: o nome é o que se procura, o
            # e-mail é o que identifica sem ambiguidade, e lado a lado nenhum
            # dos dois caberia na coluna.
            (_linhas([a.usuario_nome, a.usuario_email]) if a.usuario_email
             else _cel(a.usuario_nome, larg[5])),
            _cel(a.alvo, larg[6]),
            _cel(a.finalidade, larg[7]),
            _cel(a.ip, larg[8]),
            (a.hash_atual or "")[:10],
        ])
        if n % 2 == 0:
            estilo_linhas.append(("BACKGROUND", (0, n), (-1, n), ZEBRA))
        # Só o que foge do comum ganha cor. Pintar também as linhas "info" —
        # a maioria esmagadora — encheria o estilo de milhares de comandos sem
        # destacar nada, que é o oposto do que a cor serve para fazer.
        if sev in ("err", "warn", "ok"):
            estilo_linhas.append(("TEXTCOLOR", (3, n), (3, n), COR_SEV[sev]))

    tabela = Table(dados, colWidths=larg, repeatRows=1)
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), CABECALHO),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        # Vale para as células que são texto puro; as que viraram Paragraph
        # carregam o próprio estilo.
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 6.6),
        ("LEADING", (0, 1), (-1, -1), 8.2),
        ("TEXTCOLOR", (0, 1), (-1, -1), TINTA),
        ("FONTNAME", (3, 1), (3, -1), "Helvetica-Bold"),
        ("FONTNAME", (9, 1), (9, -1), "Courier"),
        ("FONTSIZE", (9, 1), (9, -1), 6.2),
        ("TEXTCOLOR", (9, 1), (9, -1), TINTA_FRACA),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("GRID", (0, 0), (-1, -1), 0.25, LINHA),
        ("ALIGN", (0, 0), (0, -1), "RIGHT"),
    ] + estilo_linhas))
    return tabela


def gerar_pdf(linhas: list, meta: dict, fuso=None) -> bytes:
    """Monta o documento. Síncrono e pesado — o chamador roda fora do loop."""
    buffer = io.BytesIO()
    titulo = f"Trilha de auditoria · {settings.APP_NAME}"
    subtitulo = f"Emitido em {meta['quando']} · {meta['operador']}"
    doc = _documento(buffer, titulo, subtitulo)

    historia = _capa(meta)
    if linhas:
        historia.append(_tabela(linhas, fuso))
    else:
        historia.append(Paragraph("Nenhum registro no recorte selecionado.", _nota))

    def fabricar(*args, **kw):
        return _CanvasNumerado(*args, titulo=titulo, subtitulo=subtitulo, **kw)

    doc.build(historia, canvasmaker=fabricar)
    return buffer.getvalue()
