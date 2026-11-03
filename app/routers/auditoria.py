"""Consulta e verificação da trilha de auditoria.

A trilha não tem rota de escrita nem de exclusão — só a aplicação grava, por
`audit.registrar()`, dentro da mesma transação da operação auditada.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige
from app.models import Auditoria, Usuario
from app.schemas import AuditoriaOut, Pagina, VerificacaoCadeia
from app.services import relatorio

router = APIRouter(prefix="/auditoria", tags=["Auditoria"])

# Quantos registros cabem num documento. Veja a justificativa na rota de
# exportação: é teto de espera do operador, não de capacidade do servidor.
MAX_LINHAS_PDF = 5_000


def _filtros_trilha(q: Optional[str], acao: Optional[str],
                    usuario_email: Optional[str],
                    desde: Optional[datetime], ate: Optional[datetime]) -> list:
    """Os filtros da consulta, em um lugar só.

    Listagem e exportação precisam concordar: a tela mostra o resultado de um
    filtro e o botão de exportar fica ao lado dele. Enquanto cada rota montava
    o seu, exportar de uma tela filtrada por LOGIN entregava a trilha inteira
    — e quem recebesse o arquivo leria um recorte que ninguém pediu.
    """
    filtros = []
    if q:
        termo = f"%{q.strip()}%"
        filtros.append(or_(
            Auditoria.usuario_nome.ilike(termo), Auditoria.usuario_email.ilike(termo),
            Auditoria.acao.ilike(termo), Auditoria.descricao.ilike(termo),
            Auditoria.alvo.ilike(termo), Auditoria.ip.ilike(termo),
        ))
    if acao:
        filtros.append(Auditoria.acao == acao)
    if usuario_email:
        filtros.append(Auditoria.usuario_email == usuario_email.lower())
    if desde:
        filtros.append(Auditoria.ocorrido_em >= desde)
    if ate:
        filtros.append(Auditoria.ocorrido_em <= ate)
    return filtros


def _descrever_filtro(q, acao, usuario_email, desde, ate) -> str:
    """Em palavras, o que entrou no arquivo — para a linha da própria trilha.

    Sem isto, a trilha registra "Exportação da trilha (87 linhas)" e não há
    como saber, depois, quais 87. Em corregedoria essa é exatamente a pergunta
    que se faz do arquivo recebido.
    """
    partes = []
    if acao:
        partes.append(f"ação={acao}")
    if q:
        partes.append(f"busca={q.strip()!r}")
    if usuario_email:
        partes.append(f"usuário={usuario_email.lower()}")
    if desde:
        partes.append(f"de {desde:%d/%m/%Y %H:%M}")
    if ate:
        partes.append(f"até {ate:%d/%m/%Y %H:%M}")
    return "; ".join(partes) if partes else "trilha completa, sem filtro"


@router.get("", response_model=Pagina, summary="Consultar trilha")
async def listar(
    q: Optional[str] = None,
    acao: Optional[str] = None,
    usuario_email: Optional[str] = None,
    desde: Optional[datetime] = None,
    ate: Optional[datetime] = None,
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("audit")),
):
    filtros = _filtros_trilha(q, acao, usuario_email, desde, ate)

    total = (await db.execute(
        select(func.count()).select_from(Auditoria).where(*filtros)
    )).scalar_one()
    linhas = (
        await db.execute(
            select(Auditoria).where(*filtros).order_by(Auditoria.id.desc())
            .offset((pagina - 1) * por_pagina).limit(por_pagina)
        )
    ).scalars().all()

    return Pagina(total=total, pagina=pagina, por_pagina=por_pagina,
                  itens=[AuditoriaOut.model_validate(a) for a in linhas])


@router.get("/acoes", summary="Ações distintas registradas")
async def acoes(db: AsyncSession = Depends(get_db), usuario: Usuario = Depends(exige("audit"))):
    linhas = (
        await db.execute(select(Auditoria.acao, func.count())
                         .group_by(Auditoria.acao).order_by(func.count().desc()))
    ).all()
    return [{"acao": a, "total": n} for a, n in linhas]


@router.get("/verificar", response_model=VerificacaoCadeia,
            summary="Verificar integridade do encadeamento")
async def verificar(limite: int = Query(10000, ge=1, le=200000),
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("audit"))):
    """Recalcula toda a cadeia de hashes e aponta a primeira divergência.

    Rode periodicamente por cron e alarme em caso de falha — é o que dá valor
    probatório à trilha.
    """
    return await audit.verificar_cadeia(db, limite)


def _periodo_por_extenso(desde: Optional[datetime], ate: Optional[datetime],
                         linhas: list, fuso) -> str:
    """O intervalo que o documento cobre de fato.

    Quando ninguém filtrou por data, o período não é "desde sempre": é o que as
    linhas dizem. Imprimir "todo o período" numa capa de documento entregue a
    terceiro deixa o leitor sem saber o que tem em mãos.
    """
    if desde or ate:
        de = f"{desde.astimezone(fuso):%d/%m/%Y %H:%M}" if desde else "o início da trilha"
        até = f"{ate.astimezone(fuso):%d/%m/%Y %H:%M}" if ate else "o último registro"
        return f"{de} até {até}"
    if not linhas:
        return "sem registros"
    def local(d):
        return d.astimezone(fuso) if d.tzinfo else d
    return (f"{local(linhas[0].ocorrido_em):%d/%m/%Y %H:%M}"
            f" até {local(linhas[-1].ocorrido_em):%d/%m/%Y %H:%M}")


@router.get("/exportar", summary="Exportar trilha em PDF")
async def exportar(request: Request,
                   q: Optional[str] = None,
                   acao: Optional[str] = None,
                   usuario_email: Optional[str] = None,
                   desde: Optional[datetime] = None,
                   ate: Optional[datetime] = None,
                   finalidade: str = Query(..., min_length=8,
                                           description="Justificativa — vai para a própria trilha"),
                   db: AsyncSession = Depends(get_db),
                   usuario: Usuario = Depends(exige("audit", "export"))):
    filtros = _filtros_trilha(q, acao, usuario_email, desde, ate)

    # Montar o documento custa cerca de sete segundos a cada mil e trezentas
    # linhas, e o navegador desiste da requisição em quarenta e cinco. Sem o
    # teto, uma trilha de um ano produziria uma espera que termina em erro de
    # rede — e, se terminasse, entregaria um PDF de mil páginas que ninguém lê.
    # Recusar na entrada, dizendo como estreitar, é mais útil que a espera.
    total = (await db.execute(
        select(func.count()).select_from(Auditoria).where(*filtros)
    )).scalar_one()
    if total > MAX_LINHAS_PDF:
        raise HTTPException(
            status_code=413,
            detail=(
                f"O recorte tem {total:,} registros e o documento comporta "
                f"{MAX_LINHAS_PDF:,}. Estreite por período, ação ou usuário "
                "antes de exportar."
            ).replace(",", "."),
        )

    linhas = (
        await db.execute(select(Auditoria).where(*filtros).order_by(Auditoria.id))
    ).scalars().all()

    # O selo da capa. Recalcular a cadeia aqui custa uma varredura, mas é o que
    # dá ao documento valor de prova: sem ele o PDF afirma um conteúdo sem
    # dizer se o conteúdo ainda corresponde aos hashes gravados.
    verificacao = await audit.verificar_cadeia(db)

    fuso = datetime.now().astimezone().tzinfo
    agora = datetime.now(fuso)
    recorte = _descrever_filtro(q, acao, usuario_email, desde, ate)
    meta = {
        "periodo": _periodo_por_extenso(desde, ate, list(linhas), fuso),
        "recorte": recorte,
        "total": len(linhas),
        "finalidade": finalidade,
        "operador": f"{usuario.nome} · {usuario.matricula} · {usuario.perfil.value}",
        "quando": f"{agora:%d/%m/%Y %H:%M:%S}",
        "integra": verificacao.integra,
        "mensagem": verificacao.mensagem,
        "hash_final": linhas[-1].hash_atual if linhas else "",
    }

    # Montar o PDF é trabalho de CPU e o reportlab é síncrono. Numa rota async
    # ele seguraria o event loop pelo tempo todo da geração — e este processo
    # também roda a inferência das 24 câmeras. Vai para uma thread.
    pdf = await run_in_threadpool(relatorio.gerar_pdf, list(linhas), meta, fuso)

    # A própria exportação é auditada, com o recorte que foi levado.
    await audit.registrar(db, acao="EXPORTACAO",
                          descricao=f"Exportação da trilha em PDF ({len(linhas)} linhas) — {recorte}",
                          severidade="warn", usuario=usuario,
                          alvo=f"{len(linhas)} registro(s)", finalidade=finalidade,
                          request=request)

    nome = f"trilha_auditoria_{agora:%Y%m%d_%H%M%S}.pdf"
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )
