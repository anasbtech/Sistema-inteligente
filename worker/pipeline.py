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
from app.services.vision import (
    AnalisadorAcoes, detectar_objetos, familia_de, ponto_em_poligono,
)

log = logging.getLogger("seguranca.worker")

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

    A espera é longa porque o detector oscila: ele perde um rosto que continua
    diante da câmera e o reencontra alguns ciclos depois. Com três ciclos,
    quem ficava parado na recepção saía fatiado em vários registros de uma
    captura só — medidos vinte e um em sete minutos, quase todos "melhor de
    1". Seis ciclos dão cerca de meio minuto de tolerância no ritmo desta
    instalação, e o custo de errar para este lado é só o registro sair mais
    tarde; para o outro, é a lista voltar a encher.
    """

    def __init__(self, similaridade_minima: float = 0.45, iou_minimo: float = 0.25,
                 ciclos_de_espera: int = 6, janela_maxima_s: float = 1800.0) -> None:
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
        # Cabendo nos candidatos já abertos, é gente que continua em cena:
        # cada rosto vai para o mais próximo em vez de abrir presença nova.
        if self._candidatos and len(faces) <= len(self._candidatos):
            self._casar(faces, quadro, quando)
            return
        for face in faces:
            self.adicionar(face, quadro, quando)

    def _casar(self, faces: list, quadro: np.ndarray, quando: datetime) -> None:
        """Distribui os rostos do quadro entre os candidatos abertos.

        Um candidato recebe no máximo um rosto por quadro: dois rostos ao
        mesmo tempo são pessoas diferentes, e isso o detector sabe.
        """
        livres = list(self._candidatos)
        for face in faces:
            if not livres:
                self.adicionar(face, quadro, quando)
                continue
            caixa = cantos_para_caixa(face.bbox)
            # Por posição, e não por `remove`: o candidato carrega o embedding
            # num array, e comparar dataclasses compararia array com array —
            # que não resolve para verdadeiro ou falso.
            i = min(range(len(livres)), key=lambda k: self._afastamento(livres[k], caixa))
            self._absorver(livres.pop(i), face, quadro, quando)

    @staticmethod
    def _afastamento(cand: _Candidato, caixa: Sequence[float]) -> float:
        """Quanto o rosto destoa do candidato — menor é mais perto.

        Vale a sobreposição quando existe; na falta dela, a distância entre os
        centros. Quem sobrepõe sempre ganha de quem não sobrepõe, porque a
        sobreposição entra negativa e a distância, positiva.
        """
        ref = cantos_para_caixa(cand.melhor.bbox)
        sobreposicao = iou(ref, caixa)
        if sobreposicao > 0:
            return -sobreposicao
        cx, cy = ref[0] + ref[2] / 2, ref[1] + ref[3] / 2
        fx, fy = caixa[0] + caixa[2] / 2, caixa[1] + caixa[3] / 2
        return float(np.hypot(cx - fx, cy - fy))

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
        cada um com o seu melhor quadro. A janela é longa de propósito — quem
        atende na recepção a manhã inteira não precisa de uma linha a cada
        cinco minutos para que se saiba que esteve lá.
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


def suprimir_sobrepostas(objetos: Sequence, iou_maximo: float = 0.7) -> list:
    """Uma caixa em cena, uma classe — a de maior confiança.

    O YOLO pontua as classes de forma independente, então um mesmo furgão
    pontua na cabeça de `car` a 0,68 *e* na de `truck` a 0,66 — duas caixas
    no mesmo lugar, hoje ambas rotuladas `carro`. A deduplicação por passagem
    não resolve isso: ela compara dentro de cada classe, e duas caixas
    distintas no mesmo ponto lhe parecem dois objetos. O resultado eram dois
    registros com o mesmo recorte.
    """
    mantidos: list = []
    for obj in sorted(objetos, key=lambda o: o.confianca, reverse=True):
        caixa = cantos_para_caixa(obj.bbox)
        if any(iou(caixa, cantos_para_caixa(m.bbox)) > iou_maximo for m in mantidos):
            continue
        mantidos.append(obj)
    return mantidos


@dataclass
class _ObjetoEmCena:
    """Um objeto acompanhado entre quadros, à espera da melhor leitura."""
    melhor: object                 # ObjetoDetectado de maior confiança
    recorte: Optional[str]         # JPEG base64 do melhor instante
    hash_evidencia: str
    quando: datetime
    bbox: list                     # normalizada, do melhor instante
    familia: str
    track_id: Optional[int]
    caixa_atual: list              # última posição vista, para casar por IoU
    zona_nome: str
    primeiro: float
    ciclo: int
    amostras: int = 1
    ja_registrado: bool = False    # retomada de uma passagem já emitida


@dataclass
class _PassagemEmitida:
    """Lembrança de um objeto que já virou registro, para não virar de novo."""
    familia: str
    caixa: list
    quando: float                  # monotonic, renovado a cada reaparição


class SeletorMelhorObjeto:
    """Uma passagem, um registro — o da leitura mais confiante.

    Antes o registro saía na primeira aparição, quando o objeto mal entrou no
    quadro: longe, cortado pela borda, e com a confiança mais baixa de toda a
    passagem. Agora o objeto é acompanhado enquanto está em cena e só vira
    registro ao sair, com o quadro em que o modelo mais teve certeza.

    Isso não atrasa a caixa no vídeo: a sobreposição do mosaico é publicada
    à parte, a cada análise, com tudo que está em cena.

    Dois sinais para reconhecer quem já está sendo acompanhado, porque nenhum
    basta sozinho nesta cadência:

    * `track_id` — estável enquanto o rastreador acompanha o objeto;
    * sobreposição das caixas — um carro estacionado ocupa o mesmo lugar em
      toda análise, mesmo que a trilha se perca e ele ganhe um id novo.

    A comparação é por *família* de classe, não pelo rótulo cru: quando o
    modelo troca de rótulo conforme o ângulo, comparar a classe exata faz
    cada oscilação parecer um objeto novo chegando ao mesmo ponto. Hoje a
    família só importa como costura — carro, caminhão e ônibus já chegam
    unificados de `MAPA_CLASSES`.

    A espera é contada em ciclos de análise, não em segundos — o intervalo
    entre análises varia com a carga da máquina, e todo prazo em segundos
    liberava a captura antes de a seguinte existir.

    Depois de emitir, a passagem continua lembrada por `memoria_s`, e a
    lembrança se renova enquanto o objeto reaparecer naquele ponto. É o que
    impede um objeto parado de ser registrado de novo: a confiança de quem
    fica longe da câmera ronda o limiar — uma moto medida nesta instalação
    oscilava entre 0,65 e 0,71 contra um limiar de 0,65 — então o detector a
    perde e a reencontra sem parar, e cada reencontro parecia uma chegada
    nova. Eram dez registros da mesma moto parada em vinte e cinco minutos.

    Reaparecer não interrompe o acompanhamento: a caixa segue no vídeo
    enquanto o objeto estiver em cena. O que não acontece é virar registro
    outra vez.
    """

    def __init__(self, iou_minimo: float = 0.35, ciclos_de_espera: int = 3,
                 janela_maxima_s: float = 1800.0, memoria_s: float = 600.0) -> None:
        self.iou_minimo = iou_minimo
        self.ciclos_de_espera = ciclos_de_espera
        self.janela_maxima_s = janela_maxima_s
        self.memoria_s = memoria_s
        self._emcena: list[_ObjetoEmCena] = []
        self._emitidas: list[_PassagemEmitida] = []
        self._ciclo = 0

    def _emitida_em(self, familia: str, caixa: Sequence[float]) -> Optional[_PassagemEmitida]:
        """Passagem já registrada naquele ponto, se houver."""
        for antiga in self._emitidas:
            if antiga.familia == familia and iou(antiga.caixa, caixa) >= self.iou_minimo:
                return antiga
        return None

    def _alvo(self, familia: str, track_id: Optional[int],
              caixa: Sequence[float]) -> Optional[_ObjetoEmCena]:
        for item in self._emcena:
            if item.familia != familia:
                continue
            if track_id is not None and item.track_id == track_id:
                return item
        melhor, melhor_iou = None, self.iou_minimo
        for item in self._emcena:
            if item.familia != familia:
                continue
            sobreposicao = iou(item.caixa_atual, caixa)
            if sobreposicao >= melhor_iou:
                melhor, melhor_iou = item, sobreposicao
        return melhor

    def adicionar(self, obj, caixa: Sequence[float], zona_nome: str,
                  capturar, quando: datetime) -> None:
        """Registra a aparição do objeto nesta análise.

        `capturar() -> (recorte_jpeg, hash)` só é chamado quando esta leitura
        é a melhor da passagem até agora. Codificar um JPEG e o hash de todo
        objeto em todo ciclo custaria CPU para depois jogar quase tudo fora.
        """
        familia = familia_de(obj.classe)
        alvo = self._alvo(familia, obj.track_id, caixa)

        if alvo is None:
            # Já houve registro naquele ponto? Então isto é o mesmo objeto
            # reaparecendo, não um chegando: a confiança dele ronda o limiar e
            # o detector o perde e reencontra sem parar. Segue acompanhado,
            # para a caixa continuar no vídeo, mas não vira registro de novo.
            antiga = self._emitida_em(familia, caixa)
            if antiga is not None:
                antiga.quando = time.monotonic()
            recorte, hash_ev = capturar()
            self._emcena.append(_ObjetoEmCena(
                melhor=obj, recorte=recorte, hash_evidencia=hash_ev,
                quando=quando, bbox=list(caixa), familia=familia,
                track_id=obj.track_id, caixa_atual=list(caixa),
                zona_nome=zona_nome, primeiro=time.monotonic(), ciclo=self._ciclo,
                ja_registrado=antiga is not None,
            ))
            return

        alvo.ciclo = self._ciclo
        alvo.amostras += 1
        alvo.caixa_atual = list(caixa)
        if obj.track_id is not None:
            alvo.track_id = obj.track_id
        if alvo.ja_registrado:
            # Enquanto continua ali, a lembrança não envelhece — senão um
            # objeto parado voltaria a ser registrado ao vencer a memória.
            antiga = self._emitida_em(familia, caixa)
            if antiga is not None:
                antiga.quando = time.monotonic()
        if obj.confianca > alvo.melhor.confianca:
            alvo.recorte, alvo.hash_evidencia = capturar()
            alvo.melhor, alvo.bbox = obj, list(caixa)
            alvo.zona_nome, alvo.quando = zona_nome, quando

    def prontos(self) -> list[_ObjetoEmCena]:
        """Objetos cuja passagem terminou — saíram de cena ou demoraram demais.

        Quem fica parado além da janela é encerrado do mesmo jeito: a análise
        seguinte abre uma passagem nova, e um veículo estacionado rende um
        registro por janela em vez de um por ciclo.
        """
        self._ciclo += 1
        agora = time.monotonic()
        self._emitidas = [e for e in self._emitidas
                          if agora - e.quando < self.memoria_s]

        saindo, ficando = [], []
        for item in self._emcena:
            sumiu = self._ciclo - item.ciclo >= self.ciclos_de_espera
            demorou = agora - item.primeiro >= self.janela_maxima_s
            (saindo if sumiu or demorou else ficando).append(item)
        self._emcena = ficando

        novos = []
        for item in saindo:
            antiga = self._emitida_em(item.familia, item.caixa_atual)
            if item.ja_registrado or antiga is not None:
                # Já contado: renova a lembrança e fica por isso mesmo.
                if antiga is not None:
                    antiga.quando, antiga.caixa = agora, list(item.caixa_atual)
                continue
            self._emitidas.append(_PassagemEmitida(
                familia=item.familia, caixa=list(item.caixa_atual), quando=agora,
            ))
            novos.append(item)
        return novos


class PipelineCamera:
    """Laço de análise de uma câmera."""

    def __init__(self, cfg: ConfigCamera, cliente: httpx.AsyncClient) -> None:
        self.cfg = cfg
        self.cliente = cliente
        self.leitor = LeitorRTSP(cfg.rtsp_url)
        self.acoes = AnalisadorAcoes()
        self.seletor = SeletorMelhorQuadro()
        self.objetos = SeletorMelhorObjeto()
        self._tinha_caixas = False
        self.ultimo_heartbeat = 0.0
        self.quadros = 0

    # -- envio ---------------------------------------------------------
    async def _enviar(self, payload: dict) -> None:
        try:
            r = await self.cliente.post(f"{API}/deteccoes/ingest", json=payload, timeout=10.0)
            if r.status_code in (401, 403):
                # Nomear a causa: com token vencido tudo continua rodando e só
                # o registro some, o que na interface parece defeito do modelo.
                log.error("Ingest recusado (%s): credencial inválida ou vencida - "
                          "a detecção foi DESCARTADA. Reinicie o worker com "
                          "--usuario/--senha para renovação automática.", r.status_code)
            elif r.status_code >= 400:
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

    async def _enviar_objeto(self, item: "_ObjetoEmCena") -> None:
        obj = item.melhor
        await self._enviar({
            "tipo": "OBJETO",
            "camera_codigo": self.cfg.codigo,
            # O instante é o da melhor leitura, não o do fim da passagem.
            "ocorrido_em": item.quando.isoformat(),
            "classe": obj.classe,
            "confianca": round(obj.confianca, 4),
            "bbox": item.bbox,
            "track_id": item.track_id,
            "zona_nome": item.zona_nome,
            "snapshot_jpeg": item.recorte,
            "hash_evidencia": item.hash_evidencia,
        })
        log.info("%s: %s enviado (confiança %.2f, melhor de %d leituras)",
                 self.cfg.codigo, obj.classe, obj.confianca, item.amostras)

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
                objetos = suprimir_sobrepostas(objetos)
                for obj in objetos:
                    caixa = obj.bbox_normalizado(largura, altura)
                    # Toda classe é acompanhada; o que evita o ruído não é
                    # filtrar classe, é registrar cada passagem uma vez só.
                    self.objetos.adicionar(
                        obj, caixa,
                        self._zona_do_ponto(
                            (obj.centro()[0] / largura, obj.centro()[1] / altura)
                        ),
                        lambda q=quadro, b=obj.bbox: (
                            self._recorte(q, b), hash_evidencia(q.tobytes())
                        ),
                        agora,
                    )
                for item in self.objetos.prontos():
                    await self._enviar_objeto(item)

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

