"""Um modelo ONNX rodando no OpenVINO com a cara de uma sessão do onnxruntime.

O InsightFace só sabe falar com `onnxruntime.InferenceSession` (`get_inputs`,
`get_outputs`, `run`). O onnxruntime instalado aqui só tem CPU, e o vídeo
integrado Intel — onde o detector de objetos já roda pelo OpenVINO — fica
de fora. Esta classe compila o mesmo arquivo .onnx no OpenVINO e responde
pelos mesmos três métodos, então o InsightFace nem percebe a troca.

Medido em 13/11/2026 num quadro da CAM-016 com o parque ligado (4 rostos):
detecção + reconhecimento no onnxruntime/CPU 3.758 ms, no OpenVINO/GPU
1.541 ms, com vetores iguais (similaridade 0,9997 por rosto).

Forma fixa: o detector sempre redimensiona a imagem para `det_size` antes
de rodar, e o reconhecimento recebe 112 × 112. Forma dinâmica no vídeo
integrado recompila a cada tamanho novo — é o que não se quer.
"""
from __future__ import annotations

import threading

import numpy as np


class _Entrada:
    def __init__(self, nome: str, forma):
        self.name, self.shape = nome, forma


class SessaoOpenVINO:
    def __init__(self, caminho: str, dispositivo: str, forma: list[int]):
        import openvino as ov

        core = ov.Core()
        modelo = core.read_model(caminho)
        self._entrada = modelo.inputs[0].any_name
        modelo.reshape({self._entrada: forma})
        self._saidas = [o.any_name for o in modelo.outputs]
        self._compilado = core.compile_model(modelo, dispositivo, {"PERFORMANCE_HINT": "LATENCY"})
        self._pedido = self._compilado.create_infer_request()
        # Um pedido de inferência não é seguro entre threads; o onnxruntime
        # é. Quem chama (análise ao vivo, conferência, EPI) roda em threads
        # diferentes, então a vez é por trava.
        self._trava = threading.Lock()
        self.forma = list(forma)
        self.dispositivo = dispositivo

    def get_inputs(self):
        return [_Entrada(self._entrada, list(self.forma))]

    def get_outputs(self):
        return [_Entrada(n, None) for n in self._saidas]

    def run(self, nomes, alimentacao):
        x = np.ascontiguousarray(next(iter(alimentacao.values())), dtype=np.float32)
        nomes = nomes or self._saidas
        lote = self.forma[0]
        # Lote diferente do compilado (o reconhecimento recebe vários rostos
        # de uma vez em alguns caminhos): um por vez, e junta.
        if x.shape[0] != lote and lote == 1:
            partes = [self.run(nomes, {self._entrada: x[i:i + 1]}) for i in range(x.shape[0])]
            return [np.concatenate([p[k] for p in partes], axis=0) for k in range(len(nomes))]
        with self._trava:
            r = self._pedido.infer({self._entrada: x})
            por_nome = {k.any_name: np.array(v) for k, v in r.items()}
        return [por_nome[n] for n in nomes]


def gpu_disponivel() -> bool:
    try:
        import openvino as ov
        return any(d.startswith("GPU") for d in ov.Core().available_devices)
    except Exception:  # noqa: BLE001
        return False
