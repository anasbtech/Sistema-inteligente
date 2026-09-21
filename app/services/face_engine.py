"""Motor facial: detecção, embedding 512-D e busca 1:N.

O que este módulo faz e o que deliberadamente NÃO faz
------------------------------------------------------
FAZ     detectar rostos, medir qualidade, produzir vetor L2-normalizado,
        buscar o vizinho mais próximo e aplicar o limiar da câmera.
NÃO FAZ inferir sexo, idade ou etnia a partir da imagem, nem criar registros.
        Identidade só existe se já houver cadastro; abaixo do limiar o
        resultado é "não identificado" — nunca um palpite.

Busca em dois modos:
* **PostgreSQL + pgvector** — operador `<=>` com índice HNSW. Escala para
  centenas de milhares de registros.
* **SQLite** — força bruta em numpy. Suficiente para demonstração e para
  bases de alguns milhares; o custo cresce linearmente.

InsightFace e OpenCV são carregados sob demanda: a API sobe e o sistema
inteiro funciona sem eles, com o simulador no lugar das câmeras.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AmostraFacial, Pessoa
from app.tipos import dialeto_suporta_vetor

log = logging.getLogger("vigia.face")

_app = None


class ModelosIndisponiveis(RuntimeError):
    """InsightFace não instalado — só o caminho com imagem real é afetado."""


@dataclass
class FaceDetectada:
    bbox: tuple[int, int, int, int]
    embedding: np.ndarray
    qualidade: float
    det_score: float
    landmarks: Optional[np.ndarray] = None

    def bbox_normalizado(self, largura: int, altura: int) -> List[float]:
        x1, y1, x2, y2 = self.bbox
        return [x1 / largura, y1 / altura, (x2 - x1) / largura, (y2 - y1) / altura]


@dataclass
class ResultadoBusca:
    pessoa_id: Optional[str]
    id_biometrico: Optional[str]
    similaridade: float
    limiar: float

    @property
    def match(self) -> bool:
        return self.pessoa_id is not None and self.similaridade >= self.limiar


# ----------------------------------------------------------------------
# Modelo
# ----------------------------------------------------------------------
def modelos_disponiveis() -> bool:
    try:
        import insightface  # noqa: F401
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def obter_modelo():
    """Carrega o InsightFace uma vez por processo."""
    global _app
    if _app is not None:
        return _app
    try:
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        raise ModelosIndisponiveis(
            "InsightFace não está instalado. Para usar imagens reais:\n"
            "    pip install insightface onnxruntime\n"
            "O restante do sistema funciona sem ele."
        ) from exc

    provedores = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                  if settings.DEVICE == "cuda" else ["CPUExecutionProvider"])
    _app = FaceAnalysis(name=settings.FACE_MODEL, providers=provedores)
    _app.prepare(ctx_id=0 if settings.DEVICE == "cuda" else -1,
                 det_size=(settings.FACE_DET_SIZE, settings.FACE_DET_SIZE))
    log.info("InsightFace '%s' carregado (%s)", settings.FACE_MODEL, settings.DEVICE)
    return _app


# ----------------------------------------------------------------------
# Qualidade
# ----------------------------------------------------------------------
def _nitidez(recorte: np.ndarray) -> float:
    import cv2
    if recorte.size == 0:
        return 0.0
    cinza = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
    return float(min(cv2.Laplacian(cinza, cv2.CV_64F).var() / 500.0, 1.0))


def _frontalidade(landmarks: Optional[np.ndarray]) -> float:
    if landmarks is None or len(landmarks) < 3:
        return 0.7
    olho_e, olho_d, nariz = landmarks[0], landmarks[1], landmarks[2]
    meio_x = (olho_e[0] + olho_d[0]) / 2.0
    dist = float(np.linalg.norm(olho_d - olho_e)) or 1.0
    return float(max(0.0, 1.0 - abs(nariz[0] - meio_x) / dist * 1.8))


def calcular_qualidade(recorte: np.ndarray, det_score: float,
                       landmarks: Optional[np.ndarray], altura_face: int) -> float:
    """Nitidez, frontalidade, resolução do rosto e confiança do detector.

    Rosto ruim gera match menos confiável, então a qualidade entra no registro
    da detecção e pode barrar o cadastro de uma amostra fraca.
    """
    escala = min(altura_face / 112.0, 1.0)   # 112px é a entrada do ArcFace
    q = (0.30 * _nitidez(recorte) + 0.25 * _frontalidade(landmarks)
         + 0.25 * escala + 0.20 * float(min(det_score, 1.0)))
    return round(float(max(0.0, min(q, 1.0))), 4)


def extrair_faces(quadro: np.ndarray, min_qualidade: Optional[float] = None) -> List[FaceDetectada]:
    """Detecta rostos no quadro BGR e devolve embeddings normalizados."""
    limite = settings.MIN_FACE_QUALITY if min_qualidade is None else min_qualidade
    modelo = obter_modelo()
    altura, largura = quadro.shape[:2]

    saida: List[FaceDetectada] = []
    for face in modelo.get(quadro):
        x1, y1, x2, y2 = [int(v) for v in face.bbox]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(largura, x2), min(altura, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        recorte = quadro[y1:y2, x1:x2]
        lm = getattr(face, "kps", None)
        q = calcular_qualidade(recorte, float(face.det_score), lm, y2 - y1)
        if q < limite:
            continue
        saida.append(FaceDetectada(
            bbox=(x1, y1, x2, y2),
            embedding=np.asarray(face.normed_embedding, dtype=np.float32),
            qualidade=q, det_score=float(face.det_score), landmarks=lm,
        ))
    return saida


# ----------------------------------------------------------------------
# Álgebra
# ----------------------------------------------------------------------
def normalizar(v: Sequence[float]) -> np.ndarray:
    arr = np.asarray(v, dtype=np.float32)
    norma = float(np.linalg.norm(arr))
    return arr / norma if norma else arr


def media_embeddings(embeddings: Sequence[Sequence[float]]) -> np.ndarray:
    """Template da pessoa = média L2-normalizada das amostras."""
    if not embeddings:
        raise ValueError("Nenhuma amostra fornecida")
    return normalizar(np.mean([normalizar(e) for e in embeddings], axis=0))


def similaridade_cosseno(a: Sequence[float], b: Sequence[float]) -> float:
    return float(np.dot(normalizar(a), normalizar(b)))


# ----------------------------------------------------------------------
# Busca 1:N
# ----------------------------------------------------------------------
async def _topk_postgres(db: AsyncSession, vetor: list, k: int) -> list[tuple]:
    from pgvector.sqlalchemy import Vector  # noqa: F401

    linhas = (
        await db.execute(
            select(Pessoa.id, Pessoa.id_biometrico, Pessoa.nome, Pessoa.status,
                   (1 - Pessoa.embedding.cosine_distance(vetor)).label("sim"))
            .where(Pessoa.embedding.isnot(None), Pessoa.ativo.is_(True))
            .order_by(Pessoa.embedding.cosine_distance(vetor))
            .limit(k)
        )
    ).all()
    return [(r.id, r.id_biometrico, r.nome, r.status, float(r.sim)) for r in linhas]


async def _topk_memoria(db: AsyncSession, vetor: np.ndarray, k: int) -> list[tuple]:
    """Força bruta: carrega os templates e multiplica a matriz de uma vez.

    Com vetores normalizados, o produto escalar já é a similaridade cosseno.
    """
    linhas = (
        await db.execute(
            select(Pessoa.id, Pessoa.id_biometrico, Pessoa.nome,
                   Pessoa.status, Pessoa.embedding)
            .where(Pessoa.embedding.isnot(None), Pessoa.ativo.is_(True))
        )
    ).all()
    if not linhas:
        return []

    matriz = np.array([r.embedding for r in linhas], dtype=np.float32)
    normas = np.linalg.norm(matriz, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    sims = (matriz / normas) @ vetor

    melhores = np.argsort(-sims)[:k]
    return [
        (linhas[i].id, linhas[i].id_biometrico, linhas[i].nome,
         linhas[i].status, float(sims[i]))
        for i in melhores
    ]


async def buscar_pessoa(db: AsyncSession, embedding: Sequence[float],
                        limiar: Optional[float] = None) -> ResultadoBusca:
    """Vizinho mais próximo por similaridade cosseno.

    Devolve `pessoa_id=None` quando nada supera o limiar — a captura fica
    registrada como "não identificada" e nenhuma identidade é atribuída.
    """
    lim = settings.FACE_THRESHOLD if limiar is None else limiar
    vetor = normalizar(embedding)

    if dialeto_suporta_vetor(db):
        topo = await _topk_postgres(db, vetor.tolist(), 1)
    else:
        topo = await _topk_memoria(db, vetor, 1)

    if not topo:
        return ResultadoBusca(None, None, 0.0, lim)

    pessoa_id, id_bio, _nome, _status, sim = topo[0]
    if sim < lim:
        return ResultadoBusca(None, None, round(sim, 4), lim)
    return ResultadoBusca(str(pessoa_id), id_bio, round(sim, 4), lim)


async def buscar_candidatos(db: AsyncSession, embedding: Sequence[float],
                            k: int = 5) -> List[dict]:
    """Top-k para conferência humana — não para despacho automático."""
    vetor = normalizar(embedding)
    topo = (await _topk_postgres(db, vetor.tolist(), k)
            if dialeto_suporta_vetor(db) else await _topk_memoria(db, vetor, k))
    return [
        {
            "pessoa_id": str(pid), "id_biometrico": idb, "nome": nome,
            "status": status.value if hasattr(status, "value") else str(status),
            "similaridade": round(sim, 4),
        }
        for pid, idb, nome, status, sim in topo
    ]


async def recalcular_template(db: AsyncSession, pessoa: Pessoa) -> None:
    """Refaz o template a partir das amostras e atualiza qualidade e contagem."""
    amostras = (
        await db.execute(select(AmostraFacial).where(AmostraFacial.pessoa_id == pessoa.id))
    ).scalars().all()

    if not amostras:
        pessoa.embedding = None
        pessoa.qualidade = 0.0
        pessoa.amostras = 0
        return

    pessoa.embedding = media_embeddings([a.embedding for a in amostras]).tolist()
    pessoa.qualidade = round(float(np.mean([a.qualidade for a in amostras])), 4)
    pessoa.amostras = len(amostras)
