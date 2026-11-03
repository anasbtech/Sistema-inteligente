"""Detectores de classes que o modelo do parque não conhece.

O YOLO que roda nas 26 câmeras é treinado no COCO, e o COCO tem 80 classes —
pessoa, carro, faca, cachorro. **Capacete e arma de fogo não estão entre elas**,
e nenhum ajuste de limiar faz aparecer o que o modelo nunca viu. Para essas
duas o caminho é outro modelo, rodando sobre o mesmo quadro.

Aqui fica só o encaixe: carregar um modelo YOLO extra, rodá-lo nas câmeras que
pedirem e traduzir a saída dele para as classes do SEGURANÇA. O modelo em si
não vem embarcado, e isso é deliberado — veja abaixo.

**Por que o modelo não vem junto.** Procurei os disponíveis publicamente antes
de escrever isto. Para capacete, o mais usado é treinado em capacete de obra
(EPI), que é outro objeto: num presídio o que interessa costuma ser o de
motociclista, porque esconde o rosto. Para arma de fogo, todos os modelos que
encontrei tinham zero downloads registrados — nenhum com uso comprovado.

Um detector de arma que erra para mais manda equipe armada atrás de nada; um
que erra para menos dá falsa segurança a quem confia nele. Num sistema que
responde por segurança prisional, embarcar um classificador não validado cria
risco em vez de reduzir. Então o encaixe fica pronto, e o modelo entra quando a
instituição escolher e validar o seu — apontando o caminho em
`EXTRA_CAPACETE_MODEL` / `EXTRA_ARMA_MODEL`.

Sem modelo configurado o serviço simplesmente não roda, e nada no resto do
sistema muda.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.extras")


@dataclass(frozen=True)
class Detector:
    """Um modelo extra e como ler o que ele devolve.

    `mapa` traduz o rótulo do modelo para a classe do SEGURANÇA. É por câmera
    que se decide usá-lo; aqui só se descreve o que ele sabe fazer.
    """
    chave: str
    caminho: str
    mapa: Dict[str, str]
    limiar: float


def _catalogo() -> Dict[str, Detector]:
    return {
        "capacete": Detector(
            "capacete", settings.EXTRA_CAPACETE_MODEL,
            # Os nomes variam conforme o treino. Cobrir as grafias usuais evita
            # que um modelo válido entre mudo só por chamar a classe de outro
            # jeito; o que não estiver no mapa é ignorado em silêncio.
            {"helmet": "capacete", "hardhat": "capacete", "hard hat": "capacete",
             "hard-hat": "capacete", "capacete": "capacete", "head": "",
             "person": "", "no-helmet": "", "no_helmet": ""},
            settings.EXTRA_CAPACETE_MIN),
        "arma": Detector(
            "arma", settings.EXTRA_ARMA_MODEL,
            {"gun": "arma", "pistol": "arma", "rifle": "arma", "weapon": "arma",
             "handgun": "arma", "firearm": "arma", "knife": "arma",
             "arma": "arma"},
            settings.EXTRA_ARMA_MIN),
    }


_modelos: Dict[str, object] = {}
_indisponivel: set = set()


def configurado(chave: str) -> bool:
    """Há modelo apontado e o arquivo existe?"""
    d = _catalogo().get(chave)
    return bool(d and d.caminho and os.path.exists(d.caminho))


def disponiveis() -> Dict[str, bool]:
    return {k: configurado(k) for k in _catalogo()}


def _carregar(chave: str):
    """Carrega na primeira vez. Falhou, não tenta de novo.

    Sem esta trava, um parque sem o modelo tentaria abrir o arquivo a cada
    quadro de cada câmera — e são 24 câmeras em série.
    """
    if chave in _indisponivel:
        return None
    if chave in _modelos:
        return _modelos[chave]
    d = _catalogo().get(chave)
    if not d or not d.caminho or not os.path.exists(d.caminho):
        _indisponivel.add(chave)
        return None
    try:
        from ultralytics import YOLO
        modelo = YOLO(d.caminho)
        _modelos[chave] = modelo
        log.info("Detector extra '%s' carregado de %s", chave, d.caminho)
        return modelo
    except Exception as e:                                   # noqa: BLE001
        log.warning("Detector extra '%s' indisponível: %s", chave, e)
        _indisponivel.add(chave)
        return None


def detectar(quadro: np.ndarray, chaves: List[str]) -> List[tuple]:
    """Roda os detectores pedidos e devolve (classe, confiança, caixa).

    Devolve tupla crua em vez de `ObjetoDetectado` para não amarrar este
    módulo ao de visão — quem chama monta o objeto no formato que já usa.
    """
    achados: List[tuple] = []
    for chave in chaves:
        modelo = _carregar(chave)
        if modelo is None:
            continue
        d = _catalogo()[chave]
        try:
            resultados = modelo(quadro, conf=d.limiar, verbose=False,
                                imgsz=settings.OBJECT_IMGSZ)
        except Exception as e:                               # noqa: BLE001
            log.warning("Detector '%s' falhou no quadro: %s", chave, e)
            _indisponivel.add(chave)
            continue
        for r in resultados:
            if r.boxes is None:
                continue
            nomes = r.names
            for box in r.boxes:
                bruto = str(nomes[int(box.cls[0])]).lower().strip()
                classe = d.mapa.get(bruto)
                if not classe:        # fora do mapa, ou mapeado para vazio
                    continue
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                achados.append((classe, float(box.conf[0]), (x1, y1, x2, y2)))
    return achados
