"""Central de alertas: triagem, atribuição e encerramento com desfecho."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige, usuario_de_query
from app.models import Alerta, Camera, StatusAlerta, TipoDeteccao, Usuario
from app.schemas import AlertaFechaIn, AlertaLoteIn, AlertaOut, Pagina
from app.services import evidencias
from app.services.events import publicar
from app.tipos import como_utc

router = APIRouter(prefix="/alertas", tags=["Alertas"])


async def _obter(db: AsyncSession, codigo: str) -> Alerta:
    a = (
        await db.execute(select(Alerta).where(Alerta.codigo == codigo))
    ).scalars().unique().one_or_none()
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Alerta '{codigo}' não encontrado.")
    return a


@router.get("", response_model=Pagina, summary="Listar alertas")
async def listar(
    status_alerta: Optional[StatusAlerta] = Query(None, alias="status"),
    severidade: Optional[int] = Query(None, ge=1, le=4),
    tipo: Optional[TipoDeteccao] = None,
    camera: Optional[str] = None,
    # A tela mandava o período desde sempre, e a rota não o lia: o filtro de
    # datas da Central de alertas não filtrava nada.
    desde: Optional[datetime] = None,
    ate: Optional[datetime] = None,
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("alerts")),
):
    filtros = []
    if desde:
        filtros.append(Alerta.disparado_em >= desde)
    if ate:
        filtros.append(Alerta.disparado_em <= ate)
    if status_alerta:
        filtros.append(Alerta.status == status_alerta)
    if severidade:
        filtros.append(Alerta.severidade == severidade)
    if tipo:
        filtros.append(Alerta.tipo == tipo)
    if camera:
        cam = (await db.execute(select(Camera).where(Camera.codigo == camera))).scalar_one_or_none()
        if cam is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
        filtros.append(Alerta.camera_id == cam.id)

    total = (await db.execute(select(func.count()).select_from(Alerta).where(*filtros))).scalar_one()
    linhas = (
        await db.execute(
            select(Alerta).where(*filtros)
            .order_by(Alerta.severidade.desc(), Alerta.disparado_em.desc())
            .offset((pagina - 1) * por_pagina).limit(por_pagina)
        )
    ).scalars().unique().all()

    return Pagina(total=total, pagina=pagina, por_pagina=por_pagina,
                  itens=[AlertaOut.model_validate(a) for a in linhas])


@router.get("/resumo", summary="Contadores e tempo médio de resposta")
async def resumo(db: AsyncSession = Depends(get_db), usuario: Usuario = Depends(exige("alerts"))):
    por_status = dict((s, n) for s, n in (
        await db.execute(select(Alerta.status, func.count()).group_by(Alerta.status))
    ).all())

    # `extract(epoch ...)` é do PostgreSQL; calculamos em Python para manter
    # o mesmo resultado nos dois bancos.
    fechados = (
        await db.execute(
            select(Alerta.disparado_em, Alerta.fechado_em)
            .where(Alerta.fechado_em.isnot(None)).limit(5000)
        )
    ).all()
    tmr = (sum((como_utc(f) - como_utc(d)).total_seconds() for d, f in fechados) / len(fechados)
           if fechados else None)

    criticos = (
        await db.execute(
            select(func.count()).select_from(Alerta)
            .where(Alerta.severidade >= 4, Alerta.status != StatusAlerta.RESOLVIDO)
        )
    ).scalar_one()

    return {
        "abertos": por_status.get(StatusAlerta.ABERTO, 0),
        "em_andamento": por_status.get(StatusAlerta.EM_ANDAMENTO, 0),
        "resolvidos": por_status.get(StatusAlerta.RESOLVIDO, 0),
        "criticos_pendentes": criticos,
        "tempo_medio_resposta_s": round(float(tmr), 1) if tmr else None,
    }


@router.post("/encerrar-lote", summary="Encerrar vários alertas com o mesmo desfecho")
async def encerrar_lote(dados: AlertaLoteIn, request: Request,
                        db: AsyncSession = Depends(get_db),
                        usuario: Usuario = Depends(exige("alerts", "edit"))):
    """A triagem em massa, com as mesmas regras do encerramento individual.

    Cada alerta ganha a sua linha na trilha — o lote não apaga a
    rastreabilidade: daqui a um ano, "quem encerrou ALR-01604 e por quê"
    continua tendo resposta. Os já encerrados são pulados, não recusam o
    lote: a fila muda enquanto o operador seleciona.
    """
    codigos = list(dict.fromkeys(c.strip() for c in dados.codigos if c.strip()))
    alertas = (
        await db.execute(select(Alerta).where(Alerta.codigo.in_(codigos)))
    ).scalars().unique().all()
    achados = {a.codigo for a in alertas}
    resolucao = dados.resolucao.strip()
    agora = datetime.now(timezone.utc)

    encerrados, ja_encerrados = [], []
    for a in alertas:
        if a.status == StatusAlerta.RESOLVIDO:
            ja_encerrados.append(a.codigo)
            continue
        a.status = StatusAlerta.RESOLVIDO
        a.resolucao = resolucao
        a.fechado_em = agora
        a.atribuido_a = a.atribuido_a or usuario.id
        await audit.registrar(
            db, acao="ALERTA_FECHA",
            descricao="Alerta encerrado com registro de desfecho (em lote)",
            severidade="ok", usuario=usuario, alvo=a.codigo, finalidade=resolucao[:200],
            request=request,
            metadados={"severidade": a.severidade, "lote": len(codigos),
                       "tempo_resposta_s": int((agora - como_utc(a.disparado_em)).total_seconds())},
        )
        encerrados.append(a.codigo)

    if encerrados:
        await publicar("alerta_atualizado", {"lote": len(encerrados), "status": "RESOLVIDO"})
    return {
        "encerrados": len(encerrados),
        "ja_encerrados": ja_encerrados,
        "nao_encontrados": [c for c in codigos if c not in achados],
    }


@router.get("/{codigo}/snapshot", summary="Imagem do alerta (JPEG)")
async def snapshot(codigo: str, token: str = Query(..., description="Access token JWT"),
                   db: AsyncSession = Depends(get_db)):
    """A imagem do alerta, quando ele carrega a própria.

    Alerta de sala reporta um estado — "ficou vazia" — e não nasce de uma
    detecção; sem esta rota a coluna CAPTURA ficava em branco e o operador
    tinha de ir procurar a imagem noutro lugar. Quando o alerta veio de uma
    detecção, quem serve a imagem continua sendo `/deteccoes/{codigo}/snapshot`.

    Não gera linha de auditoria, pela mesma razão da rota equivalente de
    detecção: a lista carrega uma miniatura por linha, e auditar cada uma
    encheria a trilha de ruído paralelo.
    """
    await usuario_de_query(token, db, "alerts")

    alerta = (
        await db.execute(select(Alerta).where(Alerta.codigo == codigo))
    ).scalars().unique().one_or_none()
    if alerta is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alerta não encontrado.")

    imagem = evidencias.ler(alerta.snapshot_key)
    if imagem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Sem imagem guardada para este alerta.")
    return Response(content=imagem, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/{codigo}", response_model=AlertaOut, summary="Detalhe do alerta")
async def obter(codigo: str, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("alerts"))):
    return AlertaOut.model_validate(await _obter(db, codigo))


@router.post("/{codigo}/assumir", response_model=AlertaOut, summary="Assumir alerta")
async def assumir(codigo: str, request: Request, db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("alerts", "edit"))):
    a = await _obter(db, codigo)
    if a.status == StatusAlerta.RESOLVIDO:
        raise HTTPException(status.HTTP_409_CONFLICT, "Alerta já encerrado.")

    a.status = StatusAlerta.EM_ANDAMENTO
    a.atribuido_a = usuario.id

    await audit.registrar(db, acao="ALERTA_ASSUME", descricao="Alerta assumido por operador",
                          severidade="info", usuario=usuario, alvo=codigo, request=request)
    await publicar("alerta_atualizado", {"codigo": codigo, "status": a.status.value,
                                         "responsavel": usuario.nome})
    return AlertaOut.model_validate(a)


@router.post("/{codigo}/encerrar", response_model=AlertaOut, summary="Encerrar com desfecho")
async def encerrar(codigo: str, dados: AlertaFechaIn, request: Request,
                   db: AsyncSession = Depends(get_db),
                   usuario: Usuario = Depends(exige("alerts", "edit"))):
    """O desfecho é obrigatório: um alerta sem registro do que foi feito não
    serve nem à operação nem à prestação de contas."""
    a = await _obter(db, codigo)
    if a.status == StatusAlerta.RESOLVIDO:
        raise HTTPException(status.HTTP_409_CONFLICT, "Alerta já encerrado.")

    a.status = StatusAlerta.RESOLVIDO
    a.resolucao = dados.resolucao.strip()
    a.fechado_em = datetime.now(timezone.utc)
    a.atribuido_a = a.atribuido_a or usuario.id

    await audit.registrar(
        db, acao="ALERTA_FECHA", descricao="Alerta encerrado com registro de desfecho",
        severidade="ok", usuario=usuario, alvo=codigo, finalidade=a.resolucao[:200],
        request=request,
        metadados={"severidade": a.severidade,
                   "tempo_resposta_s": int((como_utc(a.fechado_em) - como_utc(a.disparado_em)).total_seconds())},
    )
    await publicar("alerta_atualizado", {"codigo": codigo, "status": a.status.value})
    return AlertaOut.model_validate(a)


@router.delete("/{codigo}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Excluir alerta")
async def excluir(codigo: str, request: Request,
                  motivo: str = Query(..., min_length=8, max_length=300,
                                      description="Por que este alerta está sendo excluído"),
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("settings"))):
    """Apaga um alerta. O motivo é exigido pelo contrato, não pela interface.

    `Query(...)` sem valor padrão: chamada sem motivo é recusada com 422 antes
    de chegar aqui. Deixar a obrigação só na tela significaria que `curl`, um
    script ou uma tela futura apagariam sem justificar.

    A detecção que deu origem ao alerta NÃO é apagada junto — ela é o registro
    de que algo foi visto, e continua na lista de detecções. O que sai é a
    pendência de triagem.

    Admin apenas, porque não se desfaz: encerrar com desfecho é o caminho
    normal, e preserva o histórico. Excluir é para o alerta que nasceu de uma
    regra que deixou de existir e só polui a fila.
    """
    a = await _obter(db, codigo)

    await audit.registrar(
        db, acao="ALERTA_EXCLUSAO",
        descricao=f"Alerta {codigo} excluído ({a.titulo})",
        severidade="warn", usuario=usuario, alvo=codigo,
        finalidade=motivo.strip(), request=request,
        metadados={"titulo": a.titulo, "severidade": a.severidade,
                   "status": a.status.value, "tipo": a.tipo.value,
                   "disparado_em": a.disparado_em.isoformat() if a.disparado_em else None,
                   "deteccao_id": str(a.deteccao_id) if a.deteccao_id else None},
    )
    await db.delete(a)
    await publicar("alerta_excluido", {"codigo": codigo})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
