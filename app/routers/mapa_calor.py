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

COLUNAS, LINHAS = 160, 90         # 16:9, ~12 px por célula numa tela de 1920
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
    # Por hora do dia, esparso ({índice da célula: contagem}): é o que a linha
    # do tempo da tela anima, sem mandar 24 grades cheias.
    por_hora: list = [Counter() for _ in range(24)]
    semana = [[0] * 24 for _ in range(7)]
    por_dia: Counter = Counter()
    sem_posicao = 0
    for bbox, quando in linhas:
        hora = None
        if quando is not None:
            local = (quando if quando.tzinfo else quando.replace(tzinfo=timezone.utc)).astimezone()
            semana[local.weekday()][local.hour] += 1
            por_dia[local.date().isoformat()] += 1
            hora = local.hour
        p = _ponto(bbox, camada)
        if p is None:
            sem_posicao += 1
            continue
        col = min(COLUNAS - 1, int(p[0] * COLUNAS))
        lin = min(LINHAS - 1, int(p[1] * LINHAS))
        grade[lin][col] += 1
        if hora is not None:
            por_hora[hora][lin * COLUNAS + col] += 1

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
        viz = _vizinhanca(grade, 3)
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
        "por_hora": [[[i, n] for i, n in h.items()] for h in por_hora],
        "ocupacao": round(sum(1 for l in grade for v in l if v) / (COLUNAS * LINHAS), 4),
        "por_dia": [{"dia": k, "total": v} for k, v in sorted(por_dia.items())],
        "destaques": destaques,
    }


@router.get("/analise", summary="Análise histórica de uma câmera: ocupação, passagens, aglomerações e comparativo")
async def analise(camera: str, desde: Optional[datetime] = None, ate: Optional[datetime] = None,
                  base: str = Query("auto", pattern="^(auto|ocupacao|passagens)$"),
                  comparar: bool = True, db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("live"))):
    """O mapa de calor de um período, com o que ele significa.

    `base=ocupacao` pinta o tempo de presença (onde as pessoas ficam);
    `base=passagens` pinta o ponto em que cada passagem registrada apareceu.
    `auto` usa a ocupação quando o período tem dados dela. Com `comparar`, o
    mesmo intervalo imediatamente anterior volta junto, para a variação."""
    from app.config import settings
    from app.models import OcupacaoHora
    from app.services import ocupacao_analise as oa

    inicio, fim = _periodo(desde, ate)
    if fim - inicio > timedelta(days=400):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Período máximo: 400 dias.")
    cam = (await db.execute(select(Camera).where(Camera.codigo == camera))).scalars().unique().one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
    grao = oa.granularidade(inicio, fim)
    atual = await oa.coletar(db, cam, inicio, fim, grao)
    usar = base if base != "auto" else ("ocupacao" if atual["kpis"]["presenca_s"] > 0 else "passagens")
    G = atual["G"][usar]
    limite = cam.limite_aglomeracao or settings.ACAO_AGLOMERACAO_N
    desde_dados = (await db.execute(select(func.min(OcupacaoHora.inicio))
                                    .where(OcupacaoHora.camera_id == cam.id))).scalar_one_or_none()

    saida = {
        "camera": {"codigo": cam.codigo, "nome": cam.nome, "setor": cam.setor or ""},
        "desde": inicio, "ate": fim, "granularidade": grao, "base": usar,
        "ocupacao_desde": desde_dados,
        "grade": {"colunas": oa.COLUNAS, "linhas": oa.LINHAS, "valores": oa.compactar(G)},
        "grade_grupos": oa.compactar(atual["G"]["grupos"]),
        "kpis": atual["kpis"], "serie": atual["serie"],
        "semana": atual["semana"] if usar == "ocupacao" else atual["semana_passagens"],
        "dias": oa.DIAS,
        "zonas": oa.zonas(G), "zonas_dim": [oa.ZONAS_L, oa.ZONAS_C],
        "focos": oa.focos(G),
        "focos_grupo": oa.focos(atual["G"]["grupos"], 3, 0.08),
        "aglomeracao": {
            "limite": limite, "grupo_min": 3,
            "acima_do_limite": [{"pessoas": n, "em": em, "duracao_s": round(s)}
                                for n, em, s in atual["grupos_hora"] if n >= limite][:20],
            "maiores": [{"pessoas": n, "em": em, "duracao_s": round(s)} for n, em, s in atual["grupos_hora"][:8]],
        },
    }
    if comparar:
        dur = fim - inicio
        ant = await oa.coletar(db, cam, inicio - dur, inicio, grao)
        Ga = ant["G"][usar]
        saida["anterior"] = {"desde": inicio - dur, "ate": inicio, "kpis": ant["kpis"], "serie": ant["serie"],
                             "zonas": oa.zonas(Ga) if Ga.sum() > 0 else None}
    return saida


@router.get("/cameras", summary="Comparativo entre câmeras no período")
async def comparativo_cameras(desde: Optional[datetime] = None, ate: Optional[datetime] = None,
                              db: AsyncSession = Depends(get_db),
                              usuario: Usuario = Depends(exige("live"))):
    """Uma linha por câmera: passagens, tempo de presença, pico e maior grupo,
    com a variação de passagens sobre o período anterior de mesma duração."""
    from app.models import OcupacaoHora

    inicio, fim = _periodo(desde, ate)
    dur = fim - inicio

    async def passagens(a, b):
        return dict((await db.execute(
            select(Deteccao.camera_id, func.count(Deteccao.id))
            .where(*_filtro_camada("pessoas"), Deteccao.ocorrido_em >= a, Deteccao.ocorrido_em < b)
            .group_by(Deteccao.camera_id))).all())

    agora_p, antes_p = await passagens(inicio, fim), await passagens(inicio - dur, inicio)
    ocup = {cid: (pres, obs, pico, grupo) for cid, pres, obs, pico, grupo in (await db.execute(
        select(OcupacaoHora.camera_id, func.sum(OcupacaoHora.presenca_s), func.sum(OcupacaoHora.observado_s),
               func.max(OcupacaoHora.pico), func.max(OcupacaoHora.maior_grupo))
        .where(OcupacaoHora.inicio >= inicio - timedelta(hours=1), OcupacaoHora.inicio < fim)
        .group_by(OcupacaoHora.camera_id))).all()}
    cams = (await db.execute(select(Camera).order_by(Camera.codigo))).scalars().unique().all()
    linhas = []
    for c in cams:
        pres, obs, pico, grupo = ocup.get(c.id, (0, 0, 0, 0))
        p, pa = agora_p.get(c.id, 0), antes_p.get(c.id, 0)
        if not (p or pa or pres):
            continue
        linhas.append({"codigo": c.codigo, "nome": c.nome, "setor": c.setor or "",
                       "passagens": p, "passagens_antes": pa,
                       "variacao": round((p - pa) / pa, 4) if pa else None,
                       "presenca_s": round(float(pres or 0), 1),
                       "ocupacao_media": round(float(pres) / float(obs), 2) if obs else None,
                       "pico": int(pico or 0), "maior_grupo": int(grupo or 0)})
    linhas.sort(key=lambda l: (-l["presenca_s"], -l["passagens"]))
    return {"desde": inicio, "ate": fim, "cameras": linhas}


# Veículos que a visão térmica aquece, além de pessoa.
_TERMICO_CLASSES = {"pessoa", "carro", "motocicleta", "caminhao", "onibus", "bicicleta", "picape", "van",
                    "kombi", "caminhao_pequeno", "caminhao_grande", "veiculo"}


@router.get("/pessoas/{codigo}", summary="Pessoas e veículos no quadro atual, para a visão térmica")
async def pessoas_agora(codigo: str, varredura: bool = Query(False),
                        db: AsyncSession = Depends(get_db),
                        usuario: Usuario = Depends(exige("live"))):
    """Detecção na hora, só da câmera aberta na visão térmica.

    As caixas publicadas pela análise geral chegam espaçadas e com o limiar
    de registro; quem está sentado, meio encoberto ou ao fundo some da imagem
    térmica. Aqui o quadro atual passa pelo detector com limiar mais baixo e,
    a cada poucas chamadas (`varredura=true`), também pela varredura em
    ladrilhos, que é o que acha quem está pequeno ao fundo. Nada é gravado:
    a resposta só pinta a tela. Sem rastreio — o ByteTrack é do laço geral, e
    dividi-lo bagunçaria as identidades de lá.
    """
    import asyncio

    from app.services import video, vision

    cam = (await db.execute(select(Camera).where(Camera.codigo == codigo))).scalars().unique().one_or_none()
    if cam is None or not cam.rtsp_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")

    def rodar():
        quadro = video.quadro_bgr(cam.codigo, cam.rtsp_url, True)
        if quadro is None:
            return None
        h, w = quadro.shape[:2]
        objs = vision.detectar_objetos(quadro, 0.22, False, set(), "sempre" if varredura else "nunca",
                                       urgente=True)
        saida = []
        for o in objs:
            if o.classe not in _TERMICO_CLASSES:
                continue
            x1, y1, x2, y2 = o.bbox
            saida.append({"classe": o.classe, "confianca": round(float(o.confianca), 3),
                          "bbox": [x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h],
                          "varredura": bool(o.de_varredura)})
        return saida

    caixas = await asyncio.to_thread(rodar)
    if caixas is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Câmera sem imagem no momento.")
    return {"camera": cam.codigo, "varredura": varredura, "caixas": caixas,
            "em": datetime.now(timezone.utc)}
