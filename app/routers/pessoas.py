"""Base biométrica: cadastro, consulta e amostras faciais.

O identificador é emitido aqui e em nenhum outro lugar. Veja
`app/services/biometrics.py` para a regra completa.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi.responses import Response

from app import audit
from app.database import get_db
from app.deps import exige, pode_ver_pii, usuario_atual, usuario_de_query
from app.models import (
    AmostraFacial, Deteccao, Pessoa, Sexo, StatusPessoa, TipoDeteccao, Usuario,
)
from app.schemas import MatchOut, Pagina, PessoaCreate, PessoaOut, PessoaUpdate
from app.security import mascarar_cpf, mascarar_rg
from app.services import evidencias
from app.services import biometrics, face_engine

router = APIRouter(prefix="/pessoas", tags=["Base biométrica"])


def _saida(p: Pessoa, usuario: Usuario) -> PessoaOut:
    dados = PessoaOut.model_validate(p)
    if not pode_ver_pii(usuario):
        dados.cpf = mascarar_cpf(p.cpf)
        dados.rg = mascarar_rg(p.rg)
    return dados


async def _ler_imagem(arquivo: UploadFile) -> np.ndarray:
    import cv2

    conteudo = await arquivo.read()
    if not conteudo:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Arquivo de imagem vazio.")
    quadro = cv2.imdecode(np.frombuffer(conteudo, np.uint8), cv2.IMREAD_COLOR)
    if quadro is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Não foi possível decodificar a imagem.")
    return quadro


# ======================================================================
# Esquema de identificadores
# ======================================================================
@router.get("/esquema-ids", summary="Contadores e próximo ID de cada prefixo")
async def esquema_ids(db: AsyncSession = Depends(get_db),
                      usuario: Usuario = Depends(exige("biometrics"))):
    return {
        "prefixos": await biometrics.contagem_por_sexo(db),
        "regra": (
            "O identificador é emitido uma única vez, no cadastro, a partir do sexo "
            "informado no registro civil. O motor de reconhecimento não classifica "
            "sexo a partir da imagem — ele apenas devolve o ID já existente. "
            "Corrigir o sexo de um cadastro preserva o identificador original."
        ),
    }


@router.get("/proximo-id", summary="Prever o ID que será emitido")
async def proximo_id(sexo: Sexo, db: AsyncSession = Depends(get_db),
                     usuario: Usuario = Depends(exige("biometrics"))):
    return {
        "sexo": sexo.value,
        "rotulo": biometrics.ROTULO_SEXO[sexo],
        "proximo_id": await biometrics.proximo_id_previsto(db, sexo),
    }


# ======================================================================
# CRUD
# ======================================================================
@router.get("", response_model=Pagina, summary="Listar registros biométricos")
async def listar(
    q: Optional[str] = Query(None, description="Nome, ID biométrico, CPF ou mandado"),
    sexo: Optional[Sexo] = None,
    status_pessoa: Optional[StatusPessoa] = Query(None, alias="status"),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics")),
):
    filtros = [Pessoa.ativo.is_(True)]
    if q:
        termo = f"%{q.strip()}%"
        filtros.append(or_(
            Pessoa.nome.ilike(termo),
            Pessoa.id_biometrico.ilike(termo),
            Pessoa.cpf.ilike(termo),
            Pessoa.mandado.ilike(termo),
            Pessoa.alcunha.ilike(termo),
        ))
    if sexo:
        filtros.append(Pessoa.sexo == sexo)
    if status_pessoa:
        filtros.append(Pessoa.status == status_pessoa)

    total = (await db.execute(select(func.count()).select_from(Pessoa).where(*filtros))).scalar_one()
    linhas = (
        await db.execute(
            select(Pessoa).where(*filtros)
            .order_by(Pessoa.ultima_deteccao.desc().nullslast(), Pessoa.criado_em.desc())
            .offset((pagina - 1) * por_pagina).limit(por_pagina)
        )
    ).scalars().all()

    return Pagina(total=total, pagina=pagina, por_pagina=por_pagina,
                  itens=[_saida(p, usuario) for p in linhas])


@router.get("/{id_biometrico}", response_model=PessoaOut, summary="Abrir ficha biométrica")
async def obter(id_biometrico: str, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("biometrics"))):
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")

    # Toda leitura de dado biométrico é rastreada — exigência de auditoria.
    await audit.registrar(
        db, acao="CONSULTA_BIO", descricao="Consulta a registro biométrico",
        severidade="info", usuario=usuario, alvo=pessoa.id_biometrico,
        finalidade="Consulta operacional", request=request,
    )
    return _saida(pessoa, usuario)


@router.post("", response_model=PessoaOut, status_code=status.HTTP_201_CREATED,
             summary="Cadastrar pessoa e emitir identificador")
async def criar(dados: PessoaCreate, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("biometrics", "edit"))):
    if dados.status == StatusPessoa.PROCURADO and not dados.mandado.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Situação 'PROCURADO' exige o número do mandado judicial.")

    # Aviso de homônimo — não bloqueia, mas fica registrado.
    homonimo = (
        await db.execute(select(Pessoa).where(func.lower(Pessoa.nome) == dados.nome.strip().lower()))
    ).scalar_one_or_none()

    id_bio = await biometrics.emitir_id_biometrico(db, dados.sexo)

    pessoa = Pessoa(
        id_biometrico=id_bio,
        sexo=dados.sexo,
        sexo_emissao=dados.sexo,      # congela o prefixo emitido
        nome=dados.nome.strip(),
        alcunha=dados.alcunha,
        nascimento=dados.nascimento,
        cpf=dados.cpf,
        rg=dados.rg,
        status=dados.status,
        mandado=dados.mandado,
        tipificacao=dados.tipificacao,
        orgao_expedidor=dados.orgao_expedidor,
        unidade=dados.unidade,
        base_legal=dados.base_legal,
        observacoes=dados.observacoes,
        cadastrado_por=usuario.id,
    )
    db.add(pessoa)
    await db.flush()

    await audit.registrar(
        db, acao="PESSOA_ADD", descricao="Registro biométrico criado e identificador emitido",
        severidade="ok", usuario=usuario, alvo=id_bio,
        finalidade=f"Cadastro — {dados.status.value}", request=request,
        metadados={
            "sexo_registro_civil": dados.sexo.value,
            "prefixo": biometrics.PREFIXO[dados.sexo],
            "homonimo_existente": homonimo.id_biometrico if homonimo else None,
        },
    )
    return _saida(pessoa, usuario)


@router.patch("/{id_biometrico}", response_model=PessoaOut, summary="Editar cadastro")
async def atualizar(id_biometrico: str, dados: PessoaUpdate, request: Request,
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("biometrics", "edit"))):
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")

    campos = dados.model_dump(exclude_unset=True)
    resumo_sexo = None

    # O sexo é tratado à parte: corrigir NÃO reemite o identificador.
    if "sexo" in campos:
        resumo_sexo = await biometrics.corrigir_sexo(db, pessoa, campos.pop("sexo"))

    novo_status = campos.get("status", pessoa.status)
    mandado = campos.get("mandado", pessoa.mandado)
    if novo_status == StatusPessoa.PROCURADO and not (mandado or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Situação 'PROCURADO' exige o número do mandado judicial.")

    for campo, valor in campos.items():
        setattr(pessoa, campo, valor)

    await audit.registrar(
        db, acao="PESSOA_EDIT", descricao="Registro biométrico alterado", severidade="warn",
        usuario=usuario, alvo=pessoa.id_biometrico, finalidade="Manutenção cadastral",
        request=request,
        metadados={"campos": sorted(campos.keys()), "correcao_sexo": resumo_sexo},
    )
    return _saida(pessoa, usuario)


@router.delete("/{id_biometrico}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Desativar registro")
async def desativar(id_biometrico: str, request: Request, motivo: str = Query(..., min_length=8),
                    db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("biometrics", "edit"))):
    """Exclusão lógica: o identificador não é reutilizado, e o histórico de
    capturas permanece íntegro. O template biométrico é apagado."""
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")

    pessoa.ativo = False
    pessoa.embedding = None          # deixa de participar de buscas 1:N
    for a in pessoa.amostras_rel:
        await db.delete(a)
    pessoa.amostras = 0

    await audit.registrar(
        db, acao="PESSOA_DEL", descricao="Registro biométrico desativado e template removido",
        severidade="warn", usuario=usuario, alvo=pessoa.id_biometrico,
        finalidade=motivo, request=request,
    )


# ======================================================================
# Amostras faciais
# ======================================================================
@router.post("/{id_biometrico}/amostras", summary="Adicionar amostra facial")
async def adicionar_amostra(
    id_biometrico: str, request: Request, imagem: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics", "edit")),
):
    """Extrai o template da imagem e descarta a fotografia.

    Só o vetor 512-D é persistido — não guardamos o arquivo original.
    """
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")

    quadro = await _ler_imagem(imagem)
    faces = face_engine.extrair_faces(quadro)

    if not faces:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Nenhum rosto com qualidade suficiente foi encontrado na imagem.")
    if len(faces) > 1:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"{len(faces)} rostos na imagem. Envie um recorte com uma pessoa apenas.")

    face = faces[0]
    db.add(AmostraFacial(
        pessoa_id=pessoa.id, embedding=face.embedding.tolist(),
        qualidade=face.qualidade, origem="cadastro",
    ))
    await db.flush()
    await face_engine.recalcular_template(db, pessoa)

    await audit.registrar(
        db, acao="BIO_AMOSTRA", descricao="Amostra facial adicionada ao template",
        severidade="warn", usuario=usuario, alvo=pessoa.id_biometrico,
        finalidade="Cadastro biométrico", request=request,
        metadados={"qualidade": face.qualidade, "amostras_totais": pessoa.amostras},
    )
    return {
        "id_biometrico": pessoa.id_biometrico,
        "qualidade_amostra": face.qualidade,
        "qualidade_template": pessoa.qualidade,
        "amostras": pessoa.amostras,
        "observacao": "A fotografia não foi armazenada; apenas o template vetorial.",
    }


@router.delete("/{id_biometrico}/amostras/{amostra_id}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remover amostra")
async def remover_amostra(id_biometrico: str, amostra_id: uuid.UUID, request: Request,
                          db: AsyncSession = Depends(get_db),
                          usuario: Usuario = Depends(exige("biometrics", "edit"))):
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro não encontrado.")
    amostra = await db.get(AmostraFacial, amostra_id)
    if amostra is None or amostra.pessoa_id != pessoa.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Amostra não encontrada.")

    await db.delete(amostra)
    await db.flush()
    await face_engine.recalcular_template(db, pessoa)

    await audit.registrar(db, acao="BIO_AMOSTRA", descricao="Amostra facial removida",
                          severidade="warn", usuario=usuario, alvo=pessoa.id_biometrico,
                          request=request)


# ======================================================================
# Busca 1:N
# ======================================================================
@router.post("/buscar", response_model=MatchOut, summary="Identificar rosto em imagem (1:N)")
async def buscar(
    request: Request, imagem: UploadFile = File(...),
    limiar: Optional[float] = Query(None, ge=0.5, le=1.0),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics")),
):
    """Compara o rosto com a base e devolve o vizinho mais próximo.

    Abaixo do limiar a resposta é `match=False` e `pessoa=None`: a API **não**
    devolve um palpite, e nenhuma identidade é atribuída à captura.
    """
    quadro = await _ler_imagem(imagem)
    faces = face_engine.extrair_faces(quadro)
    if not faces:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Nenhum rosto com qualidade suficiente na imagem.")

    face = max(faces, key=lambda f: f.qualidade)
    resultado = await face_engine.buscar_pessoa(db, face.embedding, limiar)

    pessoa = None
    if resultado.match:
        pessoa = (
            await db.execute(select(Pessoa).where(Pessoa.id == uuid.UUID(resultado.pessoa_id)))
        ).scalar_one_or_none()

    await audit.registrar(
        db, acao="CONSULTA_BIO", descricao="Busca biométrica 1:N a partir de imagem",
        severidade="warn", usuario=usuario,
        alvo=resultado.id_biometrico or "sem correspondência",
        finalidade="Identificação sob demanda", request=request,
        metadados={"similaridade": round(resultado.similaridade, 4),
                   "limiar": resultado.limiar,
                   "qualidade_face": face.qualidade},
    )

    return MatchOut(
        pessoa=_saida(pessoa, usuario) if pessoa else None,
        similaridade=round(resultado.similaridade, 4),
        limiar=resultado.limiar,
        match=resultado.match,
        qualidade_face=face.qualidade,
    )


@router.post("/candidatos", summary="Top-k candidatos para conferência humana")
async def candidatos(
    request: Request, imagem: UploadFile = File(...), k: int = Query(5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics")),
):
    """Lista ordenada para revisão por operador. Não deve alimentar despacho
    automático: decisão sobre pessoa exige confirmação humana."""
    quadro = await _ler_imagem(imagem)
    faces = face_engine.extrair_faces(quadro)
    if not faces:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Nenhum rosto detectado.")

    face = max(faces, key=lambda f: f.qualidade)
    lista = await face_engine.buscar_candidatos(db, face.embedding, k)

    await audit.registrar(
        db, acao="CONSULTA_BIO", descricao=f"Consulta de {k} candidatos para conferência",
        severidade="warn", usuario=usuario, alvo="busca top-k",
        finalidade="Conferência humana", request=request,
    )
    return {"qualidade_face": face.qualidade, "candidatos": lista}


@router.get("/{id_biometrico}/capturas", summary="Histórico de capturas da pessoa")
async def capturas(id_biometrico: str, request: Request, limite: int = Query(50, ge=1, le=500),
                   db: AsyncSession = Depends(get_db),
                   usuario: Usuario = Depends(exige("biometrics"))):
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro não encontrado.")

    linhas = (
        await db.execute(
            select(Deteccao).where(Deteccao.pessoa_id == pessoa.id)
            .order_by(Deteccao.ocorrido_em.desc()).limit(limite)
        )
    ).scalars().all()

    await audit.registrar(db, acao="CONSULTA_BIO", descricao="Consulta ao histórico de capturas",
                          severidade="info", usuario=usuario, alvo=pessoa.id_biometrico,
                          request=request)

    return [
        {
            "codigo": d.codigo,
            "ocorrido_em": d.ocorrido_em,
            "camera": d.camera.nome if d.camera else "",
            "camera_codigo": d.camera.codigo if d.camera else "",
            "similaridade": d.similaridade,
            "snapshot_key": d.snapshot_key,
            "revisado": d.revisado,
            "veredito": d.veredito,
        }
        for d in linhas
    ]


@router.get("/{id_biometrico}/face", summary="Rosto da pessoa, da captura mais recente")
async def face(id_biometrico: str, token: str = Query(..., description="Access token JWT"),
               db: AsyncSession = Depends(get_db)):
    """O rosto que a ficha mostra, para quem revisa reconhecer de quem se trata.

    É o recorte de uma captura, não uma fotografia de cadastro: a foto enviada
    no cadastro é descartada assim que o template sai dela, e continua sendo.
    Guardá-la mudaria o que um vazamento desta base expõe — de vetores, que
    não reconstroem ninguém, para um álbum de pessoas monitoradas.

    Quem ainda não foi capturado não tem rosto aqui, e a ficha fica com o
    retrato do template, que não finge ser fotografia de alguém.

    Não gera linha de auditoria, pela mesma razão que o recorte da captura não
    gera: a imagem é a mesma, e as listas trazem uma por registro — auditar
    cada miniatura encheria a trilha de ruído paralelo ao acesso que importa,
    que é abrir a ficha.
    """
    await usuario_de_query(token, db, "facial")

    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro não encontrado.")

    det = (
        await db.execute(
            select(Deteccao)
            .where(Deteccao.pessoa_id == pessoa.id,
                   Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.snapshot_key != "")
            .order_by(Deteccao.ocorrido_em.desc())
            .limit(1)
        )
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sem captura guardada desta pessoa.")

    imagem = evidencias.ler(det.snapshot_key)
    if imagem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recorte já expurgado.")

    return Response(content=imagem, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})
