"""Worker de inferência sobre o vídeo das câmeras.

Lê o quadro corrente de cada câmera on-line, roda os modelos que aquela
câmera tem habilitados e registra pela mesma rota que o worker externo usa —
`processar_deteccao`. O que entra no banco é o que o modelo viu: classe,
confiança e caixa saem da inferência, e o recorte é da região que o modelo
apontou.

É a diferença para o simulador, que sorteia classe e caixa. Um rótulo
sorteado sobre um recorte real produz registro que não se sustenta: a ficha
diz "Capacete 90,9%" sobre uma imagem de asfalto, e quem revisa não tem como
saber se o sistema erra assim sempre ou só ali.

Dedução de carga: cada objeto é registrado uma vez por janela, identificado
pela trilha do rastreador. Sem isso um carro estacionado vira uma detecção a
cada ciclo, e em uma hora o registro tem mil linhas do mesmo carro parado.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import select

from app.config import settings
from app.database import db_context
from app.models import Camera, StatusCamera, TipoDeteccao
from app.schemas import DeteccaoIn
from app.services import face_engine, video, vision
from app.services.events import publicar

log = logging.getLogger("seguranca.inferencia")


class Inferencia:
    """Laço que percorre as câmeras on-line aplicando os modelos."""

    def __init__(self, intervalo: float = 3.0, janela_track: float = 45.0) -> None:
        self.intervalo = intervalo
        self.janela_track = janela_track
        self._task: Optional[asyncio.Task] = None
        self._visto: Dict[Tuple[str, str], float] = {}
        self.gerados = 0

    async def iniciar(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        log.info(
            "Inferência ativa (ciclo ~%.1fs, objeto=%s, face=%s)",
            self.intervalo, vision.yolo_disponivel(), face_engine.modelos_disponiveis(),
        )

    async def parar(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # Deixa a API subir antes de disputar CPU com ela.
        await asyncio.sleep(4)
        while True:
            try:
                await self._rodada()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha na rodada de inferência: %s", exc)
            await asyncio.sleep(self.intervalo)

    async def _rodada(self) -> None:
        async with db_context() as db:
            cameras = (
                await db.execute(
                    select(Camera).where(Camera.status == StatusCamera.ONLINE)
                )
            ).scalars().unique().all()

            for cam in cameras:
                if not cam.rtsp_url:
                    continue
                try:
                    await self._processar(db, cam)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Inferência falhou em %s: %s", cam.codigo, exc)

    def _confiavel(self, obj) -> bool:
        """Descarta o que o modelo classifica sem base suficiente.

        Confiança baixa e caixa pequena são de onde vem a troca de classe: o
        modelo acerta que há *algo* ali e erra o que é. Um registro com rótulo
        errado é pior que registro nenhum — manda o operador atrás da coisa
        errada e contamina a estatística por classe.
        """
        if obj.confianca < settings.INFERENCIA_CONFIANCA_MIN:
            return False
        x1, y1, x2, y2 = obj.bbox
        return min(x2 - x1, y2 - y1) >= settings.INFERENCIA_LADO_MIN_PX

    def _repetido(self, chave: Tuple[str, str]) -> bool:
        """Mesma trilha dentro da janela — já foi registrada."""
        agora = time.monotonic()
        anterior = self._visto.get(chave)
        if anterior is not None and (agora - anterior) < self.janela_track:
            return True
        self._visto[chave] = agora
        # Poda o que envelheceu, senão o dicionário cresce sem teto.
        if len(self._visto) > 4000:
            corte = agora - self.janela_track
            self._visto = {k: v for k, v in self._visto.items() if v > corte}
        return False

    async def _processar(self, db, cam: Camera) -> None:
        from app.routers.deteccoes import processar_deteccao

        quadro = await asyncio.to_thread(video.quadro_bgr, cam.codigo, cam.rtsp_url)
        if quadro is None:
            return
        altura, largura = quadro.shape[:2]
        agora = datetime.now(timezone.utc)

        caixas_do_quadro = []

        if cam.analitico_objeto and vision.yolo_disponivel():
            objetos = await asyncio.to_thread(
                vision.detectar_objetos, quadro, cam.limiar_objeto, True
            )
            for obj in objetos:
                # A sobreposição mostra o que a câmera está vendo agora, no
                # limiar que o operador configurou nela. Sai antes das podas:
                # a caixa acompanha o objeto enquanto ele está em cena, e o
                # registro nasce uma vez por passagem — o carro parado segue
                # enquadrado no vídeo sem voltar à lista a cada ciclo.
                caixas_do_quadro.append({
                    "classe": obj.classe,
                    "confianca": round(float(obj.confianca), 4),
                    "bbox": obj.bbox_normalizado(largura, altura),
                    "track_id": obj.track_id,
                })

                # O piso extra vale só para o registro: o que fica gravado com
                # rótulo errado manda o operador atrás da coisa errada, e a
                # estatística por classe passa a mentir. No vídeo, o mesmo
                # objeto duvidoso está à vista de quem julga.
                if not self._confiavel(obj):
                    continue

                # Sem trilha, a posição serve de identidade aproximada. A
                # célula é grossa de propósito: fina demais, o mesmo objeto
                # andando alguns pixels vira uma marca nova e volta ao
                # registro — justamente o que a trilha existe para evitar.
                marca = str(obj.track_id) if obj.track_id is not None else (
                    f"{obj.classe}:{obj.bbox[0] // 160}:{obj.bbox[1] // 160}"
                )
                if self._repetido((cam.codigo, f"obj:{marca}")):
                    continue

                jpeg = await asyncio.to_thread(video.recortar_bgr, quadro, obj.bbox)
                await processar_deteccao(db, DeteccaoIn(
                    tipo=TipoDeteccao.OBJETO,
                    camera_codigo=cam.codigo,
                    ocorrido_em=agora,
                    classe=obj.classe,
                    confianca=round(float(obj.confianca), 4),
                    bbox=obj.bbox_normalizado(largura, altura),
                    track_id=obj.track_id,
                    snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
                ), origem="inferencia")
                self.gerados += 1

        if cam.analitico_facial and face_engine.modelos_disponiveis():
            faces = await asyncio.to_thread(face_engine.extrair_faces, quadro)
            for face in faces:
                x1, y1, x2, y2 = face.bbox
                caixas_do_quadro.append({
                    "classe": "rosto",
                    "confianca": round(float(face.qualidade), 4),
                    "bbox": face.bbox_normalizado(largura, altura),
                    "track_id": None,
                })
                if self._repetido((cam.codigo, f"face:{x1 // 160}:{y1 // 160}")):
                    continue

                jpeg = await asyncio.to_thread(video.recortar_bgr, quadro, face.bbox)
                await processar_deteccao(db, DeteccaoIn(
                    tipo=TipoDeteccao.FACIAL,
                    camera_codigo=cam.codigo,
                    ocorrido_em=agora,
                    embedding=face.embedding.tolist(),
                    qualidade_face=round(float(face.qualidade), 4),
                    bbox=face.bbox_normalizado(largura, altura),
                    snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
                ), origem="inferencia")
                self.gerados += 1

        # Publicado sempre, inclusive vazio: a lista é a cena atual, e mandar
        # vazio é o que apaga a caixa de um objeto que acabou de sair. Sem
        # isso a última caixa ficaria pairando sobre o vídeo até expirar.
        await publicar("quadro_ao_vivo", {
            "camera_codigo": cam.codigo,
            "caixas": caixas_do_quadro,
        })


inferencia = Inferencia(settings.INFERENCIA_INTERVALO, settings.INFERENCIA_JANELA_S)
