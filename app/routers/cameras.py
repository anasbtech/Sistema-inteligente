"""Câmeras: cadastro, configuração, zonas, PTZ e heartbeat do worker."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import settings
from app.database import get_db
from app.deps import exige
from app.models import Camera, StatusCamera, Usuario, Zona
from app.schemas import CameraCreate, CameraOut, CameraUpdate, PTZComando, ZonaIn
from app.security import CREDENCIAL_OCULTA
from app.services.vision import FICHA_ACAO, janela_horario

router = APIRouter(prefix="/cameras", tags=["Câmeras"])


async def _proximo_codigo(db: AsyncSession) -> str:
    # Do maior código emitido, não de COUNT(*): apagar uma câmera faria a
    # contagem cair e o cadastro seguinte reemitir um código que já existe.
    codigos = (
        await db.execute(select(Camera.codigo).where(Camera.codigo.like("CAM-%")))
    ).scalars().all()
    maior = 0
    for c in codigos:
        try:
            maior = max(maior, int(c.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"CAM-{maior + 1:03d}"


def rtsp_do_ip(ip: str) -> str:
    """Monta a URL RTSP a partir do IP, com o padrão do parque.

    O percent-encoding da credencial é feito aqui. A senha das câmeras tem
    `@` e `?`, que quebram a URL se entrarem crus — exigir que quem cadastra
    saiba disso é um erro esperando para acontecer.
    """
    usuario = quote(settings.CAMERA_USUARIO, safe="")
    senha = quote(settings.CAMERA_SENHA, safe="")
    credencial = f"{usuario}:{senha}@" if usuario or senha else ""
    caminho = settings.CAMERA_RTSP_CAMINHO
    if caminho and not caminho.startswith("/"):
        caminho = "/" + caminho
    return f"rtsp://{credencial}{ip}:{settings.CAMERA_RTSP_PORTA}{caminho}"


async def _obter(db: AsyncSession, codigo: str) -> Camera:
    cam = (await db.execute(select(Camera).where(Camera.codigo == codigo))).scalar_one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Câmera '{codigo}' não encontrada.")
    return cam


def _acoes_sem_efeito(cam: Camera) -> List[dict]:
    """Ações ligadas nesta câmera que, do jeito que ela está, não disparam.

    Ligar "Invasão de perímetro" sem desenhar a linha que define o perímetro
    deixa a câmera anunciando uma vigilância que não exerce. O diagnóstico é
    onde isso precisa aparecer — a configuração aceita, mas não cala.
    """
    tipos = {getattr(z.tipo, "value", z.tipo) for z in (cam.zonas or ())}
    pendencias: List[dict] = []
    for codigo in (cam.acoes_ativas or ()):
        ficha = FICHA_ACAO.get(codigo)
        if ficha is None:
            continue
        if ficha.exige_zona and ficha.exige_zona not in tipos:
            pendencias.append({
                "acao": codigo, "rotulo": ficha.rotulo,
                "motivo": f"Depende de uma zona do tipo {ficha.exige_zona.lower()}, "
                          f"e esta câmera não tem nenhuma.",
            })
        # Janela que começa onde termina são 24 h permitidas: a ação fica
        # ligada e nunca pode disparar. É configuração válida — serve para
        # deixar a ação armada e suspender a vigilância sem desmarcá-la — mas
        # precisa aparecer, ou a ficha anuncia uma guarda que ninguém faz.
        if codigo == "fora_de_horario":
            ini, fim = janela_horario(cam.horario_inicio, cam.horario_fim)
            if ini == fim:
                pendencias.append({
                    "acao": codigo, "rotulo": ficha.rotulo,
                    "motivo": f"A janela permitida vai de {ini} a {fim} — 24 horas. "
                              f"Nada fica fora dela, então a ação nunca dispara.",
                })
    return pendencias


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
    # A câmera técnica do vídeo de teste não é câmera do parque.
    filtros = [Camera.codigo != settings.VIDEO_TESTE_CAMERA]
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


# Lugares por onde se passa, não onde alguém fica. A câmera existe neles e
# continua precisando dizer onde está, mas ninguém é "do elevador": oferecê-los
# no cadastro de pessoa só produz lotação que não quer dizer nada.
SETORES_DE_PASSAGEM = {"Elevador", "Descanso"}


@router.get("/setores", summary="Setores cadastrados")
async def setores(
    lotacao: bool = Query(False, description="Só onde alguém pode ser lotado"),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("live")),
):
    linhas = (await db.execute(
        select(Camera.setor).where(Camera.codigo != settings.VIDEO_TESTE_CAMERA)
        .distinct().order_by(Camera.setor))).scalars().all()
    if lotacao:
        linhas = [s for s in linhas if s not in SETORES_DE_PASSAGEM]
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
    if not campos.get("rtsp_url") and campos.get("ip"):
        campos["rtsp_url"] = rtsp_do_ip(campos["ip"])
    if not campos.get("rtsp_url"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Informe o endereço IP da câmera — sem ele não há stream para processar.",
        )

    cam = Camera(codigo=codigo, **campos)
    _aplicar_zonas(cam, dados.zonas)
    db.add(cam)
    await db.flush()

    await audit.registrar(db, acao="CAM_ADD", descricao="Câmera cadastrada no parque",
                          severidade="ok", usuario=usuario, alvo=codigo, request=request,
                          metadados={"setor": cam.setor, "analiticos": {
                              "facial": cam.analitico_facial,
                              "objeto": cam.analitico_objeto,
                              "acao": cam.analitico_acao,
                              "acoes": list(cam.acoes_ativas or ())}})
    return await _resposta(db, cam)


@router.patch("/{codigo}", response_model=CameraOut, summary="Configurar câmera")
async def atualizar(codigo: str, dados: CameraUpdate, request: Request,
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("cams", "edit"))):
    cam = await _obter(db, codigo)
    campos = dados.model_dump(exclude_unset=True)
    zonas = campos.pop("zonas", None)

    # A leitura devolve o endereço com a credencial mascarada. Quem buscar a
    # câmera, mudar um campo e devolver o objeto inteiro gravaria a máscara
    # por cima do endereço real e deixaria a câmera sem conseguir conectar —
    # sem erro, porque é uma string válida. Descartar é mais seguro que
    # recusar: o resto da edição passa, e o endereço fica como estava.
    if campos.get("rtsp_url") and CREDENCIAL_OCULTA in campos["rtsp_url"]:
        campos.pop("rtsp_url")

    # Trocar o IP reaponta o stream: a URL derivada acompanha, a menos que
    # uma URL própria tenha sido informada na mesma edição.
    if campos.get("ip") and not campos.get("rtsp_url"):
        campos["rtsp_url"] = rtsp_do_ip(campos["ip"])

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
            "acoes": list(cam.acoes_ativas or ()),
            # A janela que está valendo aqui, já resolvida: o diagnóstico não
            # deve obrigar quem lê a saber que nulo significa "a do sistema".
            "horario_permitido": "{} às {}".format(*janela_horario(
                cam.horario_inicio, cam.horario_fim)),
            "horario_proprio": bool(cam.horario_inicio or cam.horario_fim),
            # Também já resolvido: quem lê o diagnóstico quer o número que
            # está valendo aqui, não "nulo, veja o padrão do sistema".
            "limite_aglomeracao": (cam.limite_aglomeracao
                                   or settings.ACAO_AGLOMERACAO_N),
            "limite_proprio": cam.limite_aglomeracao is not None,
        },
        # O que está ligado mas não pode disparar aqui. Ação que depende de
        # zona sem zona desenhada é vigilância anunciada e não exercida, e o
        # diagnóstico da câmera é onde isso tem de aparecer.
        "acoes_sem_efeito": _acoes_sem_efeito(cam),
        "zonas": len(cam.zonas),
    }
