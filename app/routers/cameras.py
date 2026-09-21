"""Câmeras: cadastro, configuração, zonas, PTZ e heartbeat do worker."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige
from app.models import Camera, StatusCamera, Usuario, Zona
from app.schemas import CameraCreate, CameraOut, CameraUpdate, PTZComando, ZonaIn

router = APIRouter(prefix="/cameras", tags=["Câmeras"])


async def _proximo_codigo(db: AsyncSession) -> str:
    total = (await db.execute(select(func.count()).select_from(Camera))).scalar_one()
    return f"CAM-{total + 1:03d}"


async def _obter(db: AsyncSession, codigo: str) -> Camera:
    cam = (await db.execute(select(Camera).where(Camera.codigo == codigo))).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Câmera '{codigo}' não encontrada.")
    return cam


def _aplicar_zonas(cam: Camera, zonas: List[ZonaIn]) -> None:
    cam.zonas.clear()
    for z in zonas:
        cam.zonas.append(Zona(nome=z.nome, tipo=z.tipo, cor=z.cor,
                              pontos=z.pontos, regras=z.regras))


async def _resposta(db: AsyncSession, cam: Camera) -> CameraOut:
    """Serializa a câmera com as zonas já carregadas.

    Depois de um flush, `cam.zonas` pode estar sem carregar; o Pydantic leria o
    atributo fora do contexto async e o SQLAlchemy levantaria MissingGreenlet.
    """
    await db.refresh(cam, attribute_names=["zonas"])
    return CameraOut.model_validate(cam)


@router.get("", response_model=List[CameraOut], summary="Listar câmeras")
async def listar(
    q: Optional[str] = None,
    setor: Optional[str] = None,
    status_cam: Optional[StatusCamera] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("live")),
):
    filtros = []
    if q:
        termo = f"%{q.strip()}%"
        filtros.append(or_(Camera.nome.ilike(termo), Camera.codigo.ilike(termo),
                           Camera.ip.ilike(termo), Camera.setor.ilike(termo)))
    if setor:
        filtros.append(Camera.setor == setor)
    if status_cam:
        filtros.append(Camera.status == status_cam)

    linhas = (
        await db.execute(select(Camera).where(*filtros).order_by(Camera.codigo))
    ).scalars().unique().all()
    return [CameraOut.model_validate(c) for c in linhas]


@router.get("/setores", summary="Setores cadastrados")
async def setores(db: AsyncSession = Depends(get_db), usuario: Usuario = Depends(exige("live"))):
    linhas = (await db.execute(select(Camera.setor).distinct().order_by(Camera.setor))).scalars().all()
    return {"setores": list(linhas)}


@router.get("/{codigo}", response_model=CameraOut, summary="Ficha da câmera")
async def obter(codigo: str, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("live"))):
    return CameraOut.model_validate(await _obter(db, codigo))


@router.post("", response_model=CameraOut, status_code=status.HTTP_201_CREATED,
             summary="Cadastrar câmera")
async def criar(dados: CameraCreate, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("cams", "edit"))):
    codigo = dados.codigo or await _proximo_codigo(db)
    if (await db.execute(select(Camera).where(Camera.codigo == codigo))).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, f"Já existe câmera com o código '{codigo}'.")

    campos = dados.model_dump(exclude={"codigo", "zonas"})
    cam = Camera(codigo=codigo, **campos)
    _aplicar_zonas(cam, dados.zonas)
    db.add(cam)
    await db.flush()

    await audit.registrar(db, acao="CAM_ADD", descricao="Câmera cadastrada no parque",
                          severidade="ok", usuario=usuario, alvo=codigo, request=request,
                          metadados={"setor": cam.setor, "analiticos": {
                              "facial": cam.analitico_facial,
                              "objeto": cam.analitico_objeto,
                              "acao": cam.analitico_acao}})
    return await _resposta(db, cam)


@router.patch("/{codigo}", response_model=CameraOut, summary="Configurar câmera")
async def atualizar(codigo: str, dados: CameraUpdate, request: Request,
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("cams", "edit"))):
    cam = await _obter(db, codigo)
    campos = dados.model_dump(exclude_unset=True)
    zonas = campos.pop("zonas", None)

    limiar_antigo = cam.limiar_facial
    for campo, valor in campos.items():
        setattr(cam, campo, valor)
    if zonas is not None:
        _aplicar_zonas(cam, [ZonaIn(**z) if isinstance(z, dict) else z for z in zonas])

    await db.flush()

    # Mudança de limiar facial é sensível: registra com destaque.
    if "limiar_facial" in campos and campos["limiar_facial"] != limiar_antigo:
        await audit.registrar(
            db, acao="LIMIAR_EDIT",
            descricao="Limiar de similaridade facial alterado", severidade="warn",
            usuario=usuario, alvo=codigo, finalidade="Ajuste de sensibilidade",
            request=request,
            metadados={"anterior": limiar_antigo, "novo": cam.limiar_facial},
        )

    await audit.registrar(db, acao="CAM_EDIT", descricao="Configuração de câmera alterada",
                          severidade="info", usuario=usuario, alvo=codigo, request=request,
                          metadados={"campos": sorted(campos.keys())})
    return await _resposta(db, cam)


@router.delete("/{codigo}", status_code=status.HTTP_204_NO_CONTENT, summary="Remover câmera")
async def remover(codigo: str, request: Request, motivo: str = Query(..., min_length=8),
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("cams", "edit"))):
    cam = await _obter(db, codigo)
    await db.delete(cam)
    await audit.registrar(db, acao="CAM_DEL", descricao="Câmera removida do parque",
                          severidade="warn", usuario=usuario, alvo=codigo,
                          finalidade=motivo, request=request)


# ======================================================================
# PTZ
# ======================================================================
@router.post("/{codigo}/ptz", summary="Comando PTZ (ONVIF)")
async def ptz(codigo: str, cmd: PTZComando, request: Request,
              db: AsyncSession = Depends(get_db),
              usuario: Usuario = Depends(exige("cams", "edit"))):
    cam = await _obter(db, codigo)
    if not cam.ptz:
        raise HTTPException(status.HTTP_409_CONFLICT, "Esta câmera é fixa — não possui PTZ.")
    if cam.status != StatusCamera.ONLINE:
        raise HTTPException(status.HTTP_409_CONFLICT, "Câmera fora de operação.")

    from app.services.onvif_ptz import executar_ptz

    await executar_ptz(cam, cmd.comando, cmd.velocidade, cmd.preset)
    await audit.registrar(db, acao="PTZ", descricao=f"Comando PTZ '{cmd.comando}'",
                          severidade="info", usuario=usuario, alvo=codigo, request=request)
    return {"ok": True, "camera": codigo, "comando": cmd.comando}


# ======================================================================
# Heartbeat do worker
# ======================================================================
@router.post("/{codigo}/heartbeat", summary="Sinal de vida do worker de inferência")
async def heartbeat(codigo: str, latencia_ms: int = Query(0, ge=0),
                    online: bool = Query(True),
                    db: AsyncSession = Depends(get_db)):
    """Chamado pelo worker a cada poucos segundos. Sem autenticação de usuário —
    proteja por rede interna ou chave de serviço no gateway."""
    cam = await _obter(db, codigo)
    cam.ultimo_heartbeat = datetime.now(timezone.utc)
    cam.latencia_ms = latencia_ms
    if cam.status != StatusCamera.MANUTENCAO:
        cam.status = StatusCamera.ONLINE if online else StatusCamera.OFFLINE
    return {"ok": True}


@router.get("/{codigo}/saude", summary="Diagnóstico da câmera")
async def saude(codigo: str, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("live"))):
    cam = await _obter(db, codigo)
    agora = datetime.now(timezone.utc)
    atraso = (agora - cam.ultimo_heartbeat).total_seconds() if cam.ultimo_heartbeat else None
    return {
        "codigo": cam.codigo,
        "status": cam.status.value,
        "latencia_ms": cam.latencia_ms,
        "ultimo_heartbeat": cam.ultimo_heartbeat,
        "atraso_s": round(atraso, 1) if atraso is not None else None,
        "saudavel": atraso is not None and atraso < 30,
        "analiticos": {
            "facial": cam.analitico_facial,
            "objeto": cam.analitico_objeto,
            "acao": cam.analitico_acao,
        },
        "zonas": len(cam.zonas),
    }
