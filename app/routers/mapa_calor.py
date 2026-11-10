"""Mapa de calor: onde e quando há movimento em cada câmera.

**Onde** é uma grade sobre a imagem da câmera. Cada detecção entra pelo seu
ponto de apoio: o pé da caixa para pessoa e veículo (é o que toca o chão — o
centro da caixa de uma pessoa cai no peito dela e borra a passagem de quem
anda perto da câmera) e o centro para rosto, que não tem chão.

**Quando** é uma grade de dia da semana por hora, no fuso do servidor.

Nada aqui identifica ninguém: são contagens por célula. Por isso a consulta
não gera trilha de auditoria por pessoa — o mesmo critério das listas.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import exige
from app.models import Camera, Deteccao, TipoDeteccao, Usuario

router = APIRouter(prefix="/mapa-calor", tags=["Mapa de calor"])

COLUNAS, LINHAS = 96, 54          # 16:9, ~20 px por célula numa tela de 1920
VEICULOS = ("carro", "motocicleta", "caminhao", "onibus", "bicicleta")
CAMADAS = {
    "pessoas": "Pessoas (corpo inteiro, pelo ponto em que pisam)",
    "veiculos": "Veículos (pelo ponto em que rodam)",
    "rostos": "Rostos (capturas do reconhecimento facial)",
}
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def _filtro_camada(camada: str):
    if camada == "pessoas":
        return [Deteccao.tipo == TipoDeteccao.OBJETO, Deteccao.classe == "pessoa"]
    if camada == "veiculos":
        return [Deteccao.tipo == TipoDeteccao.OBJETO, Deteccao.classe.in_(VEICULOS)]
    if camada == "rostos":
        return [Deteccao.tipo == TipoDeteccao.FACIAL]
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                        f"Camada desconhecida. Use uma de: {', '.join(CAMADAS)}.")


def _periodo(desde: Optional[datetime], ate: Optional[datetime]) -> tuple[datetime, datetime]:
    agora = datetime.now(timezone.utc)
    fim = ate or agora
    inicio = desde or (fim - timedelta(days=7))
    if inicio.tzinfo is None:
        inicio = inicio.astimezone()
    if fim.tzinfo is None:
        fim = fim.astimezone()
    if inicio >= fim:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "O início precisa vir antes do fim.")
    return inicio, fim


def _ponto(bbox, camada: str) -> Optional[tuple[float, float]]:
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except ValueError:
            return None
    if not bbox or len(bbox) < 4:
        return None
    x, y, w, h = (float(v) for v in bbox[:4])
    px = x + w / 2
    py = y + h / 2 if camada == "rostos" else y + h * 0.97
    if not (0.0 <= px <= 1.0 and 0.0 <= py <= 1.0):
        return None
    return px, py


def _vizinhanca(grade: list, r: int) -> list:
    """Soma de cada célula com as vizinhas num raio r (tabela de somas)."""
    L, C = len(grade), len(grade[0])
    acc = [[0] * (C + 1) for _ in range(L + 1)]
    for y in range(L):
        for x in range(C):
            acc[y + 1][x + 1] = grade[y][x] + acc[y][x + 1] + acc[y + 1][x] - acc[y][x]
    out = [[0] * C for _ in range(L)]
    for y in range(L):
        y0, y1 = max(0, y - r), min(L, y + r + 1)
        for x in range(C):
            x0, x1 = max(0, x - r), min(C, x + r + 1)
            out[y][x] = acc[y1][x1] - acc[y0][x1] - acc[y1][x0] + acc[y0][x0]
    return out


def _regiao(col: int, lin: int) -> str:
    """Nome legível da região da imagem onde está a célula."""
    v = "no alto" if lin < LINHAS / 3 else ("embaixo" if lin >= 2 * LINHAS / 3 else "no meio")
    h = "à esquerda" if col < COLUNAS / 3 else ("à direita" if col >= 2 * COLUNAS / 3 else "ao centro")
    return f"{v}, {h}" if not (v == "no meio" and h == "ao centro") else "no centro da imagem"


@router.get("/camadas", summary="Camadas disponíveis")
async def camadas(usuario: Usuario = Depends(exige("live"))):
    return {"camadas": [{"valor": k, "nome": v} for k, v in CAMADAS.items()]}


@router.get("/ranking", summary="Câmeras com mais movimento no período")
async def ranking(camada: str = "pessoas", desde: Optional[datetime] = None,
                  ate: Optional[datetime] = None, db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("live"))):
    inicio, fim = _periodo(desde, ate)
    linhas = (await db.execute(
        select(Camera.codigo, Camera.nome, Camera.setor, func.count(Deteccao.id))
        .join(Deteccao, Deteccao.camera_id == Camera.id)
        .where(*_filtro_camada(camada), Deteccao.ocorrido_em >= inicio, Deteccao.ocorrido_em <= fim)
        .group_by(Camera.codigo, Camera.nome, Camera.setor)
        .order_by(func.count(Deteccao.id).desc()))).all()
    return {"camada": camada, "desde": inicio, "ate": fim,
            "cameras": [{"codigo": c, "nome": n, "setor": s or "", "total": t} for c, n, s, t in linhas]}


@router.get("", summary="Mapa de calor de uma câmera")
async def mapa(camera: str, camada: str = "pessoas", desde: Optional[datetime] = None,
               ate: Optional[datetime] = None, db: AsyncSession = Depends(get_db),
               usuario: Usuario = Depends(exige("live"))):
    """Grade de contagens sobre a imagem, grade semana × hora e os destaques."""
    inicio, fim = _periodo(desde, ate)
    cam = (await db.execute(select(Camera).where(Camera.codigo == camera))).scalars().unique().one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
    linhas = (await db.execute(
        select(Deteccao.bbox, Deteccao.ocorrido_em)
        .where(Deteccao.camera_id == cam.id, *_filtro_camada(camada),
               Deteccao.ocorrido_em >= inicio, Deteccao.ocorrido_em <= fim))).all()

    grade = [[0] * COLUNAS for _ in range(LINHAS)]
    semana = [[0] * 24 for _ in range(7)]
    por_dia: Counter = Counter()
    sem_posicao = 0
    for bbox, quando in linhas:
        if quando is not None:
            local = (quando if quando.tzinfo else quando.replace(tzinfo=timezone.utc)).astimezone()
            semana[local.weekday()][local.hour] += 1
            por_dia[local.date().isoformat()] += 1
        p = _ponto(bbox, camada)
        if p is None:
            sem_posicao += 1
            continue
        col = min(COLUNAS - 1, int(p[0] * COLUNAS))
        lin = min(LINHAS - 1, int(p[1] * LINHAS))
        grade[lin][col] += 1

    total = len(linhas)
    destaques: dict = {"total": total, "sem_posicao": sem_posicao}
    if total:
        d, h = max(((d, h) for d in range(7) for h in range(24)), key=lambda t: semana[t[0]][t[1]])
        horas = [sum(semana[d][hh] for d in range(7)) for hh in range(24)]
        hp = max(range(24), key=lambda hh: horas[hh])
        dias = [sum(semana[dd]) for dd in range(7)]
        dp = max(range(7), key=lambda dd: dias[dd])
        # Pico pela vizinhança (5 × 5), como o mapa suavizado mostra: pela
        # célula sozinha, ganhava o canto onde uma pessoa ficou parada.
        viz = _vizinhanca(grade, 2)
        lin, col = max(((ll, cc) for ll in range(LINHAS) for cc in range(COLUNAS)),
                       key=lambda t: viz[t[0]][t[1]])
        destaques.update({
            "pico_semana": {"dia": DIAS[d], "hora": h, "total": semana[d][h]},
            "hora_pico": {"hora": hp, "total": horas[hp]},
            "dia_pico": {"dia": DIAS[dp], "total": dias[dp]},
            "area_pico": {"coluna": col, "linha": lin, "regiao": _regiao(col, lin)},
            "media_diaria": round(total / max(1, (fim - inicio).total_seconds() / 86400), 1),
        })
    return {
        "camera": {"codigo": cam.codigo, "nome": cam.nome, "setor": cam.setor or ""},
        "camada": camada, "desde": inicio, "ate": fim,
        "colunas": COLUNAS, "linhas": LINHAS, "grade": grade,
        "semana": semana, "dias": DIAS,
        "por_dia": [{"dia": k, "total": v} for k, v in sorted(por_dia.items())],
        "destaques": destaques,
    }
