"""Testa com vídeo gravado se o sistema detecta cada ação.

Passa o vídeo pelo **mesmo caminho da câmera ao vivo** — detector YOLO,
rastreio por posição e `AnalisadorAcoes` —, uma ação por vez, e diz se ela
disparou e em que segundo do vídeo. Nada é gravado no banco nem vira alerta.

Um vídeo, uma ação:

    py -3.13 -m scripts.testar_acoes_video meu_video.mp4 --acao roubo

Uma bateria inteira (o que serve para apresentar resultado):

    py -3.13 -m scripts.testar_acoes_video --pasta videos_teste

    videos_teste/
      roubo/
        deve_detectar/        vídeos em que a ação ACONTECE
        nao_deve_detectar/    vídeos parecidos em que ela NÃO acontece
      queda/
        deve_detectar/
        nao_deve_detectar/
      ...

    O nome de cada pasta é o código da ação (veja --listar). Vídeo solto
    direto na pasta da ação conta como "deve_detectar".

Ver as ações e os códigos:

    py -3.13 -m scripts.testar_acoes_video --listar

O resultado vai para `resultados_teste/<data-hora>/`: um relatório HTML com
foto de cada disparo, uma planilha CSV e, para cada vídeo, uma cópia anotada
com as caixas e o momento do disparo.

Os vídeos "nao_deve_detectar" importam tanto quanto os outros: uma ação que
dispara em tudo acerta todos os positivos e não serve para nada.
"""
from __future__ import annotations

import argparse
import csv
import html
import sys
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import numpy as np

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import chama, extras, vision  # noqa: E402

EXTENSOES = {".mp4", ".avi", ".mkv", ".mov", ".m4v", ".wmv", ".webm"}

# Quadros por segundo do vídeo que entram na análise. ~0,22 s entre quadros
# é o espaçamento da rajada ao vivo, que é o que as ações de movimento
# esperam. Analisar todos os quadros seria mais lento e *diferente* do que a
# câmera faz.
AMOSTRAS_POR_S = 4.5

# Direções aceitas em --direcao (o sentido PERMITIDO no contrafluxo).
DIRECOES = {"direita": [1, 0], "esquerda": [-1, 0], "baixo": [0, 1], "cima": [0, -1]}

# Estas dependem de zona desenhada. Sem --zonas, o teste usa uma zona padrão
# que cobre o quadro — veja `zonas_padrao`.
PRECISAM_DE_ZONA = {"invasao", "contrafluxo", "area_restrita", "veiculo_proibido"}

# Relógio do analisador = este valor + segundo do vídeo. O analisador guarda
# "último alerta" começando em zero; com o vídeo também começando em zero, o
# primeiro disparo parecia vir colado a um anterior e caía no cooldown.
T0 = 1_000_000.0


# ----------------------------------------------------------------------
# Configuração do teste
# ----------------------------------------------------------------------
def zonas_padrao(acao: str, direcao: str) -> list:
    """Zona que torna a ação testável sem desenhar nada.

    Cobrem o quadro inteiro, então o que se testa é a detecção em si — se a
    pessoa (ou o veículo) é vista e se o movimento é medido. Para testar uma
    zona específica, passe --zonas com o JSON dela.
    """
    tudo = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
    if acao == "area_restrita":
        return [{"nome": "Quadro inteiro", "tipo": "POLIGONO", "pontos": tudo,
                 "regras": {"restrita": True}}]
    if acao == "veiculo_proibido":
        return [{"nome": "Quadro inteiro", "tipo": "POLIGONO", "pontos": tudo,
                 "regras": {"veiculo_proibido": True}}]
    if acao == "contrafluxo":
        return [{"nome": "Quadro inteiro", "tipo": "POLIGONO", "pontos": tudo,
                 "regras": {"contrafluxo": True, "direcao": DIRECOES[direcao]}}]
    if acao == "invasao":
        # Linha vertical no meio, nos dois sentidos: cruzar para qualquer
        # lado conta. Para um sentido só, use --zonas.
        meio = [[0.5, 0.0], [0.5, 1.0]]
        return [{"nome": "Linha central", "tipo": "LINHA", "pontos": meio,
                 "regras": {"invasao": True}},
                {"nome": "Linha central", "tipo": "LINHA", "pontos": meio[::-1],
                 "regras": {"invasao": True}}]
    return []


def preparar_analisador(acao: str, args) -> vision.AnalisadorAcoes:
    an = vision.AnalisadorAcoes()
    an.acoes_ativas = {acao}
    an.ttl_trilha = 8.0
    if args.permanencia_s is not None:
        an.seg_permanencia = args.permanencia_s
    if args.abandono_s is not None:
        an.seg_abandono = args.abandono_s
    if args.limite_aglomeracao is not None:
        an.n_aglomeracao = args.limite_aglomeracao
    if acao == "fora_de_horario":
        # O vídeo foi gravado em outra hora; o que se testa é se a pessoa é
        # vista. Uma janela que exclui o agora faz qualquer pessoa contar
        # como fora de horário.
        daqui_a = datetime.now() + timedelta(hours=6)
        an.horario_inicio = daqui_a.strftime("%H:%M")
        an.horario_fim = (daqui_a + timedelta(minutes=1)).strftime("%H:%M")
    return an


# ----------------------------------------------------------------------
# Execução
# ----------------------------------------------------------------------
# Folga para casar um disparo com um trecho do gabarito. Antes do início: o
# gabarito marca quando a anomalia fica visível, e o arranque pode vir um
# instante antes. Depois do fim: a ação exige persistência, então o disparo
# chega depois de o corpo já ter acelerado.
FOLGA_ANTES_S = 0.5
FOLGA_DEPOIS_S = 1.5


@dataclass
class Disparo:
    segundo: float
    rotulo: str
    confianca: float
    foto: str = ""
    # Preenchidos quando há gabarito: "acerto", "outro" ou "falso".
    situacao: str = ""
    contexto: str = ""


@dataclass
class Janela:
    """Um trecho do gabarito: quando algo acontece no vídeo."""
    inicio_s: float
    fim_s: float
    tipo: str            # código da ação testada, ou "outro"
    descricao: str = ""
    detectado_em: Optional[float] = None

    def contem(self, t: float) -> bool:
        return self.inicio_s - FOLGA_ANTES_S <= t <= self.fim_s + FOLGA_DEPOIS_S


@dataclass
class Resultado:
    acao: str
    video: Path
    esperado: Optional[bool]          # None = vídeo avulso, sem gabarito
    disparos: List[Disparo] = field(default_factory=list)
    quadros: int = 0
    duracao_s: float = 0.0
    tempo_s: float = 0.0
    anotado: str = ""
    erro: str = ""
    conjunto: str = ""
    janelas: Optional[List[Janela]] = None   # None = sem gabarito por trecho

    @property
    def disparou(self) -> bool:
        return bool(self.disparos)

    @property
    def alvos(self) -> List[Janela]:
        return [j for j in (self.janelas or []) if j.tipo == self.acao]

    @property
    def falsos(self) -> List[Disparo]:
        if self.janelas is None:
            return [] if self.esperado is not False else list(self.disparos)
        return [d for d in self.disparos if d.situacao != "acerto"]

    def avaliar(self) -> None:
        """Casa cada disparo com o gabarito, quando há gabarito por trecho."""
        if self.janelas is None:
            if self.esperado is False:
                for d in self.disparos:
                    d.situacao, d.contexto = "falso", "vídeo sem a ação"
            return
        for d in self.disparos:
            alvo = next((j for j in self.alvos if j.contem(d.segundo)), None)
            if alvo is not None:
                d.situacao, d.contexto = "acerto", alvo.descricao
                if alvo.detectado_em is None:
                    alvo.detectado_em = d.segundo
                continue
            outro = next((j for j in self.janelas
                          if j.tipo != self.acao and j.contem(d.segundo)), None)
            if outro is not None:
                d.situacao, d.contexto = "outro", f"durante: {outro.descricao}"
            else:
                d.situacao, d.contexto = "falso", "cena normal"

    @property
    def veredito(self) -> str:
        if self.erro:
            return "ERRO"
        if self.janelas is not None:
            partes = []
            if self.alvos:
                achou = sum(j.detectado_em is not None for j in self.alvos)
                partes.append(f"DETECTOU {achou} DE {len(self.alvos)}")
            if self.falsos:
                partes.append(f"{len(self.falsos)} ALARME(S) FALSO(S)")
            return " · ".join(partes) or "ACERTOU (nada a detectar, nada disparou)"
        if self.esperado is None:
            return "DISPAROU" if self.disparou else "NÃO DISPAROU"
        if self.esperado and self.disparou:
            return "ACERTOU"
        if self.esperado:
            return "NÃO DETECTOU"
        return "ALARME FALSO" if self.disparou else "ACERTOU"


def _ascii(texto: str) -> str:
    """O OpenCV não desenha acento — sai '??' no lugar."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def _desenhar(quadro, objetos, eventos_vivos):
    for o in objetos:
        x1, y1, x2, y2 = map(int, o.bbox)
        cor = (80, 200, 80) if o.classe == "pessoa" else (200, 160, 60)
        cv2.rectangle(quadro, (x1, y1), (x2, y2), cor, 2)
        etiqueta = f"{_ascii(o.classe)} #{o.track_id}" if o.track_id else _ascii(o.classe)
        cv2.putText(quadro, etiqueta, (x1, max(15, y1 - 6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, cor, 1, cv2.LINE_AA)
    for ev in eventos_vivos:
        if ev.bbox:
            x1, y1, x2, y2 = map(int, ev.bbox)
            cv2.rectangle(quadro, (x1, y1), (x2, y2), (40, 40, 230), 3)
    if eventos_vivos:
        texto = "DISPAROU: " + _ascii(eventos_vivos[0].rotulo)
        cv2.rectangle(quadro, (0, 0), (quadro.shape[1], 40), (40, 40, 200), -1)
        cv2.putText(quadro, texto, (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (255, 255, 255), 2, cv2.LINE_AA)
    return quadro


def testar_video(acao: str, video: Path, esperado: Optional[bool], args,
                 saida: Path, conjunto: str = "") -> Resultado:
    res = Resultado(acao, video, esperado, conjunto=conjunto)
    inicio = time.monotonic()

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        res.erro = "o OpenCV não conseguiu abrir o arquivo"
        return res
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    res.duracao_s = total / fps if total else 0.0
    pular = max(1, round(fps / args.amostras))

    analisador = preparar_analisador(acao, args)
    rastreio = vision.RastreioPorPosicao(ttl_s=8.0)
    zonas = args.zonas_json if args.zonas_json is not None else zonas_padrao(acao, args.direcao)
    usa_chama = acao in ("fogo", "fumaca")
    janela_chama: deque = deque(maxlen=settings.CHAMA_QUADROS)
    ultima_chama = -1e9
    usa_extra_arma = acao == "arma_detectada" and extras.configurado("arma")

    escritor = None
    pasta_fotos = saida / "fotos"
    pasta_fotos.mkdir(parents=True, exist_ok=True)
    # O conjunto entra no nome: datasets repetem "01.avi" no treino e no teste.
    base = f"{acao}__{conjunto + '_' if conjunto else ''}{video.stem}"
    faixa_ate = -1.0
    eventos_vivos: list = []

    indice = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if indice % pular:
            indice += 1
            continue
        ok, quadro = cap.retrieve()
        if not ok or quadro is None:
            indice += 1
            continue
        t = indice / fps
        indice += 1
        res.quadros += 1
        altura, largura = quadro.shape[:2]

        objetos = vision.detectar_objetos(
            quadro, args.limiar, False, set(), "nunca",
            args.imgsz or (320 if args.rapido else None))
        if usa_extra_arma:
            objetos += [vision.ObjetoDetectado(c, conf, cx, None)
                        for c, conf, cx in extras.detectar(quadro, ["arma"])]
        objetos = rastreio.atribuir(objetos, agora=T0 + t)
        eventos = analisador.atualizar(objetos, largura, altura, zonas, agora=T0 + t)

        if usa_chama:
            janela_chama.append(quadro.copy())
            if (len(janela_chama) >= 3 and t - ultima_chama >= settings.ACAO_COOLDOWN_S
                    and chama.ha_candidato(quadro)):
                pessoas = [o.bbox for o in objetos if o.classe == "pessoa"]
                for c in chama.analisar(list(janela_chama), pessoas):
                    if c.classe == acao:
                        ultima_chama = t
                        eventos.append(vision.EventoAcao(
                            classe=c.classe,
                            rotulo=vision.ROTULO_ACAO.get(c.classe, c.classe),
                            confianca=float(c.confianca),
                            severidade=vision.SEVERIDADE_ACAO.get(c.classe, 4),
                            duracao_s=0, envolvidos=0, bbox=c.bbox))

        eventos = [e for e in eventos if e.classe == acao]
        if eventos:
            faixa_ate = t + 2.0          # a faixa fica 2 s no vídeo anotado
            eventos_vivos = eventos
        anotado = _desenhar(quadro.copy(), objetos,
                            eventos_vivos if t <= faixa_ate else [])

        for n, ev in enumerate(eventos):
            foto = pasta_fotos / f"{base}__{t:07.2f}s_{n + 1}.jpg"
            cv2.imwrite(str(foto), anotado)
            res.disparos.append(Disparo(round(t, 2), ev.rotulo,
                                        round(float(ev.confianca), 2),
                                        str(foto.relative_to(saida))))
            print(f"    disparou em {t:6.1f}s  ({ev.confianca:.0%})")

        if not args.sem_video:
            if escritor is None:
                caminho = saida / "videos" / f"{base}.mp4"
                caminho.parent.mkdir(parents=True, exist_ok=True)
                escritor = cv2.VideoWriter(str(caminho), cv2.VideoWriter_fourcc(*"mp4v"),
                                           args.amostras, (largura, altura))
                res.anotado = str(caminho.relative_to(saida))
            escritor.write(anotado)

        if res.quadros % 25 == 0:
            print(f"    … {t:6.1f}s analisados", flush=True)

    cap.release()
    if escritor is not None:
        escritor.release()
    res.tempo_s = time.monotonic() - inicio
    return res


# ----------------------------------------------------------------------
# Relatório
# ----------------------------------------------------------------------
COR_SITUACAO = {"acerto": "#1b7f3b", "outro": "#b4231f", "falso": "#b4231f"}
NOME_SITUACAO = {"acerto": "acerto", "outro": "alarme falso (outro comportamento)",
                 "falso": "alarme falso (cena normal)"}


def _cor(veredito: str) -> str:
    if veredito.startswith("ERRO"):
        return "#8a5a00"
    if "FALSO" in veredito or "NÃO DETECTOU" in veredito:
        return "#b4231f"
    if veredito.startswith("DETECTOU"):
        a, _, b = veredito.split(" · ")[0].removeprefix("DETECTOU ").partition(" DE ")
        return "#1b7f3b" if a == b else "#b06a00"
    if veredito.startswith("ACERTOU"):
        return "#1b7f3b"
    return "#1f5f9b"


def _fotos(disparos, limite=6):
    e = html.escape
    return "".join(
        f'<figure><a href="{e(d.foto)}"><img src="{e(d.foto)}"></a>'
        f"<figcaption>{d.segundo:.1f}s · {d.confianca:.0%}</figcaption></figure>"
        for d in disparos[:limite] if d.foto)


def escrever_relatorio(resultados: List[Resultado], saida: Path, args) -> Path:
    e = html.escape
    with open(saida / "relatorio.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["acao", "conjunto", "video", "esperado", "veredito", "disparos",
                    "segundos", "situacao_de_cada_disparo", "duracao_video_s", "erro"])
        for r in resultados:
            w.writerow([r.acao, r.conjunto, r.video.name,
                        {True: "deve detectar", False: "não deve detectar",
                         None: "por trecho" if r.janelas is not None else "sem gabarito"}[r.esperado],
                        r.veredito, len(r.disparos),
                        " ".join(f"{d.segundo:.1f}" for d in r.disparos),
                        " | ".join(f"{d.situacao}: {d.contexto}" for d in r.disparos),
                        f"{r.duracao_s:.1f}", r.erro])
    if any(r.janelas is not None for r in resultados):
        with open(saida / "eventos.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["video", "inicio_s", "fim_s", "descricao", "detectado",
                        "detectado_em_s", "atraso_s"])
            for r in resultados:
                for j in r.alvos:
                    w.writerow([r.video.name, j.inicio_s, j.fim_s, j.descricao,
                                "sim" if j.detectado_em is not None else "não",
                                "" if j.detectado_em is None else j.detectado_em,
                                "" if j.detectado_em is None
                                else round(j.detectado_em - j.inicio_s, 1)])

    validos = [r for r in resultados if not r.erro]
    minutos = sum(r.duracao_s for r in validos) / 60
    alvos = [(r, j) for r in validos for j in r.alvos]
    achados = [(r, j) for r, j in alvos if j.detectado_em is not None]
    falsos = [(r, d) for r in validos for d in r.falsos]
    neg = [r for r in validos if r.esperado is False]
    acao = validos[0].acao if validos else (resultados[0].acao if resultados else "")
    rotulo = vision.ROTULO_ACAO.get(acao, acao)

    # --- números de cabeça ----------------------------------------------
    cartoes = []
    if alvos:
        taxa = len(achados) / len(alvos)
        cartoes.append(("Eventos detectados", f"{len(achados)} de {len(alvos)}",
                        f"{taxa:.0%} das ocorrências do gabarito"))
        atrasos = [j.detectado_em - j.inicio_s for _, j in achados]
        if atrasos:
            cartoes.append(("Tempo até disparar", f"{np.median(atrasos):.1f} s",
                            "mediana, do início do evento ao alerta"))
    cartoes.append(("Alarmes falsos", str(len(falsos)),
                    f"em {minutos:.1f} min de vídeo"
                    + (f" · {len(falsos) / minutos:.2f} por minuto" if minutos else "")))
    if neg:
        limpos = sum(not r.disparou for r in neg)
        cartoes.append(("Vídeos sem a ação que ficaram quietos",
                        f"{limpos} de {len(neg)}", "nenhum disparo no vídeo inteiro"))
    html_cartoes = "".join(
        f'<div class="cartao"><div class="rot">{e(a)}</div><div class="num">{e(b)}</div>'
        f'<div class="nota">{e(c)}</div></div>' for a, b, c in cartoes)

    # --- evento a evento ------------------------------------------------
    linhas_ev = []
    for r, j in alvos:
        ok = j.detectado_em is not None
        foto = next((d for d in r.disparos if d.situacao == "acerto"
                     and d.segundo == j.detectado_em), None)
        linhas_ev.append(
            f"<tr><td>{e(r.video.name)}</td><td>{j.inicio_s:.1f}–{j.fim_s:.1f}s</td>"
            f"<td>{e(j.descricao)}</td>"
            f'<td><b style="color:{"#1b7f3b" if ok else "#b4231f"}">'
            f'{"DETECTOU" if ok else "NÃO DETECTOU"}</b>'
            + (f"<br><small>em {j.detectado_em:.1f}s · "
               f"{j.detectado_em - j.inicio_s:+.1f}s do início</small>" if ok else "")
            + f'</td><td class="fotos">{_fotos([foto]) if foto else ""}</td></tr>')

    # --- alarmes falsos -------------------------------------------------
    linhas_falsos = [
        f"<tr><td>{e(r.conjunto)}</td><td>{e(r.video.name)}</td><td>{d.segundo:.1f}s</td>"
        f'<td style="color:{COR_SITUACAO.get(d.situacao, "#b4231f")}">'
        f"{e(NOME_SITUACAO.get(d.situacao, d.situacao))}<br><small>{e(d.contexto)}</small></td>"
        f'<td class="fotos">{_fotos([d])}</td></tr>'
        for r, d in falsos]

    # --- vídeo a vídeo --------------------------------------------------
    linhas = []
    for r in resultados:
        video = f'<a href="{e(r.anotado)}">vídeo anotado</a>' if r.anotado else ""
        esperado = ({True: "deve detectar", False: "não deve detectar", None: "—"}[r.esperado]
                    if r.janelas is None else
                    f"{len(r.alvos)} evento(s) no gabarito")
        linhas.append(
            f"<tr><td>{e(r.conjunto)}</td>"
            f"<td>{e(r.video.name)}<br><small>{r.duracao_s:.0f}s de vídeo · "
            f"{r.tempo_s:.0f}s de análise</small></td><td>{esperado}</td>"
            f'<td><b style="color:{_cor(r.veredito)}">{e(r.veredito)}</b>'
            f'{"<br><small>" + e(r.erro) + "</small>" if r.erro else ""}</td>'
            f'<td class="fotos">{_fotos(r.disparos)}{video}</td></tr>')

    config = (f"Detector YOLO em {args.imgsz or (320 if args.rapido else settings.OBJECT_IMGSZ)} px · "
              f"limiar {args.limiar} · {args.amostras:g} quadros analisados por segundo de vídeo · "
              f"folga para casar disparo com o gabarito: {FOLGA_ANTES_S:g}s antes, "
              f"{FOLGA_DEPOIS_S:g}s depois")
    secao_eventos = (f"<h2>Evento a evento</h2><p class='sub'>Cada ocorrência de "
                     f"{e(rotulo.lower())} marcada no gabarito, e se o sistema a pegou.</p>"
                     f"<table><tr><th>Vídeo</th><th>Trecho</th><th>O que acontece</th>"
                     f"<th>Resultado</th><th>Foto do alerta</th></tr>{''.join(linhas_ev)}</table>"
                     if linhas_ev else "")
    secao_falsos = (f"<h2>Alarmes falsos</h2><p class='sub'>Disparos fora de qualquer "
                    f"ocorrência de {e(rotulo.lower())}.</p><table><tr><th>Conjunto</th>"
                    f"<th>Vídeo</th><th>Quando</th><th>Situação</th><th>Foto</th></tr>"
                    f"{''.join(linhas_falsos)}</table>"
                    if linhas_falsos else
                    "<h2>Alarmes falsos</h2><p>Nenhum.</p>")

    pagina = f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<title>Teste de {e(rotulo)} por vídeo</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:32px;color:#1d2b38;background:#f5f7fa}}
h1{{margin:0 0 4px}} h2{{margin:32px 0 6px}} .sub{{color:#5a6b7b;margin:0 0 14px}}
table{{border-collapse:collapse;width:100%;background:#fff}}
th,td{{border:1px solid #dde3ea;padding:8px 10px;text-align:left;vertical-align:top;font-size:14px}}
th{{background:#0b121a;color:#fff;font-weight:600}}
small{{color:#5a6b7b}} .fotos figure{{display:inline-block;margin:0 8px 8px 0}}
.fotos img{{height:110px;border:2px solid #c33}} figcaption{{font-size:12px;color:#5a6b7b}}
.cartoes{{display:flex;gap:14px;flex-wrap:wrap;margin:18px 0}}
.cartao{{background:#fff;border:1px solid #dde3ea;border-radius:8px;padding:14px 18px;min-width:200px}}
.cartao .rot{{font-size:12px;color:#5a6b7b;text-transform:uppercase;letter-spacing:.5px}}
.cartao .num{{font-size:28px;font-weight:700;margin:4px 0}} .cartao .nota{{font-size:12px;color:#5a6b7b}}
.cond{{background:#fff;border-left:4px solid #35a7de;padding:10px 14px;margin:10px 0}}
</style></head><body>
<h1>Teste de {e(rotulo)} por vídeo</h1>
<p class="sub">SEGURANÇA · {datetime.now():%d/%m/%Y %H:%M} · {len(resultados)} vídeo(s),
{minutos:.1f} min · mesmo detector, rastreio e analisador da câmera ao vivo</p>
<div class="cond"><b>Quando a ação dispara:</b> {e(vision.detalhe_acao(acao))}<br>
<small>{e(config)}</small></div>
<div class="cartoes">{html_cartoes}</div>
{secao_eventos}
{secao_falsos}
<h2>Vídeo a vídeo</h2>
<table><tr><th>Conjunto</th><th>Vídeo</th><th>Esperado</th><th>Resultado</th>
<th>Disparos</th></tr>{"".join(linhas)}</table>
</body></html>"""
    caminho = saida / "relatorio.html"
    caminho.write_text(pagina, encoding="utf-8")
    return caminho


# ----------------------------------------------------------------------
# Linha de comando
# ----------------------------------------------------------------------
def listar() -> None:
    print("\nAções do sistema (use o código em --acao ou como nome de pasta):\n")
    for f in vision.CATALOGO_ACOES:
        situacao = "OK" if f.disponivel else "SEM DETECTOR"
        extra = []
        if f.exige_continua:
            extra.append("movimento")
        if f.codigo in PRECISAM_DE_ZONA:
            extra.append("zona")
        print(f"  {f.codigo:<18} {f.rotulo:<34} {situacao:<13} {', '.join(extra)}")
        if f.indisponivel:
            print(f"  {'':<18} motivo: {f.indisponivel}")
    print()


def coletar(pasta: Path) -> List[tuple]:
    """(acao, video, esperado, conjunto, janelas) para cada vídeo da bateria."""
    codigos = {f.codigo for f in vision.CATALOGO_ACOES}
    itens = []
    for sub in sorted(p for p in pasta.iterdir() if p.is_dir()):
        if sub.name not in codigos:
            print(f"  ignorando pasta '{sub.name}': não é código de ação (veja --listar)")
            continue
        for arq in sorted(sub.rglob("*")):
            if arq.suffix.lower() not in EXTENSOES:
                continue
            partes = {p.lower() for p in arq.relative_to(sub).parts[:-1]}
            esperado = not any(p.startswith("nao") or p.startswith("não") for p in partes)
            itens.append((sub.name, arq, esperado,
                          "deve_detectar" if esperado else "nao_deve_detectar", None))
    return itens


def ler_gabarito(caminho: Path) -> dict:
    """video -> [Janela]. Colunas: video;inicio_s;fim_s;tipo;descricao.

    `tipo` é o código da ação testada, ou "outro" para o que acontece no vídeo
    e não é a ação — serve para dizer em que cena um alarme falso caiu.
    """
    gab: dict = {}
    with open(caminho, encoding="utf-8-sig", newline="") as f:
        for linha in csv.DictReader(f, delimiter=";"):
            chave = linha["video"].strip()
            gab.setdefault(chave, []).append(Janela(
                float(linha["inicio_s"]), float(linha["fim_s"]),
                (linha.get("tipo") or "").strip(), (linha.get("descricao") or "").strip()))
    return gab


def _janelas_do_video(gab: dict, video: Path) -> List[Janela]:
    for chave in (video.name, video.stem, video.stem.lstrip("0")):
        if chave in gab:
            # Cópia: o mesmo gabarito pode servir a mais de uma rodada.
            return [Janela(j.inicio_s, j.fim_s, j.tipo, j.descricao) for j in gab[chave]]
    return []


def _videos_de(pasta: Path) -> List[Path]:
    return sorted(a for a in pasta.rglob("*") if a.suffix.lower() in EXTENSOES)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Testa com vídeo gravado se o sistema detecta cada ação.")
    p.add_argument("video", nargs="?", type=Path, help="arquivo de vídeo")
    p.add_argument("--acao", help="código da ação (veja --listar)")
    p.add_argument("--pasta", type=Path, help="bateria: pasta com uma subpasta por ação")
    p.add_argument("--videos", type=Path,
                   help="pasta de vídeos em que a ação acontece (use com --acao)")
    p.add_argument("--gabarito", type=Path,
                   help="CSV com os trechos de cada vídeo de --videos (video;inicio_s;fim_s;tipo;descricao)")
    p.add_argument("--negativos", type=Path,
                   help="pasta de vídeos em que a ação NÃO acontece (use com --acao)")
    p.add_argument("--listar", action="store_true", help="mostra as ações e os códigos")
    p.add_argument("--saida", type=Path, default=RAIZ / "resultados_teste")
    p.add_argument("--zonas", type=Path, help="JSON com as zonas (formato da câmera)")
    p.add_argument("--direcao", choices=sorted(DIRECOES), default="direita",
                   help="contrafluxo: sentido PERMITIDO na zona padrão")
    p.add_argument("--limiar", type=float, default=settings.OBJECT_THRESHOLD)
    p.add_argument("--amostras", type=float, default=AMOSTRAS_POR_S,
                   help="quadros analisados por segundo de vídeo")
    p.add_argument("--imgsz", type=int,
                   help="lado em px que o detector usa (padrão: o da câmera ao vivo)")
    p.add_argument("--rapido", action="store_true",
                   help="detector em 320 px — mais rápido, perde gente pequena")
    p.add_argument("--sem-video", action="store_true", help="não grava o vídeo anotado")
    p.add_argument("--permanencia-s", type=float, help="encurta a permanência para o teste")
    p.add_argument("--abandono-s", type=float, help="encurta o abandono para o teste")
    p.add_argument("--limite-aglomeracao", type=int, help="pessoas para aglomeração")
    args = p.parse_args()

    if args.listar:
        listar()
        return 0

    itens: List[tuple] = []
    if args.pasta:
        if not args.pasta.is_dir():
            print(f"Pasta não encontrada: {args.pasta}")
            return 2
        itens = coletar(args.pasta)
    elif args.acao and (args.videos or args.negativos):
        if args.videos:
            if not args.videos.is_dir():
                print(f"Pasta não encontrada: {args.videos}")
                return 2
            gab = ler_gabarito(args.gabarito) if args.gabarito else None
            for v in _videos_de(args.videos):
                janelas = _janelas_do_video(gab, v) if gab is not None else None
                itens.append((args.acao, v, None if gab is not None else True,
                              args.videos.name, janelas))
        if args.negativos:
            if not args.negativos.is_dir():
                print(f"Pasta não encontrada: {args.negativos}")
                return 2
            for v in _videos_de(args.negativos):
                itens.append((args.acao, v, False, args.negativos.name, None))
    elif args.video and args.acao:
        if not args.video.is_file():
            print(f"Vídeo não encontrado: {args.video}")
            return 2
        itens = [(args.acao, args.video, None, "", None)]
    else:
        p.print_help()
        print("\nInforme um vídeo com --acao, uma --pasta de bateria, "
              "ou --acao com --videos/--negativos.")
        return 2

    if not itens:
        print("Nenhum vídeo encontrado.")
        return 2

    args.zonas_json = None
    if args.zonas:
        import json
        args.zonas_json = json.loads(args.zonas.read_text(encoding="utf-8"))

    if not vision.yolo_disponivel():
        print("O detector YOLO não está disponível nesta instalação.")
        return 2

    saida = args.saida / datetime.now().strftime("%Y%m%d-%H%M%S")
    saida.mkdir(parents=True, exist_ok=True)

    resultados: List[Resultado] = []
    inicio_total = time.monotonic()
    for n, (acao, video, esperado, conjunto, janelas) in enumerate(itens, 1):
        ficha = vision.FICHA_ACAO.get(acao)
        rotulo = ficha.rotulo if ficha else acao
        if janelas is not None:
            quanto = f"{sum(j.tipo == acao for j in janelas)} evento(s) no gabarito"
        elif esperado is None:
            quanto = ""
        else:
            quanto = "deve detectar" if esperado else "não deve detectar"
        print(f"\n[{n}/{len(itens)}] [{acao}] {rotulo} — {conjunto + '/' if conjunto else ''}"
              f"{video.name}" + (f"  ({quanto})" if quanto else ""))
        if ficha is None:
            r = Resultado(acao, video, esperado, conjunto=conjunto,
                          erro="ação inexistente — veja --listar")
        elif acao == "vandalismo":
            r = Resultado(acao, video, esperado, conjunto=conjunto,
                          erro=f"sem detector: {ficha.indisponivel}")
        else:
            if not ficha.disponivel:
                print(f"    atenção: desligada no sistema ({ficha.indisponivel}); "
                      "testando o detector mesmo assim")
            if acao in PRECISAM_DE_ZONA and args.zonas_json is None:
                print("    usando a zona padrão do teste (quadro inteiro"
                      + (", linha vertical no meio" if acao == "invasao" else "")
                      + (f", sentido permitido: {args.direcao}" if acao == "contrafluxo" else "")
                      + ")")
            try:
                r = testar_video(acao, video, esperado, args, saida, conjunto)
            except Exception as exc:  # noqa: BLE001
                r = Resultado(acao, video, esperado, conjunto=conjunto,
                              erro=f"{type(exc).__name__}: {exc}")
        r.janelas = janelas
        r.avaliar()
        print(f"    => {r.veredito}" + (f" ({r.erro})" if r.erro else ""))
        resultados.append(r)
        # Relatório parcial a cada vídeo: uma bateria longa pode ser
        # interrompida, e o que já rodou não deve se perder.
        escrever_relatorio(resultados, saida, args)

    relatorio = escrever_relatorio(resultados, saida, args)
    alvos = [j for r in resultados for j in r.alvos]
    print("\n" + "-" * 64)
    if alvos:
        print(f"  Eventos detectados: {sum(j.detectado_em is not None for j in alvos)} "
              f"de {len(alvos)}")
    print(f"  Alarmes falsos: {sum(len(r.falsos) for r in resultados)}")
    neg = [r for r in resultados if r.esperado is False and not r.erro]
    if neg:
        print(f"  Vídeos sem a ação que ficaram quietos: "
              f"{sum(not r.disparou for r in neg)} de {len(neg)}")
    print(f"  Tempo total: {(time.monotonic() - inicio_total) / 60:.1f} min")
    print(f"  Relatório: {relatorio}")
    print("-" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
