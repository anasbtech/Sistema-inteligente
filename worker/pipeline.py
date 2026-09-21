"""Worker de inferência: lê RTSP, roda os modelos e envia detecções à API.

Um processo por câmera (ou um por GPU com várias câmeras em threads). O worker
NÃO decide identidade: ele extrai o embedding e manda para a API, que faz a
busca 1:N e aplica o limiar. Isso mantém limiar, auditoria e regra de alerta
num lugar só.

    python -m worker.run --camera CAM-001
    python -m worker.run --todas
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import cv2
import httpx
import numpy as np

from app.config import settings
from app.security import hash_evidencia
from app.services import face_engine
from app.services.vision import AnalisadorAcoes, detectar_objetos, ponto_em_poligono

log = logging.getLogger("vigia.worker")

API = "http://localhost:8000"


@dataclass
class ConfigCamera:
    codigo: str
    nome: str
    rtsp_url: str
    fps: int = 25
    limiar_facial: float = 0.88
    limiar_objeto: float = 0.65
    analitico_facial: bool = True
    analitico_objeto: bool = True
    analitico_acao: bool = False
    zonas: list = None

    @classmethod
    def de_api(cls, dados: dict) -> "ConfigCamera":
        return cls(
            codigo=dados["codigo"], nome=dados["nome"], rtsp_url=dados["rtsp_url"],
            fps=dados.get("fps", 25),
            limiar_facial=dados.get("limiar_facial", settings.FACE_THRESHOLD),
            limiar_objeto=dados.get("limiar_objeto", settings.OBJECT_THRESHOLD),
            analitico_facial=dados.get("analitico_facial", True),
            analitico_objeto=dados.get("analitico_objeto", True),
            analitico_acao=dados.get("analitico_acao", False),
            zonas=dados.get("zonas", []),
        )


class LeitorRTSP:
    """Lê sempre o quadro mais recente, descartando atraso acumulado.

    Sem isso, o buffer do OpenCV cresce e a análise fica minutos atrás do vivo.
    """

    def __init__(self, url: str, reconectar_s: float = 5.0) -> None:
        self.url = url
        self.reconectar_s = reconectar_s
        self.cap: Optional[cv2.VideoCapture] = None
        self._abrir()

    def _abrir(self) -> None:
        if self.cap is not None:
            self.cap.release()
        self.cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self.cap.isOpened():
            log.warning("Não abriu o stream %s", self.url)

    def ler(self) -> Optional[np.ndarray]:
        if self.cap is None or not self.cap.isOpened():
            time.sleep(self.reconectar_s)
            self._abrir()
            return None
        ok, quadro = self.cap.read()
        if not ok:
            log.warning("Quadro perdido em %s — reconectando", self.url)
            time.sleep(self.reconectar_s)
            self._abrir()
            return None
        return quadro

    def fechar(self) -> None:
        if self.cap is not None:
            self.cap.release()


class PipelineCamera:
    """Laço de análise de uma câmera."""

    def __init__(self, cfg: ConfigCamera, cliente: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.cliente = cliente
        self.leitor = LeitorRTSP(cfg.rtsp_url)
        self.acoes = AnalisadorAcoes()
        self.ultimo_heartbeat = 0.0
        self.ultima_face_por_track: dict[int, float] = {}
        self.quadros = 0

    # -- envio ---------------------------------------------------------
    async def _enviar(self, payload: dict) -> None:
        try:
            r = await self.cliente.post(f"{API}/deteccoes/ingest", json=payload, timeout=10.0)
            if r.status_code >= 400:
                log.error("Ingest recusado (%s): %s", r.status_code, r.text[:200])
        except httpx.HTTPError as exc:
            log.error("Falha ao enviar detecção: %s", exc)

    async def _heartbeat(self, latencia_ms: int) -> None:
        agora = time.time()
        if agora - self.ultimo_heartbeat < 10:
            return
        self.ultimo_heartbeat = agora
        try:
            await self.cliente.post(
                f"{API}/cameras/{self.cfg.codigo}/heartbeat",
                params={"latencia_ms": latencia_ms, "online": True}, timeout=5.0,
            )
        except httpx.HTTPError:
            pass

    def _zona_do_ponto(self, ponto: tuple[float, float]) -> str:
        for z in (self.cfg.zonas or []):
            if z.get("tipo") == "POLIGONO" and ponto_em_poligono(ponto, z.get("pontos") or []):
                return z.get("nome", "")
        return ""

    # -- laço principal ------------------------------------------------
    async def rodar(self, intervalo_analise: int = 5) -> None:
        """`intervalo_analise`: roda os modelos a cada N quadros. Em 25 fps,
        N=5 dá 5 análises por segundo — suficiente e muito mais barato."""
        log.info("Iniciando pipeline de %s (%s)", self.cfg.codigo, self.cfg.nome)
        while True:
            t0 = time.perf_counter()
            quadro = await asyncio.to_thread(self.leitor.ler)
            if quadro is None:
                await asyncio.sleep(0.5)
                continue

            self.quadros += 1
            if self.quadros % intervalo_analise:
                await asyncio.sleep(0)
                continue

            altura, largura = quadro.shape[:2]
            agora = datetime.now(timezone.utc)

            objetos = []
            if self.cfg.analitico_objeto:
                objetos = await asyncio.to_thread(
                    detectar_objetos, quadro, self.cfg.limiar_objeto, True
                )
                for obj in objetos:
                    # Só classes de severidade real viram registro — pessoa e
                    # veículo em fluxo normal gerariam ruído sem informação.
                    if obj.classe in ("pessoa", "veiculo", "bicicleta"):
                        continue
                    await self._enviar({
                        "tipo": "OBJETO",
                        "camera_codigo": self.cfg.codigo,
                        "ocorrido_em": agora.isoformat(),
                        "classe": obj.classe,
                        "confianca": round(obj.confianca, 4),
                        "bbox": obj.bbox_normalizado(largura, altura),
                        "track_id": obj.track_id,
                        "zona_nome": self._zona_do_ponto(
                            (obj.centro()[0] / largura, obj.centro()[1] / altura)
                        ),
                        "snapshot_key": await self._guardar(quadro, obj.bbox, agora),
                        "hash_evidencia": hash_evidencia(quadro.tobytes()),
                    })

            if self.cfg.analitico_facial:
                faces = await asyncio.to_thread(face_engine.extrair_faces, quadro)
                for face in faces:
                    await self._enviar({
                        "tipo": "FACIAL",
                        "camera_codigo": self.cfg.codigo,
                        "ocorrido_em": agora.isoformat(),
                        # O worker manda o vetor; quem identifica é a API.
                        "embedding": face.embedding.tolist(),
                        "qualidade_face": face.qualidade,
                        "bbox": face.bbox_normalizado(largura, altura),
                        "snapshot_key": await self._guardar(quadro, face.bbox, agora),
                        "hash_evidencia": hash_evidencia(quadro.tobytes()),
                    })

            if self.cfg.analitico_acao and objetos:
                eventos = self.acoes.atualizar(objetos, largura, altura, self.cfg.zonas)
                for ev in eventos:
                    await self._enviar({
                        "tipo": "ACAO",
                        "camera_codigo": self.cfg.codigo,
                        "ocorrido_em": agora.isoformat(),
                        "classe": ev.classe,
                        "confianca": round(ev.confianca, 4),
                        "duracao_s": ev.duracao_s,
                        "envolvidos": ev.envolvidos,
                        "bbox": (list(ev.bbox) if ev.bbox else None),
                        "track_id": ev.track_id,
                        "snapshot_key": await self._guardar(quadro, ev.bbox, agora),
                        "hash_evidencia": hash_evidencia(quadro.tobytes()),
                    })

            await self._heartbeat(int((time.perf_counter() - t0) * 1000))
            await asyncio.sleep(0)

    async def _guardar(self, quadro: np.ndarray, bbox, quando: datetime) -> str:
        """Grava o recorte no armazenamento de evidências e devolve a chave.

        Substitua por upload real (boto3/aioboto3) para S3/MinIO. Aqui só
        montamos a chave determinística.
        """
        chave = f"{self.cfg.codigo}/{quando:%Y/%m/%d}/{quando:%H%M%S%f}.jpg"
        # Exemplo de upload:
        # recorte = recortar(quadro, bbox)
        # ok, buf = cv2.imencode(".jpg", recorte, [cv2.IMWRITE_JPEG_QUALITY, 88])
        # await s3.put_object(Bucket=settings.S3_BUCKET, Key=chave, Body=buf.tobytes())
        return chave

    def fechar(self) -> None:
        self.leitor.fechar()


def recortar(quadro: np.ndarray, bbox, margem: float = 0.25) -> np.ndarray:
    """Recorte com folga ao redor da caixa — contexto ajuda na revisão humana."""
    if bbox is None:
        return quadro
    altura, largura = quadro.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in bbox]
    mx, my = int((x2 - x1) * margem), int((y2 - y1) * margem)
    return quadro[max(0, y1 - my):min(altura, y2 + my),
                  max(0, x1 - mx):min(largura, x2 + mx)]
