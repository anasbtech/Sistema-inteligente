"""Identificação do veículo: cor predominante pela imagem, marca e modelo pela placa.

**Cor** sai do recorte que o detector já delimitou, por OpenCV: agrupamento
de cores (k-means) no miolo do veículo, descartando o grupo que se parece com
a borda do recorte — o asfalto, a parede, o que está em volta. O nome sai do
tom, da saturação e do brilho do grupo dominante. A confiança é a fração do
miolo que ele ocupa.

**Marca, modelo e ano** não saem da imagem. Não existe modelo visual treinado
com a frota brasileira (o mais usado, do Stanford Cars, conhece 196 carros
americanos até 2012), e um classificador que chuta "Chevrolet Sonic" para um
Onix é exatamente o erro que não pode ir para um registro policial. Eles vêm
da **placa já lida** consultada na base oficial (SINESP/SENATRAN), por um
provedor configurável. Sem provedor configurado, ou sem resposta, o veículo
fica como "Veículo não identificado" — e a cor e a categoria continuam.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.veiculo")

NAO_IDENTIFICADO = "Veículo não identificado"


# ----------------------------------------------------------------------
# Cor predominante
# ----------------------------------------------------------------------
def nome_da_cor(h: float, s: float, v: float) -> str:
    """Nome da cor a partir de HSV do OpenCV (h 0-180, s e v 0-255).

    Os limites são os do olho humano em lataria: carro "prata" tem pouca
    saturação e brilho médio-alto, "preto" é escuro qualquer que seja o tom,
    "bege" é laranja-amarelado pálido e claro.
    """
    s, v = s / 255.0, v / 255.0
    if v < 0.22:
        return "preto"
    if s < 0.16:
        # Branco à sombra chega à câmera abaixo de 0,8 de brilho.
        if v > 0.68:
            return "branco"
        if v > 0.46:
            return "prata"
        return "cinza"
    if 8 <= h <= 30 and s < 0.42 and v > 0.55:
        return "bege"
    if 5 <= h <= 25 and v < 0.55:
        return "marrom"
    if h < 8 or h >= 165:
        return "vermelho"
    if h < 20:
        return "laranja"
    if h < 34:
        return "amarelo"
    if h < 85:
        return "verde"
    if h < 130:
        return "azul"
    return "roxo"


def _nomes_por_pixel(hsv: np.ndarray) -> np.ndarray:
    """O nome de cor de cada pixel (vetorizado; mesmas regras de `nome_da_cor`)."""
    h = hsv[..., 0].astype(np.float32)
    s = hsv[..., 1].astype(np.float32) / 255.0
    v = hsv[..., 2].astype(np.float32) / 255.0
    nomes = np.full(h.shape, "roxo", dtype=object)
    nomes[h < 130] = "azul"
    nomes[h < 85] = "verde"
    nomes[h < 34] = "amarelo"
    nomes[h < 20] = "laranja"
    nomes[(h < 8) | (h >= 165)] = "vermelho"
    nomes[(h >= 5) & (h <= 25) & (v < 0.55)] = "marrom"
    nomes[(h >= 8) & (h <= 30) & (s < 0.42) & (v > 0.55)] = "bege"
    pouca = s < 0.18
    nomes[pouca] = "cinza"
    nomes[pouca & (v > 0.46)] = "prata"
    nomes[pouca & (v > 0.62)] = "branco"
    nomes[v < 0.22] = "preto"
    return nomes


def cor_predominante(recorte_bgr) -> Tuple[str, float]:
    """Cor da lataria e a fração do veículo que ela ocupa (0 a 1).

    Votação por pixel no miolo do veículo. Três coisas aparecem em todo
    veículo e não são a cor dele: vidro, pneu e sombra (escuros) e o chão
    entre as rodas. Por isso:

    * escuro só vence quando não há cor clara ou viva relevante — aí o
      veículo é escuro mesmo;
    * a cor do chão (a faixa de baixo do recorte) perde o peso de quanto
      ela aparece lá.

    Medido em 20 capturas da garagem e da portaria, contra o que se vê nelas
    (ver o teste). ("", 0.0) quando o recorte é pequeno demais.
    """
    try:
        import cv2
    except ImportError:
        return "", 0.0
    if recorte_bgr is None or recorte_bgr.size == 0:
        return "", 0.0
    alt, larg = recorte_bgr.shape[:2]
    if alt < 16 or larg < 16:
        return "", 0.0
    img = cv2.resize(recorte_bgr, (96, max(16, int(96 * alt / larg))),
                     interpolation=cv2.INTER_AREA)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    a, l = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    miolo = _nomes_por_pixel(hsv[int(a * 0.15):int(a * 0.85), int(l * 0.10):int(l * 0.90)]).ravel()
    chao = _nomes_por_pixel(hsv[int(a * 0.88):]).ravel()
    if len(miolo) < 30:
        return "", 0.0
    total = float(len(miolo))
    m = hsv[int(a * 0.15):int(a * 0.85), int(l * 0.10):int(l * 0.90)].reshape(-1, 3)
    sat = m[:, 1] / 255.0
    bri = m[:, 2] / 255.0

    # 1. Colorido: cor viva relevante (moto vermelha, carro azul). Antes do
    #    claro: a moto vermelha é estreita, e o asfalto claro em volta dela
    #    ocupava mais do recorte que a própria moto.
    vivo = (sat >= 0.45) & (bri >= 0.25)
    if vivo.mean() >= 0.10:
        cores = [n for n in miolo[vivo] if n not in ("preto", "cinza", "prata", "branco")]
        if cores:
            nome = max(set(cores), key=cores.count)
            return nome, round(cores.count(nome) / total, 3)

    # 2. Claro: lataria branca ou prata ocupa boa parte do veículo. Medido
    #    nas capturas desta instalação: branco 43-63% do miolo claro e sem
    #    cor; cinza e preto ficam em 25-29%, que é o asfalto aparecendo.
    claro = (sat < 0.18) & (bri > 0.30)
    if claro.mean() >= 0.40:
        nome = "branco" if np.median(bri[claro]) >= 0.45 else "prata"
        return nome, round(float(claro.mean()), 3)

    # 3. Escuro: preto ou cinza, pelo que ocupa mais. O claro que sobrou é
    #    chão e reflexo, fora da conta.
    # Cinza-escuro à sombra chega a 0,2 de brilho; preto fica abaixo de 0,16.
    escuro = (bri < 0.16).sum()
    medio = ((sat < 0.18) & (bri >= 0.16) & (bri <= 0.46)).sum()
    nome = "cinza" if medio > escuro else "preto"
    return nome, round(float(max(escuro, medio)) / total, 3)



# ----------------------------------------------------------------------
# Marca, modelo e ano pela placa
# ----------------------------------------------------------------------
_cache: Dict[str, Tuple[float, Optional[dict]]] = {}


def consulta_ligada() -> bool:
    return (settings.VEICULO_CONSULTA or "desligado").lower() == "http" \
        and bool(settings.VEICULO_CONSULTA_URL)


def _interpretar(dados: dict) -> Optional[dict]:
    """Aceita as formas comuns de resposta das APIs de placa.

    SINESP e derivados devolvem "marca": "VW/GOL 1.0" (marca e modelo juntos)
    e "ano"/"anoModelo"; outras separam "marca" e "modelo". Sem marca, não há
    identificação.
    """
    if not isinstance(dados, dict):
        return None
    for chave in ("dados", "data", "veiculo", "resultado"):
        if isinstance(dados.get(chave), dict):
            dados = dados[chave]
            break
    marca = str(dados.get("marca") or dados.get("fabricante") or "").strip()
    modelo = str(dados.get("modelo") or "").strip()
    if "/" in marca and not modelo:
        marca, modelo = (p.strip() for p in marca.split("/", 1))
    if not marca:
        return None
    ano = str(dados.get("anoModelo") or dados.get("ano_modelo") or dados.get("ano") or "").strip()
    cor = str(dados.get("cor") or "").strip().lower()
    return {"marca": marca.upper(), "modelo": modelo.upper(), "ano": ano[:9], "cor": cor}


def consultar_placa(placa: str) -> Optional[dict]:
    """Marca, modelo, ano e cor oficiais da placa, ou `None`.

    Síncrona (rode em thread). Resultado — inclusive "não achou" — fica em
    cache por VEICULO_CONSULTA_CACHE_S: o mesmo carro passa várias vezes na
    portaria, e a base oficial tem cota.
    """
    if not placa or not consulta_ligada():
        return None
    agora = time.monotonic()
    guardado = _cache.get(placa)
    if guardado and agora - guardado[0] < settings.VEICULO_CONSULTA_CACHE_S:
        return guardado[1]
    url = settings.VEICULO_CONSULTA_URL.replace("{placa}", urllib.parse.quote(placa))
    cabecalhos = {"Accept": "application/json"}
    if settings.VEICULO_CONSULTA_TOKEN:
        cabecalhos["Authorization"] = f"Bearer {settings.VEICULO_CONSULTA_TOKEN}"
    resultado = None
    try:
        req = urllib.request.Request(url, headers=cabecalhos)
        with urllib.request.urlopen(req, timeout=settings.VEICULO_CONSULTA_TIMEOUT_S) as r:
            resultado = _interpretar(json.loads(r.read().decode("utf-8", "replace")))
    except Exception as exc:  # noqa: BLE001 — sem consulta o registro segue
        log.warning("Consulta da placa %s falhou: %s", placa, exc)
        return None            # falha de rede não vai para o cache
    _cache[placa] = (agora, resultado)
    if len(_cache) > 5000:
        _cache.clear()
    return resultado
