"""Conformidade de EPI: o que cada pessoa em cena está usando.

O modelo do parque (COCO) não conhece capacete, colete, luva nem óculos. Este
serviço roda um detector próprio de EPI — SafetyVision v2 (YOLOv8s, 13
classes, 80 mil imagens, mAP@0,5 de teste 0,75; AGPL-3.0, como o Ultralytics
que o sistema já usa) — no **recorte de cada pessoa**, ampliado: na cena
inteira, luva e óculos têm poucos pixels.

O modelo vem em ONNX de propósito: só pesos, sem código executável ao abrir.

Cada item sai em um de três estados, e o terceiro é o que torna a tela
honesta:

* **detectado** — o modelo viu o item;
* **ausente** — o modelo viu a *falta* (ele tem classes "sem capacete",
  "sem colete"...);
* **nao_verificado** — nem um nem outro: rosto de costas, item fora do
  quadro, ou item que o modelo não cobre (botas).

"Em conformidade" só quando todo item exigido foi *detectado*; qualquer
ausente é "fora de conformidade"; o resto é "inconclusivo". Medido nas
imagens de referência: colete e capacete saem bem, óculos razoável, luvas
fracas — e botas não existem no modelo.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.epi")

# Itens que o sistema conhece, na ordem do corpo (cabeça aos pés).
ITENS: Dict[str, str] = {
    "capacete": "Capacete",
    "oculos": "Óculos de proteção",
    "mascara": "Máscara",
    "colete": "Colete refletivo",
    "luvas": "Luvas",
    "botas": "Botas de segurança",
}
# Classe do modelo -> (item, estado).
MAPA = {
    "Hardhat": ("capacete", "detectado"), "NO-Hardhat": ("capacete", "ausente"),
    "Goggles": ("oculos", "detectado"), "NO-Goggles": ("oculos", "ausente"),
    "Mask": ("mascara", "detectado"), "NO-Mask": ("mascara", "ausente"),
    "Safety Vest": ("colete", "detectado"), "NO-Safety Vest": ("colete", "ausente"),
    "Gloves": ("luvas", "detectado"), "NO-Gloves": ("luvas", "ausente"),
}
# O modelo não tem classe de bota: o item existe na tela, sempre "não verificado".
SEM_DETECTOR = {"botas"}

_modelo = None
_trava = threading.Lock()
_indisponivel = False


def disponivel() -> bool:
    return not _indisponivel and Path(settings.EPI_MODEL).exists()


def _carregar():
    global _modelo, _indisponivel
    if _modelo is not None or _indisponivel:
        return _modelo
    with _trava:
        if _modelo is None and not _indisponivel:
            try:
                from ultralytics import YOLO
                _modelo = YOLO(settings.EPI_MODEL, task="detect")
                log.info("Detector de EPI carregado (%s)", Path(settings.EPI_MODEL).name)
            except Exception as exc:  # noqa: BLE001
                _indisponivel = True
                log.warning("Detector de EPI indisponível: %s", exc)
    return _modelo


def recorte_da_pessoa(quadro: np.ndarray, bbox) -> Tuple[np.ndarray, Tuple[int, int]]:
    """A pessoa com folga: capacete passa da caixa por cima, luva pelos lados."""
    alt, larg = quadro.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    w, h = x2 - x1, y2 - y1
    X1, Y1 = max(0, int(x1 - w * .18)), max(0, int(y1 - h * .10))
    X2, Y2 = min(larg, int(x2 + w * .18)), min(alt, int(y2 + h * .04))
    return quadro[Y1:Y2, X1:X2], (X1, Y1)


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def _procurar_luvas(m, quadro: np.ndarray, bbox) -> List[tuple]:
    """Segunda passada só para luvas: a área da pessoa em 6 pedaços ampliados.

    Na passada normal a luva tem poucos pixels e o modelo não a vê — medido
    numa foto com as duas mãos enluvadas e à vista: nada, nem com limiar 5%.
    Ampliando a região da mão, 81% e 84%. Como a mão pode estar levantada,
    abaixada ou aberta para o lado, a área ganha folga larga para os lados e
    para cima e é varrida em pedaços, em vez de adivinhar onde a mão está.
    Devolve (estado, confiança, caixa) por mão, sem repetir a mesma mão.
    """
    import cv2

    alt, larg = quadro.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    w, h = x2 - x1, y2 - y1
    X1, Y1 = max(0, int(x1 - w * .45)), max(0, int(y1 - h * .30))
    X2, Y2 = min(larg, int(x2 + w * .45)), min(alt, int(y2 + h * .05))
    area = quadro[Y1:Y2, X1:X2]
    ah, aw = area.shape[:2]
    if ah < 16 or aw < 16:
        return []
    achados = []
    for cx in range(3):
        for cy in range(2):
            tw, th = aw / 3 * 1.35, ah / 2 * 1.35
            tx1 = max(0, int(min(aw - tw, cx * aw / 3 - (tw - aw / 3) / 2)))
            ty1 = max(0, int(min(ah - th, cy * ah / 2 - (th - ah / 2) / 2)))
            peca = area[ty1:int(ty1 + th), tx1:int(tx1 + tw)]
            if peca.size == 0:
                continue
            f = 640 / max(peca.shape[:2])
            r = m(cv2.resize(peca, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC),
                  conf=settings.EPI_CONF_MIN, imgsz=640, verbose=False)[0]
            for c, p, b in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist(), r.boxes.xyxy.tolist()):
                par = MAPA.get(m.names[int(c)])
                if not par or par[0] != "luvas":
                    continue
                achados.append((par[1], float(p), [X1 + tx1 + b[0] / f, Y1 + ty1 + b[1] / f,
                                                   X1 + tx1 + b[2] / f, Y1 + ty1 + b[3] / f]))
    achados.sort(key=lambda a: -a[1])
    maos: List[tuple] = []
    for a in achados:                       # uma caixa por mão
        if all(_iou(a[2], o[2]) < 0.3 for o in maos):
            maos.append(a)
    return maos[:2]


def _pele(img: np.ndarray) -> float:
    """Fração de pixels com tom de pele (faixa YCrCb clássica, vale para peles
    claras e escuras: o que muda entre elas é o brilho, não o Cr/Cb)."""
    import cv2

    if img.size == 0:
        return 0.0
    y = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    cr, cb = y[..., 1], y[..., 2]
    return float(((cr >= 135) & (cr <= 180) & (cb >= 80) & (cb <= 135)).mean())


def _conferir_mascara(quadro: np.ndarray, bbox) -> Optional[dict]:
    """Máscara pelo rosto, não pelo modelo de EPI.

    O SafetyVision não reconhece máscara descartável (medido: zero, mesmo
    com o rosto ampliado e limiar de 5%). O detector de rosto do sistema
    marca olhos, nariz e boca; daí compara-se a pele da testa com a da
    região nariz–queixo. Medido: com máscara a boca dá 0% de pele contra
    77–100% na testa; sem máscara, 90–100% nas duas. Testa sem pele visível
    (rosto de lado, sombra) não afirma nada. Barba cheia pode passar por
    máscara — por isso a confiança fica abaixo da do detector.
    """
    import cv2

    try:
        from app.services import face_engine
        det = face_engine.obter_modelo().det_model
    except Exception:  # noqa: BLE001
        return None
    alt, larg = quadro.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in bbox)
    w, h = x2 - x1, y2 - y1
    X1, Y1 = max(0, int(x1 - w * .15)), max(0, int(y1 - h * .12))
    X2, Y2 = min(larg, int(x2 + w * .15)), min(alt, int(y1 + h * .45))
    cab = quadro[Y1:Y2, X1:X2]
    if cab.shape[0] < 16 or cab.shape[1] < 16:
        return None
    f = min(640 / max(cab.shape[:2]), 6.0)
    img = cv2.resize(cab, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    try:
        caixas, pontos = det.detect(img, max_num=0, metric="default")
    except Exception:  # noqa: BLE001
        return None
    if caixas is None or len(caixas) == 0 or pontos is None:
        return None
    i = int(np.argmax(caixas[:, 4]))
    score, k = float(caixas[i, 4]), pontos[i]
    d = float(np.linalg.norm(k[1] - k[0]))
    if score < 0.5 or d < 8:
        return None
    olhos_y = (k[0][1] + k[1][1]) / 2
    boca_y = (k[3][1] + k[4][1]) / 2
    # Só rosto de frente. De perfil ou olhando para a mesa, a faixa
    # "nariz–queixo" cai na bochecha, no pescoço ou na sombra, e a falta de
    # pele ali parecia máscara (medido: operador de cabeça baixa, olhos a
    # 17 px num rosto de 99 px, saía "máscara 57%"). De frente, os olhos
    # ficam a ~40% da largura do rosto e nariz e boca entre eles.
    largura = float(caixas[i, 2] - caixas[i, 0])
    olho_e, olho_d = sorted((k[0][0], k[1][0]))
    # E rosto com pixels de verdade: olhos a menos de 11 px na imagem original
    # (pessoa ao fundo, rosto de ~16 px) viram borrão cinza, e cinza na boca
    # parece máscara. Rostos lidos certo nas fotos de teste: olhos a 16–28 px.
    if (d / f < 11 or d < largura * .3 or not (olho_e <= k[2][0] <= olho_d)
            or not (olhos_y < k[2][1] < boca_y)):
        return None
    xa, xb = int(min(k[3][0], k[0][0])), int(max(k[4][0], k[1][0]))
    testa = _pele(img[max(0, int(olhos_y - .55 * d)):max(0, int(olhos_y - .25 * d)), max(0, xa):xb])
    baixo = _pele(img[int(k[2][1]):int(boca_y + .35 * d), max(0, xa):xb])
    if testa < 0.4:
        return None
    caixa = [X1 + (xa - .25 * d) / f, Y1 + (k[2][1] - .45 * d) / f,
             X1 + (xb + .25 * d) / f, Y1 + (boca_y + .6 * d) / f]
    caixa = [round(max(0.0, float(v)), 1) for v in caixa]      # float do numpy não vira JSON
    if baixo <= 0.25:
        return {"estado": "detectado", "confianca": round(min(.9, score * (1 - baixo / testa)), 3),
                "bbox": caixa}
    if baixo >= 0.6:
        return {"estado": "ausente", "confianca": round(min(.9, score * min(1.0, baixo / testa)), 3),
                "bbox": caixa}
    return None


def _cor(quadro: np.ndarray, b, parte: str) -> Optional[str]:
    """Cor do equipamento, para o boneco vestir o que a IA viu.

    A caixa do capacete pega testa e fundo; a do colete, camisa e braço. Por
    isso: só a parte onde o equipamento está (copa do capacete, miolo do
    colete) e, dela, os 35% de pixels mais vivos — capacete e colete são
    feitos para chamar a atenção, o que está em volta não.
    """
    import cv2

    x1, y1, x2, y2 = (int(v) for v in b)
    w, h = x2 - x1, y2 - y1
    if parte == "capacete":             # a copa: abaixo dela já é testa
        m = quadro[max(0, y1 + h // 12):max(0, y1 + h * 3 // 10), max(0, x1 + w // 4):max(0, x2 - w // 4)]
        if m.size < 12:
            return None
        return _hex(np.median(m.reshape(-1, 3), axis=0))
    else:
        m = quadro[max(0, y1 + h // 6):max(0, y2 - h // 4), max(0, x1 + w // 8):max(0, x2 - w // 8)]
    if m.size < 12:
        return None
    px = m.reshape(-1, 3)
    hsv = cv2.cvtColor(m, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(float)
    vivo = hsv[:, 1] * .6 + hsv[:, 2] * .4
    sel = px[vivo >= np.percentile(vivo, 65)]
    return _hex(np.median(sel, axis=0))


def _hex(bgr) -> str:
    """BGR -> #RRGGBB, clareado como sob luz boa: a sombra da cena não é a cor
    do equipamento (capacete branco na sombra sai cinza)."""
    bgr = np.asarray(bgr, float)
    bgr = np.clip(bgr * (235 / max(1.0, bgr.max())), 0, 255).astype(int)
    return "#%02X%02X%02X" % (bgr[2], bgr[1], bgr[0])


CABECA = {"capacete", "oculos", "mascara"}


def _pertence(item: str, caixa, pessoa) -> float:
    """Quanto um item achado no recorte é *desta* pessoa (0 = não é).

    O recorte tem folga para os lados — é por onde entram capacete inclinado
    e mão aberta —, mas numa equipe lado a lado essa folga traz o capacete do
    vizinho, que às vezes sai mais confiante que o da própria pessoa. Item de
    cabeça precisa estar sobre o eixo do corpo e na parte de cima dele; o
    colete, sobre o tronco. Luva fica com a mão, que pode estar longe.
    """
    x1, y1, x2, y2 = (float(v) for v in pessoa)
    w, h = max(1.0, x2 - x1), max(1.0, y2 - y1)
    cx, cy = (caixa[0] + caixa[2]) / 2, (caixa[1] + caixa[3]) / 2
    meio = (x1 + x2) / 2
    if item in CABECA:
        if cy > y1 + h * .45:
            return 0.0
        return max(0.0, 1 - abs(cx - meio) / (w * .5))
    if item == "colete":
        return max(0.0, 1 - abs(cx - meio) / (w * .75))
    return 1.0


def analisar_pessoa(quadro: np.ndarray, bbox, exigidos: List[str],
                    detalhar: bool = True, mascara: Optional[bool] = None) -> dict:
    """EPI de uma pessoa: estado de cada item, caixas e o resultado."""
    m = _carregar()
    itens = {k: {"estado": "nao_verificado", "confianca": None, "bbox": None} for k in ITENS}
    if m is not None:
        rec, (ox, oy) = recorte_da_pessoa(quadro, bbox)
        if rec.size:
            r = m(rec, conf=settings.EPI_CONF_MIN, imgsz=640, verbose=False)[0]
            pesos: Dict[str, float] = {}
            for cls, conf, xyxy in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist(),
                                       r.boxes.xyxy.tolist()):
                par = MAPA.get(m.names[int(cls)])
                if par is None:
                    continue
                item, estado = par
                caixa = [xyxy[0] + ox, xyxy[1] + oy, xyxy[2] + ox, xyxy[3] + oy]
                peso = conf * _pertence(item, caixa, bbox)
                if peso <= 0:
                    continue            # é do vizinho: a folga do recorte o trouxe junto
                # "Ausente" só vence "detectado" se for mais confiante.
                if peso > pesos.get(item, 0):
                    pesos[item] = peso
                    itens[item] = {"estado": estado, "confianca": round(float(conf), 3),
                                   "bbox": caixa}
        # Luva exigida e não vista: a passada ampliada (custa ~1 s por pedaço
        # com a máquina ocupada, por isso só quando faz diferença).
        if detalhar and "luvas" in exigidos and itens["luvas"]["estado"] != "detectado":
            maos = _procurar_luvas(m, quadro, bbox)
            if maos:
                melhor = maos[0]
                itens["luvas"] = {"estado": melhor[0], "confianca": round(melhor[1], 3),
                                  "bbox": [round(v, 1) for v in melhor[2]],
                                  "caixas": [[round(v, 1) for v in x[2]] for x in maos]}
        # Máscara: na tela sempre (o boneco mostra o que se vê), no ao vivo só se exigida.
        olhar_rosto = (detalhar if mascara is None else mascara) or "mascara" in exigidos
        if olhar_rosto and itens["mascara"]["estado"] != "detectado":
            masc = _conferir_mascara(quadro, bbox)
            if masc:
                itens["mascara"] = masc
        for k in ("capacete", "colete"):
            if itens[k]["estado"] == "detectado" and itens[k]["bbox"]:
                itens[k]["cor"] = _cor(quadro, itens[k]["bbox"], k)
    exig = [e for e in exigidos if e in ITENS]
    estados = [itens[e]["estado"] for e in exig]
    if not exig:
        resultado = "sem_exigencia"
    elif "ausente" in estados:
        resultado = "fora"
    elif all(s == "detectado" for s in estados):
        resultado = "conforme"
    else:
        resultado = "inconclusivo"
    return {"itens": itens, "exigidos": exig, "resultado": resultado,
            "faltando": [e for e in exig if itens[e]["estado"] == "ausente"]}


def analisar_quadro(quadro: np.ndarray, pessoas: List[tuple], exigidos: List[str],
                    detalhar_ate: int = 2, mascara_ate: int = 6) -> List[dict]:
    """`pessoas`: lista de (bbox, confiança, track_id), as mais próximas primeiro.

    A passada ampliada das luvas (~5 s) só nas `detalhar_ate` primeiras; a
    checagem de máscara (~1 s) nas `mascara_ate` primeiras. Quem está ao
    fundo não tem mão nem rosto com pixels para isso.
    """
    saida = []
    for n, (bbox, conf, tid) in enumerate(pessoas):
        r = analisar_pessoa(quadro, bbox, exigidos, detalhar=n < detalhar_ate,
                             mascara=n < mascara_ate)
        r.update({"pessoa_bbox": [float(v) for v in bbox], "pessoa_conf": round(float(conf), 3),
                  "track_id": tid})
        saida.append(r)
    return saida
