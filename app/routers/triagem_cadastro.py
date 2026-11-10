"""Aprovação humana das "pessoas novas" encontradas na triagem.

A análise (scripts/triagem_extrair.py + triagem_agrupar.py) agrupa capturas
sem cadastro que são, com folga, a mesma pessoa. Nada disso vira ficha
sozinho: aqui um operador olha cada grupo, escolhe o sexo (o motor nunca o
deduz da imagem — medido, ele errou mulheres como homens em 2 de 3 grupos),
desmarca o que não for a pessoa e aprova. Só então nasce a ficha, como
funcionário, com amostras do rosto e as capturas na trajetória.

Quando o grupo é de alguém que **já tem ficha** (o rosto não chegou ao
limiar de reconhecimento nesta câmera ou neste ângulo), o operador vincula
em vez de aprovar: as capturas entram na trajetória da ficha existente e,
se ele quiser, as compatíveis viram amostras — é isso que faz o sistema
passar a reconhecer a pessoa naquele ângulo. As sugestões vêm das fichas
mais parecidas com o grupo; a decisão é sempre de quem olha.

Registrado antes de `pessoas.router`: senão `/pessoas/{id_biometrico}`
capturaria `/pessoas/triagem-novas`.
"""
from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.database import get_db
from app.deps import exige
from app.models import Camera, Deteccao, Pessoa, Sexo, StatusPessoa, TipoDeteccao, Usuario
from app.services import biometrics, face_engine
from app.tipos import como_utc

router = APIRouter(prefix="/pessoas/triagem-novas", tags=["Base biométrica"])

PASTA = Path(__file__).resolve().parents[2] / "data" / "cadastro_triagem"
MIN_CAPTURAS = 3        # depois de o operador desmarcar, abaixo disto não é grupo
AMOSTRAS_MAX = 8        # as melhores viram amostra; as demais só trajetória
# Ao vincular a uma ficha existente, só vira amostra a captura que se parece
# com alguma amostra da ficha pelo menos isto. É o mesmo apoio exigido da
# amostra automática (FACE_AUTO_SAMPLE_APOIO): pessoas diferentes quase nunca
# passam de 0,30 nesta base (p99,9), e amostra errada contamina a ficha.
VINCULO_AMOSTRA_MIN = 0.30
SUGESTOES = 6

_cache_vetores: dict = {}


def _vetores() -> dict:
    """código da captura → vetor do rosto, do que a extração já calculou."""
    import numpy as np
    arq = PASTA / "vetores.npz"
    if not arq.exists():
        return {}
    marca = arq.stat().st_mtime
    if _cache_vetores.get("marca") != marca:
        with np.load(arq, allow_pickle=False) as d:
            V = d["vet"].astype(np.float32)
            V /= np.maximum(np.linalg.norm(V, axis=1, keepdims=True), 1e-9)
            _cache_vetores.update(marca=marca, mapa=dict(zip(d["codigos"].tolist(), V)))
    return _cache_vetores["mapa"]


async def _amostras_ativas(db: AsyncSession):
    """(matriz de amostras normalizadas, dono de cada linha, dados de cada dono)."""
    import numpy as np
    from app.models import AmostraFacial
    linhas = (await db.execute(
        select(Pessoa.id, Pessoa.id_biometrico, Pessoa.nome, Pessoa.sexo, Pessoa.status,
               AmostraFacial.embedding)
        .join(AmostraFacial, AmostraFacial.pessoa_id == Pessoa.id)
        .where(Pessoa.ativo.is_(True)))).all()
    if not linhas:
        return None, [], {}
    A = np.array([json.loads(e) if isinstance(e, str) else e for *_, e in linhas], np.float32)
    A /= np.maximum(np.linalg.norm(A, axis=1, keepdims=True), 1e-9)
    donos = [r[0] for r in linhas]
    dados = {r[0]: {"id_biometrico": r[1], "nome": r[2],
                    "sexo": r[3].value if hasattr(r[3], "value") else str(r[3]),
                    "status": r[4].value if hasattr(r[4], "value") else str(r[4])} for r in linhas}
    return A, donos, dados


def _por_pessoa(S, donos: list):
    """S: capturas × amostras. Devolve {pessoa: vetor com a melhor amostra
    dela para cada captura} — o máximo por amostra, como na busca 1:N."""
    import numpy as np
    saida: dict = {}
    donos_arr = np.array([str(d) for d in donos])
    for pid in set(donos):
        cols = donos_arr == str(pid)
        saida[pid] = S[:, cols].max(1)
    return saida


def _grupos_brutos() -> list:
    arq = PASTA / "grupos.json"
    return json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else []


def _descartados() -> set:
    arq = PASTA / "descartados.json"
    return set(json.loads(arq.read_text(encoding="utf-8"))) if arq.exists() else set()


def _guardar_descartados(codigos: set) -> None:
    PASTA.mkdir(parents=True, exist_ok=True)
    tmp = PASTA / "descartados.tmp.json"
    tmp.write_text(json.dumps(sorted(codigos)), encoding="utf-8")
    tmp.replace(PASTA / "descartados.json")


def _nome(sexo: str, semente: str, usados: set) -> str:
    from scripts.triagem_agrupar import MEIOS, MEIOS_F, NOMES_F, NOMES_M, SOBRENOMES
    rnd = random.Random(f"{semente}:{sexo}")
    base, meios = (NOMES_M, MEIOS) if sexo == "M" else (NOMES_F, MEIOS_F)
    for _ in range(5000):
        nome = f"{rnd.choice(base)} {rnd.choice(meios)} {rnd.choice(SOBRENOMES)}"
        if nome.lower() not in usados:
            usados.add(nome.lower())
            return nome
    raise HTTPException(status.HTTP_409_CONFLICT, "Nomes fictícios esgotados.")


async def _pendentes(db: AsyncSession) -> list:
    """Grupos ainda por decidir, só com as capturas que seguem sem pessoa."""
    grupos = _grupos_brutos()
    if not grupos:
        return []
    todos = {c for g in grupos for c in g["codigos"]}
    livres = {
        cod: (cam_cod, cam_nome, quando)
        for cod, cam_cod, cam_nome, quando in (await db.execute(
            select(Deteccao.codigo, Camera.codigo, Camera.nome, Deteccao.ocorrido_em)
            .join(Camera, Camera.id == Deteccao.camera_id)
            .where(Deteccao.codigo.in_(todos), Deteccao.pessoa_id.is_(None),
                   Deteccao.tipo == TipoDeteccao.FACIAL))).all()
    }
    fora = _descartados()
    usados = {n.lower() for (n,) in (await db.execute(select(Pessoa.nome))).all()}
    saida = []
    for g in grupos:
        cods = [c for c in g["codigos"] if c in livres and c not in fora]
        if len(cods) < MIN_CAPTURAS:
            continue
        chave = g["codigos"][0]
        saida.append({
            "chave": chave,
            "capturas": [{"codigo": c, "camera_codigo": livres[c][0], "camera": livres[c][1],
                          "ocorrido_em": livres[c][2]} for c in cods],
            "cameras": sorted({livres[c][1] for c in cods}),
            "sexo_sugerido": g["sexo"], "sexo_conf": g.get("sexo_conf"),
            "nome_m": _nome("M", chave, usados), "nome_f": _nome("F", chave, usados),
            "coesao": g.get("coesao"),
        })
    return saida


@router.get("", summary="Grupos de pessoas novas aguardando aprovação")
async def listar(db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("biometrics"))):
    pendentes = await _pendentes(db)
    return {"total": len(pendentes), "grupos": pendentes}


class AprovarIn(BaseModel):
    chave: str
    sexo: Sexo
    nome: str = Field(..., min_length=5, max_length=200)
    codigos: List[str] = Field(..., min_length=1)


class DescartarIn(BaseModel):
    chave: str


@router.post("/aprovar", status_code=status.HTTP_201_CREATED,
             summary="Criar a ficha do grupo aprovado")
async def aprovar(dados: AprovarIn, request: Request, db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("biometrics", "edit"))):
    from app.routers.pessoas import _capturas_viram_amostras

    if dados.sexo not in (Sexo.M, Sexo.F):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Escolha masculino ou feminino.")
    grupo = next((g for g in await _pendentes(db) if g["chave"] == dados.chave), None)
    if grupo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grupo não encontrado ou já decidido.")
    do_grupo = [c["codigo"] for c in grupo["capturas"]]
    codigos = [c for c in do_grupo if c in set(dados.codigos)]      # ordem: melhor primeiro
    if len(codigos) != len(set(dados.codigos)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Há capturas que não pertencem a este grupo.")
    if len(codigos) < MIN_CAPTURAS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"Mantenha ao menos {MIN_CAPTURAS} capturas da pessoa.")
    nome = " ".join(dados.nome.split())
    if (await db.execute(select(Pessoa.id).where(func.lower(Pessoa.nome) == nome.lower()))).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "Já existe uma ficha com esse nome.")

    pessoa = Pessoa(
        id_biometrico=await biometrics.emitir_id_biometrico(db, dados.sexo),
        sexo=dados.sexo, sexo_emissao=dados.sexo, nome=nome,
        status=StatusPessoa.FUNCIONARIO, base_legal="Controle de acesso predial",
        observacoes="Cadastrada a partir da triagem, por conferência humana. Nome fictício.",
        cadastrado_por=usuario.id,
    )
    db.add(pessoa)
    await db.flush()

    # As melhores viram amostra (é o que faz o sistema reconhecê-la depois);
    # sem nenhuma amostra a ficha não reconheceria ninguém, então é exigida.
    aceitas, _ = await _capturas_viram_amostras(db, pessoa, codigos[:AMOSTRAS_MAX],
                                                exigir_amostra=True)
    # As demais entram na trajetória: confirmadas por quem aprovou.
    resto = [c for c in codigos if c not in set(aceitas)]
    dets = (await db.execute(select(Deteccao).where(Deteccao.codigo.in_(resto)))).scalars().unique().all()
    for det in dets:
        det.pessoa_id = pessoa.id
        det.similaridade = 1.0
        det.expurgo_em = None
    await db.flush()

    ligadas = (await db.execute(select(Deteccao).where(Deteccao.pessoa_id == pessoa.id)
                                .order_by(Deteccao.ocorrido_em.desc()))).scalars().unique().all()
    pessoa.total_deteccoes = len(ligadas)
    if ligadas:
        pessoa.ultima_deteccao = ligadas[0].ocorrido_em
        cam = await db.get(Camera, ligadas[0].camera_id)
        pessoa.ultima_camera = cam.nome if cam else ""

    await audit.registrar(
        db, acao="PESSOA_ADD", severidade="warn", usuario=usuario, alvo=pessoa.id_biometrico,
        descricao="Ficha criada a partir de grupo da triagem, por conferência humana (nome fictício)",
        finalidade="Cadastro da triagem", request=request,
        metadados={"capturas": len(ligadas), "amostras": pessoa.amostras,
                   "sexo_escolhido": dados.sexo.value, "sexo_sugerido": grupo["sexo_sugerido"],
                   "desmarcadas": len(do_grupo) - len(codigos)},
    )
    return {"id_biometrico": pessoa.id_biometrico, "nome": pessoa.nome,
            "capturas": len(ligadas), "amostras": pessoa.amostras}


@router.get("/{chave}/parecidas", summary="Fichas existentes mais parecidas com o grupo")
async def parecidas(chave: str, db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("biometrics"))):
    """Sugestão para quem confere: de quem este grupo pode ser. A nota de cada
    ficha é a média, entre as capturas do grupo, da melhor amostra dela."""
    import numpy as np
    grupo = next((g for g in await _pendentes(db) if g["chave"] == chave), None)
    if grupo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grupo não encontrado ou já decidido.")
    mapa = _vetores()
    V = [mapa[c["codigo"]] for c in grupo["capturas"] if c["codigo"] in mapa]
    A, donos, dados = await _amostras_ativas(db)
    if not V or A is None:
        return {"sugestoes": []}
    S = np.stack(V) @ A.T
    notas = sorted(((float(v.mean()), float(v.max()), pid) for pid, v in _por_pessoa(S, donos).items()),
                   reverse=True)[:SUGESTOES]
    return {"sugestoes": [dict(dados[pid], similaridade=round(media, 3), melhor=round(melhor, 3))
                          for media, melhor, pid in notas]}


class VincularIn(BaseModel):
    chave: str
    id_biometrico: str
    codigos: List[str] = Field(..., min_length=1)
    incluir_amostras: bool = True


@router.post("/vincular", summary="Juntar o grupo a uma ficha que já existe")
async def vincular(dados: VincularIn, request: Request, db: AsyncSession = Depends(get_db),
                   usuario: Usuario = Depends(exige("biometrics", "edit"))):
    import numpy as np
    from app.routers.pessoas import _capturas_viram_amostras

    grupo = next((g for g in await _pendentes(db) if g["chave"] == dados.chave), None)
    if grupo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grupo não encontrado ou já decidido.")
    pessoa = (await db.execute(select(Pessoa).where(Pessoa.id_biometrico == dados.id_biometrico,
                                                    Pessoa.ativo.is_(True)))).scalars().first()
    if pessoa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ficha não encontrada.")
    do_grupo = [c["codigo"] for c in grupo["capturas"]]
    codigos = [c for c in do_grupo if c in set(dados.codigos)]
    if len(codigos) != len(set(dados.codigos)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Há capturas que não pertencem a este grupo.")

    # Quais se parecem com a ficha o bastante para virar amostra.
    compativeis: list = []
    if dados.incluir_amostras:
        A, donos, _ = await _amostras_ativas(db)
        mapa = _vetores()
        cols = [k for k, d in enumerate(donos) if d == pessoa.id]
        if A is not None and cols:
            Ap = A[cols]
            notas = [(float((mapa[c] @ Ap.T).max()), c) for c in codigos if c in mapa]
            compativeis = [c for s, c in sorted(notas, reverse=True)
                           if s >= VINCULO_AMOSTRA_MIN][:AMOSTRAS_MAX]
        else:
            # Ficha sem amostra nenhuma: as capturas conferidas são o rosto dela.
            compativeis = codigos[:AMOSTRAS_MAX]

    amostras_antes = pessoa.amostras or 0
    aceitas: list = []
    if compativeis:
        aceitas, _ = await _capturas_viram_amostras(db, pessoa, compativeis, exigir_amostra=False)
    resto = [c for c in codigos if c not in set(aceitas)]
    dets = (await db.execute(select(Deteccao).where(Deteccao.codigo.in_(resto)))).scalars().unique().all()
    for det in dets:
        det.pessoa_id = pessoa.id
        det.similaridade = 1.0
        det.expurgo_em = None
    await db.flush()
    await db.refresh(pessoa)

    ligadas = (await db.execute(select(Deteccao).where(Deteccao.pessoa_id == pessoa.id)
                                .order_by(Deteccao.ocorrido_em.desc()))).scalars().unique().all()
    pessoa.total_deteccoes = len(ligadas)
    if ligadas:
        pessoa.ultima_deteccao = ligadas[0].ocorrido_em
        cam = await db.get(Camera, ligadas[0].camera_id)
        pessoa.ultima_camera = cam.nome if cam else ""
    novas = max(0, (pessoa.amostras or 0) - amostras_antes)

    await audit.registrar(
        db, acao="BIO_TRIAGEM", severidade="warn", usuario=usuario, alvo=pessoa.id_biometrico,
        descricao="Grupo da triagem vinculado a ficha existente, por conferência humana",
        finalidade="Cadastro da triagem", request=request,
        metadados={"chave": dados.chave, "capturas": len(codigos), "amostras_novas": novas,
                   "incluir_amostras": dados.incluir_amostras,
                   "desmarcadas": len(do_grupo) - len(codigos)})
    return {"id_biometrico": pessoa.id_biometrico, "nome": pessoa.nome,
            "capturas": len(codigos), "amostras_novas": novas}


@router.post("/descartar", summary="Não cadastrar este grupo")
async def descartar(dados: DescartarIn, request: Request, db: AsyncSession = Depends(get_db),
                    usuario: Usuario = Depends(exige("biometrics", "edit"))):
    grupo = next((g for g in await _pendentes(db) if g["chave"] == dados.chave), None)
    if grupo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grupo não encontrado ou já decidido.")
    fora = _descartados() | {c["codigo"] for c in grupo["capturas"]}
    _guardar_descartados(fora)
    await audit.registrar(
        db, acao="BIO_TRIAGEM", severidade="info", usuario=usuario, alvo=dados.chave,
        descricao="Grupo da triagem descartado: não vira ficha (capturas seguem na triagem)",
        finalidade="Cadastro da triagem", request=request,
        metadados={"capturas": len(grupo["capturas"])})
    return {"descartadas": len(grupo["capturas"])}
