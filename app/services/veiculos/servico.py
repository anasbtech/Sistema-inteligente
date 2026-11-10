"""Laço da inteligência veicular.

Duas partes, para a lenta nunca segurar a rápida:

* **um laço por câmera** (as que têm `inteligencia_veicular`): quadro →
  detector de veículos → ByteTrack da câmera → caixas na tela →
  passagens. Roda a VEICULO_FPS e lê o mesmo quadro que o resto do sistema
  (`video.quadro_continuo`): nenhuma conexão RTSP a mais.
* **uma prévia do tipo** por trilha, assim que o veículo tem alguns quadros
  bons: só contorno + vetor + sonda de tipo, para a tarja da caixa dizer
  "van" ou "caminhão grande" enquanto ele ainda está em cena — o detector
  sozinho chamava a Kombi de "ônibus". Uma por vez; se estiver ocupada, a
  trilha espera o próximo quadro.
* **uma fila de identificação**: cada passagem encerrada recebe contorno,
  cor, marca/modelo e placa (~2 s em CPU) e é gravada. Uma por vez, para o
  pico de carros na portaria não disputar CPU com o reconhecimento facial.

As caixas vão para a tela num evento próprio (`veiculos_ao_vivo`), numa
camada separada da sobreposição geral: um não apaga o outro.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

import numpy as np

from app.config import settings

log = logging.getLogger("seguranca.veiculos")


@dataclass
class Retrato:
    """O que a identificação precisa de uma passagem, congelado na hora."""
    camera: str
    trilha: int
    inicio: datetime
    fim: datetime
    quadros: int
    categoria: str
    categoria_conf: float
    conf: float
    quadro: np.ndarray
    bbox: tuple
    recortes_placa: list


def retratar(p) -> Retrato:
    return Retrato(p.camera, p.trilha, p.inicio, p.fim or p.inicio, p.quadros,
                   p.categoria, p.categoria_conf, p.melhor_conf, p.melhor_quadro,
                   p.melhor_bbox, [r for _, r in p.recortes_placa])


class ServicoVeicular:
    def __init__(self) -> None:
        self._tarefas: Dict[str, asyncio.Task] = {}
        self._supervisor: Optional[asyncio.Task] = None
        self._identificador: Optional[asyncio.Task] = None
        self._fila: asyncio.Queue = asyncio.Queue(maxsize=200)
        # O que já se sabe de cada trilha em cena, para a tarja da caixa.
        self._rotulos: Dict[tuple, dict] = {}
        self._previa_ocupada = False
        self.estado_cameras: Dict[str, dict] = {}
        self.registradas = 0
        self.descartadas = 0
        self.ativo = False

    # ------------------------------------------------------------------
    async def iniciar(self) -> None:
        if not settings.VEICULO_ATIVO or self.ativo:
            return
        self.ativo = True
        self._supervisor = asyncio.create_task(self._supervisionar(), name="veiculos-supervisor")
        self._identificador = asyncio.create_task(self._identificar(), name="veiculos-identificacao")
        log.info("Inteligência veicular iniciada")

    async def parar(self) -> None:
        self.ativo = False
        for t in [self._supervisor, self._identificador, *self._tarefas.values()]:
            if t:
                t.cancel()
        for t in [self._supervisor, self._identificador, *self._tarefas.values()]:
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._tarefas.clear()

    def camera_ativa(self, codigo: str) -> bool:
        """O laço geral pergunta: os veículos desta câmera são meus?"""
        t = self._tarefas.get(codigo)
        return self.ativo and t is not None and not t.done()

    # ------------------------------------------------------------------
    async def _cameras_ligadas(self) -> list:
        from sqlalchemy import select

        from app.database import db_context
        from app.models import Camera

        async with db_context() as db:
            cams = (await db.execute(select(Camera).where(
                Camera.inteligencia_veicular.is_(True), Camera.detecta_veiculo.is_(True))
            )).scalars().unique().all()
            return [(c.codigo, c.rtsp_url) for c in cams if c.rtsp_url]

    async def _supervisionar(self) -> None:
        """Liga e desliga o laço de cada câmera conforme o interruptor."""
        from app.services.veiculos import deteccao

        while self.ativo:
            try:
                ligadas = dict(await self._cameras_ligadas())
                if ligadas and not await asyncio.to_thread(deteccao.disponivel):
                    ligadas = {}
                for codigo in list(self._tarefas):
                    if codigo not in ligadas or self._tarefas[codigo].done():
                        self._tarefas.pop(codigo).cancel()
                        self.estado_cameras.pop(codigo, None)
                        await self._publicar(codigo, [])
                for codigo, url in ligadas.items():
                    if codigo not in self._tarefas:
                        self._tarefas[codigo] = asyncio.create_task(
                            self._camera(codigo, url), name=f"veiculos-{codigo}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Supervisor veicular: %s", exc)
            await asyncio.sleep(10)

    async def _camera(self, codigo: str, url: str) -> None:
        from app.services import video
        from app.services.veiculos import deteccao
        from app.services.veiculos.passagem import Passagens
        from app.services.veiculos.rastreio import RastreadorCamera

        rastreador = RastreadorCamera(settings.VEICULO_FPS)
        passagens = Passagens(codigo)
        periodo = 1.0 / max(0.5, settings.VEICULO_FPS)
        ultimo_id = None
        estado = self.estado_cameras.setdefault(codigo, {})
        log.info("%s: inteligência veicular ligada (%.1f qps)", codigo, settings.VEICULO_FPS)
        try:
            while self.ativo:
                t0 = time.monotonic()
                quadro = await asyncio.to_thread(video.quadro_continuo, codigo, url)
                if quadro is None or id(quadro) == ultimo_id:
                    # Sem quadro novo: o tempo ainda corre para as passagens.
                    for p in passagens.atualizar(quadro, [], mono=t0):
                        await self._enfileirar(p)
                    await asyncio.sleep(periodo)
                    continue
                ultimo_id = id(quadro)
                dets = await asyncio.to_thread(deteccao.detectar, quadro)
                trilhas = rastreador.atualizar(dets, quadro.shape)
                prontas = passagens.atualizar(quadro, trilhas, mono=t0)
                await self._publicar(codigo, trilhas, quadro.shape, passagens)
                self._pedir_previa(codigo, passagens)
                for p in prontas:
                    await self._enfileirar(p)
                gasto = time.monotonic() - t0
                estado.update({"em_cena": len(trilhas), "ms_por_quadro": round(gasto * 1000),
                               "motor": deteccao.MOTOR,
                               "atualizado": datetime.now(timezone.utc).isoformat()})
                await asyncio.sleep(max(0.0, periodo - gasto))
        except asyncio.CancelledError:
            for p in passagens.encerrar_todas():
                await self._enfileirar(p)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: laço veicular caiu: %s", codigo, exc)
            estado["erro"] = str(exc)

    async def _enfileirar(self, p) -> None:
        try:
            self._fila.put_nowait(retratar(p))
        except asyncio.QueueFull:
            self.descartadas += 1
            log.warning("%s: fila de identificação cheia; passagem %d descartada",
                        p.camera, p.trilha)

    # ------------------------------------------------------------------
    def _pedir_previa(self, codigo: str, passagens) -> None:
        """Uma trilha por vez ganha a prévia do tipo, na ordem em que surgem."""
        if self._previa_ocupada:
            return
        for p in passagens.abertas.values():
            if (not p.previa_pedida and p.melhor_quadro is not None
                    and p.quadros >= 3 and p.categoria != "motocicleta"):
                p.previa_pedida = True
                self._previa_ocupada = True
                asyncio.create_task(self._previa(p), name=f"veiculos-previa-{codigo}")
                return

    async def _previa(self, p) -> None:
        from app.services.veiculos import identificacao
        from app.services.veiculos.passagem import recortar
        try:
            recorte = recortar(p.melhor_quadro, p.melhor_bbox)
            tipo, conf, _ = await asyncio.to_thread(identificacao.tipo_previa, recorte, p.categoria)
            if p.tipo is None:
                p.tipo = (tipo, round(float(conf), 3))
        except Exception as exc:  # noqa: BLE001
            log.debug("Prévia de tipo falhou: %s", exc)
        finally:
            self._previa_ocupada = False

    @staticmethod
    def _classe_ao_vivo(t, passagens) -> tuple[str, float]:
        """O tipo da tarja: o que a sonda já disse da trilha; senão, a votação
        dos quadros com a ressalva de porte — nunca a classe crua de um quadro."""
        p = passagens.abertas.get(t.id) if passagens is not None else None
        if p is None:
            return t.classe, t.confianca
        if p.tipo:
            return p.tipo[0], p.tipo[1] or t.confianca
        return p.categoria, t.confianca

    async def _publicar(self, codigo: str, trilhas, forma=None, passagens=None) -> None:
        from app.services.events import publicar

        caixas = []
        if trilhas and forma is not None:
            alt, larg = forma[:2]
            for t in trilhas:
                x1, y1, x2, y2 = t.bbox
                classe, conf = self._classe_ao_vivo(t, passagens)
                c = {"classe": classe, "confianca": round(conf, 3),
                     "bbox": [x1 / larg, y1 / alt, (x2 - x1) / larg, (y2 - y1) / alt],
                     "track_id": t.id, "veiculo": True}
                c.update(self._rotulos.get((codigo, t.id), {}))
                caixas.append(c)
        await publicar("veiculos_ao_vivo", {
            "camera_codigo": codigo, "caixas": caixas,
            "validade_s": max(1.5, 3.0 / max(0.5, settings.VEICULO_FPS)),
        })

    # ------------------------------------------------------------------
    async def _identificar(self) -> None:
        from app.services.veiculos import identificacao, persistencia
        from app.services.veiculos.passagem import recortar

        while True:
            r: Retrato = await self._fila.get()
            try:
                recorte = recortar(r.quadro, r.bbox)
                res = await asyncio.to_thread(
                    identificacao.identificar, recorte, r.recortes_placa, r.categoria,
                    r.categoria_conf)
                info = await persistencia.gravar(r, res)
                self.registradas += 1
                rot = {k: v for k, v in (("classe", res.tipo), ("cor", res.cor), ("marca", res.marca),
                                         ("modelo", res.modelo), ("placa", res.placa)) if v}
                if res.tipo and res.tipo_conf is not None:
                    rot["confianca"] = res.tipo_conf
                if rot:
                    self._rotulos[(r.camera, r.trilha)] = rot
                    if len(self._rotulos) > 500:
                        self._rotulos = dict(list(self._rotulos.items())[-250:])
                log.info("%s: %s #%d registrado (%s)", r.camera, res.tipo or r.categoria, r.trilha,
                         info.get("codigo"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Identificação veicular falhou: %s", exc)
            finally:
                self._fila.task_done()

    def resumo(self) -> dict:
        from app.services.veiculos import deteccao
        return {"ativo": self.ativo, "motor": deteccao.MOTOR,
                "cameras": self.estado_cameras, "registradas": self.registradas,
                "na_fila": self._fila.qsize(), "descartadas": self.descartadas}


servico = ServicoVeicular()
