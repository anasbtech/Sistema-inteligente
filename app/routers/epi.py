"""Conformidade de EPI: estado ao vivo, números do dia e análise de imagem."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import exige
from app.models import Alerta, Camera, Deteccao, Usuario
from app.services import epi, vision

router = APIRouter(prefix="/epi", tags=["Conformidade de EPI"])


@router.get("/itens", summary="Itens de EPI que o sistema conhece")
async def itens(usuario: Usuario = Depends(exige("live"))):
    return {"itens": [{"codigo": k, "rotulo": v, "detector": k not in epi.SEM_DETECTOR}
                      for k, v in epi.ITENS.items()],
            "modelo_disponivel": epi.disponivel()}


@router.get("/estado", summary="Última análise de EPI de cada câmera, e os números do dia")
async def estado(db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("live"))):
    from app.services.inferencia import inferencia

    cams = (await db.execute(select(Camera).order_by(Camera.codigo))).scalars().unique().all()
    com_epi = [c for c in cams if c.epi_obrigatorios]
    hoje = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    alertas = (await db.execute(
        select(func.count()).select_from(Alerta).join(Deteccao, Deteccao.id == Alerta.deteccao_id)
        .where(Deteccao.classe == "epi_ausente", Alerta.disparado_em >= hoje))).scalar_one()
    dia = inferencia.epi_dia if inferencia.epi_dia.get("dia") == hoje.date().isoformat() else {}
    analisadas = dia.get("analisadas", 0)
    return {
        "modelo_disponivel": epi.disponivel(),
        "cameras": [{
            "codigo": c.codigo, "nome": c.nome, "setor": c.setor,
            "exigidos": c.epi_obrigatorios,
            "ultima": inferencia.epi_estado.get(c.codigo),
        } for c in com_epi],
        "dia": {
            "analisadas": analisadas,
            "conforme": dia.get("conforme", 0),
            "fora": dia.get("fora", 0),
            "inconclusivo": dia.get("inconclusivo", 0),
            "conformidade": round(dia.get("conforme", 0) / analisadas, 3) if analisadas else None,
            "faltando": dia.get("faltando", {}),
            "alertas": alertas,
        },
    }


@router.post("/analisar", summary="Analisar o EPI das pessoas numa imagem enviada")
async def analisar(imagem: UploadFile = File(...),
                   exigidos: str = Query("capacete,colete,oculos,luvas"),
                   usuario: Usuario = Depends(exige("live"))):
    import cv2

    if not epi.disponivel():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Detector de EPI não instalado.")
    dados = await imagem.read()
    if len(dados) > 15 * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Imagem acima de 15 MB.")
    quadro = cv2.imdecode(np.frombuffer(dados, np.uint8), cv2.IMREAD_COLOR)
    if quadro is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Arquivo não é uma imagem.")
    lista = [e.strip() for e in exigidos.split(",") if e.strip() in epi.ITENS]

    def rodar():
        pessoas = [o for o in vision.detectar_objetos(quadro, 0.40, False, set(), "nunca")
                   if o.classe == "pessoa"]
        return epi.analisar_quadro(quadro, [(o.bbox, o.confianca, i) for i, o in enumerate(pessoas)],
                                   lista)

    analises = await asyncio.to_thread(rodar)
    return {"largura": quadro.shape[1], "altura": quadro.shape[0],
            "exigidos": lista, "pessoas": analises}
