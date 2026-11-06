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


@router.get("/camera/{codigo}", summary="Analisar agora o EPI das pessoas na câmera")
async def analisar_camera(codigo: str, exigidos: str = Query(""),
                          db: AsyncSession = Depends(get_db),
                          usuario: Usuario = Depends(exige("live"))):
    """O quadro atual da câmera, analisado na hora — é o que a tela mostra.

    Só roda enquanto alguém está com a tela aberta, e só nas pessoas mais
    próximas (até 6): EPI de quem está ao fundo, com poucos pixels, não se
    afirma de um jeito nem de outro.
    """
    from app.services import video

    if not epi.disponivel():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Detector de EPI não instalado.")
    cam = (await db.execute(select(Camera).where(Camera.codigo == codigo))).scalars().unique().one_or_none()
    if cam is None or not cam.rtsp_url:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
    lista = ([e.strip() for e in exigidos.split(",") if e.strip() in epi.ITENS]
             or list(cam.epi_obrigatorios or []) or ["capacete", "colete", "oculos", "luvas"])

    def rodar():
        quadro = video.quadro_bgr(cam.codigo, cam.rtsp_url, True)
        if quadro is None:
            return None
        pessoas = [o for o in vision.detectar_objetos(quadro, 0.45, False, set(), "nunca", urgente=True)
                   if o.classe == "pessoa"]
        pessoas.sort(key=lambda o: -(o.bbox[2] - o.bbox[0]) * (o.bbox[3] - o.bbox[1]))
        return quadro.shape, epi.analisar_quadro(
            quadro, [(o.bbox, o.confianca, i) for i, o in enumerate(pessoas[:6])], lista)

    r = await asyncio.to_thread(rodar)
    if r is None:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Câmera sem imagem no momento.")
    forma, analises = r
    return {"camera": cam.codigo, "largura": forma[1], "altura": forma[0],
            "exigidos": lista, "pessoas": analises,
            "em": datetime.now(timezone.utc)}
