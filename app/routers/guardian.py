"""Aizon Guardian: ocorrências, evidências, avaliação humana e estatísticas.

Acesso pelo perfil de quem trata alertas (`alerts`); avaliar exige também
`edit`. Toda abertura de ocorrência, todo vídeo assistido e toda avaliação
entram na trilha de auditoria — são imagens de pessoas em possível situação
de risco, o material mais sensível do sistema (LGPD, art. 11).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige, usuario_de_query
from app.models import Camera, OcorrenciaGuardian, Usuario
from app.services import evidencias
from app.services.guardian.correlacao import ABERTAS, PRIORIDADES, SINAIS, STATUS

router = APIRouter(prefix="/guardian", tags=["Aizon Guardian"])

ORDEM = {p: i for i, p in enumerate(PRIORIDADES)}
ROTULO_STATUS = {"NOVA": "Nova", "EM_ANALISE": "Em análise", "RISCO_CONFIRMADO": "Risco confirmado",
                 "FALSO_ALARME": "Falso alarme", "ENCERRADA": "Encerrada"}


def _utc(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _saida(o: OcorrenciaGuardian, completo: bool = False) -> dict:
    d = {
        "codigo": o.codigo, "prioridade": o.prioridade, "pontuacao": o.pontuacao,
        "confianca": o.confianca, "status": o.status, "status_rotulo": ROTULO_STATUS.get(o.status, o.status),
        "camera": {"codigo": o.camera.codigo, "nome": o.camera.nome, "setor": o.camera.setor or ""}
        if o.camera else None,
        "inicio": _utc(o.inicio), "ultimo_sinal": _utc(o.ultimo_sinal),
        "sinais_resumo": sorted({s["rotulo"] for s in (o.sinais or [])}),
        "n_sinais": len(o.sinais or []),
        "tem_imagem": bool(o.imagem_key), "tem_clipe": bool(o.clipe_key),
        "alerta": o.alerta_codigo, "avaliado_em": _utc(o.avaliado_em),
    }
    if completo:
        d["sinais"] = o.sinais or []
        d["historico"] = o.historico or []
    return d


async def _ocorrencia(db: AsyncSession, codigo: str) -> OcorrenciaGuardian:
    o = (await db.execute(select(OcorrenciaGuardian).where(OcorrenciaGuardian.codigo == codigo))
         ).scalars().unique().one_or_none()
    if o is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ocorrência não encontrada.")
    return o


@router.get("/estado", summary="Câmeras do Guardian, cobertura do gesto e ocorrências abertas")
async def estado(db: AsyncSession = Depends(get_db), usuario: Usuario = Depends(exige("alerts"))):
    from app.services.guardian.servico import servico
    cams = (await db.execute(select(Camera).where(Camera.guardian.is_(True)).order_by(Camera.codigo))
            ).scalars().unique().all()
    abertas = (await db.execute(select(OcorrenciaGuardian.prioridade, func.count())
                                .where(OcorrenciaGuardian.status.in_(ABERTAS))
                                .group_by(OcorrenciaGuardian.prioridade))).all()
    hoje = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    n_hoje = (await db.execute(select(func.count()).select_from(OcorrenciaGuardian)
                               .where(OcorrenciaGuardian.inicio >= hoje))).scalar_one()
    resumo = servico.resumo()
    return {
        "servico": resumo,
        "cameras": [{"codigo": c.codigo, "nome": c.nome, "setor": c.setor or "",
                     "ao_vivo": resumo["cameras"].get(c.codigo)} for c in cams],
        "abertas": {p: 0 for p in PRIORIDADES} | {p: n for p, n in abertas},
        "hoje": n_hoje,
        "sinais": [{"tipo": k, "rotulo": v[0]} for k, v in SINAIS.items()],
    }


@router.get("/ocorrencias", summary="Ocorrências, das mais graves e recentes")
async def listar(status_: Optional[str] = Query(None, alias="status"), prioridade: Optional[str] = None,
                 camera: Optional[str] = None, abertas: bool = False,
                 desde: Optional[datetime] = None, ate: Optional[datetime] = None,
                 pagina: int = Query(1, ge=1), por_pagina: int = Query(25, ge=1, le=200),
                 db: AsyncSession = Depends(get_db), usuario: Usuario = Depends(exige("alerts"))):
    filtros = []
    if status_:
        filtros.append(OcorrenciaGuardian.status == status_)
    if abertas:
        filtros.append(OcorrenciaGuardian.status.in_(ABERTAS))
    if prioridade:
        filtros.append(OcorrenciaGuardian.prioridade == prioridade)
    if camera:
        filtros.append(Camera.codigo == camera)
    if desde:
        filtros.append(OcorrenciaGuardian.inicio >= desde)
    if ate:
        filtros.append(OcorrenciaGuardian.inicio <= ate)
    base = (select(OcorrenciaGuardian).join(Camera, Camera.id == OcorrenciaGuardian.camera_id).where(*filtros))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    linhas = (await db.execute(base.order_by(OcorrenciaGuardian.ultimo_sinal.desc())
                               .offset((pagina - 1) * por_pagina).limit(por_pagina))).scalars().unique().all()
    itens = [_saida(o) for o in linhas]
    if abertas:
        itens.sort(key=lambda x: (ORDEM.get(x["prioridade"], 9), -x["ultimo_sinal"].timestamp()))
    return {"total": total, "pagina": pagina, "por_pagina": por_pagina, "itens": itens}


@router.get("/ocorrencias/{codigo}", summary="Detalhe de uma ocorrência")
async def obter(codigo: str, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("alerts"))):
    o = await _ocorrencia(db, codigo)
    await audit.registrar(db, acao="GUARDIAN_CONSULTA", descricao=f"Ocorrência {codigo} aberta",
                          usuario=usuario, alvo=codigo, request=request,
                          finalidade="Avaliação de possível situação de risco")
    return _saida(o, completo=True)


@router.get("/ocorrencias/{codigo}/imagem", summary="Imagem do sinal mais grave (JPEG)")
async def imagem(codigo: str, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    await usuario_de_query(token, db, "alerts")
    o = await _ocorrencia(db, codigo)
    dados = evidencias.ler(o.imagem_key) if o.imagem_key else None
    if dados is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Imagem não guardada.")
    return Response(content=dados, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})


@router.get("/ocorrencias/{codigo}/clipe", summary="Vídeo de evidência (MJPEG, decifrado na hora)")
async def clipe(codigo: str, request: Request, token: str = Query(...),
                db: AsyncSession = Depends(get_db)):
    from app.services.guardian import evidencia
    usuario = await usuario_de_query(token, db, "alerts")
    o = await _ocorrencia(db, codigo)
    if not o.clipe_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Esta ocorrência não tem vídeo.")
    quadros = evidencia.ler_clipe(o.clipe_key)
    if not quadros:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vídeo não está mais em disco.")
    await audit.registrar(db, acao="GUARDIAN_VIDEO", descricao=f"Vídeo da ocorrência {codigo} assistido",
                          usuario=usuario, alvo=codigo, request=request,
                          finalidade="Avaliação de possível situação de risco")
    await db.commit()
    return StreamingResponse(evidencia.tocar(quadros), media_type="multipart/x-mixed-replace; boundary=quadro",
                             headers={"Cache-Control": "no-store"})


class AvaliacaoIn(BaseModel):
    status: str
    nota: str = Field(..., min_length=10, max_length=2000)


@router.post("/ocorrencias/{codigo}/avaliacao", summary="Avaliação humana da ocorrência")
async def avaliar(codigo: str, dados: AvaliacaoIn, request: Request, db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("alerts", "edit"))):
    """Quem decide é a pessoa: a IA só abriu a ocorrência. A justificativa é
    obrigatória — "falso alarme" sem motivo não ensina nada a ninguém."""
    if dados.status not in STATUS or dados.status == "NOVA":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Use EM_ANALISE, RISCO_CONFIRMADO, FALSO_ALARME ou ENCERRADA.")
    o = await _ocorrencia(db, codigo)
    anterior = o.status
    o.status = dados.status
    o.avaliado_por, o.avaliado_em = usuario.id, datetime.now(timezone.utc)
    hist = list(o.historico or [])
    hist.append({"em": o.avaliado_em.isoformat(), "por": usuario.nome, "evento": "avaliacao",
                 "status": dados.status, "texto": dados.nota.strip()})
    o.historico = hist
    await db.flush()
    await audit.registrar(db, acao="GUARDIAN_AVALIACAO", severidade="warn",
                          descricao=f"Ocorrência {codigo}: {ROTULO_STATUS[anterior]} → {ROTULO_STATUS[dados.status]}",
                          usuario=usuario, alvo=codigo, request=request,
                          finalidade=dados.nota.strip()[:200],
                          metadados={"de": anterior, "para": dados.status, "prioridade": o.prioridade})
    return _saida(o, completo=True)


@router.get("/estatisticas", summary="Indicadores do período")
async def estatisticas(dias: int = Query(30, ge=1, le=365), db: AsyncSession = Depends(get_db),
                       usuario: Usuario = Depends(exige("alerts"))):
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    ocs = (await db.execute(select(OcorrenciaGuardian).where(OcorrenciaGuardian.inicio >= desde))
           ).scalars().unique().all()
    por_prio = {p: 0 for p in PRIORIDADES}
    por_status = {s: 0 for s in STATUS}
    por_sinal = {k: 0 for k in SINAIS}
    por_camera: dict = {}
    por_dia: dict = {}
    tempos = []
    for o in ocs:
        por_prio[o.prioridade] = por_prio.get(o.prioridade, 0) + 1
        por_status[o.status] = por_status.get(o.status, 0) + 1
        for s in o.sinais or []:
            por_sinal[s["tipo"]] = por_sinal.get(s["tipo"], 0) + 1
        nome = o.camera.nome if o.camera else "?"
        por_camera[nome] = por_camera.get(nome, 0) + 1
        dia = _utc(o.inicio).astimezone().date().isoformat()
        por_dia[dia] = por_dia.get(dia, 0) + 1
        if o.avaliado_em:
            tempos.append((_utc(o.avaliado_em) - _utc(o.inicio)).total_seconds())
    avaliadas = por_status["RISCO_CONFIRMADO"] + por_status["FALSO_ALARME"]
    return {
        "dias": dias, "total": len(ocs), "por_prioridade": por_prio, "por_status": por_status,
        "por_sinal": [{"tipo": k, "rotulo": SINAIS[k][0], "total": v} for k, v in por_sinal.items()],
        "por_camera": sorted(({"camera": k, "total": v} for k, v in por_camera.items()), key=lambda x: -x["total"]),
        "por_dia": [{"dia": k, "total": v} for k, v in sorted(por_dia.items())],
        "taxa_falso_alarme": round(por_status["FALSO_ALARME"] / avaliadas, 3) if avaliadas else None,
        "tempo_medio_avaliacao_s": round(sum(tempos) / len(tempos)) if tempos else None,
    }
