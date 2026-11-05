"""Toca um vídeo com a análise da IA desenhada por cima, ao vivo.

Serve para *ver* o sistema decidir: cada pessoa ganha caixa, número de
rastreio, rastro e a velocidade que o analisador está medindo. Quando a ação
dispara, aparece uma faixa vermelha; quando o gabarito diz que a ação está
acontecendo, uma faixa amarela embaixo — é a comparação que importa.

    py -3.13 -m scripts.ver_acao_video "videos_teste/Avenue Dataset/testing_videos/04.avi" --acao corrida --gabarito "videos_teste/Avenue Dataset/gabarito_corrida.csv"

Uma pasta inteira (um vídeo atrás do outro):

    py -3.13 -m scripts.ver_acao_video "videos_teste/Avenue Dataset/testing_videos" --acao corrida --gabarito ...

Cada disparo vira um print: o quadro do momento, com a caixa vermelha e a
faixa de horário (tempo no vídeo e hora do registro). O print aparece no
painel ao lado, com os dados do disparo alinhados à direita dele, e é gravado
em `resultados_teste/ao_vivo/prints/` — o caminho vai no JSON (`print`).

Com o sistema ligado, cada disparo também vira **alerta na Central de
alertas**, com o print, o tempo do vídeo e os dados — marcado "(vídeo de
teste)" e numa câmera técnica que não aparece no mosaico. `--sem-alerta`
desliga; `--servidor` aponta outro endereço (padrão http://127.0.0.1:8000).

Teclas na janela:
    espaço   pausa / continua
    p        grava um print da janela inteira (vídeo + painel)
    n        próximo vídeo
    q / Esc  sai

Na primeira vez de cada vídeo a IA analisa tudo, com uma barra de progresso,
e guarda o resultado em `resultados_teste/ao_vivo/cache/`; nas seguintes o
vídeo abre na hora e toca em tempo real. `--reanalisar` força uma análise
nova (o cache também se renova sozinho quando o vídeo ou o código mudam).

Usa o mesmo detector, o mesmo rastreio e o mesmo analisador da câmera ao vivo
(`scripts/testar_acoes_video.py` faz a bateria com relatório).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.services import vision  # noqa: E402
from scripts.testar_acoes_video import (  # noqa: E402
    AMOSTRAS_POR_S, EXTENSOES, T0, _janelas_do_video, ler_gabarito,
    preparar_analisador, zonas_padrao,
)

JANELA = "SEGURANCA - teste de acao por video"
VERMELHO, AMARELO, VERDE, BRANCO = (40, 40, 230), (0, 200, 255), (80, 200, 80), (255, 255, 255)


def _a(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def _faixa(img, y, texto, fundo, cor=BRANCO, escala=0.7):
    cv2.rectangle(img, (0, y), (img.shape[1], y + 34), fundo, -1)
    cv2.putText(img, _a(texto), (12, y + 24), cv2.FONT_HERSHEY_SIMPLEX, escala, cor, 2,
                cv2.LINE_AA)


PAINEL_L = 440
CINZA = (170, 170, 170)
# Miniatura do print no painel, em 16:9, e quantas cabem de uma vez.
MINI_L, MINI_A = 176, 99
MAX_CAPTURAS = 2
# Quanto da tela a janela pode ocupar. Em notebook com escala de 125% a tela
# tem 1536x864 pontos, e a janela antiga (1544 px) passava da largura.
FRACAO_TELA_L, FRACAO_TELA_A = 0.80, 0.75


def _tela() -> tuple:
    """Tamanho da tela em pontos (o que a janela do OpenCV usa)."""
    try:
        import ctypes
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:  # noqa: BLE001 — fora do Windows: um tamanho comum
        return 1366, 768


def _zoom_para_tela(larg_video: int, alt_video: int, pedido) -> float:
    """O zoom que faz vídeo + painel caberem na tela, sem passar do pedido."""
    tl, ta = _tela()
    cabe = min((tl * FRACAO_TELA_L - PAINEL_L) / larg_video, (ta * FRACAO_TELA_A) / alt_video)
    return round(max(0.5, min(cabe, pedido) if pedido else cabe), 2)


def _tempo(t: float) -> str:
    """Tempo no vídeo como 00:11.2 — o que se lê de relance na apresentação."""
    return f"{int(t // 60):02d}:{t % 60:04.1f}"


def _carimbar(img, linhas, escala=0.55):
    """Faixa escura embaixo da imagem com o horário do disparo."""
    passo = int(26 * escala / 0.55)
    alt = 12 + passo * len(linhas)
    h, w = img.shape[:2]
    faixa = img[h - alt:h].copy()
    cv2.rectangle(faixa, (0, 0), (w, alt), (20, 20, 20), -1)
    img[h - alt:h] = cv2.addWeighted(faixa, 0.82, img[h - alt:h], 0.18, 0)
    cv2.rectangle(img, (0, h - alt), (5, h), VERMELHO, -1)
    y = h - alt + int(22 * escala / 0.55)
    for i, linha in enumerate(linhas):
        cv2.putText(img, _a(linha), (14, y), cv2.FONT_HERSHEY_SIMPLEX, escala,
                    BRANCO, 2 if i == 0 else 1, cv2.LINE_AA)
        y += passo
    return img


def _fazer_print(img, bbox_janela, registro, pasta: Path) -> dict:
    """Grava o print do disparo e devolve a miniatura para o painel.

    O arquivo é o quadro inteiro da janela, com a caixa vermelha e o horário
    carimbado. A miniatura é um recorte em volta da pessoa: o quadro inteiro
    reduzido à miniatura deixaria quem corre do tamanho de um ponto.
    """
    rotulo = _a(registro["rotulo"]).upper()
    cheio = _carimbar(img.copy(), [
        f"{rotulo}  {registro['confianca']:.0%}   |   {registro['video']}   "
        f"t = {_tempo(registro['segundo'])}   |   quadro {registro['quadro']}",
        f"registrado em {registro['horario_registro']}   -   pessoa {registro['pessoa_id']}",
    ], 0.5)
    pasta.mkdir(parents=True, exist_ok=True)
    arquivo = pasta / (f"{registro['acao']}_{Path(registro['video']).stem}_"
                       f"{registro['segundo']:06.2f}s.jpg")
    cv2.imwrite(str(arquivo), cheio, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

    h, w = img.shape[:2]
    x1, y1, x2, y2 = bbox_janela
    cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
    # Aproxima até o corpo ocupar ~60% da altura, e desce o recorte um pouco:
    # a faixa do horário cobre o terço de baixo da miniatura, e é ali que
    # ficariam as pernas de quem corre.
    alt = min(h, max(MINI_A * 1.2, (y2 - y1) * 1.7))
    larg = min(w, alt * MINI_L / MINI_A)
    alt = larg * MINI_A / MINI_L
    rx = int(min(max(0, cx - larg / 2), w - larg))
    ry = int(min(max(0, cy - alt * 0.42), h - alt))
    mini = cv2.resize(img[ry:ry + int(alt), rx:rx + int(larg)], (MINI_L, MINI_A),
                      interpolation=cv2.INTER_AREA)
    mini = _carimbar(mini, [_tempo(registro["segundo"])], 0.45)
    cv2.rectangle(mini, (0, 0), (MINI_L - 1, MINI_A - 1), VERMELHO, 2)
    return {"registro": registro, "mini": mini, "arquivo": arquivo}


_avisou_sem_servidor = False


def _enviar_alerta(servidor: str, registro: dict, arquivo_print: Path) -> None:
    """Manda o disparo à Central de alertas, sem segurar o vídeo.

    Roda numa thread: a requisição leva de dezenas de milissegundos a alguns
    segundos com o servidor ocupado, e o vídeo não pode parar esperando.
    Sistema desligado não é erro do teste — avisa uma vez e segue.
    """
    import base64
    import threading
    import urllib.request

    def trabalho():
        global _avisou_sem_servidor
        try:
            corpo = {
                "video": registro["video"], "acao": registro["acao"],
                "segundo": registro["segundo"], "tempo_video": registro["tempo_video"],
                "quadro": registro["quadro"], "confianca": registro["confianca"],
                "pessoa_id": registro["pessoa_id"],
                "velocidade_corpos_s": registro["velocidade_corpos_s"],
                "gabarito": (registro.get("gabarito") or {}).get("resultado", ""),
                "bbox_normalizado": registro["bbox_normalizado"],
                "print_jpeg": base64.b64encode(arquivo_print.read_bytes()).decode("ascii"),
            }
            req = urllib.request.Request(
                servidor.rstrip("/") + "/deteccoes/video-teste",
                data=json.dumps(corpo).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
            registro["alerta"] = resp.get("alerta")
            print(f"  alerta criado: {resp.get('alerta')} ({resp.get('codigo')})")
        except Exception as exc:  # noqa: BLE001
            if not _avisou_sem_servidor:
                _avisou_sem_servidor = True
                print(f"  (sem alerta no sistema: {exc})")

    threading.Thread(target=trabalho, daemon=True).start()


def _texto(painel, y, texto, cor=BRANCO, escala=0.5, grosso=1, x=14):
    cv2.putText(painel, _a(texto), (x, y), cv2.FONT_HERSHEY_SIMPLEX, escala, cor, grosso,
                cv2.LINE_AA)


def _bloco(painel, y, altura, fundo):
    cv2.rectangle(painel, (10, y), (PAINEL_L - 10, y + altura), fundo, -1)


def _com_painel(img, capturas, caminho_json, estado):
    """Painel ao lado do vídeo com tudo que não é caixa.

    O vídeo fica livre: nada de faixa, placar ou legenda por cima da cena —
    numa câmera de baixa resolução, como a do UMN, essas faixas cobriam
    justamente as pessoas que se queria ver.
    """
    h = img.shape[0]
    painel = np.full((h, PAINEL_L, 3), (24, 24, 24), np.uint8)
    y = 12

    # 1. Estado da IA
    situacao = estado["situacao"]
    fundo = {"detectada": VERMELHO, "pausado": (90, 90, 90),
             "fim": (60, 60, 60)}.get(situacao, (55, 55, 55))
    _bloco(painel, y, 38, fundo)
    titulo = {"detectada": f"{estado['rotulo'].upper()} DETECTADA",
              "pausado": "PAUSADO - espaco continua",
              "fim": "FIM DO VIDEO - tecla segue"}.get(situacao, f"IA analisando: {estado['rotulo']}")
    _texto(painel, y + 25, titulo, BRANCO, 0.55, 2, 20)
    y += 56

    # 2. Placar, numa linha só
    _texto(painel, y, f"{estado['video']}   t = {_tempo(estado['t'])}", BRANCO, 0.5, 1)
    y += 20
    placar = f"disparos da IA: {estado['disparos']}"
    if estado["tem_gabarito"]:
        placar += f"    gabarito: {estado['detectou']} de {estado['no_gabarito']} detectadas"
    _texto(painel, y, placar, CINZA, 0.42)
    y += 10
    # 3. O gabarito agora
    if estado["tem_gabarito"] and estado["gabarito_agora"]:
        y += 4
        _bloco(painel, y, 34, AMARELO)
        _texto(painel, y + 14, "GABARITO: acontecendo agora", (0, 0, 0), 0.4, 1, 20)
        _texto(painel, y + 28, estado["gabarito_agora"][:52], (0, 0, 0), 0.36, 1, 20)
        y += 36
    y += 6
    cv2.line(painel, (10, y), (PAINEL_L - 10, y), (70, 70, 70), 1)
    y += 20

    # 4. Prints dos disparos, cada um com os dados alinhados ao lado
    _texto(painel, y, f"CAPTURAS - {estado['rotulo'].upper()}", BRANCO, 0.5, 2)
    if capturas:
        _texto(painel, y, f"{len(capturas)} no total", CINZA, 0.4, 1, PAINEL_L - 86)
    y += 10
    if not capturas:
        y += 18
        _texto(painel, y, "nenhuma deteccao ainda", (150, 150, 150), 0.42)
    xd = 14 + MINI_L + 12
    rodape = h - 36
    for n, cap in reversed(list(enumerate(capturas, 1))):
        if y + MINI_A > rodape or n <= len(capturas) - MAX_CAPTURAS:
            break
        r = cap["registro"]
        painel[y:y + MINI_A, 14:14 + MINI_L] = cap["mini"]
        g = r.get("gabarito", {})
        cor_g = VERDE if g.get("resultado") == "acerto" else VERMELHO if g else CINZA
        _texto(painel, y + 13, f"#{n}   t = {_tempo(r['segundo'])}", BRANCO, 0.46, 2, xd)
        # Rótulo e valor em colunas fixas: a fonte não é monoespaçada, e
        # alinhar com espaços deixava cada valor num lugar.
        campos = [
            ("confianca", f"{r['confianca']:.0%}", BRANCO),
            ("pessoa_id", str(r["pessoa_id"]), BRANCO),
            ("velocidade", f"{r['velocidade_corpos_s']} corpos/s", BRANCO),
            ("quadro", str(r["quadro"]), BRANCO),
            ("registro", r["horario_registro"][-8:], BRANCO),
        ]
        if g:
            campos.append(("gabarito", g["resultado"], cor_g))
        ly = y + 29
        for chave, valor, cor in campos:
            _texto(painel, ly, chave, CINZA, 0.38, 1, xd)
            _texto(painel, ly, valor, cor, 0.38, 1, xd + 78)
            ly += 14
        y += MINI_A + 8

    # 5. Legenda e onde ficou gravado
    cv2.line(painel, (10, rodape), (PAINEL_L - 10, rodape), (70, 70, 70), 1)
    _texto(painel, rodape + 14, f"verde: pessoa (confianca)   vermelho: {estado['rotulo'].lower()}",
           CINZA, 0.36)
    _texto(painel, rodape + 29, f"JSON e prints: resultados_teste/ao_vivo/", CINZA, 0.36)
    return np.hstack([img, painel])


def _desenhar_caixas(quadro, objs, zoom, vermelhos, confianca_acao, t, rotulo):
    """Só as caixas, desenhadas já na escala da janela.

    Desenhar no quadro original e ampliar depois engordava as letras junto
    com a imagem — num vídeo de 320x240 a tarja de cada pessoa cobria a
    pessoa ao lado. `objs` são as pessoas da análise: dicionários com
    `bbox`, `conf` e `id`.
    """
    img = cv2.resize(quadro, None, fx=zoom, fy=zoom)
    for o in objs:
        disparou = vermelhos.get(o["id"], -1) >= t
        cor = VERMELHO if disparou else VERDE
        x1, y1, x2, y2 = (int(v * zoom) for v in o["bbox"])
        cv2.rectangle(img, (x1, y1), (x2, y2), cor, 2 if disparou else 1)
        if disparou:
            texto = f"{_a(rotulo.upper())} {confianca_acao.get(o['id'], 0):.0%}"
            escala, cor_txt = 0.45, BRANCO
        else:
            texto = f"{o['conf']:.0%}"
            escala, cor_txt = 0.36, (0, 0, 0)
        (tw, th), _ = cv2.getTextSize(texto, cv2.FONT_HERSHEY_SIMPLEX, escala, 1)
        ty = max(0, y1 - th - 4)
        cv2.rectangle(img, (x1, ty), (x1 + tw + 4, ty + th + 4), cor, -1)
        cv2.putText(img, texto, (x1 + 2, ty + th + 1), cv2.FONT_HERSHEY_SIMPLEX, escala,
                    cor_txt, 1, cv2.LINE_AA)
    return img


# ---------------------------------------------------------------------------
# Análise (uma vez por vídeo) e reprodução (em tempo real)
# ---------------------------------------------------------------------------
# A análise roda o detector a 4,5 quadros por segundo de vídeo, e cada
# passada leva de 0,2 s (máquina livre) a 1,4 s (com o servidor ligado), mais
# 18 s para carregar o modelo na primeira. Analisar enquanto toca deixava o
# vídeo lento e aos saltos. Por isso a análise é feita antes, uma vez, e
# guardada: a reprodução lê o resultado e toca a 25 quadros por segundo.
#
# É o mesmo detector, o mesmo rastreio e o mesmo analisador — e o resultado
# é o mesmo de analisar tocando, porque o analisador só vê os quadros
# amostrados, em ordem, com o tempo de cada um.
CACHE_VERSAO = 1


def _chave_cache(video: Path, acao: str, args) -> str:
    """Muda quando muda o vídeo, os parâmetros ou o código da análise."""
    import hashlib

    from app.config import settings

    st = video.stat()
    codigo = [int((RAIZ / f).stat().st_mtime) for f in
              ("app/services/vision.py", "scripts/testar_acoes_video.py")]
    # Só as configurações que mudam o que a IA vê e decide: mexer em e-mail,
    # alerta ou tela não pode obrigar o vídeo a ser analisado de novo.
    codigo.append({k: v for k, v in settings.model_dump().items()
                   if k.startswith(("ACAO_", "OBJECT_", "YOLO"))})
    partes = [CACHE_VERSAO, video.name, st.st_size, int(st.st_mtime), acao, args.imgsz,
              args.limiar, args.amostras, args.direcao, args.bytetrack, args.sem_subita,
              args.permanencia_s, args.abandono_s, args.limite_aglomeracao, codigo]
    return hashlib.sha1(json.dumps(partes, default=str).encode()).hexdigest()[:12]


def analisar(video: Path, acao: str, args, progresso: dict) -> Optional[dict]:
    """Roda a IA nos quadros amostrados e devolve o que ela viu em cada um.

    `progresso` é compartilhado com a tela de espera: a fração feita, e
    `cancelar` para parar no meio.
    """
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    pular = max(1, round(fps / args.amostras))
    an = preparar_analisador(acao, args)
    if args.sem_subita and acao == "corrida":
        an.salto_corrida = 0.0
    rastreio = vision.RastreioPorPosicao(ttl_s=8.0)
    if args.bytetrack:
        vision.obter_yolo().predictor = None
    zonas = zonas_padrao(acao, args.direcao)

    amostras = []
    indice = 0
    while not progresso.get("cancelar"):
        if not cap.grab():
            break
        if indice % pular:
            indice += 1
            continue
        ok, quadro = cap.retrieve()
        i, t = indice, indice / fps
        indice += 1
        if not ok:
            continue
        altura, largura = quadro.shape[:2]
        if args.bytetrack:
            objs = vision.detectar_objetos(quadro, args.limiar, True, set(), "nunca", args.imgsz)
        else:
            objs = rastreio.atribuir(
                vision.detectar_objetos(quadro, args.limiar, False, set(), "nunca", args.imgsz),
                agora=T0 + t)
        eventos = []
        for e in an.atualizar(objs, largura, altura, zonas, agora=T0 + t):
            if e.classe != acao:
                continue
            tr = an.trilhas.get(e.track_id)
            eventos.append({
                "id": e.track_id,
                "bbox": [float(v) for v in e.bbox] if e.bbox else None,
                "confianca": float(e.confianca),
                "severidade": e.severidade,
                "duracao_s": e.duracao_s,
                "velocidade": round(tr.arranque()[0], 2) if tr else None,
            })
        # Quem segue rápido: a caixa de quem já disparou continua vermelha
        # enquanto a pessoa continua correndo.
        rapidos = []
        if acao == "corrida":
            for o in objs:
                tr = an.trilhas.get(o.track_id)
                if tr is not None and tr.arranque()[0] >= an.piso_corrida_atual:
                    rapidos.append(o.track_id)
        amostras.append({
            "i": i, "t": t, "w": largura, "h": altura,
            "objs": [{"bbox": [round(float(v), 1) for v in o.bbox],
                      "conf": round(float(o.confianca), 3), "id": o.track_id}
                     for o in objs if o.classe == "pessoa"],
            "eventos": eventos, "rapidos": rapidos,
        })
        progresso["fracao"] = min(1.0, indice / total)
    cap.release()
    if progresso.get("cancelar"):
        return None
    return {"fps": fps, "amostras": amostras}


def _interpolar(atual: dict, proxima: Optional[dict], indice: int) -> list:
    """As caixas no quadro `indice`, entre duas análises.

    A IA olha 4,5 quadros por segundo e o vídeo tem 25: sem isto a caixa
    ficaria parada por cinco quadros e saltaria. Quem aparece nas duas
    análises desliza de uma posição à outra.
    """
    if proxima is None or proxima["i"] <= atual["i"]:
        return atual["objs"]
    f = (indice - atual["i"]) / (proxima["i"] - atual["i"])
    seguinte = {o["id"]: o for o in proxima["objs"] if o["id"] is not None}
    saida = []
    for o in atual["objs"]:
        n = seguinte.get(o["id"])
        if n is None:
            saida.append(o)
        else:
            saida.append(dict(o, bbox=[a + (b - a) * f for a, b in zip(o["bbox"], n["bbox"])]))
    return saida


def _tela_preparando(quadro, zoom, video: str, fracao: float):
    """Janela que abre na hora, enquanto a IA analisa o vídeo pela primeira vez."""
    img = cv2.resize(quadro, None, fx=zoom, fy=zoom)
    img = (img * 0.45).astype(np.uint8)
    h, w = img.shape[:2]
    painel = np.full((h, PAINEL_L, 3), (24, 24, 24), np.uint8)
    _bloco(painel, 12, 38, (55, 55, 55))
    _texto(painel, 37, "PREPARANDO A ANALISE DA IA", BRANCO, 0.55, 2, 20)
    _texto(painel, 86, video, BRANCO, 0.5)
    _texto(painel, 110, "Primeira vez deste video: a IA analisa", CINZA, 0.42)
    _texto(painel, 128, "tudo uma vez e guarda o resultado.", CINZA, 0.42)
    _texto(painel, 146, "Nas proximas, ele abre na hora.", CINZA, 0.42)
    x1, x2, y = 14, PAINEL_L - 14, 172
    cv2.rectangle(painel, (x1, y), (x2, y + 18), (70, 70, 70), -1)
    cv2.rectangle(painel, (x1, y), (x1 + int((x2 - x1) * fracao), y + 18), VERDE, -1)
    _texto(painel, y + 40, f"{fracao:.0%}", BRANCO, 0.5, 1)
    _texto(painel, h - 14, "q cancela", CINZA, 0.38)
    return np.hstack([img, painel])


def obter_analise(video: Path, acao: str, args) -> Optional[dict]:
    """A análise guardada deste vídeo, ou uma nova, com tela de progresso."""
    import threading

    pasta = args.saida / "cache"
    arquivo = pasta / f"{video.stem}_{acao}_{_chave_cache(video, acao, args)}.json"
    if arquivo.exists() and not args.reanalisar:
        try:
            return json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    cap = cv2.VideoCapture(str(video))
    ok, primeiro = cap.read()
    cap.release()
    if not ok:
        return None
    zoom = _zoom_para_tela(primeiro.shape[1], primeiro.shape[0], args.zoom_pedido)
    progresso: dict = {"fracao": 0.0}
    resultado: dict = {}
    trabalho = threading.Thread(
        target=lambda: resultado.update(dados=analisar(video, acao, args, progresso)),
        daemon=True)
    trabalho.start()
    while trabalho.is_alive():
        cv2.imshow(JANELA, _tela_preparando(primeiro, zoom, video.name, progresso["fracao"]))
        if (cv2.waitKey(60) & 0xFF) in (ord("q"), 27):
            progresso["cancelar"] = True
            trabalho.join()
            return None
    dados = resultado.get("dados")
    if dados:
        pasta.mkdir(parents=True, exist_ok=True)
        # Guarda só a versão atual deste vídeo: as antigas não valem mais.
        for velho in pasta.glob(f"{video.stem}_{acao}_*.json"):
            velho.unlink(missing_ok=True)
        arquivo.write_text(json.dumps(dados), encoding="utf-8")
    return dados


def tocar(video: Path, acao: str, janelas, args) -> str:
    """Devolve 'proximo' ou 'sair'."""
    dados = obter_analise(video, acao, args)
    if not dados:
        return "sair"
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"Não abriu: {video}")
        return "proximo"
    fps = dados["fps"]
    amostras = dados["amostras"]
    # Tamanho da janela pela tela de quem está assistindo, não fixo.
    args.zoom = _zoom_para_tela(int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640,
                                int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 360, args.zoom_pedido)
    rotulo = vision.ROTULO_ACAO.get(acao, acao)

    disparos = []            # (t, bbox)
    vermelhos = {}           # track_id -> até quando fica vermelho
    confianca_acao = {}      # track_id -> confiança do último disparo
    # Registro do que a IA detectou, gravado a cada disparo — dá para abrir o
    # arquivo no VS Code enquanto o vídeo toca.
    registros: list = []
    capturas: list = []      # prints dos disparos, para o painel
    pasta_prints = args.saida / "prints"
    saida_json = args.saida / f"deteccoes_{acao}_{video.stem}.json"
    saida_json.parent.mkdir(parents=True, exist_ok=True)

    def gravar_json():
        saida_json.write_text(json.dumps({
            "video": str(video), "acao": acao, "rotulo": rotulo,
            "condicao": vision.detalhe_acao(acao),
            "deteccoes": registros,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    gravar_json()
    alvos = [j for j in (janelas or []) if j.tipo == acao]
    pausado = False
    indice = 0
    k = 0                    # próxima análise a alcançar
    atual = None
    ultimo = ultimo_img = None
    estado: dict = {}
    proximo = time.perf_counter()
    while True:
        if not pausado:
            ok, quadro = cap.read()
            if not ok:
                break
            t = indice / fps
            novos = []
            # As análises deste quadro: disparos e quem segue correndo.
            while k < len(amostras) and amostras[k]["i"] <= indice:
                atual = amostras[k]
                k += 1
                ta = atual["t"]
                largura, altura = atual["w"], atual["h"]
                for e in atual["eventos"]:
                    disparos.append((ta, e["bbox"]))
                    if e["id"] is not None:
                        vermelhos[e["id"]] = ta + 2.5
                        confianca_acao[e["id"]] = e["confianca"]
                    gab = next((j for j in alvos if j.contem(ta)), None)
                    if gab is not None and gab.detectado_em is None:
                        gab.detectado_em = ta
                    outro = next((j for j in (janelas or [])
                                  if j.tipo != acao and j.contem(ta)), None)
                    x1, y1, x2, y2 = (int(v) for v in (e["bbox"] or (0, 0, largura, altura)))
                    registro = {
                        "video": video.name,
                        "segundo": round(ta, 2),
                        "quadro": atual["i"],
                        "acao": acao,
                        "rotulo": rotulo,
                        "confianca": round(e["confianca"], 2),
                        "severidade": e["severidade"],
                        "pessoa_id": e["id"],
                        "bbox": [x1, y1, x2, y2],
                        "bbox_normalizado": [round(x1 / largura, 4), round(y1 / altura, 4),
                                             round(x2 / largura, 4), round(y2 / altura, 4)],
                        "velocidade_corpos_s": e["velocidade"],
                        "duracao_s": e["duracao_s"],
                        "tempo_video": _tempo(ta),
                        "horario_registro": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                    }
                    if janelas is not None:
                        registro["gabarito"] = (
                            {"resultado": "acerto", "trecho": f"{gab.inicio_s}-{gab.fim_s}s",
                             "descricao": gab.descricao} if gab else
                            {"resultado": "alarme falso",
                             "descricao": outro.descricao if outro else "cena normal"})
                    registros.append(registro)
                    novos.append(registro)
                    print(f"  {video.name} {ta:6.1f}s  IA disparou: {rotulo} "
                          f"({e['confianca']:.0%})")
                for tid in atual["rapidos"]:
                    if tid in confianca_acao:
                        vermelhos[tid] = max(vermelhos.get(tid, -1), ta + 0.5)

            objs = _interpolar(atual, amostras[k] if k < len(amostras) else None,
                               indice) if atual else []
            img = _desenhar_caixas(quadro, objs, args.zoom, vermelhos, confianca_acao,
                                   t, rotulo)
            # O print sai do quadro já desenhado: com a caixa vermelha do
            # disparo, que é o que se quer mostrar.
            for r in novos:
                cap_ = _fazer_print(img, [int(v * args.zoom) for v in r["bbox"]], r, pasta_prints)
                r["print"] = cap_["arquivo"].relative_to(RAIZ).as_posix()
                capturas.append(cap_)
                if not args.sem_alerta:
                    _enviar_alerta(args.servidor, r, cap_["arquivo"])
            if novos:
                gravar_json()
            agora = next((j for j in alvos if j.inicio_s <= t <= j.fim_s), None)
            estado = {
                "situacao": "detectada" if any(t - d[0] <= 2.5 for d in disparos) else "analisando",
                "rotulo": rotulo, "video": video.name, "t": t,
                "disparos": len(disparos),
                "tem_gabarito": bool(alvos),
                "no_gabarito": sum(j.inicio_s <= t for j in alvos),
                "detectou": sum(j.detectado_em is not None for j in alvos),
                "gabarito_agora": agora.descricao if agora else None,
            }
            ultimo = _com_painel(img, capturas, saida_json, estado)
            ultimo_img = img
            indice += 1

        if ultimo is not None:
            if pausado:
                cv2.imshow(JANELA, _com_painel(ultimo_img, capturas, saida_json,
                                               dict(estado, situacao="pausado")))
            else:
                cv2.imshow(JANELA, ultimo)
        # Ritmo do vídeo: cada quadro na sua hora, 25 por segundo. Atrasou
        # (a máquina engasgou), recomeça a contar daqui em vez de correr.
        proximo += 1.0 / fps
        espera = proximo - time.perf_counter()
        if espera < -0.25 or pausado:
            proximo = time.perf_counter()
            espera = 0
        tecla = cv2.waitKey(100 if pausado else max(1, int(espera * 1000))) & 0xFF
        if tecla in (ord("q"), 27):
            cap.release()
            return "sair"
        if tecla == ord("n"):
            break
        if tecla == ord(" "):
            pausado = not pausado
        if tecla == ord("p") and ultimo is not None:
            pasta_prints.mkdir(parents=True, exist_ok=True)
            arq = pasta_prints / f"janela_{video.stem}_{datetime.now():%H%M%S}.png"
            cv2.imwrite(str(arq), ultimo)
            print(f"  print da janela: {arq.relative_to(RAIZ)}")
        try:
            if cv2.getWindowProperty(JANELA, cv2.WND_PROP_VISIBLE) < 1:
                cap.release()
                return "sair"
        except cv2.error:
            return "sair"

    cap.release()
    if alvos:
        print(f"  {video.name}: detectou {sum(j.detectado_em is not None for j in alvos)} "
              f"de {len(alvos)} do gabarito; {len(disparos)} disparo(s) no total")
    # Tela final do vídeo, para dar tempo de ler o placar.
    if ultimo is not None:
        cv2.imshow(JANELA, _com_painel(ultimo_img, capturas, saida_json,
                                       dict(estado, situacao="fim")))
        if (cv2.waitKey(0) & 0xFF) in (ord("q"), 27):
            return "sair"
    return "proximo"


def main() -> int:
    p = argparse.ArgumentParser(description="Toca um vídeo com a análise da IA por cima.")
    p.add_argument("caminho", type=Path, help="vídeo ou pasta de vídeos")
    p.add_argument("--acao", required=True)
    p.add_argument("--gabarito", type=Path)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--limiar", type=float, default=0.25)
    p.add_argument("--amostras", type=float, default=AMOSTRAS_POR_S)
    p.add_argument("--zoom", type=float, dest="zoom_pedido", default=None,
                   help="tamanho do vídeo (padrão: o maior que cabe na tela)")
    p.add_argument("--direcao", default="direita")
    p.add_argument("--bytetrack", action="store_true",
                   help="comparação: rastreador do YOLO no lugar do rastreio do sistema")
    p.add_argument("--sem-subita", action="store_true",
                   help="comparação: corrida sem exigir a passagem de andar para correr")
    p.add_argument("--sem-alerta", action="store_true",
                   help="não cria alerta no sistema a cada disparo")
    p.add_argument("--servidor", default="http://127.0.0.1:8000",
                   help="endereço do sistema que recebe os alertas")
    p.add_argument("--reanalisar", action="store_true",
                   help="ignora a análise guardada e roda a IA de novo")
    p.add_argument("--saida", type=Path, default=RAIZ / "resultados_teste" / "ao_vivo",
                   help="pasta do JSON com as detecções")
    p.add_argument("--permanencia-s", type=float)
    p.add_argument("--abandono-s", type=float)
    p.add_argument("--limite-aglomeracao", type=int)
    args = p.parse_args()

    if args.caminho.is_dir():
        videos = sorted(a for a in args.caminho.iterdir() if a.suffix.lower() in EXTENSOES)
    else:
        videos = [args.caminho]
    gab = ler_gabarito(args.gabarito) if args.gabarito else None

    cv2.namedWindow(JANELA, cv2.WINDOW_AUTOSIZE)
    try:
        for v in videos:
            print(f"\n> {v.name}")
            if tocar(v, args.acao, _janelas_do_video(gab, v) if gab else None, args) == "sair":
                break
    finally:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
