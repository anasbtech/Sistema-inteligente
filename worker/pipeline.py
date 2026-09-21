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
import base64
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

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


@dataclass
class _Candidato:
    """Um rosto acompanhado entre quadros, à espera do melhor ângulo."""
    embedding: np.ndarray
    melhor: object                 # FaceDetectada de maior qualidade até agora
    melhor_quadro: np.ndarray
    quando: datetime
    primeiro: float                # time.monotonic()
    ciclo: int                     # última análise em que este rosto apareceu
    amostras: int = 1


class SeletorMelhorQuadro:
    """Uma presença, um registro — o do melhor enquadramento.

    Sem isto, quem passa um minuto na recepção vira dezenas de capturas
    idênticas: ruído que afoga a revisão humana e infla a trilha de auditoria
    sem acrescentar informação.

    A espera é contada em **ciclos de análise**, não em segundos. Um prazo em
    segundos precisa ser maior que o intervalo entre análises para funcionar,
    e esse intervalo varia com a carga da máquina — de quatro a doze segundos
    nesta instalação. Toda tentativa de estimá-lo liberava a captura antes de
    a seguinte existir, e tudo saía como "melhor de 1".
    """

    def __init__(self, similaridade_minima: float = 0.45, iou_minimo: float = 0.25,
                 ciclos_de_espera: int = 3, janela_maxima_s: float = 300.0) -> None:
        self.similaridade_minima = similaridade_minima
        self.iou_minimo = iou_minimo
        self.ciclos_de_espera = ciclos_de_espera
        self.janela_maxima_s = janela_maxima_s
        self._candidatos: list[_Candidato] = []
        self._ciclo = 0

    def adicionar_quadro(self, faces: list, quadro: np.ndarray, quando: datetime) -> None:
        """Processa de uma vez todas as faces do quadro.

        Rostos no MESMO quadro são pessoas diferentes — isso o detector sabe,
        e são candidatos distintos. Entre quadros é outra história: nesta
        resolução o embedding não distingue nada (medido nesta instalação,
        capturas da mesma pessoa a seis segundos deram 0,004 de similaridade,
        quando o esperado seria acima de 0,5).

        Por isso, havendo um rosto em cena e um candidato aberto, os dois são
        a mesma presença. Abrir um segundo registro fingiria uma distinção que
        o sistema não fez — e é o que enchia a lista com a mesma pessoa.
        """
        if not faces:
            return
        if len(faces) == 1 and len(self._candidatos) == 1:
            self._absorver(self._candidatos[0], faces[0], quadro, quando)
            return
        for face in faces:
            self.adicionar(face, quadro, quando)

    def adicionar(self, face, quadro: np.ndarray, quando: datetime) -> None:
        vetor = np.asarray(face.embedding, dtype=np.float32)

        alvo, melhor_sim = None, self.similaridade_minima
        for cand in self._candidatos:
            sim = float(np.dot(cand.embedding, vetor))   # ambos L2-normalizados
            if sim >= melhor_sim:
                alvo, melhor_sim = cand, sim

        if alvo is None:
            caixa = cantos_para_caixa(face.bbox)
            for cand in self._candidatos:
                if iou(cantos_para_caixa(cand.melhor.bbox), caixa) >= self.iou_minimo:
                    alvo = cand
                    break

        if alvo is None:
            self._candidatos.append(_Candidato(
                embedding=vetor, melhor=face, melhor_quadro=quadro.copy(),
                quando=quando, primeiro=time.monotonic(), ciclo=self._ciclo,
            ))
            return

        self._absorver(alvo, face, quadro, quando)

    def _absorver(self, cand: _Candidato, face, quadro: np.ndarray,
                  quando: datetime) -> None:
        cand.ciclo = self._ciclo
        cand.amostras += 1
        if face.qualidade > cand.melhor.qualidade:
            # `copy` porque o OpenCV reaproveita o buffer do quadro seguinte.
            cand.melhor, cand.melhor_quadro, cand.quando = face, quadro.copy(), quando
            cand.embedding = np.asarray(face.embedding, dtype=np.float32)

    def prontos(self) -> list[_Candidato]:
        """Candidatos cuja janela fechou — o rosto sumiu ou demorou demais.

        Quem fica parado além da janela é encerrado do mesmo jeito: o próximo
        quadro abre um candidato novo, e o resultado é um registro por janela,
        cada um com o seu melhor quadro. Assim quem espera meia hora na
        recepção aparece na linha do tempo, em vez de uma vez só.
        """
        self._ciclo += 1
        agora = time.monotonic()
        saindo, ficando = [], []
        for cand in self._candidatos:
            sumiu = self._ciclo - cand.ciclo >= self.ciclos_de_espera
            demorou = agora - cand.primeiro >= self.janela_maxima_s
            (saindo if sumiu or demorou else ficando).append(cand)
        self._candidatos = ficando
        return saindo


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Interseção sobre união de duas caixas [x, y, largura, altura]."""
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    largura = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    altura = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    intersecao = largura * altura
    uniao = a[2] * a[3] + b[2] * b[3] - intersecao
    return intersecao / uniao if uniao > 0 else 0.0


def cantos_para_caixa(bbox: Sequence[float]) -> list[float]:
    """(x1, y1, x2, y2) -> [x, y, largura, altura]."""
    x1, y1, x2, y2 = bbox
    return [x1, y1, x2 - x1, y2 - y1]


class RegistroUnicoPorObjeto:
    """Um registro por objeto que passa, não um por quadro analisado.

    Dois sinais, porque nenhum basta sozinho nesta cadência:

    * `track_id` — estável enquanto o rastreador acompanha o objeto;
    * sobreposição das caixas — um carro estacionado ocupa o mesmo lugar em
      toda análise, mesmo que a trilha se perca e ele ganhe um id novo.

    A memória expira a partir do ritmo medido, não de um prazo fixo. Era esse
    o defeito: dez segundos de memória com análises a cada doze fazia tudo ser
    esquecido entre uma e outra, e o mesmo carro parado era registrado de novo
    a cada passada do laço.

    O registro sai na primeira aparição, e não quando o objeto some — é o que
    permite a caixa aparecer no vídeo enquanto ele ainda está em cena.
    """

    def __init__(self, fator_memoria: float = 5.0,
                 memoria_minima_s: float = 60.0, iou_minimo: float = 0.45) -> None:
        self.fator_memoria = fator_memoria
        self.memoria_minima_s = memoria_minima_s
        self.iou_minimo = iou_minimo
        self._vistos: list[dict] = []
        self._ultimo_ciclo: Optional[float] = None
        self._intervalo = 1.0

    @property
    def memoria_s(self) -> float:
        return max(self.memoria_minima_s, self._intervalo * self.fator_memoria)

    def marcar_ciclo(self) -> None:
        agora = time.monotonic()
        if self._ultimo_ciclo is not None:
            self._intervalo = self._intervalo * 0.7 + (agora - self._ultimo_ciclo) * 0.3
        self._ultimo_ciclo = agora
        limite = agora - self.memoria_s
        self._vistos = [v for v in self._vistos if v["quando"] >= limite]

    def e_passagem_nova(self, classe: str, track_id: Optional[int],
                        caixa: Sequence[float]) -> bool:
        agora = time.monotonic()
        for visto in self._vistos:
            if visto["classe"] != classe:
                continue
            mesma_trilha = track_id is not None and visto["track_id"] == track_id
            if mesma_trilha or iou(visto["caixa"], caixa) >= self.iou_minimo:
                visto.update(quando=agora, caixa=list(caixa), track_id=track_id)
                return False

        self._vistos.append({"classe": classe, "track_id": track_id,
                             "caixa": list(caixa), "quando": agora})
        return True


class PipelineCamera:
    """Laço de análise de uma câmera."""

    def __init__(self, cfg: ConfigCamera, cliente: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.cliente = cliente
        self.leitor = LeitorRTSP(cfg.rtsp_url)
        self.acoes = AnalisadorAcoes()
        self.seletor = SeletorMelhorQuadro()
        self.objetos = RegistroUnicoPorObjeto()
        self._tinha_caixas = False
        self.ultimo_heartbeat = 0.0
        self.quadros = 0

    # -- envio ---------------------------------------------------------
    async def _enviar(self, payload: dict) -> None:
        try:
            r = await self.cliente.post(f"{API}/deteccoes/ingest", json=payload, timeout=10.0)
            if r.status_code >= 400:
                log.error("Ingest recusado (%s): %s", r.status_code, r.text[:200])
        except httpx.HTTPError as exc:
            log.error("Falha ao enviar detecção: %s", exc)

    async def _enviar_face(self, cand: _Candidato, largura: int, altura: int) -> None:
        face = cand.melhor
        await self._enviar({
            "tipo": "FACIAL",
            "camera_codigo": self.cfg.codigo,
            # O instante é o da melhor captura, não o do fim da janela.
            "ocorrido_em": cand.quando.isoformat(),
            # O worker manda o vetor; quem identifica é a API.
            "embedding": face.embedding.tolist(),
            "qualidade_face": face.qualidade,
            "bbox": face.bbox_normalizado(largura, altura),
            # Folga maior no rosto: cabeça e ombros dão contexto a quem revisa,
            # e o recorte sai com mais pixels que o rosto sozinho.
            "snapshot_jpeg": self._recorte(cand.melhor_quadro, face.bbox, margem=0.6),
            "hash_evidencia": hash_evidencia(cand.melhor_quadro.tobytes()),
        })
        log.info("%s: rosto enviado (qualidade %.2f, melhor de %d capturas)",
                 self.cfg.codigo, face.qualidade, cand.amostras)

    async def _publicar_caixas(self, objetos, faces, largura: int, altura: int) -> None:
        """Sobreposição do mosaico: o que está em cena agora, sem persistir.

        A caixa acompanha o objeto enquanto ele estiver diante da câmera; o
        registro durável já saiu, uma vez só, quando ele apareceu.
        """
        caixas = [
            {"classe": o.classe, "confianca": round(o.confianca, 3),
             "bbox": o.bbox_normalizado(largura, altura), "track_id": o.track_id}
            for o in objetos
        ] + [
            {"classe": "", "confianca": f.qualidade,
             "bbox": f.bbox_normalizado(largura, altura)}
            for f in faces
        ]
        # Cena vazia só precisa ser anunciada uma vez, para apagar o que ficou.
        if not caixas and not self._tinha_caixas:
            return
        self._tinha_caixas = bool(caixas)

        try:
            await self.cliente.post(
                f"{API}/deteccoes/ao-vivo",
                json={"camera_codigo": self.cfg.codigo, "caixas": caixas},
                timeout=5.0,
            )
        except httpx.HTTPError:
            pass   # sobreposição é cosmética: perder um quadro não custa nada

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
                self.objetos.marcar_ciclo()
                for obj in objetos:
                    caixa = obj.bbox_normalizado(largura, altura)
                    # Toda classe vira registro; o que evita o ruído não é
                    # filtrar classe, é registrar cada objeto uma vez só.
                    if not self.objetos.e_passagem_nova(obj.classe, obj.track_id, caixa):
                        continue
                    await self._enviar({
                        "tipo": "OBJETO",
                        "camera_codigo": self.cfg.codigo,
                        "ocorrido_em": agora.isoformat(),
                        "classe": obj.classe,
                        "confianca": round(obj.confianca, 4),
                        "bbox": caixa,
                        "track_id": obj.track_id,
                        "zona_nome": self._zona_do_ponto(
                            (obj.centro()[0] / largura, obj.centro()[1] / altura)
                        ),
                        "snapshot_jpeg": self._recorte(quadro, obj.bbox),
                        "hash_evidencia": hash_evidencia(quadro.tobytes()),
                    })

            faces = []
            if self.cfg.analitico_facial:
                faces = await asyncio.to_thread(face_engine.extrair_faces, quadro)
                self.seletor.adicionar_quadro(faces, quadro, agora)
                for cand in self.seletor.prontos():
                    await self._enviar_face(cand, largura, altura)

            await self._publicar_caixas(objetos, faces, largura, altura)

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
                        "snapshot_jpeg": self._recorte(quadro, ev.bbox),
                        "hash_evidencia": hash_evidencia(quadro.tobytes()),
                    })

            await self._heartbeat(int((time.perf_counter() - t0) * 1000))
            await asyncio.sleep(0)

    def _recorte(self, quadro: np.ndarray, bbox, margem: float = 0.25) -> Optional[str]:
        """Recorte da captura em JPEG base64, para a API gravar.

        Quem grava é a API, não o worker: em produção eles rodam em máquinas
        diferentes, e mandar um caminho em vez da imagem só funcionaria com
        disco compartilhado.
        """
        recorte = recortar(quadro, bbox, margem)
        if recorte.size == 0:
            return None
        ok, buffer = cv2.imencode(".jpg", recorte, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return base64.b64encode(buffer.tobytes()).decode() if ok else None

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
