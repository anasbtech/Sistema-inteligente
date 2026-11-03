"""Toca um vídeo com a análise da IA desenhada por cima, ao vivo.

Serve para *ver* o sistema decidir: cada pessoa ganha caixa, número de
rastreio, rastro e a velocidade que o analisador está medindo. Quando a ação
dispara, aparece uma faixa vermelha; quando o gabarito diz que a ação está
acontecendo, uma faixa amarela embaixo — é a comparação que importa.

    py -3.13 -m scripts.ver_acao_video "videos_teste/Avenue Dataset/testing_videos/02.avi" --acao corrida --gabarito "videos_teste/Avenue Dataset/gabarito_corrida.csv"

Uma pasta inteira (um vídeo atrás do outro):

    py -3.13 -m scripts.ver_acao_video "videos_teste/Avenue Dataset/testing_videos" --acao corrida --gabarito ...

Teclas na janela:
    espaço   pausa / continua
    n        próximo vídeo
    q / Esc  sai

Usa o mesmo detector, o mesmo rastreio e o mesmo analisador da câmera ao vivo
(`scripts/testar_acoes_video.py` faz a bateria com relatório).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from pathlib import Path

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


PAINEL_L = 430


def _com_painel(img, registros, caminho_json):
    """Painel ao lado do vídeo com o JSON das últimas detecções."""
    h = img.shape[0]
    painel = np.full((h, PAINEL_L, 3), (24, 24, 24), np.uint8)
    cv2.putText(painel, "REGISTRO (JSON)", (14, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.65,
                BRANCO, 2, cv2.LINE_AA)
    cv2.putText(painel, _a(caminho_json.name), (14, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.42,
                (170, 170, 170), 1, cv2.LINE_AA)
    y = 78
    if not registros:
        cv2.putText(painel, "nenhuma deteccao ainda", (14, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.48, (150, 150, 150), 1, cv2.LINE_AA)
    else:
        # Destaque da última detecção: a ação e a confiança, grandes.
        r = registros[-1]
        cv2.rectangle(painel, (10, y - 8), (PAINEL_L - 10, y + 62), VERMELHO, -1)
        cv2.putText(painel, _a(r["rotulo"].upper()), (20, y + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, BRANCO, 2, cv2.LINE_AA)
        cv2.putText(painel, f"confianca {r['confianca']:.0%}   em {r['segundo']:.1f}s",
                    (20, y + 48), cv2.FONT_HERSHEY_SIMPLEX, 0.55, BRANCO, 1, cv2.LINE_AA)
        y += 86
    for r in reversed(registros[-3:]):
        g = r.get("gabarito", {})
        cor = (VERDE if g.get("resultado") == "acerto"
               else VERMELHO if g else AMARELO)
        linhas = [
            "{",
            f'  "segundo": {r["segundo"]},',
            f'  "acao": "{r["acao"]}",',
            f'  "rotulo": "{_a(r["rotulo"])}",',
            f'  "confianca": {r["confianca"]},',
            f'  "pessoa_id": {r["pessoa_id"]},',
            f'  "bbox": {r["bbox"]},',
            f'  "velocidade_corpos_s": {r["velocidade_corpos_s"]},',
        ]
        if g:
            linhas.append(f'  "gabarito": "{_a(g["resultado"])}"')
        linhas.append("}")
        for i, l in enumerate(linhas):
            if y > h - 10:
                break
            cv2.putText(painel, l, (14, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45,
                        cor if i in (0, len(linhas) - 1) else BRANCO, 1, cv2.LINE_AA)
            y += 19
        y += 10
    return np.hstack([img, painel])


def tocar(video: Path, acao: str, janelas, args) -> str:
    """Devolve 'proximo' ou 'sair'."""
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        print(f"Não abriu: {video}")
        return "proximo"
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    pular = max(1, round(fps / args.amostras))
    an = preparar_analisador(acao, args)
    if args.sem_subita and acao == "corrida":
        an.salto_corrida = 0.0
    rastreio = vision.RastreioPorPosicao(ttl_s=8.0)
    if args.bytetrack:
        vision.obter_yolo().predictor = None
    zonas = zonas_padrao(acao, args.direcao)
    rotulo = vision.ROTULO_ACAO.get(acao, acao)

    disparos = []            # (t, bbox)
    vermelhos = {}           # track_id -> até quando fica vermelho
    confianca_acao = {}      # track_id -> confiança do último disparo
    # Registro do que a IA detectou, gravado a cada disparo — dá para abrir o
    # arquivo no VS Code enquanto o vídeo toca.
    registros: list = []
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
    ultimo = None
    while True:
        if not pausado:
            ok = cap.grab()
            if not ok:
                break
            if indice % pular:
                indice += 1
                continue
            ok, quadro = cap.retrieve()
            t = indice / fps
            indice += 1
            if not ok:
                continue
            altura, largura = quadro.shape[:2]

            if args.bytetrack:
                objs = vision.detectar_objetos(quadro, args.limiar, True, set(), "nunca",
                                               args.imgsz)
            else:
                objs = rastreio.atribuir(
                    vision.detectar_objetos(quadro, args.limiar, False, set(), "nunca",
                                            args.imgsz), agora=T0 + t)
            eventos = [e for e in an.atualizar(objs, largura, altura, zonas, agora=T0 + t)
                       if e.classe == acao]
            for e in eventos:
                disparos.append((t, e.bbox))
                if e.track_id is not None:
                    vermelhos[e.track_id] = t + 2.5
                    confianca_acao[e.track_id] = float(e.confianca)
                tr = an.trilhas.get(e.track_id)
                gab = next((j for j in alvos if j.contem(t)), None)
                if gab is not None and gab.detectado_em is None:
                    gab.detectado_em = t
                outro = next((j for j in (janelas or [])
                              if j.tipo != acao and j.contem(t)), None)
                x1, y1, x2, y2 = (int(v) for v in (e.bbox or (0, 0, largura, altura)))
                registro = {
                    "video": video.name,
                    "segundo": round(t, 2),
                    "quadro": indice - 1,
                    "acao": acao,
                    "rotulo": rotulo,
                    "confianca": round(float(e.confianca), 2),
                    "severidade": e.severidade,
                    "pessoa_id": e.track_id,
                    "bbox": [x1, y1, x2, y2],
                    "bbox_normalizado": [round(x1 / largura, 4), round(y1 / altura, 4),
                                         round(x2 / largura, 4), round(y2 / altura, 4)],
                    "velocidade_corpos_s": round(tr.arranque()[0], 2) if tr else None,
                    "duracao_s": e.duracao_s,
                }
                if janelas is not None:
                    registro["gabarito"] = (
                        {"resultado": "acerto", "trecho": f"{gab.inicio_s}-{gab.fim_s}s",
                         "descricao": gab.descricao} if gab else
                        {"resultado": "alarme falso",
                         "descricao": outro.descricao if outro else "cena normal"})
                registros.append(registro)
                gravar_json()
                print(f"  {video.name} {t:6.1f}s  IA disparou: {rotulo} ({e.confianca:.0%})")

            img = quadro.copy()
            # caixas
            for o in objs:
                if o.classe != "pessoa":
                    continue
                disparou = vermelhos.get(o.track_id, -1) >= t
                cor = VERMELHO if disparou else VERDE
                x1, y1, x2, y2 = map(int, o.bbox)
                cv2.rectangle(img, (x1, y1), (x2, y2), cor, 3 if disparou else 2)
                # Só a caixa e a confiança. Na corrida, o rótulo da ação com a
                # confiança dela, numa tarja sobre a caixa da pessoa.
                if disparou:
                    texto = f"{_a(rotulo.upper())} {confianca_acao.get(o.track_id, 0):.0%}"
                    escala, espessura = 0.5, 1
                else:
                    texto = f"pessoa {o.confianca:.0%}"
                    escala, espessura = 0.42, 1
                (tw, th), _ = cv2.getTextSize(texto, cv2.FONT_HERSHEY_SIMPLEX, escala, espessura)
                ty = max(0, y1 - th - 8)
                cv2.rectangle(img, (x1, ty), (x1 + tw + 8, ty + th + 8), cor, -1)
                cv2.putText(img, texto, (x1 + 4, ty + th + 4), cv2.FONT_HERSHEY_SIMPLEX,
                            escala, BRANCO if disparou else (0, 0, 0), espessura, cv2.LINE_AA)

            # faixas
            vivo = [d for d in disparos if t - d[0] <= 2.5]
            if vivo:
                _faixa(img, 0, f"IA: {rotulo.upper()} DETECTADA", VERMELHO)
            else:
                _faixa(img, 0, f"IA analisando: {rotulo}", (40, 40, 40), escala=0.6)
            agora = next((j for j in alvos if j.inicio_s <= t <= j.fim_s), None)
            if agora is not None:
                _faixa(img, altura - 34, f"GABARITO: {acao} acontecendo - {agora.descricao}",
                       AMARELO, (0, 0, 0), 0.55)
            achou = sum(j.detectado_em is not None for j in alvos)
            info = (f"{video.name}  {t:5.1f}s   disparos: {len(disparos)}"
                    + (f"   corridas no gabarito: {sum(j.inicio_s <= t for j in alvos)}"
                       f"   IA detectou: {achou}" if alvos else ""))
            # Fundo escuro sob o placar e a legenda: sem ele, as linhas da
            # imagem atravessavam as letras.
            cv2.rectangle(img, (0, 36), (largura, 84), (30, 30, 30), -1)
            cv2.putText(img, _a(info), (12, 56), cv2.FONT_HERSHEY_SIMPLEX, 0.55, BRANCO, 2,
                        cv2.LINE_AA)
            cv2.putText(img, _a(f"verde: pessoa detectada   vermelho: {rotulo.lower()}"),
                        (12, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.42, BRANCO, 1, cv2.LINE_AA)
            ultimo = _com_painel(cv2.resize(img, None, fx=args.zoom, fy=args.zoom),
                                 registros, saida_json)

        if ultimo is not None:
            if pausado:
                p = ultimo.copy()
                _faixa(p, p.shape[0] // 2 - 17, "PAUSADO - espaco continua", (90, 90, 90))
                cv2.imshow(JANELA, p)
            else:
                cv2.imshow(JANELA, ultimo)
        tecla = cv2.waitKey(1 if not pausado else 100) & 0xFF
        if tecla in (ord("q"), 27):
            cap.release()
            return "sair"
        if tecla == ord("n"):
            break
        if tecla == ord(" "):
            pausado = not pausado
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
            fim = ultimo.copy()
            _faixa(fim, fim.shape[0] // 2 - 17,
                   f"FIM {video.name}: {sum(j.detectado_em is not None for j in alvos)} de "
                   f"{len(alvos)} detectadas - qualquer tecla segue", (60, 60, 60))
            cv2.imshow(JANELA, fim)
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
    p.add_argument("--zoom", type=float, default=1.6, help="tamanho da janela")
    p.add_argument("--direcao", default="direita")
    p.add_argument("--bytetrack", action="store_true",
                   help="comparação: rastreador do YOLO no lugar do rastreio do sistema")
    p.add_argument("--sem-subita", action="store_true",
                   help="comparação: corrida sem exigir a passagem de andar para correr")
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
