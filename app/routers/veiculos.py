"""Inteligência veicular: estado ao vivo, histórico de passagens e rotulagem."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige, usuario_de_query
from app.models import Camera, Deteccao, PassagemVeiculo, RotuloVeiculo, Usuario
from app.services import evidencias

router = APIRouter(prefix="/veiculos", tags=["Inteligência veicular"])

CORES = ["branco", "prata/cinza", "preto", "vermelho", "azul", "verde", "amarelo",
         "bege", "marrom", "laranja"]


def _saida(p: PassagemVeiculo, codigo_det: str = "", rotulo: Optional[RotuloVeiculo] = None) -> dict:
    return {
        "id": str(p.id), "deteccao": codigo_det, "trilha": p.trilha,
        "camera": {"codigo": p.camera.codigo, "nome": p.camera.nome} if p.camera else None,
        "inicio": p.inicio, "fim": p.fim, "quadros": p.quadros,
        "categoria": p.categoria, "categoria_conf": p.categoria_conf,
        "cor": p.cor, "cor_conf": p.cor_conf,
        "marca": p.marca, "modelo": p.modelo, "marca_modelo_conf": p.marca_modelo_conf,
        "ano": p.ano, "placa": p.placa, "placa_conf": p.placa_conf,
        "observacoes": p.observacoes or {}, "bbox": p.bbox,
        "tem_quadro": bool(p.quadro_key), "tem_placa": bool(p.placa_key),
        "rotulo": ({"tipo": rotulo.tipo or "", "cor": rotulo.cor, "marca": rotulo.marca,
                    "modelo": rotulo.modelo} if rotulo else None),
    }


@router.get("/estado", summary="Inteligência veicular: motor, câmeras e modelos treinados")
async def estado(db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("objects"))):
    from app.services.veiculos import identificacao
    from app.services.veiculos.servico import servico

    cams = (await db.execute(select(Camera).where(Camera.detecta_veiculo.is_(True))
                             .order_by(Camera.codigo))).scalars().unique().all()
    hoje = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    total_hoje = (await db.execute(select(func.count()).select_from(PassagemVeiculo)
                                   .where(PassagemVeiculo.inicio >= hoje))).scalar_one()
    rotulados = (await db.execute(select(func.count()).select_from(RotuloVeiculo))).scalar_one()
    return {
        "servico": servico.resumo(),
        "modelos": identificacao.estado(),
        "cameras": [{"codigo": c.codigo, "nome": c.nome,
                     "inteligencia_veicular": bool(c.inteligencia_veicular),
                     "ao_vivo": servico.estado_cameras.get(c.codigo)} for c in cams],
        "passagens_hoje": total_hoje,
        "rotulados": rotulados,
    }


@router.get("/catalogo", summary="Cores e modelos que o sistema sabe nomear")
async def catalogo(usuario: Usuario = Depends(exige("objects"))):
    from app.services.veiculos.catalogo_br import MODELOS
    from app.services.veiculos.identificacao import TIPOS
    return {"cores": CORES, "tipos": [{"valor": k, "nome": v} for k, v in TIPOS.items()],
            "modelos": [{"marca": m, "modelo": n, "categoria": c} for m, n, c in MODELOS]}


@router.get("/passagens", summary="Histórico de passagens de veículo")
async def listar(camera: Optional[str] = None, categoria: Optional[str] = None,
                 cor: Optional[str] = None, marca: Optional[str] = None,
                 placa: Optional[str] = None, desde: Optional[datetime] = None,
                 ate: Optional[datetime] = None,
                 rotulagem: bool = Query(False, description="Só as ainda sem rótulo humano"),
                 pagina: int = Query(1, ge=1), por_pagina: int = Query(25, ge=1, le=200),
                 db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("objects"))):
    filtros = []
    if camera:
        filtros.append(Camera.codigo == camera)
    # O que uma pessoa conferiu vale mais que o palpite da IA, também no
    # filtro: a Ducato rotulada "van" aparece em "Van", não em "Carro".
    if categoria:
        filtros.append(func.coalesce(func.nullif(RotuloVeiculo.tipo, ""),
                                     PassagemVeiculo.categoria) == categoria)
    if cor:
        filtros.append(func.coalesce(func.nullif(RotuloVeiculo.cor, ""),
                                     PassagemVeiculo.cor) == cor)
    if marca:
        filtros.append(PassagemVeiculo.marca == marca)
    if placa:
        filtros.append(PassagemVeiculo.placa.like(f"%{placa.upper().replace('-', '')}%"))
    if desde:
        filtros.append(PassagemVeiculo.inicio >= desde)
    if ate:
        filtros.append(PassagemVeiculo.inicio <= ate)
    if rotulagem:
        filtros.append(~PassagemVeiculo.id.in_(select(RotuloVeiculo.passagem_id)))
        filtros.append(PassagemVeiculo.recorte_key != "")
    base = (select(PassagemVeiculo, Deteccao.codigo, RotuloVeiculo)
            .join(Camera, Camera.id == PassagemVeiculo.camera_id)
            .outerjoin(Deteccao, Deteccao.id == PassagemVeiculo.deteccao_id)
            .outerjoin(RotuloVeiculo, RotuloVeiculo.passagem_id == PassagemVeiculo.id)
            .where(*filtros))
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    linhas = (await db.execute(base.order_by(PassagemVeiculo.inicio.desc())
                               .offset((pagina - 1) * por_pagina).limit(por_pagina))).unique().all()
    return {"total": total, "pagina": pagina, "por_pagina": por_pagina,
            "itens": [_saida(p, cod or "", rot) for p, cod, rot in linhas]}


async def _passagem(db: AsyncSession, pid: str) -> PassagemVeiculo:
    try:
        u = uuid.UUID(pid)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Passagem não encontrada.")
    p = (await db.execute(select(PassagemVeiculo).where(PassagemVeiculo.id == u))
         ).scalars().unique().one_or_none()
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Passagem não encontrada.")
    return p


@router.get("/passagens/{pid}", summary="Detalhe de uma passagem")
async def obter(pid: str, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("objects"))):
    p = await _passagem(db, pid)
    det = await db.get(Deteccao, p.deteccao_id) if p.deteccao_id else None
    rot = (await db.execute(select(RotuloVeiculo).where(RotuloVeiculo.passagem_id == p.id))
           ).scalars().first()
    return _saida(p, det.codigo if det else "", rot)


@router.get("/passagens/{pid}/imagem/{tipo}", summary="Recorte, quadro inteiro ou placa (JPEG)")
async def imagem(pid: str, tipo: str, token: str = Query(...),
                 db: AsyncSession = Depends(get_db)):
    await usuario_de_query(token, db, "objects")
    p = await _passagem(db, pid)
    chave = {"recorte": p.recorte_key, "quadro": p.quadro_key, "placa": p.placa_key}.get(tipo)
    if chave is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Tipo de imagem desconhecido.")
    dados = evidencias.ler(chave) if chave else None
    if dados is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Imagem não guardada (ou já expurgada).")
    return Response(content=dados, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


class RotuloIn(BaseModel):
    tipo: str = Field("", max_length=20)
    cor: str = Field("", max_length=20)
    marca: str = Field("", max_length=40)
    modelo: str = Field("", max_length=60)


@router.post("/passagens/{pid}/rotulo", summary="Dizer o que o veículo é (treino e medição)")
async def rotular(pid: str, dados: RotuloIn, request: Request,
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("objects", "edit"))):
    """O rótulo não muda a passagem: alimenta o treino e a medição das sondas.
    Campo vazio é "não sei" — melhor do que um chute, que ensinaria errado."""
    from app.services.veiculos.identificacao import TIPOS_ROTULO
    p = await _passagem(db, pid)
    if dados.tipo and dados.tipo not in TIPOS_ROTULO:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Tipo desconhecido. Use um de: {', '.join(TIPOS_ROTULO)}.")
    if dados.cor and dados.cor not in CORES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Cor desconhecida. Use uma de: {', '.join(CORES)}.")
    if bool(dados.marca) != bool(dados.modelo):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Marca e modelo vão juntos (ou os dois em branco).")
    rot = (await db.execute(select(RotuloVeiculo).where(RotuloVeiculo.passagem_id == p.id))
           ).scalars().first()
    if rot is None:
        rot = RotuloVeiculo(passagem_id=p.id)
        db.add(rot)
    rot.tipo = dados.tipo
    rot.cor, rot.marca, rot.modelo = dados.cor, dados.marca.strip(), dados.modelo.strip()
    rot.usuario_id = usuario.id
    await db.flush()
    await audit.registrar(db, acao="VEICULO_ROTULO", descricao="Veículo rotulado para treino",
                          usuario=usuario, alvo=str(p.id), request=request,
                          metadados={"tipo": rot.tipo, "cor": rot.cor, "marca": rot.marca,
                                     "modelo": rot.modelo})
    return {"tipo": rot.tipo, "cor": rot.cor, "marca": rot.marca, "modelo": rot.modelo}


@router.delete("/passagens/{pid}/rotulo", summary="Excluir o rótulo de uma passagem")
async def excluir_rotulo(pid: str, request: Request, db: AsyncSession = Depends(get_db),
                         usuario: Usuario = Depends(exige("objects", "edit"))):
    """Rótulo dado por engano sai daqui; a passagem volta à fila de rotulagem."""
    p = await _passagem(db, pid)
    rot = (await db.execute(select(RotuloVeiculo).where(RotuloVeiculo.passagem_id == p.id))
           ).scalars().first()
    if rot is None:
        return {"excluido": False}
    antes = {"tipo": rot.tipo, "cor": rot.cor, "marca": rot.marca, "modelo": rot.modelo}
    await db.delete(rot)
    await db.flush()
    await audit.registrar(db, acao="VEICULO_ROTULO_EXCLUIDO", descricao="Rótulo de veículo excluído",
                          usuario=usuario, alvo=str(p.id), request=request, metadados=antes)
    return {"excluido": True}


@router.delete("/passagens/{pid}", summary="Excluir uma passagem da inteligência veicular")
async def excluir_passagem(pid: str, request: Request, db: AsyncSession = Depends(get_db),
                           usuario: Usuario = Depends(exige("objects", "edit"))):
    """Passagem errada (recorte de outra coisa, veículo inexistente) sai do
    histórico e do treino, com o rótulo e o quadro inteiro. O registro geral
    da lista de objetos segue as regras dele e não é apagado daqui."""
    p = await _passagem(db, pid)
    rot = (await db.execute(select(RotuloVeiculo).where(RotuloVeiculo.passagem_id == p.id))
           ).scalars().first()
    if rot is not None:
        await db.delete(rot)
    if p.quadro_key:
        evidencias.apagar(p.quadro_key)
    meta = {"trilha": p.trilha, "camera": p.camera.codigo if p.camera else "",
            "categoria": p.categoria, "inicio": p.inicio.isoformat() if p.inicio else ""}
    await db.delete(p)
    await db.flush()
    await audit.registrar(db, acao="VEICULO_PASSAGEM_EXCLUIDA", descricao="Passagem de veículo excluída",
                          usuario=usuario, alvo=pid, request=request, metadados=meta)
    return {"excluida": True}
