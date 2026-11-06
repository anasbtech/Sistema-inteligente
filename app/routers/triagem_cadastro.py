"""Aprovação humana das "pessoas novas" encontradas na triagem.

A análise (scripts/triagem_extrair.py + triagem_agrupar.py) agrupa capturas
sem cadastro que são, com folga, a mesma pessoa. Nada disso vira ficha
sozinho: aqui um operador olha cada grupo, escolhe o sexo (o motor nunca o
deduz da imagem — medido, ele errou mulheres como homens em 2 de 3 grupos),
desmarca o que não for a pessoa e aprova. Só então nasce a ficha, como
funcionário, com amostras do rosto e as capturas na trajetória.

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
