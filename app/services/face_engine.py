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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AmostraFacial, Pessoa
from app.tipos import dialeto_suporta_vetor

log = logging.getLogger("seguranca.face")

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
    # Quem ficou em primeiro, tendo passado do limiar ou não. Separado de
    # `pessoa_id` porque abaixo do limiar isto é palpite, não identificação:
    # quem audita e quem desenha a caixa continuam lendo `pessoa_id`, e só a
    # fila de conferência olha para cá.
    candidato_id: Optional[str] = None
    candidato_bio: Optional[str] = None

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


def extrair_faces(quadro: np.ndarray, min_qualidade: Optional[float] = None,
                  min_pixels: Optional[int] = None,
                  min_nitidez: Optional[float] = None) -> List[FaceDetectada]:
    """Detecta rostos no quadro BGR e devolve embeddings normalizados.

    Resolução e nitidez têm piso próprio, aplicado antes da nota de
    qualidade. As duas são eliminatórias: rosto pequeno é esticado até os
    112x112 que o ArcFace recebe, rosto borrado não tem detalhe, e em ambos
    os casos sai um vetor que não casa nem com outra captura da mesma pessoa.

    A nota não dá conta delas porque é média ponderada — cada uma pesa pouco
    e as outras compensam. Medidas nesta instalação, passavam pela nota um
    rosto de 50 pixels nítido e de frente (0,7) e um de 145 pixels de frente
    com nitidez 0,01 (0,61). O segundo é o pior dos dois, e era o que mais
    parecia aprovado.
    """
    limite = settings.MIN_FACE_QUALITY if min_qualidade is None else min_qualidade
    piso = settings.MIN_FACE_PIXELS if min_pixels is None else min_pixels
    # Afrouxável porque nem todo chamador quer identificar: para enquadrar o
    # rosto no vídeo, um rosto borrado ou pequeno ainda é um rosto que o
    # operador precisa ver marcado.
    piso_nitidez = settings.MIN_FACE_SHARPNESS if min_nitidez is None else min_nitidez
    modelo = obter_modelo()
    altura, largura = quadro.shape[:2]

    saida: List[FaceDetectada] = []
    with _trava_detector:
        faces = modelo.get(quadro)
    for face in faces:
        x1, y1, x2, y2 = [int(v) for v in face.bbox]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(largura, x2), min(altura, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        if y2 - y1 < piso:
            continue
        recorte = quadro[y1:y2, x1:x2]
        if _nitidez(recorte) < piso_nitidez:
            continue
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
import threading as _threading

# O limiar do detector é um atributo do modelo compartilhado: baixá-lo para
# uma tentativa sem trava faria a inferência ao vivo, no mesmo instante,
# aceitar rosto a 15% de confiança.
_trava_detector = _threading.Lock()


def _com_limiar(limiar: float, quadro: np.ndarray):
    modelo = obter_modelo()
    det = getattr(modelo, "det_model", None)
    with _trava_detector:
        antes = getattr(det, "det_thresh", None)
        try:
            if det is not None and antes is not None:
                det.det_thresh = limiar
            return modelo.get(quadro)
        finally:
            if det is not None and antes is not None:
                det.det_thresh = antes


def extrair_confirmado(jpeg: bytes) -> Optional[FaceDetectada]:
    """O rosto de uma captura que um humano já confirmou ser da pessoa.

    `extrair_de_recorte` usa o detector como ele vem, e rosto de lado não
    passa no limiar de 50% dele — o operador olhava, via que era a pessoa,
    e o sistema recusava o vínculo. Aqui a decisão já foi tomada por quem
    olhou, então o detector só precisa *achar* o rosto: limiar de 15%,
    moldura maior, imagem ampliada e espelhada, nessa ordem, até achar.
    """
    face = extrair_de_recorte(jpeg)
    if face is not None:
        return face
    try:
        import cv2
    except ImportError:
        raise ModelosIndisponiveis("OpenCV não instalado.")
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    tentativas = []
    for borda in (0.4, 0.8, 0.0):
        alt, larg = img.shape[:2]
        mold = cv2.copyMakeBorder(img, int(alt * borda), int(alt * borda),
                                  int(larg * borda), int(larg * borda), cv2.BORDER_REPLICATE)
        tentativas.append(mold)
        if max(img.shape[:2]) < 160:
            tentativas.append(cv2.resize(mold, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
        tentativas.append(cv2.flip(mold, 1))
    for quadro in tentativas:
        melhor = None
        for f in _com_limiar(settings.FACE_LIMIAR_CONFIRMADO, quadro):
            x1, y1, x2, y2 = [int(v) for v in f.bbox]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(quadro.shape[1], x2), min(quadro.shape[0], y2)
            if x2 <= x1 or y2 <= y1:
                continue
            lm = getattr(f, "kps", None)
            q = calcular_qualidade(quadro[y1:y2, x1:x2], float(f.det_score), lm, y2 - y1)
            cand = FaceDetectada(bbox=(x1, y1, x2, y2),
                                 embedding=np.asarray(f.normed_embedding, dtype=np.float32),
                                 qualidade=q, det_score=float(f.det_score), landmarks=lm)
            if melhor is None or cand.qualidade > melhor.qualidade:
                melhor = cand
        if melhor is not None:
            return melhor
    return None


def extrair_de_recorte(jpeg: bytes) -> Optional[FaceDetectada]:
    """Refaz o embedding a partir do recorte gravado de uma captura.

    O recorte vem enquadrado no rosto, e aí o detector erra: o SCRFD foi
    treinado em cenas, não em retratos que ocupam o quadro inteiro, e perde
    metade dos recortes desta instalação. Uma borda replicada devolve ao
    rosto a proporção que o detector espera — medido aqui, 50% de acerto
    passa a 97%. Ampliar não ajuda: não cria detalhe e piora o embedding.
    """
    try:
        import cv2
    except ImportError:
        raise ModelosIndisponiveis("OpenCV não instalado.")

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    alt, larg = img.shape[:2]
    mx, my = int(larg * 0.4), int(alt * 0.4)
    emoldurado = cv2.copyMakeBorder(img, my, my, mx, mx, cv2.BORDER_REPLICATE)

    # Pisos zerados: o que decide se a captura serve de amostra é o
    # operador que a reconheceu na tela, não a nota automática.
    faces = extrair_faces(emoldurado, 0.0, 0, 0.0)
    if not faces:
        return None
    return max(faces, key=lambda f: f.qualidade)


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
def _melhor_por_pessoa(candidatos: list[tuple], k: int) -> list[tuple]:
    """De várias amostras da mesma pessoa, fica a que mais se parece com a busca.

    A pessoa aparece uma vez só no resultado, com a sua melhor amostra — é o
    que permite cadastrar um ângulo por câmera sem que um atrapalhe o outro.
    """
    melhor: dict = {}
    for pid, id_bio, nome, status, sim in candidatos:
        atual = melhor.get(pid)
        if atual is None or sim > atual[4]:
            melhor[pid] = (pid, id_bio, nome, status, sim)
    return sorted(melhor.values(), key=lambda t: -t[4])[:k]


async def _sem_amostra(db: AsyncSession):
    """Quem ainda não tem amostra guardada responde pelo próprio template."""
    return (
        await db.execute(
            select(Pessoa.id, Pessoa.id_biometrico, Pessoa.nome,
                   Pessoa.status, Pessoa.embedding)
            .where(Pessoa.embedding.isnot(None), Pessoa.ativo.is_(True),
                   ~Pessoa.id.in_(select(AmostraFacial.pessoa_id)))
        )
    ).all()


async def _topk_postgres(db: AsyncSession, vetor: list, k: int) -> list[tuple]:
    from pgvector.sqlalchemy import Vector  # noqa: F401

    # Mais linhas que `k` de propósito: várias podem ser da mesma pessoa, e a
    # dedupe acontece depois, em `_melhor_por_pessoa`.
    linhas = (
        await db.execute(
            select(Pessoa.id, Pessoa.id_biometrico, Pessoa.nome, Pessoa.status,
                   (1 - AmostraFacial.embedding.cosine_distance(vetor)).label("sim"))
            .join(AmostraFacial, AmostraFacial.pessoa_id == Pessoa.id)
            .where(Pessoa.ativo.is_(True))
            .order_by(AmostraFacial.embedding.cosine_distance(vetor))
            .limit(max(k * 8, 40))
        )
    ).all()
    candidatos = [(r.id, r.id_biometrico, r.nome, r.status, float(r.sim)) for r in linhas]

    restantes = await _sem_amostra(db)
    if restantes:
        alvo = np.asarray(vetor, dtype=np.float32)
        candidatos += _similaridades(restantes, alvo)
    return _melhor_por_pessoa(candidatos, k)


def _similaridades(linhas: list, vetor: np.ndarray) -> list[tuple]:
    matriz = np.array([r[4] for r in linhas], dtype=np.float32)
    normas = np.linalg.norm(matriz, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    sims = (matriz / normas) @ vetor
    return [(r[0], r[1], r[2], r[3], float(sims[i])) for i, r in enumerate(linhas)]


async def _topk_memoria(db: AsyncSession, vetor: np.ndarray, k: int) -> list[tuple]:
    """Força bruta contra TODAS as amostras, ficando a melhor de cada pessoa.

    Comparar contra a média das amostras era o que prendia cada pessoa a uma
    câmera só. Medido nesta base: as amostras de uma mesma pessoa se parecem
    entre si de 0,24 a 0,47 — são ângulos bem diferentes —, e a média delas
    fica a 0,68–0,90 de cada uma. Esse meio-termo representa pior cada ângulo
    do que a própria amostra representaria, então só o ângulo dominante ficava
    acima do limiar e os demais caíam fora.

    Com o máximo por amostra, cada ângulo cadastrado responde pelo seu, e
    acrescentar uma amostra nova nunca piora as que já existem.

    Com vetores normalizados, o produto escalar já é a similaridade cosseno.
    """
    linhas = (
        await db.execute(
            select(Pessoa.id, Pessoa.id_biometrico, Pessoa.nome,
                   Pessoa.status, AmostraFacial.embedding)
            .join(AmostraFacial, AmostraFacial.pessoa_id == Pessoa.id)
            .where(Pessoa.ativo.is_(True))
        )
    ).all()
    linhas = list(linhas) + list(await _sem_amostra(db))
    if not linhas:
        return []

    return _melhor_por_pessoa(_similaridades(linhas, vetor), k)




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
        return ResultadoBusca(None, None, round(sim, 4), lim,
                              str(pessoa_id), id_bio)
    return ResultadoBusca(str(pessoa_id), id_bio, round(sim, 4), lim,
                          str(pessoa_id), id_bio)


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


async def amostras_por_camera(db: AsyncSession, pessoa_id) -> dict:
    """Quantas amostras o template tem de cada câmera. Nulo = foto de cadastro."""
    linhas = (
        await db.execute(
            select(AmostraFacial.camera_id, func.count())
            .where(AmostraFacial.pessoa_id == pessoa_id)
            .group_by(AmostraFacial.camera_id)
        )
    ).all()
    return {cid: n for cid, n in linhas}


async def acolher_amostra(db: AsyncSession, pessoa: Pessoa, embedding: Sequence[float],
                          qualidade: float, camera_id) -> bool:
    """Guarda a captura como amostra, se ela acrescentar um ângulo novo.

    É o que faz o template crescer sozinho depois do primeiro acerto numa
    câmera: a busca compara contra cada amostra e fica com a melhor, então
    uma amostra do ângulo daquela câmera é o que a torna capaz de reconhecer
    ali — e nunca piora as câmeras já cobertas.

    Recusa em três casos, e cada um tem a sua razão:

    * **sem câmera** — não há ângulo a registrar, e sem saber de onde veio a
      cota não teria como ser contada;
    * **cota cheia** — a busca é força bruta contra todas as amostras, então
      cada uma custa em toda identificação do parque; e sem teto a câmera
      movimentada enche o cadastro sozinha enquanto as outras seguem
      descobertas;
    * **parecida demais com uma que já existe** — não acrescenta ângulo
      nenhum e gastaria a cota que a próxima posição precisa.

    Devolve se a amostra entrou, para quem chamou poder registrar o fato.
    """
    if camera_id is None:
        return False

    existentes = (
        await db.execute(
            select(AmostraFacial.embedding).where(
                AmostraFacial.pessoa_id == pessoa.id,
                AmostraFacial.camera_id == camera_id,
            )
        )
    ).scalars().all()
    if len(existentes) >= settings.FACE_SAMPLES_PER_CAMERA:
        return False

    vetor = normalizar(embedding)
    for outro in existentes:
        if similaridade_cosseno(outro, vetor) >= settings.FACE_SAMPLE_DISTINTA:
            return False

    db.add(AmostraFacial(
        pessoa_id=pessoa.id, embedding=vetor.tolist(),
        qualidade=round(float(qualidade or 0.0), 4),
        origem="captura", camera_id=camera_id,
    ))
    await db.flush()
    # O template médio deixou de ser o que a busca consulta, mas segue valendo
    # para quem não tem amostra nenhuma — e `amostras`/`qualidade` são o que a
    # ficha mostra. Mantê-lo em dia custa pouco e evita ficha desatualizada.
    await recalcular_template(db, pessoa)
    return True

