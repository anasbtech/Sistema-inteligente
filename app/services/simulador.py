"""Simulador de câmeras.

Gera detecções plausíveis sem nenhuma câmera ligada, usando **a mesma função
de ingestão** que o worker de inferência real chama. Isso importa: o caminho
exercitado em demonstração é o caminho de produção — busca 1:N, limiar da
câmera, geração de alerta, trilha de auditoria e publicação no barramento.

Para desligar: `SIMULADOR=false` no `.env`.

Nada aqui inventa identidade. Para simular um reconhecimento facial, o
simulador sorteia uma pessoa **já cadastrada**, toma o template dela e
adiciona ruído — exatamente o que uma câmera faria ao capturar aquele rosto de
outro ângulo. A busca 1:N então roda de verdade e pode, inclusive, não bater.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
from sqlalchemy import func, select

from app.config import settings
from app.database import db_context
from app.models import Camera, Pessoa, StatusCamera, StatusPessoa, TipoDeteccao
from app.schemas import DeteccaoIn
from app.services import video

log = logging.getLogger("seguranca.simulador")

rng = random.Random()
nprng = np.random.default_rng()

CLASSES_OBJETO = [
    ("pessoa", 22), ("veiculo", 18), ("motocicleta", 9), ("bicicleta", 5),
    ("mochila", 7), ("volume", 2), ("capacete", 5), ("placa", 8), ("arma", 1),
]
CLASSES_ACAO = [
    ("corrida", 14), ("queda", 4), ("aglomeracao", 8), ("briga", 3), ("roubo", 2),
    ("permanencia", 16), ("invasao", 4), ("contrafluxo", 9),
    ("abandono", 6), ("vandalismo", 3), ("fogo", 2),
]
LETRAS = "ABCDEFGHJKLMNPQRSTUVWX"


def _sortear(pares: List[tuple]) -> str:
    itens = [p[0] for p in pares]
    pesos = [p[1] for p in pares]
    return rng.choices(itens, weights=pesos, k=1)[0]


def _placa() -> str:
    return (f"{rng.choice(LETRAS)}{rng.choice(LETRAS)}{rng.choice(LETRAS)}"
            f"{rng.randint(0, 9)}{rng.choice('ABCDEFGHJ')}"
            f"{rng.randint(0, 9)}{rng.randint(0, 9)}")


def _bbox() -> List[float]:
    x, y = rng.uniform(0.08, 0.60), rng.uniform(0.20, 0.55)
    return [round(x, 3), round(y, 3),
            round(rng.uniform(0.10, 0.26), 3), round(rng.uniform(0.14, 0.30), 3)]


def _embedding_com_ruido(template: List[float], forca: float) -> List[float]:
    """Perturba o template como faria um ângulo, iluminação ou distância nova.

    `forca` baixa ⇒ captura nítida, similaridade alta. Alta ⇒ captura ruim,
    que pode cair abaixo do limiar e virar "não identificada" — é assim que a
    demonstração mostra os dois desfechos.
    """
    base = np.asarray(template, dtype=np.float32)
    ruido = nprng.normal(scale=forca, size=base.shape).astype(np.float32)
    v = base + ruido
    norma = float(np.linalg.norm(v))
    return (v / norma).tolist() if norma else base.tolist()


class Simulador:
    """Laço assíncrono que injeta detecções em intervalos irregulares."""

    def __init__(self, intervalo: float = 2.5) -> None:
        self.intervalo = intervalo
        self._task: Optional[asyncio.Task] = None
        self.gerados = 0

    async def iniciar(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        log.info("Simulador de câmeras ativo (intervalo ~%.1fs)", self.intervalo)

    async def parar(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # Deixa a API terminar de subir antes do primeiro evento.
        await asyncio.sleep(3)
        while True:
            try:
                await self._gerar()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha ao gerar detecção simulada: %s", exc)
            # Intervalo irregular: eventos reais não chegam em cadência fixa.
            await asyncio.sleep(self.intervalo * rng.uniform(0.45, 1.8))

    async def _gerar(self) -> None:
        # Importação tardia evita ciclo (routers importam services).
        from app.routers.deteccoes import processar_deteccao

        async with db_context() as db:
            cameras = (
                await db.execute(
                    select(Camera).where(Camera.status == StatusCamera.ONLINE)
                )
            ).scalars().unique().all()
            if not cameras:
                return

            cam = rng.choice(cameras)
            agora = datetime.now(timezone.utc)

            # Sorteia entre os analíticos que a câmera de fato tem ligados.
            # A escada de `elif` anterior sorteava a faixa antes de olhar o que
            # a câmera fazia: numa câmera só-facial, toda sorte acima de 0,40
            # caía na faixa de objeto, não encontrava objeto ligado e saía sem
            # gerar nada — 60% dos ciclos perdidos.
            pesos = [
                ("facial", 40 if cam.analitico_facial else 0),
                ("objeto", 40 if cam.analitico_objeto else 0),
                # Análise ligada sem nenhuma ação marcada não gera nada: pesar
                # a faixa faria o ciclo cair nela e sair vazio, que é o mesmo
                # desperdício que o comentário acima descreve.
                ("acao", 20 if cam.analitico_acao and cam.acoes_ativas else 0),
            ]
            disponiveis = [(nome, p) for nome, p in pesos if p]
            if not disponiveis:
                return

            escolha = rng.choices(
                [n for n, _ in disponiveis], weights=[p for _, p in disponiveis], k=1
            )[0]

            if escolha == "facial":
                dados = await self._facial(db, cam, agora)
            elif escolha == "objeto":
                dados = self._objeto(cam, agora)
            else:
                dados = self._acao(cam, agora)

            if dados is None:
                return

            await self._anexar_recorte(cam, dados)
            await processar_deteccao(db, dados, origem="simulador")
            self.gerados += 1

    async def _anexar_recorte(self, cam: Camera, dados: DeteccaoIn) -> None:
        """Anexa o recorte do que a câmera está vendo agora.

        Sem isso a captura fica sem imagem e a revisão humana não tem o que
        olhar. O recorte sai do vídeo real; a caixa é que é sintética, então
        o enquadramento corresponde à câmera, não ao que foi classificado.

        Roda fora do laço de eventos: decodificar e recodificar JPEG segura o
        event loop, e com ele o WebSocket de eventos e a API inteira.
        """
        if not cam.rtsp_url:
            return
        try:
            jpeg = await asyncio.to_thread(
                video.recorte, cam.codigo, cam.rtsp_url, dados.bbox
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Sem recorte para %s: %s", cam.codigo, exc)
            return
        if jpeg:
            dados.snapshot_jpeg = base64.b64encode(jpeg).decode("ascii")

    async def _facial(self, db, cam: Camera, agora: datetime) -> Optional[DeteccaoIn]:
        """Simula uma captura de rosto a partir de alguém já cadastrado."""
        total = (
            await db.execute(
                select(func.count()).select_from(Pessoa)
                .where(Pessoa.embedding.isnot(None), Pessoa.ativo.is_(True))
            )
        ).scalar_one()
        if not total:
            return None

        # Pessoas com restrição aparecem menos — como na vida real.
        pessoa = (
            await db.execute(
                select(Pessoa)
                .where(Pessoa.embedding.isnot(None), Pessoa.ativo.is_(True))
                .offset(rng.randrange(total)).limit(1)
            )
        ).scalar_one_or_none()
        if pessoa is None:
            return None
        if pessoa.status == StatusPessoa.RESTRITO and rng.random() > 0.35:
            return None

        # 15% das capturas saem ruins o bastante para não baterem no limiar.
        ruim = rng.random() < 0.15
        forca = rng.uniform(0.09, 0.16) if ruim else rng.uniform(0.012, 0.055)

        return DeteccaoIn(
            tipo=TipoDeteccao.FACIAL,
            camera_codigo=cam.codigo,
            ocorrido_em=agora,
            embedding=_embedding_com_ruido(pessoa.embedding, forca),
            qualidade_face=round(rng.uniform(0.38, 0.72) if ruim
                                 else rng.uniform(0.72, 0.99), 4),
            bbox=_bbox(),
        )

    def _objeto(self, cam: Camera, agora: datetime) -> DeteccaoIn:
        classe = _sortear(CLASSES_OBJETO)
        zona = rng.choice(cam.zonas).nome if cam.zonas else ""
        return DeteccaoIn(
            tipo=TipoDeteccao.OBJETO,
            camera_codigo=cam.codigo,
            ocorrido_em=agora,
            classe=classe,
            confianca=round(rng.uniform(cam.limiar_objeto, 0.995), 4),
            placa=_placa() if classe == "placa" else "",
            zona_nome=zona,
            bbox=_bbox(),
            track_id=rng.randint(1, 900),
        )

    def _acao(self, cam: Camera, agora: datetime) -> Optional[DeteccaoIn]:
        # A cena sintética respeita a seleção da câmera. Sortear fora dela
        # produziria registro de uma ação que aquela câmera declara não
        # vigiar — e quem revisa não teria como saber que foi invenção.
        permitidas = set(cam.acoes_ativas or ())
        pares = [p for p in CLASSES_ACAO if p[0] in permitidas]
        if not pares:
            return None
        classe = _sortear(pares)
        envolvidos = (rng.randint(6, 34) if classe == "aglomeracao"
                      else rng.randint(2, 5) if classe == "briga"
                      else 2 if classe == "roubo"
                      else rng.randint(1, 3))
        return DeteccaoIn(
            tipo=TipoDeteccao.ACAO,
            camera_codigo=cam.codigo,
            ocorrido_em=agora,
            classe=classe,
            confianca=round(rng.uniform(0.62, 0.98), 4),
            duracao_s=rng.randint(3, 240),
            envolvidos=envolvidos,
            zona_nome=rng.choice(cam.zonas).nome if cam.zonas else "",
            bbox=_bbox(),
            track_id=rng.randint(1, 900),
        )


simulador = Simulador(settings.SIMULADOR_INTERVALO)
