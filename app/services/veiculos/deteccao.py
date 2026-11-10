"""Detector de veículos (YOLO11) — só carro, moto, caminhão e ônibus.

Instância própria, separada do detector geral: roda num ritmo outro (vários
quadros por segundo nas câmeras veiculares, contra uma visita a cada dezenas
de segundos no laço geral) e não pode disputar o mesmo objeto do Ultralytics
entre threads.

Motor: CUDA se houver; senão OpenVINO no vídeo integrado Intel, com o cache
de compilação ao lado do modelo (a primeira compilação leva minutos); senão
CPU. A troca é automática e fica registrada em `MOTOR`.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import List, Optional

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.veiculos")

# COCO → nome do sistema. Bicicleta fica com o laço geral: não tem placa nem
# marca/modelo para este módulo agregar.
CLASSES = {2: "carro", 3: "motocicleta", 5: "onibus", 7: "caminhao"}

MOTOR = "indisponível"
_modelo = None
_trava = threading.Lock()
_falhou = False


def _dispositivo() -> str:
    motor = (settings.VEICULO_MOTOR or "auto").lower()
    if motor in ("auto", "cuda"):
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except ImportError:
            pass
    if motor in ("auto", "openvino-gpu", "openvino-cpu"):
        try:
            import openvino as ov
            disp = ov.Core().available_devices
            if motor != "openvino-cpu" and any(d.startswith("GPU") for d in disp):
                return "intel:gpu"
            return "intel:cpu"
        except ImportError:
            pass
    return "cpu"


def _carregar():
    global _modelo, MOTOR, _falhou
    if _modelo is not None or _falhou:
        return _modelo
    # A mesma trava do detector geral: os dois compilando para o vídeo
    # integrado ao mesmo tempo carregam o compilador (LLVM) duas vezes no
    # processo, e ele derruba o servidor inteiro ("Option 'h' registered more
    # than once") — aconteceu duas vezes seguidas na partida de 10/11/2026.
    from app.services import vision
    with _trava, vision._trava_carga:
        if _modelo is not None or _falhou:
            return _modelo
        try:
            from ultralytics import YOLO
        except ImportError:
            log.warning("Ultralytics não instalado: inteligência veicular sem detector.")
            _falhou = True
            return None
        pt = Path(settings.VEICULO_MODELO)
        if not pt.exists():
            log.warning("Modelo de veículos não encontrado: %s", pt)
            _falhou = True
            return None
        disp = _dispositivo()
        try:
            if disp.startswith("intel"):
                # A resolução vai no nome: a conversão é para um tamanho fixo, e
                # sem isso trocar VEICULO_IMGSZ reaproveitaria a conversão velha.
                pasta = pt.with_name(f"{pt.stem}_{settings.VEICULO_IMGSZ}_openvino_model")
                if not (pasta / f"{pt.stem}.xml").exists():
                    log.info("Convertendo %s para OpenVINO (só na primeira vez)...", pt.name)
                    saida = YOLO(str(pt)).export(format="openvino", half=False,
                                                 imgsz=settings.VEICULO_IMGSZ, verbose=False)
                    Path(saida).replace(pasta)
                _cache_openvino(pasta)
                modelo = YOLO(str(pasta), task="detect")
            else:
                modelo = YOLO(str(pt))
            modelo.overrides["device"] = disp
            modelo(np.zeros((720, 1280, 3), np.uint8), imgsz=settings.VEICULO_IMGSZ,
                   classes=list(CLASSES), verbose=False)
        except Exception as exc:  # noqa: BLE001
            if disp == "cpu":
                log.warning("Detector de veículos não carregou: %s", exc)
                _falhou = True
                return None
            log.warning("Detector de veículos em %s falhou (%s); usando CPU.", disp, exc)
            modelo, disp = YOLO(str(pt)), "cpu"
            modelo.overrides["device"] = "cpu"
        _modelo, MOTOR = modelo, f"{pt.stem} ({disp})"
        log.info("Detector de veículos: %s", MOTOR)
        return _modelo


def _cache_openvino(pasta: Path) -> None:
    """Mesmo cache de compilação que `vision` usa para o detector geral."""
    try:
        import openvino as ov
    except ImportError:
        return
    cache = pasta / "cache_gpu"
    cache.mkdir(exist_ok=True)
    # O Ultralytics cria o próprio `ov.Core()`: configurar outra instância não
    # vale para ele (medido: 2 min compilando a cada arranque). O cache entra
    # pela classe, como em `vision._carregar_openvino`; se o detector geral já
    # trocou a classe, o cache dele serve aos dois.
    if getattr(ov.Core, "_seguranca_cache", False):
        return
    original = ov.Core

    class _CoreComCache(original):
        _seguranca_cache = True

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            try:
                self.set_property({"CACHE_DIR": str(cache)})
            except Exception:  # noqa: BLE001
                pass
    ov.Core = _CoreComCache


def disponivel() -> bool:
    return _carregar() is not None


def detectar(quadro: np.ndarray, conf: Optional[float] = None) -> np.ndarray:
    """Matriz N x 6: x1, y1, x2, y2, confiança, classe COCO. Vazia sem modelo."""
    m = _carregar()
    if m is None or quadro is None:
        return np.zeros((0, 6), np.float32)
    with _trava:     # um objeto do Ultralytics não é seguro entre threads
        # NMS sem distinção de classe: o furgão sai ao mesmo tempo "carro" e
        # "caminhão" em caixas quase iguais, e cada uma viraria uma trilha.
        r = m(quadro, imgsz=settings.VEICULO_IMGSZ, classes=list(CLASSES),
              conf=conf if conf is not None else settings.VEICULO_CONF_MIN * 0.5,
              agnostic_nms=True, verbose=False)[0]
    if r.boxes is None or not len(r.boxes):
        return np.zeros((0, 6), np.float32)
    return r.boxes.data.cpu().numpy().astype(np.float32)


def nome(classe_coco: int) -> str:
    return CLASSES.get(int(classe_coco), "veiculo")


def caixas(dets: np.ndarray) -> List[tuple]:
    """Para depuração e testes: (bbox, conf, nome)."""
    return [((float(d[0]), float(d[1]), float(d[2]), float(d[3])), float(d[4]), nome(d[5]))
            for d in dets]
