"""Gravação de uma passagem de veículo.

Duas linhas, uma em cada lugar:

* em `deteccoes`, pelo caminho único de entrada (`processar_deteccao`): é o
  que mantém o veículo na lista de objetos, nos alertas, na retenção e na
  auditoria, como sempre esteve;
* em `passagens_veiculo`, os atributos do módulo veicular — cada um com a
  sua confiança e, quando vazio, com o motivo.

Imagens: o recorte do veículo (o mesmo da lista de objetos), o quadro
inteiro e o recorte da placa, todos em `evidencias/`.
"""
from __future__ import annotations

import base64
import logging

from app.services import evidencias

log = logging.getLogger("seguranca.veiculos")


def _jpeg(img, qualidade: int = 88) -> bytes | None:
    import cv2
    if img is None or img.size == 0:
        return None
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), qualidade])
    return buf.tobytes() if ok else None


async def gravar(r, res) -> dict:
    """`r`: Retrato da passagem; `res`: identificacao.Resultado."""
    from sqlalchemy import select

    from app.database import db_context
    from app.models import Camera, Deteccao, PassagemVeiculo, TipoDeteccao
    from app.routers.deteccoes import processar_deteccao
    from app.schemas import DeteccaoIn
    from app.services import video

    alt, larg = r.quadro.shape[:2]
    x1, y1, x2, y2 = r.bbox
    bbox = [x1 / larg, y1 / alt, (x2 - x1) / larg, (y2 - y1) / alt]
    recorte = video.recortar_bgr(r.quadro, r.bbox)          # mesma moldura da lista de objetos
    jpeg_placa = video.jpeg_ampliado(res.placa_recorte, 360) if res.placa_recorte is not None else None

    from app.services.veiculos.identificacao import CLASSE_GERAL
    tipo = res.tipo or r.categoria
    tipo_conf = res.tipo_conf if res.tipo else r.categoria_conf

    async with db_context() as db:
        info = await processar_deteccao(db, DeteccaoIn(
            tipo=TipoDeteccao.OBJETO, camera_codigo=r.camera, ocorrido_em=r.inicio,
            classe=CLASSE_GERAL.get(tipo, "carro"), confianca=round(float(r.conf), 4), bbox=bbox,
            track_id=r.trilha, placa=res.placa,
            duracao_s=max(1, int((r.fim - r.inicio).total_seconds())),
            snapshot_jpeg=base64.b64encode(recorte).decode("ascii") if recorte else None,
            placa_jpeg=base64.b64encode(jpeg_placa).decode("ascii") if jpeg_placa else None,
        ), origem="veiculos")
        det = (await db.execute(select(Deteccao).where(Deteccao.codigo == info["codigo"]))
               ).scalars().unique().one()
        cam = (await db.execute(select(Camera).where(Camera.codigo == r.camera))
               ).scalars().unique().one()
        quadro_key = ""
        jq = _jpeg(r.quadro, 82)
        if jq:
            quadro_key = evidencias.gravar(f"{det.codigo}-quadro", r.inicio, jq)
        db.add(PassagemVeiculo(
            deteccao_id=det.id, camera_id=cam.id, trilha=r.trilha,
            inicio=r.inicio, fim=r.fim, quadros=r.quadros,
            categoria=tipo, categoria_conf=tipo_conf or 0.0,
            cor=res.cor, cor_conf=res.cor_conf,
            marca=res.marca, modelo=res.modelo, marca_modelo_conf=res.marca_modelo_conf,
            placa=res.placa, placa_conf=res.placa_conf,
            observacoes=res.observacoes or None, bbox=bbox,
            recorte_key=det.snapshot_key, quadro_key=quadro_key,
            placa_key=det.placa_snapshot_key, vetor=res.vetor,
        ))
    return info
