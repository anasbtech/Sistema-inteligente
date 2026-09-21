"""Testes de ponta a ponta contra a API, com banco SQLite em memória.

    pytest -q

Exercitam o caminho real: autenticação, emissão de identificador, correção de
sexo, ingestão de detecção com busca 1:N, alerta e trilha de auditoria.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

# O banco de teste é definido em tests/conftest.py, antes destes imports.
from app.database import db_context, engine
from app.main import app
from app.models import Base


def vetor(semente: int) -> list[float]:
    r = np.random.default_rng(semente)
    v = r.normal(size=512).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


async def _preparar_banco():
    """Schema limpo + um administrador. Roda num único event loop."""
    from app.models import Perfil, SequenciaBiometrica, Sexo, Usuario
    from app.security import hash_senha

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async with db_context() as db:
        for s in Sexo:
            db.add(SequenciaBiometrica(sexo=s, proximo=1))
        db.add(Usuario(
            nome="Teste Admin", email="t@pm.gov.br", matricula="T-1",
            senha_hash=hash_senha("Teste@2026Forte"), perfil=Perfil.ADMIN,
            unidade="Teste", senha_alterada_em=datetime.now(timezone.utc),
        ))
    await engine.dispose()


@pytest.fixture(scope="module")
def cliente():
    asyncio.run(_preparar_banco())
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"identificador": "t@pm.gov.br", "senha": "Teste@2026Forte"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        yield c


# ----------------------------------------------------------------------
# Identificador biométrico
# ----------------------------------------------------------------------
def test_emissao_segue_o_sexo_do_cadastro(cliente):
    for sexo, prefixo in [("M", "BIO-M"), ("F", "BIO-F"), ("N", "BIO-N")]:
        r = cliente.post("/pessoas", json={
            "nome": f"Pessoa Teste {sexo} Sobrenome", "sexo": sexo, "status": "CIDADAO"
        })
        assert r.status_code == 201, r.text
        d = r.json()
        assert d["id_biometrico"].startswith(prefixo)
        assert d["sexo"] == sexo and d["sexo_emissao"] == sexo


def test_sequencias_sao_independentes_por_sexo(cliente):
    antes = cliente.get("/pessoas/esquema-ids").json()["prefixos"]
    cliente.post("/pessoas", json={"nome": "Mais Um Masculino", "sexo": "M"})
    depois = cliente.get("/pessoas/esquema-ids").json()["prefixos"]

    assert depois["M"]["registros"] == antes["M"]["registros"] + 1
    # Criar um homem não mexe no contador feminino.
    assert depois["F"]["proximo"] == antes["F"]["proximo"]
    assert depois["N"]["proximo"] == antes["N"]["proximo"]


def test_corrigir_sexo_preserva_o_identificador(cliente):
    criada = cliente.post("/pessoas", json={
        "nome": "Alguem Que Sera Corrigido", "sexo": "F", "status": "CIDADAO"
    }).json()
    id_original = criada["id_biometrico"]
    assert id_original.startswith("BIO-F")

    corrigida = cliente.patch(f"/pessoas/{id_original}", json={"sexo": "M"}).json()

    assert corrigida["id_biometrico"] == id_original, "o identificador é permanente"
    assert corrigida["sexo"] == "M", "o cadastro passa a constar como masculino"
    assert corrigida["sexo_emissao"] == "F", "o prefixo registra o que constava na emissão"


def test_proximo_id_previsto_bate_com_o_emitido(cliente):
    previsto = cliente.get("/pessoas/proximo-id", params={"sexo": "N"}).json()["proximo_id"]
    emitido = cliente.post("/pessoas", json={"nome": "Pessoa Sem Sexo Informado", "sexo": "N"}).json()
    assert emitido["id_biometrico"] == previsto


def test_procurado_exige_mandado(cliente):
    r = cliente.post("/pessoas", json={
        "nome": "Sem Mandado Judicial", "sexo": "M", "status": "PROCURADO"
    })
    assert r.status_code == 422
    assert "mandado" in r.text.lower()


# ----------------------------------------------------------------------
# Ingestão e busca 1:N
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def camera(cliente):
    r = cliente.post("/cameras", json={
        "nome": "Câmera de Teste", "setor": "Centro", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.1:554/s1",
        "limiar_facial": 0.85, "analitico_facial": True, "analitico_objeto": True,
    })
    assert r.status_code == 201, r.text
    return r.json()["codigo"]


def test_deteccao_facial_identifica_pessoa_cadastrada(cliente, camera):
    """Cadastra um template e confirma que a captura correspondente o encontra."""
    pessoa = cliente.post("/pessoas", json={"nome": "Rosto Conhecido Silva", "sexo": "M"}).json()
    emb = vetor(4242)

    # Grava o template direto no registro (o upload de foto exige InsightFace,
    # que é opcional). O caminho de busca exercitado é o mesmo.
    from sqlalchemy import select
    from app.models import Pessoa

    async def gravar():
        async with db_context() as db:
            p = (await db.execute(
                select(Pessoa).where(Pessoa.id_biometrico == pessoa["id_biometrico"])
            )).scalar_one()
            p.embedding = emb
    cliente.portal.call(gravar)

    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "FACIAL", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "embedding": emb, "qualidade_face": 0.9,
    })
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["match"] is True
    assert d["id_biometrico"] == pessoa["id_biometrico"]
    assert d["similaridade"] > 0.99


def test_abaixo_do_limiar_nao_atribui_identidade(cliente, camera):
    # Vetor aleatório sem relação com nenhum cadastro.
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "FACIAL", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "embedding": vetor(99999), "qualidade_face": 0.8,
    })
    assert r.status_code == 201
    d = r.json()
    assert d["match"] is False
    assert d["id_biometrico"] is None, "nenhum palpite de identidade abaixo do limiar"
    assert d["severidade"] == 1


def test_objeto_critico_gera_alerta(cliente, camera):
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "arma", "confianca": 0.93,
    })
    assert r.status_code == 201
    d = r.json()
    assert d["severidade"] == 4
    assert d["alerta"] is not None


def test_alerta_exige_desfecho_para_encerrar(cliente):
    alertas = cliente.get("/alertas", params={"status": "ABERTO", "por_pagina": 1}).json()
    if not alertas["itens"]:
        pytest.skip("nenhum alerta aberto")
    codigo = alertas["itens"][0]["codigo"]

    r = cliente.post(f"/alertas/{codigo}/encerrar", json={"resolucao": "curto"})
    assert r.status_code == 422, "desfecho curto demais deve ser recusado"

    r = cliente.post(f"/alertas/{codigo}/encerrar",
                     json={"resolucao": "Guarnição despachada e ocorrência atendida."})
    assert r.status_code == 200
    assert r.json()["status"] == "RESOLVIDO"


# ----------------------------------------------------------------------
# Auditoria
# ----------------------------------------------------------------------
def test_cadeia_de_auditoria_permanece_integra(cliente):
    r = cliente.get("/auditoria/verificar")
    assert r.status_code == 200
    d = r.json()
    assert d["integra"] is True, d["mensagem"]
    assert d["linhas_verificadas"] > 0


def test_consulta_biometrica_e_auditada(cliente):
    pessoa = cliente.post("/pessoas", json={"nome": "Consultado Para Auditar", "sexo": "F"}).json()
    cliente.get(f"/pessoas/{pessoa['id_biometrico']}")

    trilha = cliente.get("/auditoria", params={"acao": "CONSULTA_BIO", "por_pagina": 5}).json()
    alvos = [x["alvo"] for x in trilha["itens"]]
    assert pessoa["id_biometrico"] in alvos


# ----------------------------------------------------------------------
# Controle de acesso
# ----------------------------------------------------------------------
def test_rota_protegida_recusa_sem_token(cliente):
    from fastapi.testclient import TestClient as TC
    anon = TC(app)
    assert anon.get("/pessoas").status_code == 401
    assert anon.get("/auditoria").status_code == 401


def test_interface_e_info_sao_publicas(cliente):
    from fastapi.testclient import TestClient as TC
    anon = TC(app)
    assert anon.get("/").status_code == 200
    r = anon.get("/api/info")
    assert r.status_code == 200
    assert "estatisticas" in r.json()
