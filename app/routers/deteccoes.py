"""Ingestão e consulta de detecções, e geração automática de alertas.

O worker de inferência chama `POST /deteccoes/ingest`. É aqui — e não no
worker — que a busca 1:N acontece, para que o limiar, a auditoria e a regra
de alerta fiquem num lugar só.
"""
from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import settings
from app.database import get_db
from app.deps import exige, usuario_de_query
from app.models import (
    Alerta, Camera, Deteccao, Pessoa, StatusAlerta, StatusPessoa,
    TipoDeteccao, Usuario, Zona,
)
from app.schemas import DeteccaoIn, DeteccaoOut, Pagina, QuadroAoVivoIn, RevisaoIn
from app.services import evidencias, face_engine
from app.services.events import publicar
from app.services.notificacoes import notificar_alerta_critico
from app.services.vision import ROTULO_ACAO, SEVERIDADE_ACAO, severidade_objeto
from app.tipos import como_utc

log = logging.getLogger("vigia.deteccoes")
router = APIRouter(prefix="/deteccoes", tags=["Detecções"])

PREFIXO_CODIGO = {
    TipoDeteccao.FACIAL: "FR",
    TipoDeteccao.OBJETO: "OD",
    TipoDeteccao.ACAO: "AR",
}

SEVERIDADE_STATUS = {
    StatusPessoa.PROCURADO: 4,
    StatusPessoa.MONITORADO: 3,
    StatusPessoa.DESAPARECIDO: 3,
    StatusPessoa.FUNCIONARIO: 1,
    StatusPessoa.CIDADAO: 1,
}


async def _proximo_codigo(db: AsyncSession, tipo: TipoDeteccao) -> str:
    ano = datetime.now(timezone.utc).year
    pfx = PREFIXO_CODIGO[tipo]
    total = (
        await db.execute(
            select(func.count()).select_from(Deteccao).where(Deteccao.tipo == tipo)
        )
    ).scalar_one()
    return f"{pfx}-{ano}{total + 1:06d}"


async def _criar_alerta(db: AsyncSession, det: Deteccao, cam: Camera,
                        pessoa: Optional[Pessoa],
                        notificar: bool = True) -> Optional[Alerta]:
    """Severidade 3+ vira alerta. Abaixo disso fica só no registro."""
    if det.severidade < 3:
        return None

    total = (await db.execute(select(func.count()).select_from(Alerta))).scalar_one()

    if det.tipo == TipoDeteccao.FACIAL and pessoa is not None:
        rotulo = {
            StatusPessoa.PROCURADO: "Procurado",
            StatusPessoa.MONITORADO: "Monitorado",
            StatusPessoa.DESAPARECIDO: "Desaparecido",
        }.get(pessoa.status, "Pessoa")
        titulo = f"{rotulo} identificado"
        detalhe = (
            f"{pessoa.nome} ({pessoa.id_biometrico}) reconhecido com "
            f"{det.similaridade * 100:.1f}% de similaridade em {cam.nome}. "
            "Confirmação humana obrigatória antes de qualquer abordagem."
        )
    elif det.tipo == TipoDeteccao.OBJETO:
        titulo = f"{det.classe} detectado"
        detalhe = f"Classe '{det.classe}' com {(det.confianca or 0) * 100:.1f}% de confiança em {cam.nome}."
    else:
        titulo = ROTULO_ACAO.get(det.classe, det.classe)
        detalhe = (
            f"Evento comportamental por {det.duracao_s or 0}s envolvendo "
            f"{det.envolvidos or 1} pessoa(s) em {cam.nome}."
        )

    alerta = Alerta(
        codigo=f"ALR-{total + 1:05d}",
        deteccao_id=det.id,
        camera_id=cam.id,
        pessoa_id=pessoa.id if pessoa else None,
        tipo=det.tipo,
        titulo=titulo,
        detalhe=detalhe,
        severidade=det.severidade,
        status=StatusAlerta.ABERTO,
        disparado_em=det.ocorrido_em,
    )
    db.add(alerta)
    await db.flush()

    # Durante a carga histórica do seed não faz sentido acionar ninguém.
    if det.severidade >= 4 and notificar:
        await notificar_alerta_critico(titulo, detalhe, cam.codigo)
    return alerta


# ======================================================================
# Ingestão — usada pelo worker de inferência E pelo simulador
# ======================================================================
async def processar_deteccao(db: AsyncSession, dados: DeteccaoIn,
                             origem: str = "worker",
                             publicar_evento: bool = True) -> dict:
    """Caminho único de entrada de qualquer detecção no sistema.

    É aqui — e não no worker — que acontecem a busca 1:N, a aplicação do
    limiar da câmera e a decisão de alerta. Um lugar para mudar, um lugar
    para auditar, e o simulador exercita exatamente o mesmo código que a
    câmera real vai exercitar.
    """
    cam = (
        await db.execute(select(Camera).where(Camera.codigo == dados.camera_codigo))
    ).scalars().unique().one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Câmera '{dados.camera_codigo}' não cadastrada.")

    pessoa: Optional[Pessoa] = None
    similaridade: Optional[float] = None
    limiar: Optional[float] = None
    severidade = 1

    if dados.tipo == TipoDeteccao.FACIAL:
        if not cam.analitico_facial:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Análise facial desativada nesta câmera.")
        if not dados.embedding:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Detecção facial exige o campo 'embedding'.")

        limiar = cam.limiar_facial
        resultado = await face_engine.buscar_pessoa(db, dados.embedding, limiar)
        similaridade = round(resultado.similaridade, 4)

        if resultado.match:
            pessoa = (
                await db.execute(
                    select(Pessoa).where(Pessoa.id == uuid.UUID(resultado.pessoa_id))
                )
            ).scalar_one_or_none()
            if pessoa is not None:
                severidade = SEVERIDADE_STATUS.get(pessoa.status, 1)
                pessoa.total_deteccoes += 1
                anterior = como_utc(pessoa.ultima_deteccao)
                if anterior is None or como_utc(dados.ocorrido_em) > anterior:
                    pessoa.ultima_deteccao = dados.ocorrido_em
                    pessoa.ultima_camera = cam.nome
        # Sem match: severidade 1 e pessoa_id nulo — nenhuma identidade atribuída.

        # Uma presença, uma linha. Sem correspondência não existe identidade
        # para separar duas capturas, e nesta resolução o embedding também não
        # separa — então quem chega dentro da janela da anterior é tratado como
        # a mesma passagem. Sem isto, alguém que cruza o saguão deixava uma
        # dezena de linhas "não identificada", todas com o mesmo rosto.
        # O seed fica de fora: ele fabrica um histórico espalhado, não uma
        # presença contínua diante da câmera.
        if pessoa is None and origem != "seed" and settings.UNMATCHED_FACE_WINDOW_S > 0:
            desde = como_utc(dados.ocorrido_em) - timedelta(
                seconds=settings.UNMATCHED_FACE_WINDOW_S
            )
            anterior_codigo = (
                await db.execute(
                    select(Deteccao.codigo)
                    .where(
                        Deteccao.camera_id == cam.id,
                        Deteccao.tipo == TipoDeteccao.FACIAL,
                        Deteccao.pessoa_id.is_(None),
                        Deteccao.ocorrido_em >= desde,
                    )
                    .order_by(Deteccao.ocorrido_em.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if anterior_codigo is not None:
                log.info("%s: captura não identificada absorvida por %s",
                         cam.codigo, anterior_codigo)
                return {
                    "codigo": anterior_codigo,
                    "match": False,
                    "id_biometrico": None,
                    "similaridade": similaridade,
                    "severidade": 1,
                    "alerta": None,
                    "descartada": True,
                }

    elif dados.tipo == TipoDeteccao.OBJETO:
        limiar = cam.limiar_objeto
        severidade = severidade_objeto(dados.classe)
    else:
        severidade = SEVERIDADE_ACAO.get(dados.classe, 2)

    zona_id = None
    if dados.zona_nome:
        zona = (
            await db.execute(
                select(Zona).where(Zona.camera_id == cam.id, Zona.nome == dados.zona_nome)
            )
        ).scalar_one_or_none()
        zona_id = zona.id if zona else None

    # Capturas faciais sem correspondência têm prazo de validade curto.
    expurgo = None
    if dados.tipo == TipoDeteccao.FACIAL and pessoa is None:
        expurgo = dados.ocorrido_em + timedelta(days=settings.RETENTION_DAYS_UNMATCHED)

    codigo = await _proximo_codigo(db, dados.tipo)

    # O recorte é gravado sob o código da própria detecção. A chave que o
    # worker sugere não vira caminho — ela chega pela rede.
    chave = ""
    if dados.snapshot_jpeg:
        try:
            chave = evidencias.gravar(
                codigo, dados.ocorrido_em,
                base64.b64decode(dados.snapshot_jpeg, validate=True),
            )
        except (ValueError, OSError) as exc:
            log.warning("Recorte de %s descartado: %s", codigo, exc)

    det = Deteccao(
        codigo=codigo,
        tipo=dados.tipo,
        ocorrido_em=dados.ocorrido_em,
        camera_id=cam.id,
        pessoa_id=pessoa.id if pessoa else None,
        similaridade=similaridade,
        limiar_aplicado=limiar,
        qualidade_face=dados.qualidade_face,
        classe=dados.classe,
        confianca=dados.confianca,
        duracao_s=dados.duracao_s,
        envolvidos=dados.envolvidos,
        placa=dados.placa,
        zona_id=zona_id,
        bbox=dados.bbox,
        track_id=dados.track_id,
        severidade=severidade,
        snapshot_key=chave,
        hash_evidencia=dados.hash_evidencia,
        expurgo_em=expurgo,
    )
    db.add(det)
    await db.flush()

    alerta = await _criar_alerta(db, det, cam, pessoa, notificar=publicar_evento)

    if publicar_evento:
        # Publicar identificação em tempo real (para exibir nome na câmera)
        if dados.tipo == TipoDeteccao.FACIAL and pessoa is not None:
            await publicar("identificacao", {
                "camera_codigo": cam.codigo,
                "pessoa_nome": pessoa.nome,
                "id_biometrico": pessoa.id_biometrico,
                "bbox": det.bbox,
                "similaridade": similaridade,
            })

        await publicar("deteccao", {
            "codigo": det.codigo,
            "tipo": det.tipo.value,
            "ocorrido_em": det.ocorrido_em,
            "camera_codigo": cam.codigo,
            "camera_nome": cam.nome,
            "setor": cam.setor,
            "classe": det.classe,
            "classe_rotulo": ROTULO_ACAO.get(det.classe, det.classe),
            "confianca": det.confianca,
            "similaridade": det.similaridade,
            "limiar": det.limiar_aplicado,
            "severidade": det.severidade,
            "bbox": det.bbox,
            "snapshot_key": det.snapshot_key,
            "origem": origem,
            "pessoa": {
                "id_biometrico": pessoa.id_biometrico,
                "nome": pessoa.nome,
                "sexo": pessoa.sexo.value,
                "status": pessoa.status.value,
            } if pessoa else None,
            "alerta": {"codigo": alerta.codigo, "titulo": alerta.titulo,
                       "severidade": alerta.severidade} if alerta else None,
        })

    return {
        "codigo": det.codigo,
        "match": pessoa is not None,
        "id_biometrico": pessoa.id_biometrico if pessoa else None,
        "similaridade": similaridade,
        "severidade": severidade,
        "alerta": alerta.codigo if alerta else None,
    }


@router.post("/ingest", status_code=status.HTTP_201_CREATED,
             summary="Receber detecção do worker de inferência")
async def ingest(dados: DeteccaoIn, db: AsyncSession = Depends(get_db)):
    """Endpoint de serviço: exponha só na rede interna, ou exija mTLS no gateway."""
    return await processar_deteccao(db, dados, origem="worker")


@router.post("/ao-vivo", status_code=status.HTTP_204_NO_CONTENT,
             summary="Caixas do quadro atual, para a sobreposição do mosaico")
async def ao_vivo(dados: QuadroAoVivoIn) -> None:
    """Publica as caixas do quadro atual sem gravar nada.

    Detectar e registrar são coisas diferentes: a caixa acompanha o objeto
    enquanto ele estiver diante da câmera, mas o registro nasce uma vez por
    passagem. Misturar os dois encheria a base de linhas repetidas, ou faria a
    caixa sumir com o carro ainda parado ali.
    """
    await publicar("quadro_ao_vivo", {
        "camera_codigo": dados.camera_codigo,
        "caixas": [c.model_dump() for c in dados.caixas],
    })


# ======================================================================
# Consulta
# ======================================================================
@router.get("", response_model=Pagina, summary="Listar detecções")
async def listar(
    tipo: Optional[TipoDeteccao] = None,
    camera: Optional[str] = Query(None, description="Código da câmera"),
    classe: Optional[str] = None,
    id_biometrico: Optional[str] = None,
    apenas_match: Optional[bool] = None,
    desde: Optional[datetime] = None,
    ate: Optional[datetime] = None,
    severidade_min: Optional[int] = Query(None, ge=1, le=4),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("facial")),
):
    filtros = []
    if tipo:
        filtros.append(Deteccao.tipo == tipo)
    if classe:
        filtros.append(Deteccao.classe == classe)
    if camera:
        cam = (await db.execute(select(Camera).where(Camera.codigo == camera))).scalar_one_or_none()
        if cam is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
        filtros.append(Deteccao.camera_id == cam.id)
    if id_biometrico:
        p = (await db.execute(
            select(Pessoa).where(Pessoa.id_biometrico == id_biometrico)
        )).scalar_one_or_none()
        if p is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")
        filtros.append(Deteccao.pessoa_id == p.id)
    if apenas_match is not None:
        filtros.append(Deteccao.pessoa_id.isnot(None) if apenas_match
                       else Deteccao.pessoa_id.is_(None))
    if desde:
        filtros.append(Deteccao.ocorrido_em >= desde)
    if ate:
        filtros.append(Deteccao.ocorrido_em <= ate)
    if severidade_min:
        filtros.append(Deteccao.severidade >= severidade_min)

    total = (await db.execute(
        select(func.count()).select_from(Deteccao).where(*filtros)
    )).scalar_one()
    linhas = (
        await db.execute(
            select(Deteccao).where(*filtros)
            .order_by(Deteccao.ocorrido_em.desc())
            .offset((pagina - 1) * por_pagina).limit(por_pagina)
        )
    ).scalars().unique().all()

    return Pagina(total=total, pagina=pagina, por_pagina=por_pagina,
                  itens=[DeteccaoOut.model_validate(d) for d in linhas])


@router.get("/resumo", summary="Agregados para o painel")
async def resumo(horas: int = Query(24, ge=1, le=168), db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("facial"))):
    desde = datetime.now(timezone.utc) - timedelta(hours=horas)

    por_tipo = dict((t, n) for t, n in (
        await db.execute(
            select(Deteccao.tipo, func.count()).where(Deteccao.ocorrido_em >= desde)
            .group_by(Deteccao.tipo)
        )
    ).all())

    # Agrupamento por hora feito em Python: `date_trunc` é do PostgreSQL e o
    # sistema também roda em SQLite.
    brutos = (
        await db.execute(
            select(Deteccao.ocorrido_em, Deteccao.tipo)
            .where(Deteccao.ocorrido_em >= desde)
        )
    ).all()
    baldes: dict[tuple, int] = {}
    for quando, tipo in brutos:
        hora = quando.replace(minute=0, second=0, microsecond=0)
        baldes[(hora, tipo)] = baldes.get((hora, tipo), 0) + 1
    por_hora = sorted(((h, t, n) for (h, t), n in baldes.items()), key=lambda r: r[0])

    por_camera = (
        await db.execute(
            select(Camera.codigo, Camera.nome, Camera.setor, func.count(Deteccao.id).label("n"))
            .join(Deteccao, Deteccao.camera_id == Camera.id)
            .where(Deteccao.ocorrido_em >= desde)
            .group_by(Camera.codigo, Camera.nome, Camera.setor)
            .order_by(func.count(Deteccao.id).desc()).limit(10)
        )
    ).all()

    identificacoes = (
        await db.execute(
            select(func.count()).select_from(Deteccao)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.pessoa_id.isnot(None),
                   Deteccao.ocorrido_em >= desde)
        )
    ).scalar_one()

    return {
        "janela_horas": horas,
        "total": sum(por_tipo.values()),
        "por_tipo": {t.value: n for t, n in por_tipo.items()},
        "identificacoes_positivas": identificacoes,
        "serie_horaria": [
            {"hora": h, "tipo": t.value, "total": n} for h, t, n in por_hora
        ],
        "top_cameras": [
            {"codigo": c, "nome": n, "setor": s, "total": q} for c, n, s, q in por_camera
        ],
    }


@router.get("/{codigo}/snapshot", summary="Recorte da captura (JPEG)")
async def snapshot(codigo: str, token: str = Query(..., description="Access token JWT"),
                   db: AsyncSession = Depends(get_db)):
    """A imagem que a revisão humana olha antes de confirmar uma identificação.

    Não gera linha de auditoria: a lista carrega uma miniatura por registro, e
    auditar cada uma encheria a trilha de ruído paralelo. O acesso que importa
    — abrir a ficha da captura — já é registrado em `obter`.
    """
    await usuario_de_query(token, db, "facial")

    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")

    imagem = evidencias.ler(det.snapshot_key)
    if imagem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Sem recorte guardado para esta captura.")
    return Response(content=imagem, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/{codigo}", response_model=DeteccaoOut, summary="Detalhe da detecção")
async def obter(codigo: str, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("facial"))):
    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")

    if det.pessoa_id:
        await audit.registrar(
            db, acao="CONSULTA_BIO", descricao="Consulta a detecção com identificação",
            severidade="info", usuario=usuario,
            alvo=det.pessoa.id_biometrico if det.pessoa else det.codigo,
            request=request,
        )
    return DeteccaoOut.model_validate(det)


@router.post("/{codigo}/revisao", response_model=DeteccaoOut, summary="Revisar detecção")
async def revisar(codigo: str, dados: RevisaoIn, request: Request,
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("facial", "edit"))):
    """Confirmação humana. Falsos positivos alimentam o lote de reavaliação do
    limiar — e não devem ser apagados."""
    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")

    det.revisado = True
    det.veredito = dados.veredito
    det.revisado_por = usuario.id
    det.revisado_em = datetime.now(timezone.utc)

    await audit.registrar(
        db, acao="DET_REVISAO", descricao=f"Detecção revisada: {dados.veredito}",
        severidade="ok" if dados.veredito == "Confirmado" else "warn",
        usuario=usuario, alvo=det.codigo, finalidade=dados.observacao or "Revisão operacional",
        request=request,
        metadados={"similaridade": det.similaridade, "limiar": det.limiar_aplicado},
    )
    return DeteccaoOut.model_validate(det)


@router.post("/expurgo", summary="Expurgar capturas vencidas")
async def expurgo(db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("settings"))):
    """Remove capturas sem correspondência cujo prazo de retenção venceu.

    Agende via cron. Mantém as vinculadas a alerta ou já revisadas — elas podem
    integrar inquérito.
    """
    agora = datetime.now(timezone.utc)
    alvos = (
        await db.execute(
            select(Deteccao).where(
                Deteccao.expurgo_em.isnot(None),
                Deteccao.expurgo_em <= agora,
                Deteccao.pessoa_id.is_(None),
                Deteccao.revisado.is_(False),
            ).limit(5000)
        )
    ).scalars().unique().all()

    imagens = 0
    for d in alvos:
        # Apagar o registro e deixar o recorte no disco transformaria a
        # retenção em promessa vazia.
        if evidencias.apagar(d.snapshot_key):
            imagens += 1
        await db.delete(d)

    await audit.registrar(
        db, acao="EXPURGO", descricao="Expurgo de capturas não identificadas vencidas",
        severidade="warn", usuario=usuario,
        alvo=f"{len(alvos)} registro(s), {imagens} recorte(s)",
        finalidade=f"Retenção de {settings.RETENTION_DAYS_UNMATCHED} dias",
    )
    return {"removidos": len(alvos), "imagens_removidas": imagens,
            "prazo_dias": settings.RETENTION_DAYS_UNMATCHED}
