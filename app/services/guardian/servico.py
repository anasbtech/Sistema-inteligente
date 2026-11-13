"""Laço do Aizon Guardian: gesto de socorro e vídeo de evidência.

Um laço por câmera com o Guardian ligado, a `GUARDIAN_FPS`:

1. lê o quadro corrente (a mesma conexão RTSP de todo o sistema);
2. guarda-o na memória dos últimos segundos (para o vídeo de evidência);
3. pega as pessoas que a análise geral acabou de achar nessa câmera — não
   roda outro detector de pessoa — e, nas que têm altura para a mão ter
   pixels, procura as mãos (MediaPipe) e acompanha a sequência do gesto;
4. gesto fechado vira detecção `sinal_socorro` pelo caminho único
   (`processar_deteccao`), que cria o alerta crítico e a ocorrência.

Também grava o vídeo pedido por qualquer sinal da câmera (inclusive os de
trajetória, que vêm da análise de ações): os segundos antes, da memória, e
os seguintes, conforme chegam.

Cada câmera publica quão grandes as mãos aparecem nela: é o que diz à tela
se o gesto é legível ali.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import statistics
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.config import settings

log = logging.getLogger("seguranca.guardian")


class ServicoGuardian:
    def __init__(self) -> None:
        self.ativo = False
        self._supervisor: Optional[asyncio.Task] = None
        self._tarefas: Dict[str, asyncio.Task] = {}
        self._memorias: Dict[str, object] = {}
        self._pedidos: List[dict] = []
        self.estado_cameras: Dict[str, dict] = {}

    async def iniciar(self) -> None:
        if self.ativo or not settings.GUARDIAN_ATIVO:
            return
        self.ativo = True
        self._supervisor = asyncio.create_task(self._supervisionar(), name="guardian-supervisor")
        log.info("Aizon Guardian iniciado")

    async def parar(self) -> None:
        self.ativo = False
        for t in [self._supervisor, *self._tarefas.values()]:
            if t:
                t.cancel()
        self._tarefas.clear()

    # ------------------------------------------------------------------
    def pedir_clipe(self, camera: str, ocorrencia: str, quando: datetime) -> None:
        """Agenda o vídeo da ocorrência: passado da memória + os próximos segundos."""
        if camera not in self._memorias:
            return
        if any(p["ocorrencia"] == ocorrencia for p in self._pedidos):
            return
        agora = time.monotonic()
        idade = max(0.0, (datetime.now(timezone.utc) - quando).total_seconds())
        self._pedidos.append({
            "camera": camera, "ocorrencia": ocorrencia, "quando": quando,
            "inicio": agora - idade - settings.GUARDIAN_CLIPE_ANTES_S,
            "pronto_em": agora + max(0.0, settings.GUARDIAN_CLIPE_DEPOIS_S - idade)})

    async def _gravar_pedidos(self, camera: str) -> None:
        from sqlalchemy import select

        from app.database import db_context
        from app.models import OcorrenciaGuardian
        from app.services.guardian import evidencia
        agora = time.monotonic()
        prontos = [p for p in self._pedidos if p["camera"] == camera and p["pronto_em"] <= agora]
        for p in prontos:
            self._pedidos.remove(p)
            mem = self._memorias.get(camera)
            quadros = mem.desde(p["inicio"]) if mem else []
            if not quadros:
                continue
            chave = await asyncio.to_thread(evidencia.gravar_clipe, p["ocorrencia"], p["quando"], quadros)
            async with db_context() as db:
                oc = (await db.execute(select(OcorrenciaGuardian).where(
                    OcorrenciaGuardian.codigo == p["ocorrencia"]))).scalars().unique().first()
                if oc is not None and not oc.clipe_key:
                    oc.clipe_key = chave
            log.info("Guardian %s: vídeo de evidência gravado (%d quadros, cifrado)",
                     p["ocorrencia"], len(quadros))

    # ------------------------------------------------------------------
    async def _cameras(self) -> dict:
        from sqlalchemy import select

        from app.database import db_context
        from app.models import Camera
        async with db_context() as db:
            cams = (await db.execute(select(Camera).where(Camera.guardian.is_(True)))).scalars().unique().all()
            return {c.codigo: c.rtsp_url for c in cams if c.rtsp_url}

    async def _supervisionar(self) -> None:
        while self.ativo:
            try:
                ligadas = await self._cameras()
                for cod in list(self._tarefas):
                    if cod not in ligadas or self._tarefas[cod].done():
                        self._tarefas.pop(cod).cancel()
                        self._memorias.pop(cod, None)
                        self.estado_cameras.pop(cod, None)
                for cod, url in ligadas.items():
                    if cod not in self._tarefas:
                        self._tarefas[cod] = asyncio.create_task(self._camera(cod, url),
                                                                 name=f"guardian-{cod}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Guardian supervisor: %s", exc)
            await asyncio.sleep(10)

    def _pessoas(self, codigo: str, forma) -> list:
        """Pessoas que a análise geral publicou há pouco, em px, as maiores primeiro."""
        from app.services.inferencia import inferencia
        carga = getattr(inferencia, "_ultimas_caixas", {}).get(codigo)
        if not carga:
            return []
        em = carga.get("em")
        if isinstance(em, datetime):
            idade = (datetime.now(timezone.utc) - (em if em.tzinfo else em.replace(tzinfo=timezone.utc))).total_seconds()
            if idade > 15:
                return []
        alt, larg = forma[:2]
        saida = []
        for c in carga.get("caixas") or []:
            if c.get("classe") != "pessoa" or not c.get("bbox"):
                continue
            x, y, w, h = c["bbox"]
            if h * alt < settings.GUARDIAN_PESSOA_MIN_PX:
                continue
            saida.append((x * larg, y * alt, (x + w) * larg, (y + h) * alt))
        saida.sort(key=lambda b: -(b[3] - b[1]))
        return saida[:settings.GUARDIAN_PESSOAS_POR_QUADRO]

    async def _camera(self, codigo: str, url: str) -> None:
        from app.services import video
        from app.services.guardian import evidencia, gesto, maos

        mem = evidencia.Memoria(settings.GUARDIAN_CLIPE_ANTES_S + settings.GUARDIAN_CLIPE_DEPOIS_S + 4)
        self._memorias[codigo] = mem
        seq = gesto.SequenciaGesto()
        tamanhos: collections.deque = collections.deque(maxlen=200)
        est = self.estado_cameras.setdefault(codigo, {})
        periodo = 1.0 / max(0.5, settings.GUARDIAN_FPS)
        tem_maos = await asyncio.to_thread(maos.disponivel)
        ultimo_id = None
        log.info("%s: Guardian ligado (%.1f qps, gesto %s)", codigo, settings.GUARDIAN_FPS,
                 "ativo" if tem_maos else "indisponível")
        try:
            while self.ativo:
                t0 = time.monotonic()
                quadro = await asyncio.to_thread(video.quadro_continuo, codigo, url)
                if quadro is not None and id(quadro) != ultimo_id:
                    ultimo_id = id(quadro)
                    await asyncio.to_thread(mem.guardar, quadro, t0)
                    pessoas = self._pessoas(codigo, quadro.shape) if tem_maos else []
                    achadas = []
                    for b in pessoas:
                        for pts, conf in await asyncio.to_thread(maos.maos_da_pessoa, quadro, b):
                            tamanhos.append(gesto.tamanho(pts))
                            achadas.append((pts, conf))
                    for s in seq.observar(achadas, t0):
                        await self._registrar_gesto(codigo, quadro, s)
                    est.update({
                        "pessoas_avaliadas": len(pessoas), "maos_no_quadro": len(achadas),
                        "mao_mediana_px": round(statistics.median(tamanhos)) if tamanhos else None,
                        "maos_vistas": len(tamanhos), "gesto_disponivel": tem_maos,
                        "gesto_legivel": (statistics.median(tamanhos) >= gesto.MAO_MIN_PX) if len(tamanhos) >= 5 else None,
                        "ms_por_quadro": round((time.monotonic() - t0) * 1000),
                        "atualizado": datetime.now(timezone.utc).isoformat()})
                if self._pedidos:
                    await self._gravar_pedidos(codigo)
                await asyncio.sleep(max(0.0, periodo - (time.monotonic() - t0)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: laço do Guardian caiu: %s", codigo, exc)
            est["erro"] = str(exc)

    async def _registrar_gesto(self, codigo: str, quadro, s) -> None:
        """O gesto vira detecção pelo caminho único: alerta crítico + ocorrência."""
        from app.database import db_context
        from app.models import TipoDeteccao
        from app.routers.deteccoes import processar_deteccao
        from app.schemas import DeteccaoIn
        from app.services import video
        alt, larg = quadro.shape[:2]
        lado = max(s.tamanho_px * 3.0, 160)
        cx, cy = s.centro
        caixa = (cx - lado / 2, cy - lado / 2, cx + lado / 2, cy + lado / 2)
        recorte = video.recortar_bgr(quadro, caixa, margem=0.1)
        bbox = [max(0.0, caixa[0] / larg), max(0.0, caixa[1] / alt), lado / larg, lado / alt]
        async with db_context() as db:
            info = await processar_deteccao(db, DeteccaoIn(
                tipo=TipoDeteccao.ACAO, camera_codigo=codigo, ocorrido_em=datetime.now(timezone.utc),
                classe="sinal_socorro", confianca=s.confianca, bbox=bbox, envolvidos=1, duracao_s=3,
                snapshot_jpeg=base64.b64encode(recorte).decode("ascii") if recorte else None,
                detalhe_extra=(f"Sequência palma com polegar dobrado → dedos fechando sobre o polegar, "
                               f"mão com {round(s.tamanho_px)} px, vista {s.repeticoes} vez(es).")),
                origem="guardian")
        log.warning("%s: possível sinal de socorro (%.0f%%) — %s", codigo, s.confianca * 100, info.get("codigo"))

    def resumo(self) -> dict:
        from app.services.guardian import maos
        return {"ativo": self.ativo, "gesto_disponivel": maos._det is not None,
                "cameras": self.estado_cameras, "clipes_pendentes": len(self._pedidos)}


servico = ServicoGuardian()
