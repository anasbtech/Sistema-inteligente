"""Marca/modelo de carro: análise com um CLIP grande, fora do servidor.

Medido em 10/11/2026 nos 31 carros rotulados da Portaria, comparando o
recorte com os 87 modelos de carro do catálogo (zero-shot, ViT-L/14 DFN2B):
acerta o modelo exato em 14 (45%) e põe o certo entre os 3 primeiros em 21
(68%). Errou com 87–89% de confiança (Fox → Etios, Fit → Cruze). Por isso a
comparação com o catálogo **nunca afirma** nada: vira as três sugestões
que a ficha oferece para rotular com um clique.

Quem afirma é a sonda treinada com os rótulos desta instalação sobre o
vetor deste mesmo modelo (scripts/veiculos_treinar.py, atributo
marca_modelo), com o piso de cada classe validado para 95% de precisão.

Roda em processo próprio (scripts/veiculos_modelo_rodar.py): o modelo ocupa
~1,7 GB e ~3–8 s de CPU por carro, que o servidor ao vivo não pode pagar.
Moto fica de fora — a pedido da operação, só carros têm modelo por enquanto.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.veiculos")

NOME, PESOS = "ViT-L-14-quickgelu", "dfn2b"
MOLDES = ["a photo of a {}.", "a CCTV photo of a {} car.", "a photo of a {}, a type of car.",
          "a low resolution photo of a {}.", "a photo of a {} seen from above."]
# Pasta própria: a sonda antiga de marca/modelo era do CLIP pequeno (512
# dimensões), e um servidor de antes desta versão a aplicaria sobre o vetor
# errado. Com nome novo, cada versão só lê a sua.
SONDA = "modelo_carro"
OUTRO = "__outro__"          # classe de treino: conferido, mas de modelo sem exemplos
SEM_MODELO = {"motocicleta"}          # tipos que não recebem marca/modelo
PASTA = Path(settings.VEICULO_MARCA_MODELO_DIR)

_modelo = None


def carregar(threads: int = 4):
    global _modelo
    if _modelo is None:
        import open_clip
        import torch
        torch.set_num_threads(max(1, threads))
        m, _, prep = open_clip.create_model_and_transforms(NOME, pretrained=PESOS)
        m.eval()
        _modelo = (m, prep, open_clip.get_tokenizer(NOME))
        log.info("CLIP %s (%s) carregado para marca/modelo", NOME, PESOS)
    return _modelo


def catalogo_carros() -> list[tuple[str, str]]:
    from app.services.veiculos.catalogo_br import MODELOS
    return [(marca, modelo) for marca, modelo, cat in MODELOS if cat != "moto"]


def textos() -> tuple[list, np.ndarray]:
    """Vetor de texto de cada modelo do catálogo, guardado em disco."""
    carros = catalogo_carros()
    arq = PASTA / "textos_L14.npz"
    if arq.exists():
        with np.load(arq, allow_pickle=False) as d:
            if [tuple(x) for x in d["carros"].tolist()] == [tuple(c) for c in carros]:
                return carros, d["T"]
    import torch
    m, _, tok = carregar()
    with torch.no_grad():
        T = []
        for marca, modelo in carros:
            e = m.encode_text(tok([t.format(f"{marca} {modelo}") for t in MOLDES]))
            e = e / e.norm(dim=-1, keepdim=True)
            e = e.mean(0)
            T.append((e / e.norm()).numpy())
    T = np.stack(T).astype(np.float32)
    PASTA.mkdir(parents=True, exist_ok=True)
    np.savez(arq, carros=np.array(carros), T=T)
    return carros, T


def vetor(img_bgr: np.ndarray) -> np.ndarray:
    import cv2
    import torch
    from PIL import Image
    m, prep, _ = carregar()
    with torch.no_grad():
        f = m.encode_image(prep(Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))).unsqueeze(0))
    f = f[0].numpy().astype(np.float32)
    return f / (np.linalg.norm(f) or 1.0)


def sugestoes(v: np.ndarray, k: int = 3) -> list[dict]:
    carros, T = textos()
    lg = 100.0 * (T @ v)
    p = np.exp(lg - lg.max())
    p /= p.sum()
    return [{"marca": carros[j][0], "modelo": carros[j][1], "conf": round(float(p[j]), 3)}
            for j in np.argsort(-p)[:k]]


def afirmar(v: np.ndarray) -> tuple[str, str, Optional[float], str]:
    """(marca, modelo, confiança, motivo) pela sonda treinada; vazio se não há."""
    from app.services.veiculos.identificacao import aplicar_sonda, sonda
    s = sonda(SONDA)
    if s is None:
        return "", "", None, "modelos ainda sem exemplos rotulados suficientes"
    if s["W"].shape[0] != v.shape[0]:
        return "", "", None, "sonda de marca/modelo de outra versão; treine de novo"
    rotulo, conf, motivo = aplicar_sonda(s, v)
    # "Outro": carro conferido que não é nenhum modelo com exemplos. Sem esta
    # classe, a sonda empurrava todo carro desconhecido para o conhecido mais
    # parecido — em 10/11/2026 chamou picape, van e até moto de "Kwid".
    if rotulo == OUTRO or (not rotulo and s["classes"][int(np.argmax(
            (v * s["escala"]) @ s["W"] + s["b"]))] == OUTRO):
        return "", "", round(conf, 3), "não se parece com nenhum modelo já conferido"
    if not rotulo:
        return "", "", round(conf, 3), motivo
    marca, _, modelo = rotulo.partition("|")
    return marca, modelo, round(conf, 3), ""
