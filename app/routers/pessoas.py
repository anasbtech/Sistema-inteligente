"""Base biométrica: cadastro, consulta e amostras faciais.

O identificador é emitido aqui e em nenhum outro lugar. Veja
`app/services/biometrics.py` para a regra completa.
"""
from __future__ import annotations

import asyncio

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import numpy as np
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi.responses import Response

from app import audit
from app.database import get_db
from app.deps import exige, pode_ver_pii, usuario_atual, usuario_de_query
from app.models import (
    AmostraFacial, Camera, Deteccao, Pessoa, Sexo, StatusPessoa, TipoDeteccao,
    Usuario,
)
from app.schemas import MatchOut, Pagina, PessoaCreate, PessoaOut, PessoaUpdate
from app.security import mascarar_cpf, mascarar_rg
from app.config import settings
from app.services import evidencias
from app.services import biometrics, face_engine
from app.tipos import como_utc

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
    # Quantos rostos esperam triagem. Vai junto porque sem isso o painel
    # anuncia "BIO-N: 0 registros" com milhares de desconhecidos na base, e
    # quem lê conclui que não há ninguém por identificar — quando o que há é
    # ninguém triado ainda.
    aguardando = (
        await db.execute(
            select(func.count()).select_from(Deteccao)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.pessoa_id.is_(None),
                   Deteccao.pessoa_sugerida_id.is_(None),
                   Deteccao.snapshot_key != "")
        )
    ).scalar_one()
    return {
        "prefixos": await biometrics.contagem_por_sexo(db),
        "aguardando_triagem": int(aguardando),
        "regra": (
            "O identificador é emitido uma única vez, no cadastro, a partir do sexo "
            "informado no registro civil. O motor de reconhecimento não classifica "
            "sexo a partir da imagem — ele apenas devolve o ID já existente. "
            "Corrigir o sexo de um cadastro preserva o identificador original, "
            "exceto na saída de BIO-N: quem entrou sem identificação recebe o "
            "prefixo definitivo ao ser cadastrado."
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
    q: Optional[str] = Query(None, description="Nome, ID biométrico, CPF ou restrição"),
    sexo: Optional[Sexo] = None,
    status_pessoa: Optional[StatusPessoa] = Query(None, alias="status"),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics")),
):
    filtros = [Pessoa.ativo.is_(True)]
    # A lista padrão é de quem tem cadastro: BIO-N é registro provisório de
    # quem ainda não foi identificado. Pedidos explicitamente (sexo=N), eles
    # aparecem — antes ficavam de fora até assim, e o cartão BIO-N da base,
    # que filtra por eles, abria uma tabela vazia com BIO-N existentes.
    if sexo != Sexo.N:
        filtros.append(Pessoa.sexo != Sexo.N)
    if q:
        termo = f"%{q.strip()}%"
        filtros.append(or_(
            Pessoa.nome.ilike(termo),
            Pessoa.id_biometrico.ilike(termo),
            Pessoa.cpf.ilike(termo),
            Pessoa.restricao.ilike(termo),
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


@router.get("/conferencia", summary="Fila de quase-acertos para conferência humana")
async def conferencia(
    request: Request,
    limite: int = Query(60, ge=1, le=300),
    por_angulo: int = Query(3, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics")),
):
    """Capturas que pararam perto do limiar, com o candidato que o motor apontou.

    Existe para romper um nó: uma pessoa cadastrada numa câmera não é
    reconhecida em outra, e sem ser reconhecida ali nunca gera a amostra
    daquele ângulo que a faria ser reconhecida. Alguém precisa dizer "é ela"
    uma vez; daí em diante a câmera passa a responder sozinha.

    A ordem não é por similaridade. Medido nesta base, a faixa 0,38–0,50 tem
    2.601 capturas, e 2.576 delas estão nas três câmeras que já reconhecem —
    confirmá-las não acrescenta ângulo nenhum. As 25 restantes, espalhadas
    por oito câmeras, são as que abrem câmera nova. Por isso vem primeiro o
    que está numa câmera que o template ainda não cobre: ordenar por
    similaridade poria 2.576 itens inúteis na frente dos 25 que importam.

    E agrupa por par (pessoa, câmera): cinquenta capturas do mesmo ângulo são
    uma decisão só, não cinquenta. O que se quer da fila é um ângulo novo por
    vez, com algumas opções para o operador escolher a melhor.
    """
    linhas = (
        await db.execute(
            select(Deteccao, Pessoa, Camera)
            .join(Pessoa, Pessoa.id == Deteccao.pessoa_sugerida_id)
            .join(Camera, Camera.id == Deteccao.camera_id)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.pessoa_id.is_(None),
                   Deteccao.pessoa_sugerida_id.isnot(None),
                   # A faixa também aqui, e não só em quem grava: o que está
                   # longe do limiar não é quase-acerto, e pôr isso diante do
                   # operador é pedir que ele julgue um rosto que o próprio
                   # motor não considera parecido.
                   Deteccao.similaridade >= settings.FACE_REVIEW_MIN,
                   Deteccao.snapshot_key != "",
                   Pessoa.ativo.is_(True))
            .order_by(Deteccao.similaridade.desc())
            .limit(max(limite * 20, 600))
        )
    ).all()

    # Coberto = confirmar ali não ensina nada novo. Dois sinais, somados:
    #
    # * a pessoa já tem amostra daquela câmera — exato, mas vazio para as
    #   amostras anteriores a esta coluna, que não registravam a origem;
    # * a pessoa já foi identificada ali alguma vez — derivável do histórico
    #   inteiro, e é o que mantém a fila ordenada com sentido enquanto as
    #   amostras antigas seguem sem origem conhecida.
    #
    # Sozinho, o primeiro faria todas as câmeras parecerem descobertas num
    # banco já povoado, e a fila nasceria sem priorização nenhuma.
    cobertos = {
        (pid, cid)
        for pid, cid in (
            await db.execute(
                select(AmostraFacial.pessoa_id, AmostraFacial.camera_id)
                .where(AmostraFacial.camera_id.isnot(None))
            )
        ).all()
    }
    cobertos |= {
        (pid, cid)
        for pid, cid in (
            await db.execute(
                select(Deteccao.pessoa_id, Deteccao.camera_id)
                .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                       Deteccao.pessoa_id.isnot(None))
                .group_by(Deteccao.pessoa_id, Deteccao.camera_id)
            )
        ).all()
    }

    itens, vistos = [], {}
    for det, pes, cam in linhas:
        par = (pes.id, cam.id)
        if vistos.get(par, 0) >= por_angulo:
            continue
        vistos[par] = vistos.get(par, 0) + 1
        itens.append({
            "codigo": det.codigo,
            "ocorrido_em": det.ocorrido_em,
            "similaridade": det.similaridade,
            "limiar": det.limiar_aplicado,
            "qualidade_face": det.qualidade_face,
            "snapshot_key": det.snapshot_key,
            "camera_codigo": cam.codigo,
            "camera": cam.nome,
            "setor": cam.setor,
            # O que faz este item valer a atenção: numa câmera já coberta a
            # confirmação não abre nada, porque aquele ângulo já responde.
            "camera_coberta": par in cobertos,
            "sugerido": {
                "id_biometrico": pes.id_biometrico,
                "nome": pes.nome,
                "status": pes.status.value,
            },
        })

    itens.sort(key=lambda i: (i["camera_coberta"], -(i["similaridade"] or 0)))
    itens = itens[:limite]

    await audit.registrar(
        db, acao="CONSULTA_BIO", descricao="Consulta à fila de conferência biométrica",
        severidade="info", usuario=usuario, alvo="fila de conferência", request=request)

    return {
        "itens": itens,
        "faixa": {"de": settings.FACE_REVIEW_MIN, "ate": settings.FACE_THRESHOLD},
        "abre_camera_nova": sum(1 for i in itens if not i["camera_coberta"]),
    }


@router.get("/desconhecidos", summary="Rostos sem candidato algum na base")
async def desconhecidos(
    request: Request,
    limite: int = Query(60, ge=1, le=300),
    horas: int = Query(24, ge=1, le=720),
    agrupar: bool = Query(True),
    limiar: float = Query(0.45, ge=0.0, le=1.0),
    min_amostras: int = Query(3, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics")),
):
    """Capturas que não se parecem com ninguém — nem o bastante para conferir.

    Com `agrupar=true` (padrão), retorna grupos de capturas que provavelmente
    são a mesma pessoa, ordenados por tamanho. Sem isto, retorna capturas
    individuais para triagem manual tradicional.

    O agrupamento usa o mesmo algoritmo do lote: ligação por semelhança.
    Permite que o operador confirme grupos em vez de marcar capturas uma a uma.
    """
    desde = datetime.now(timezone.utc) - timedelta(hours=horas)
    linhas = (
        await db.execute(
            select(Deteccao, Camera)
            .join(Camera, Camera.id == Deteccao.camera_id)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.pessoa_id.is_(None),
                   Deteccao.pessoa_sugerida_id.is_(None),
                   Deteccao.snapshot_key != "",
                   Deteccao.ocorrido_em >= desde)
            .order_by(Deteccao.ocorrido_em.desc())
            .limit(limite)
        )
    ).all()

    await audit.registrar(
        db, acao="CONSULTA_BIO",
        descricao=f"Fila de rostos sem cadastro consultada (agrupar={agrupar})",
        severidade="info", usuario=usuario, alvo="-", request=request)

    # Sem agrupamento: retorna capturas individuais.
    if not agrupar or not linhas:
        return {
            "janela_horas": horas,
            "agrupado": False,
            "itens": [
                {
                    "codigo": d.codigo,
                    "ocorrido_em": d.ocorrido_em,
                    "camera": cam.nome,
                    "camera_codigo": cam.codigo,
                    "setor": cam.setor,
                    "qualidade_face": d.qualidade_face,
                    "snapshot_key": d.snapshot_key,
                }
                for d, cam in linhas
            ],
        }

    # Com agrupamento: extrai embeddings e forma grupos por ligação.
    import numpy as np

    # Fora da linha principal do servidor: reanalisar 60 rostos leva dezenas
    # de segundos com a máquina ocupada (medido: 46 s), e na linha principal
    # isso parava o sistema inteiro para todo mundo enquanto durasse.
    def _extrair():
        saida = []
        for d, cam in linhas:
            jpeg = evidencias.ler(d.snapshot_key)
            face = face_engine.extrair_de_recorte(jpeg) if jpeg is not None else None
            if face is not None:
                saida.append((d, cam, np.asarray(face.embedding, dtype=np.float32)))
        return saida

    vetores, deteccoes_map = [], {}
    for d, cam, vetor in await asyncio.to_thread(_extrair):
        deteccoes_map[len(vetores)] = (d, cam)
        vetores.append(vetor)

    if not vetores:
        return {"janela_horas": horas, "agrupado": True, "grupos": []}

    # Agrupamento por ligação — mesmo algoritmo do script lote.
    matriz = np.vstack([v / np.linalg.norm(v) for v in vetores])
    semelhantes = np.argwhere(np.triu(matriz @ matriz.T >= limiar, k=1))
    pai = list(range(len(matriz)))

    def raiz(i: int) -> int:
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    for i, j in semelhantes:
        a, b = raiz(int(i)), raiz(int(j))
        if a != b:
            pai[a] = b

    grupos_dict: dict[int, list[int]] = {}
    for i in range(len(matriz)):
        r = raiz(i)
        if r not in grupos_dict:
            grupos_dict[r] = []
        grupos_dict[r].append(i)

    # Ordena por tamanho decrescente e filtra por tamanho mínimo.
    grupos_ordenados = sorted(
        (g for g in grupos_dict.values() if len(g) >= min_amostras),
        key=len, reverse=True)

    return {
        "janela_horas": horas,
        "agrupado": True,
        "limiar": limiar,
        "min_amostras": min_amostras,
        "grupos": [
            {
                "tamanho": len(indices),
                "codigos": [deteccoes_map[i][0].codigo for i in indices if i in deteccoes_map],
                "capturas": [
                    {
                        "codigo": deteccoes_map[i][0].codigo,
                        "ocorrido_em": deteccoes_map[i][0].ocorrido_em,
                        "camera": deteccoes_map[i][1].nome,
                        "camera_codigo": deteccoes_map[i][1].codigo,
                        "setor": deteccoes_map[i][1].setor,
                        "qualidade_face": deteccoes_map[i][0].qualidade_face,
                        "snapshot_key": deteccoes_map[i][0].snapshot_key,
                    }
                    for i in indices if i in deteccoes_map
                ],
            }
            for indices in grupos_ordenados
        ],
    }


@router.post("/nao-identificado", status_code=status.HTTP_201_CREATED,
             summary="Abrir BIO-N a partir de capturas de um rosto sem cadastro")
async def abrir_nao_identificado(
    request: Request, codigos: List[str],
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics", "edit")),
):
    """O operador diz "estas capturas são a mesma pessoa, e não sei quem é".

    Nasce um BIO-N com essas capturas como amostras. O prefixo não afirma
    nada sobre a pessoa — diz que ainda não se sabe quem ela é —, e sai dele
    quando alguém completar o cadastro e informar o sexo, quando o
    identificador passa a BIO-M ou BIO-F.

    Pede várias capturas pelo mesmo motivo que o cadastro por captura pede:
    medido nesta base, duas capturas da mesma pessoa só se reconhecem em 12%
    dos pares. Uma amostra sozinha não reconheceria a própria pessoa no
    encontro seguinte, e o registro recém-aberto já nasceria inútil.
    """
    if not codigos:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Informe ao menos uma captura.")

    pessoa = await biometrics.abrir_registro_nao_identificado(db, usuario.id)
    aceitas, recusadas = await _capturas_viram_amostras(db, pessoa, codigos)

    await audit.registrar(
        db, acao="PESSOA_ADD", severidade="warn", usuario=usuario,
        alvo=pessoa.id_biometrico,
        descricao="Registro aberto para rosto sem cadastro, por conferência humana",
        finalidade="Triagem de desconhecidos", request=request,
        metadados={"capturas": aceitas, "recusadas": recusadas,
                   "amostras": pessoa.amostras},
    )
    return {
        "id_biometrico": pessoa.id_biometrico,
        "aceitas": aceitas,
        "recusadas": recusadas,
        "amostras": pessoa.amostras,
        "qualidade_template": pessoa.qualidade,
    }


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
    if dados.status == StatusPessoa.RESTRITO and not dados.restricao.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Situação 'RESTRITO' exige o motivo da restrição.")

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
        restricao=dados.restricao,
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
    restricao = campos.get("restricao", pessoa.restricao)
    if novo_status == StatusPessoa.RESTRITO and not (restricao or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Situação 'RESTRITO' exige o motivo da restrição.")

    for campo, valor in campos.items():
        setattr(pessoa, campo, valor)

    await audit.registrar(
        db, acao="PESSOA_EDIT", descricao="Registro biométrico alterado", severidade="warn",
        usuario=usuario, alvo=pessoa.id_biometrico, finalidade="Manutenção cadastral",
        request=request,
        metadados={"campos": sorted(campos.keys()), "correcao_sexo": resumo_sexo},
    )
    return _saida(pessoa, usuario)


@router.delete("/{id_biometrico}", summary="Excluir registro biométrico")
async def excluir(id_biometrico: str, request: Request, motivo: str = Query(..., min_length=8),
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("biometrics", "edit"))):
    """Apaga a pessoa, suas amostras e o template, em definitivo.

    O que a pessoa deixa para trás não é apagado junto: as capturas e os
    alertas dela voltam a ser "não identificados" e seguem no histórico. O
    registro de que ela passou por ali é da operação, não do cadastro — e a
    trilha de auditoria guarda quem apagou, quando e por quê.

    O identificador volta a ficar livre. Era por isso que a exclusão aqui
    era lógica; quem precisar da garantia de que um BIO-* nunca se repete
    deve desativar em vez de excluir.
    """
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")

    # Contados antes: depois do delete o vínculo já não existe para ser visto,
    # e é isto que a auditoria precisa registrar sobre o alcance da exclusão.
    capturas = (await db.execute(
        select(func.count()).select_from(Deteccao).where(Deteccao.pessoa_id == pessoa.id)
    )).scalar_one()
    nome, bio, amostras = pessoa.nome, pessoa.id_biometrico, pessoa.amostras

    # As amostras saem por CASCADE e as capturas viram órfãs por SET NULL,
    # ambos declarados nas chaves estrangeiras e com PRAGMA foreign_keys=ON.
    await db.delete(pessoa)
    await db.flush()

    await audit.registrar(
        db, acao="PESSOA_DEL", severidade="warn", usuario=usuario, alvo=bio,
        descricao=f"Registro biométrico excluído em definitivo ({nome})",
        finalidade=motivo, request=request,
        metadados={"amostras_removidas": amostras,
                   "capturas_desvinculadas": capturas},
    )
    return {"id_biometrico": bio, "nome": nome,
            "amostras_removidas": amostras, "capturas_desvinculadas": capturas}


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


async def _capturas_viram_amostras(db: AsyncSession, pessoa: Pessoa,
                                   codigos: List[str],
                                   exigir_amostra: bool = True) -> tuple[list, list]:
    """Converte capturas gravadas em amostras do template de `pessoa`.

    O embedding é extraído do recorte em disco, e não reaproveitado do que o
    worker calculou: o recorte é o que o operador viu ao decidir, e é dele que
    a amostra tem de sair para a decisão e o template falarem do mesmo rosto.

    Serve aos dois caminhos da fila — "é ela", que anexa a um cadastro, e "é
    alguém novo", que anexa a um BIO-N recém-aberto. Levanta 422 se nenhuma
    captura render rosto legível, porque template vazio não reconhece nada e
    devolver sucesso aí esconderia a falha de quem conferiu.
    """
    deteccoes = (
        await db.execute(
            select(Deteccao).where(Deteccao.codigo.in_(codigos),
                                   Deteccao.tipo == TipoDeteccao.FACIAL)
        )
    ).scalars().unique().all()
    achadas = {d.codigo for d in deteccoes}
    recusadas = [{"codigo": c, "motivo": "captura não encontrada"}
                 for c in codigos if c not in achadas]

    aceitas = []
    sem_amostra: list = []
    for det in deteccoes:
        # Captura que estava na ficha de outra pessoa e mudou de dono: o rosto
        # dela não pode continuar no template de quem ela não é.
        if det.pessoa_id is not None and det.pessoa_id != pessoa.id:
            await face_engine.remover_amostras_da_captura(db, det.id)
        jpeg = evidencias.ler(det.snapshot_key)
        if jpeg is None:
            recusadas.append({"codigo": det.codigo, "motivo": "recorte não está mais em disco"})
            continue
        face = face_engine.extrair_confirmado(jpeg)
        if face is None:
            if exigir_amostra:
                recusadas.append({"codigo": det.codigo,
                                  "motivo": "nenhum rosto legível no recorte"})
                continue
            # Quem olhou disse que é a pessoa, e a IA não decide contra isso:
            # a captura fica vinculada à ficha mesmo sem virar amostra (o
            # rosto não pôde ser extraído — de costas, encoberto, borrado).
            det.pessoa_id = pessoa.id
            det.similaridade = None
            det.expurgo_em = None
            aceitas.append(det.codigo)
            sem_amostra.append(det.codigo)
            continue
        db.add(AmostraFacial(
            pessoa_id=pessoa.id, embedding=face.embedding.tolist(),
            qualidade=round(float(face.qualidade), 4), origem="captura",
            # De onde veio o ângulo. Sem isto a cobertura não teria como ser
            # calculada, e era justamente a confirmação humana que abria a
            # câmera nova — o registro mais importante de todos para a cota.
            camera_id=det.camera_id,
            deteccao_id=det.id,
        ))
        # A captura passa a apontar para a pessoa: ela deixa de ser uma
        # passagem anônima e some do expurgo curto das não identificadas.
        det.pessoa_id = pessoa.id
        det.similaridade = 1.0
        det.expurgo_em = None
        aceitas.append(det.codigo)

    if not aceitas:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Nenhuma das capturas rendeu um rosto legível: " +
            "; ".join(f"{r['codigo']}: {r['motivo']}" for r in recusadas),
        )

    await db.flush()
    if len(sem_amostra) < len(aceitas):
        await face_engine.recalcular_template(db, pessoa)
    recusadas += [{"codigo": c, "motivo": "vinculada sem amostra: rosto não extraível",
                   "vinculada": True} for c in sem_amostra]
    return aceitas, recusadas


@router.post("/{id_biometrico}/amostras-de-capturas",
             summary="Promover capturas das câmeras a amostras do cadastro")
async def amostras_de_capturas(
    id_biometrico: str, request: Request, codigos: List[str],
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics", "edit")),
):
    """Usa capturas já gravadas como amostras do template.

    Uma foto de documento casa mal com o rosto de 80 pixels que a câmera do
    corredor entrega: ângulo, lente e iluminação são outros. O template que
    reconhece bem é o feito do mesmo material que se vai reconhecer — daí
    cadastrar pela própria captura.

    Quem diz que a captura é daquela pessoa é o operador, olhando o recorte;
    o servidor só recalcula o vetor e registra quem afirmou o quê.
    """
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")
    if not codigos:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Informe ao menos uma captura.")

    # Vincular a uma ficha existente: o operador afirmou quem é, então a
    # captura vincula mesmo que o rosto não vire amostra.
    aceitas, recusadas = await _capturas_viram_amostras(db, pessoa, codigos,
                                                        exigir_amostra=False)

    await audit.registrar(
        db, acao="BIO_AMOSTRA", severidade="warn", usuario=usuario,
        alvo=pessoa.id_biometrico, finalidade="Cadastro biométrico por captura",
        descricao=f"{len(aceitas)} captura(s) vinculada(s) ao template",
        request=request,
        metadados={"capturas": aceitas, "recusadas": recusadas,
                   "amostras_totais": pessoa.amostras},
    )
    return {
        "id_biometrico": pessoa.id_biometrico,
        "aceitas": aceitas,
        "recusadas": recusadas,
        "amostras": pessoa.amostras,
        "qualidade_template": pessoa.qualidade,
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
                   tipo: Optional[str] = Query(
                       None, pattern="^(rosto|corpo)$",
                       description="Filtra o histórico: `rosto` traz as identificações "
                                   "faciais, `corpo` traz os corpos costurados a um rosto "
                                   "reconhecido. Sem o parâmetro, vêm os dois."),
                   db: AsyncSession = Depends(get_db),
                   usuario: Usuario = Depends(exige("biometrics"))):
    """Os registros individuais que a trajetória resume.

    A trajetória agrupa em passagens, que é o que responde "por onde ela
    andou". Aqui é o oposto: um registro por linha, que é o que responde
    "mostre-me os quadros". São usos diferentes, e por isso `tipo` separa os
    dois modos de reconhecer alguém — o rosto, que identifica, e o corpo.

    O corpo é **histórico**: o sistema deixou de costurar identidade a
    caixa de corpo em 26/10/2026, a pedido de quem opera. Quem identifica
    é o rosto. `tipo=corpo` segue lendo o que foi gravado antes disso, e
    não recebe nada novo.
    """
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro não encontrado.")

    filtros = [Deteccao.pessoa_id == pessoa.id]
    if tipo == "rosto":
        filtros.append(Deteccao.tipo == TipoDeteccao.FACIAL)
    elif tipo == "corpo":
        # Só o que foi costurado antes de 26/10/2026. Corpo anônimo nunca
        # teve identidade para constar na ficha de ninguém.
        filtros.append(and_(Deteccao.tipo == TipoDeteccao.OBJETO,
                            Deteccao.classe == "pessoa"))

    linhas = (
        await db.execute(
            select(Deteccao).where(*filtros)
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
            "setor": d.camera.setor if d.camera else "",
            # Qual dos dois reconhecimentos trouxe este registro. Sem isto a
            # lista misturada não diria por que uma linha tem similaridade e
            # a vizinha não.
            "reconhecimento": "rosto" if d.tipo == TipoDeteccao.FACIAL else "corpo",
            "similaridade": d.similaridade,
            "confianca": d.confianca,
            "snapshot_key": d.snapshot_key,
            "revisado": d.revisado,
            "veredito": d.veredito,
        }
        for d in linhas
    ]


@router.get("/{id_biometrico}/trajetoria", summary="Por onde a pessoa passou")
async def trajetoria(
    id_biometrico: str, request: Request,
    desde: Optional[datetime] = Query(None),
    ate: Optional[datetime] = Query(None),
    intervalo_min: int = Query(10, ge=1, le=180),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("biometrics")),
):
    """A trilha da pessoa pelo parque, em passagens e não em capturas.

    Quem fica diante da câmera gera uma captura por volta do worker — nesta
    base, 2.423 numa câmera só. Lidas uma a uma não dizem nada; agrupadas,
    viram "esteve no Service Desk das 09:12 às 09:47". A pergunta "por onde
    ela andou" se responde com passagens, não com capturas.

    Uma passagem termina quando a câmera muda, ou quando o intervalo entre
    duas capturas passa de `intervalo_min`: voltar ao mesmo lugar depois de
    um tempo é outra passagem, não a continuação da anterior.
    """
    pessoa = (
        await db.execute(select(Pessoa).where(Pessoa.id_biometrico == id_biometrico))
    ).scalar_one_or_none()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro não encontrado.")

    # Rosto e corpo, não só rosto. Rosto legível é a exceção: quem está de
    # costas, longe ou de perfil aparece como corpo e mais nada. Com só os
    # rostos, a trajetória some justamente nos trechos em que a pessoa estava
    # andando — que é o que a pergunta "por onde ela passou" quer saber.
    # Os corpos que entram são os que o worker costurou a um rosto
    # identificado no mesmo quadro; corpo anônimo não tem identidade para
    # constar na trajetória de ninguém.
    filtros = [
        Deteccao.pessoa_id == pessoa.id,
        or_(Deteccao.tipo == TipoDeteccao.FACIAL,
            and_(Deteccao.tipo == TipoDeteccao.OBJETO, Deteccao.classe == "pessoa")),
    ]
    if desde is not None:
        filtros.append(Deteccao.ocorrido_em >= desde)
    if ate is not None:
        filtros.append(Deteccao.ocorrido_em <= ate)

    # Só as colunas que a trilha usa. Carregar a detecção inteira trazia junto,
    # por relacionamento, a câmera, a ficha da pessoa e as amostras do rosto
    # dela — para cada uma das ~2.500 capturas de quem passa o dia diante de
    # uma câmera. Medido em 05/11/2026: 6,2 s para abrir a ficha do André.
    linhas = (
        await db.execute(
            select(Deteccao.codigo, Deteccao.tipo, Deteccao.ocorrido_em,
                   Deteccao.similaridade, Deteccao.snapshot_key,
                   Camera.codigo.label("cam_codigo"), Camera.nome.label("cam_nome"),
                   Camera.setor.label("cam_setor"))
            .join(Camera, Camera.id == Deteccao.camera_id)
            .where(*filtros).order_by(Deteccao.ocorrido_em.asc())
        )
    ).all()

    class _Cam:
        __slots__ = ("codigo", "nome", "setor")

        def __init__(self, codigo, nome, setor):
            self.codigo, self.nome, self.setor = codigo, nome, setor

    folga = intervalo_min * 60
    passagens: List[dict] = []
    atual: Optional[dict] = None
    for det in linhas:
        cam = _Cam(det.cam_codigo, det.cam_nome, det.cam_setor)
        quando = como_utc(det.ocorrido_em)
        rosto = det.tipo == TipoDeteccao.FACIAL
        sim = (det.similaridade or 0.0) if rosto else 0.0
        if (atual is None
                or atual["camera_codigo"] != cam.codigo
                or (quando - atual["_fim"]).total_seconds() > folga):
            atual = {
                "camera_codigo": cam.codigo, "camera": cam.nome, "setor": cam.setor,
                "inicio": det.ocorrido_em, "fim": det.ocorrido_em,
                "capturas": 1,
                # Separados porque dizem coisas diferentes sobre a mesma
                # presença: o rosto é identificação, o corpo é a pessoa vista
                # sem o rosto dar para ler. Uma passagem só de corpo ainda
                # vale — alguém esteve ali —, mas quem revisa precisa saber
                # que a confirmação veio de outro quadro.
                "rostos": 1 if rosto else 0,
                "corpos": 0 if rosto else 1,
                "similaridade_max": sim,
                # O melhor recorte da passagem, não o primeiro: é ele que
                # permite conferir a identificação sem abrir captura por
                # captura. Rosto sempre ganha de corpo, por mostrar a cara.
                "codigo": det.codigo, "snapshot_key": det.snapshot_key,
                "_fim": quando, "_melhor": sim, "_tem_rosto": rosto,
            }
            passagens.append(atual)
            continue
        atual["capturas"] += 1
        atual["rostos" if rosto else "corpos"] += 1
        atual["fim"] = det.ocorrido_em
        atual["_fim"] = quando
        if rosto and (not atual["_tem_rosto"] or sim > atual["_melhor"]):
            atual["_melhor"] = sim
            atual["_tem_rosto"] = True
            atual["similaridade_max"] = sim
            atual["codigo"] = det.codigo
            atual["snapshot_key"] = det.snapshot_key

    for p in passagens:
        p["duracao_s"] = int((p.pop("_fim") - como_utc(p["inicio"])).total_seconds())
        p.pop("_melhor"); p.pop("_tem_rosto")
        p["similaridade_max"] = round(p["similaridade_max"], 4)

    await audit.registrar(
        db, acao="CONSULTA_BIO", descricao="Consulta à trajetória da pessoa",
        severidade="info", usuario=usuario, alvo=pessoa.id_biometrico, request=request)

    # Onde esteve, por lugar. Sem isto a lista mente por omissão: medido
    # nesta base, uma pessoa com 903 capturas tem 902 numa câmera e UMA em
    # outra, no meio de 95 passagens ordenadas pela mais recente. O resumo
    # dizia "2 câmeras" e a lista parecia mostrar uma só — a passagem rara,
    # que é justamente a que interessa a quem pergunta por onde a pessoa
    # andou, ficava invisível.
    por_camera: dict = {}
    for p in passagens:
        c = por_camera.setdefault(p["camera_codigo"], {
            "camera_codigo": p["camera_codigo"], "camera": p["camera"],
            "setor": p["setor"], "passagens": 0, "capturas": 0,
            "rostos": 0, "corpos": 0,
            "primeira": p["inicio"], "ultima": p["fim"],
        })
        c["passagens"] += 1
        c["capturas"] += p["capturas"]
        c["rostos"] += p["rostos"]
        c["corpos"] += p["corpos"]
        if p["inicio"] < c["primeira"]:
            c["primeira"] = p["inicio"]
        if p["fim"] > c["ultima"]:
            c["ultima"] = p["fim"]

    return {
        "id_biometrico": pessoa.id_biometrico,
        "nome": pessoa.nome,
        "passagens": list(reversed(passagens)),   # a mais recente primeiro
        # Da mais visitada para a mais rara. A rara é a notícia: a câmera onde
        # a pessoa esteve uma vez só diz mais sobre o deslocamento dela que a
        # mesa onde ela passa o dia.
        "por_camera": sorted(por_camera.values(), key=lambda c: -c["passagens"]),
        "resumo": {
            "passagens": len(passagens),
            "cameras": len(por_camera),
            "capturas": sum(p["capturas"] for p in passagens),
            "rostos": sum(p["rostos"] for p in passagens),
            "corpos": sum(p["corpos"] for p in passagens),
            "primeira": passagens[0]["inicio"] if passagens else None,
            "ultima": passagens[-1]["fim"] if passagens else None,
        },
    }


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
