"""Identificação de uma passagem: contorno, vetor, cor, marca/modelo e placa.

Roda uma vez por passagem, fora do laço de rastreio (custa ~2 s em CPU).

**Cor e marca/modelo** saem de "sondas": uma camada linear treinada sobre o
vetor CLIP do veículo, com exemplos **rotulados nesta instalação**
(scripts/veiculos_treinar.py). Sem sonda treinada, o atributo fica vazio com
o motivo. Com sonda, cada classe tem o seu piso, calculado na validação para
a precisão-alvo; classe que não alcançou a meta tem piso infinito e nunca é
afirmada.

Por que não usar o CLIP sozinho, sem treino: medido em 06/11/2026, ele
chamou 8 de 11 Kombis brancas da Portaria de outra coisa — uma de "Jeep
Renegade" com 69% de confiança. Para cor, sem treino, acertou 56%.

**Tipo** (carro, picape, van, caminhão pequeno, caminhão grande, ônibus,
moto) também sai de uma sonda. O detector geral só conhece carro, moto,
caminhão e ônibus — não existe "van" nele —, e chamava a Kombi da Portaria
de "ônibus" a 49% (09/11/2026). Sem sonda confiante, vale a votação do
detector com a ressalva de porte: caminhão e ônibus só com convicção; na
dúvida, "veículo", que não é falso para nenhum deles.

**Placa** reaproveita `services/placa.py` sobre os maiores recortes da
passagem, e só é afirmada por consenso (VEICULO_PLACA_VOTOS) ou por uma
leitura acima de 0,90.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.veiculos")

CLIP_NOME, CLIP_PESOS = "ViT-B-16", "laion2b_s34b_b88k"
COCO_VEICULOS = [2, 3, 5, 7]
PASTA_SONDAS = Path(settings.VEICULO_MARCA_MODELO_DIR).parent

_clip = None
_seg = None
_trava = threading.Lock()
_sondas: dict = {}

# Tipos que o sistema sabe nomear. "veiculo" é a resposta honesta quando
# nenhuma fonte tem convicção — nunca é rótulo de treino.
TIPOS = {
    "carro": "Carro", "picape": "Picape", "kombi": "Kombi", "van": "Van / utilitário",
    "caminhao_pequeno": "Caminhão pequeno", "caminhao_grande": "Caminhão grande",
    "onibus": "Ônibus", "motocicleta": "Motocicleta", "veiculo": "Veículo",
}
TIPOS_ROTULO = [t for t in TIPOS if t != "veiculo"]
# O registro geral (lista de objetos, regras de zona) fala o vocabulário do
# detector. Picape e van circulam como carro; os dois portes, como caminhão.
CLASSE_GERAL = {"carro": "carro", "picape": "carro", "van": "carro", "kombi": "carro",
                "veiculo": "carro",
                "caminhao_pequeno": "caminhao", "caminhao_grande": "caminhao",
                "onibus": "onibus", "motocicleta": "motocicleta"}


@dataclass
class Resultado:
    tipo: str = ""
    tipo_conf: Optional[float] = None
    cor: str = ""
    cor_conf: Optional[float] = None
    marca: str = ""
    modelo: str = ""
    marca_modelo_conf: Optional[float] = None
    placa: str = ""
    placa_conf: Optional[float] = None
    placa_recorte: Optional[np.ndarray] = None
    vetor: Optional[list] = None
    observacoes: dict = field(default_factory=dict)


# ----------------------------------------------------------------------
# Modelos (carregados uma vez, na primeira passagem)
# ----------------------------------------------------------------------
def _carregar_clip():
    global _clip
    if _clip is not None:
        return _clip
    try:
        import open_clip
        import torch
        torch.set_num_threads(max(1, settings.VEICULO_CLIP_THREADS))
        modelo, _, prep = open_clip.create_model_and_transforms(CLIP_NOME, pretrained=CLIP_PESOS)
        modelo.eval()
        _clip = (modelo, prep)
        log.info("CLIP %s carregado para a identificação veicular", CLIP_NOME)
    except Exception as exc:  # noqa: BLE001
        log.warning("CLIP indisponível (%s): sem cor e sem marca/modelo.", exc)
        _clip = False
    return _clip


def _carregar_seg():
    global _seg
    if _seg is not None:
        return _seg
    try:
        from ultralytics import YOLO
        _seg = YOLO(settings.VEICULO_MODELO_SEG)
        _seg.overrides["device"] = "cpu"
    except Exception as exc:  # noqa: BLE001
        log.warning("Segmentação de veículo indisponível (%s): cor sem contorno.", exc)
        _seg = False
    return _seg


def sonda(nome: str) -> Optional[dict]:
    """A camada treinada `nome` (cor | marca_modelo), relida se o arquivo mudou."""
    arq = PASTA_SONDAS / nome / "sonda.npz"
    if not arq.exists():
        _sondas.pop(nome, None)
        return None
    marca = arq.stat().st_mtime
    atual = _sondas.get(nome)
    if atual and atual["_mtime"] == marca:
        return atual
    with np.load(arq, allow_pickle=False) as d:
        s = {"W": d["W"], "b": d["b"], "escala": float(d["escala"]),
             "classes": [str(c) for c in d["classes"]], "pisos": d["pisos"].astype(float),
             "_mtime": marca}
    _sondas[nome] = s
    log.info("Sonda '%s' carregada: %d classes, %d liberadas", nome,
             len(s["classes"]), int(np.isfinite(s["pisos"]).sum()))
    return s


def aplicar_sonda(s: dict, vetor: np.ndarray) -> tuple[str, float, str]:
    """(classe, confiança, motivo). Classe vazia quando não alcança o piso."""
    logits = (vetor * s["escala"]) @ s["W"] + s["b"]
    p = np.exp(logits - logits.max())
    p /= p.sum()
    k = int(np.argmax(p))
    conf = float(p[k])
    if not np.isfinite(s["pisos"][k]):
        return "", conf, f"'{s['classes'][k]}' ainda sem exemplos validados"
    if conf < s["pisos"][k]:
        return "", conf, f"confiança {conf:.0%} abaixo do piso de '{s['classes'][k]}'"
    return s["classes"][k], conf, ""


# ----------------------------------------------------------------------
# Etapas
# ----------------------------------------------------------------------
def mascara(recorte: np.ndarray) -> Optional[np.ndarray]:
    """Contorno do veículo principal (o maior e mais central) no recorte."""
    seg = _carregar_seg()
    if not seg:
        return None
    h, w = recorte.shape[:2]
    with _trava:
        r = seg(recorte, imgsz=640, conf=0.25, classes=COCO_VEICULOS,
                verbose=False, retina_masks=True)[0]
    if r.masks is None or not len(r.boxes):
        return None
    melhor, nota = None, -9.0
    for k, b in enumerate(r.boxes.xyxy.tolist()):
        area = (b[2] - b[0]) * (b[3] - b[1]) / (w * h)
        cx, cy = (b[0] + b[2]) / 2 / w, (b[1] + b[3]) / 2 / h
        n = area - abs(cx - .5) - abs(cy - .5)
        if n > nota:
            melhor, nota = k, n
    return r.masks.data[melhor].cpu().numpy() > 0.5


def isolar(recorte: np.ndarray, m: Optional[np.ndarray]) -> np.ndarray:
    """O veículo sobre fundo cinza neutro, recortado no contorno — é a mesma
    preparação com que as sondas foram treinadas."""
    if m is None or m.sum() < 200:
        return recorte
    ys, xs = np.where(m)
    y1, y2, x1, x2 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    img = recorte[y1:y2, x1:x2].copy()
    img[~m[y1:y2, x1:x2]] = 128
    return img


def vetor_clip(img_bgr: np.ndarray) -> Optional[np.ndarray]:
    c = _carregar_clip()
    if not c:
        return None
    import cv2
    import torch
    from PIL import Image
    modelo, prep = c
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    with _trava, torch.no_grad():
        f = modelo.encode_image(prep(pil).unsqueeze(0))
    f = f[0].numpy().astype(np.float32)
    return f / (np.linalg.norm(f) or 1.0)


def ler_placa(recortes: list) -> tuple:
    """Consenso entre os recortes: (texto, confiança, recorte) ou ('', None, None)."""
    from app.services import placa
    leituras = []
    for rec in recortes:
        lida = placa.ler_com_recorte(rec)
        if lida:
            leituras.append(lida)
    if not leituras:
        return "", None, None
    votos = Counter(t for t, _, _ in leituras)
    texto, n = votos.most_common(1)[0]
    da_placa = [x for x in leituras if x[0] == texto]
    melhor = max(da_placa, key=lambda x: x[1])
    if n >= settings.VEICULO_PLACA_VOTOS or melhor[1] >= 0.90:
        return texto, round(float(melhor[1]), 3), melhor[2]
    return "", None, None


NEUTRAS = ("branco", "prata/cinza", "preto")


def tom_vivo(recorte: np.ndarray, m: Optional[np.ndarray]) -> tuple[float, Optional[str]]:
    """Fração da lataria com cor viva e o tom dominante dela (pixels, sem modelo)."""
    import cv2
    if m is None or m.shape[:2] != recorte.shape[:2] or m.sum() < 200:
        h, w = recorte.shape[:2]
        m = np.zeros((h, w), bool)
        m[int(h * .2):int(h * .8), int(w * .15):int(w * .85)] = True
    hsv = cv2.cvtColor(cv2.GaussianBlur(recorte, (3, 3), 0), cv2.COLOR_BGR2HSV)[m]
    s, v, h = hsv[:, 1] / 255.0, hsv[:, 2] / 255.0, hsv[:, 0]
    viva = (s >= 0.45) & (v >= 0.30)
    if viva.sum() < 30:
        return float(viva.mean()), None
    hh = h[viva]
    nomes = np.where((hh < 8) | (hh >= 165), "vermelho",
            np.where(hh < 20, "laranja", np.where(hh < 34, "amarelo",
            np.where(hh < 85, "verde", np.where(hh < 130, "azul", "roxo")))))
    valores, contagem = np.unique(nomes, return_counts=True)
    return float(viva.mean()), str(valores[contagem.argmax()])


def conferir_tom(recorte: np.ndarray, m: Optional[np.ndarray], cor: str) -> str:
    """Segunda opinião, pelos pixels, sobre a cor que a sonda afirmou.

    A sonda só conhece as cores que já têm exemplo; um carro amarelo (zero
    exemplos) saiu "branco" a 98% em 09/11/2026, e uma moto azul "vermelho".
    Medido nos 94 recortes rotulados: branco, preto e prata/cinza têm no
    máximo 5% da lataria em cor viva — 15% é folga de três vezes.
    Devolve o motivo para não afirmar, ou "" se a cor se sustenta.
    """
    fracao, tom = tom_vivo(recorte, m)
    if cor in NEUTRAS and fracao >= 0.15:
        return (f"a lataria tem cor viva ({tom or 'indefinida'}) que o modelo "
                "ainda não sabe nomear")
    if cor not in NEUTRAS and fracao >= 0.10 and tom and tom != cor:
        return f"o tom da lataria ({tom}) não confere com {cor}"
    return ""


def decidir_tipo(v: Optional[np.ndarray], categoria: str,
                 categoria_conf: float = 0.0) -> tuple[str, float, str]:
    """(tipo, confiança, motivo). A sonda manda quando tem convicção; senão,
    vale a categoria do detector (que já passou pela ressalva de porte)."""
    s = sonda("tipo")
    motivo = ""
    if v is not None and s is not None:
        tipo, conf, motivo = aplicar_sonda(s, v)
        if tipo:
            return tipo, conf, ""
    elif v is None:
        motivo = "CLIP indisponível"
    else:
        motivo = "modelo de tipo ainda não treinado"
    if categoria in ("caminhao", "onibus"):
        # Mesmo convicto, o detector não sabe o porte: caminhão sem a sonda
        # sai "veículo" em vez de um chute entre pequeno e grande.
        if categoria == "caminhao":
            return "veiculo", categoria_conf, motivo or "porte do caminhão não identificado"
        return "onibus", categoria_conf, motivo
    return (categoria if categoria in TIPOS else "veiculo"), categoria_conf, motivo


def tipo_previa(recorte: np.ndarray, categoria: str) -> tuple[str, float, str]:
    """Só o tipo, para a tarja ao vivo enquanto o veículo ainda está em cena."""
    v = vetor_clip(isolar(recorte, mascara(recorte)))
    return decidir_tipo(v, categoria)


def identificar(recorte: np.ndarray, recortes_placa: list, categoria: str,
                categoria_conf: float = 0.0) -> Resultado:
    res = Resultado()
    m = mascara(recorte)
    if m is None:
        res.observacoes["contorno"] = "segmentação não achou o veículo; usado o retângulo"
    v = vetor_clip(isolar(recorte, m))
    if v is not None:
        res.vetor = [round(float(x), 5) for x in v]

    tipo, conf, motivo = decidir_tipo(v, categoria, categoria_conf)
    res.tipo, res.tipo_conf = tipo, round(float(conf), 3)
    if motivo:
        res.observacoes["tipo"] = motivo

    s = sonda("cor")
    if v is None:
        res.observacoes["cor"] = "CLIP indisponível"
    elif s is None:
        res.observacoes["cor"] = "modelo de cor ainda não treinado"
    else:
        cor, conf, motivo = aplicar_sonda(s, v)
        if cor and not motivo:
            motivo = conferir_tom(recorte, m, cor)
            if motivo:
                cor = ""
        res.cor, res.cor_conf = cor, round(conf, 3)
        if motivo:
            res.observacoes["cor"] = motivo

    # Marca/modelo sai depois, do analisador em processo próprio
    # (services/veiculos/modelo_fino.py): o CLIP pequeno daqui não separa
    # modelos — carros diferentes chegam a 0,90 de semelhança nele.
    if res.tipo == "motocicleta" or categoria == "motocicleta":
        res.observacoes["marca_modelo"] = "modelo de moto não é identificado (só carros)"
    else:
        res.observacoes["marca_modelo"] = "aguardando a análise de modelo"

    texto, conf, rec = ler_placa(recortes_placa)
    res.placa, res.placa_conf, res.placa_recorte = texto, conf, rec
    if not texto:
        res.observacoes["placa"] = "nenhuma leitura com qualidade suficiente"
    return res


def estado() -> dict:
    """Para a tela: o que está pronto, o que falta."""
    def info(nome):
        s = sonda(nome)
        rel = PASTA_SONDAS / nome / "relatorio.json"
        r = json.loads(rel.read_text(encoding="utf-8")) if rel.exists() else None
        if s is None:
            return {"treinado": False, "relatorio": r}
        liberadas = [c for c, p in zip(s["classes"], s["pisos"]) if np.isfinite(p)]
        return {"treinado": True, "classes": s["classes"], "liberadas": liberadas, "relatorio": r}
    return {"tipo": info("tipo"), "cor": info("cor"), "marca_modelo": info("modelo_carro")}
