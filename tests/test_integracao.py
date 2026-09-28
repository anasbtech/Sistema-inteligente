"""Testes de ponta a ponta contra a API, com banco SQLite em memória.

    pytest -q

Exercitam o caminho real: autenticação, emissão de identificador, correção de
sexo, ingestão de detecção com busca 1:N, alerta e trilha de auditoria.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

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


def test_capturas_nao_identificadas_seguidas_viram_um_registro(cliente):
    """Uma presença, uma linha — mesmo sem saber de quem é o rosto.

    Câmera própria porque a janela é contada por câmera, e a fixture `camera`
    tem escopo de módulo: reusá-la deixaria o resultado na mão da ordem dos
    testes.
    """
    codigo = cliente.post("/cameras", json={
        "nome": "Câmera Sem Repetição", "setor": "Centro", "cena": "portaria",
        "rtsp_url": "rtsp://10.0.0.9:554/s1", "analitico_facial": True,
    }).json()["codigo"]

    agora = datetime.now(timezone.utc)
    primeira = cliente.post("/deteccoes/ingest", json={
        "tipo": "FACIAL", "camera_codigo": codigo,
        "ocorrido_em": agora.isoformat(),
        "embedding": vetor(70001), "qualidade_face": 0.62,
    })
    assert primeira.status_code == 201, primeira.text
    assert primeira.json()["match"] is False

    # Outro quadro do mesmo desconhecido, seis segundos depois. O vetor é
    # diferente de propósito: nesta resolução duas capturas da mesma pessoa
    # não se parecem, e é justamente por isso que quem decide é o relógio.
    segunda = cliente.post("/deteccoes/ingest", json={
        "tipo": "FACIAL", "camera_codigo": codigo,
        "ocorrido_em": (agora + timedelta(seconds=6)).isoformat(),
        "embedding": vetor(70002), "qualidade_face": 0.81,
    })
    assert segunda.status_code == 201, segunda.text
    d2 = segunda.json()
    assert d2["descartada"] is True, "a segunda captura não pode virar registro"
    assert d2["codigo"] == primeira.json()["codigo"], "absorvida pela anterior"

    # Passada a janela, uma presença nova volta a ser registrada.
    from app.config import settings
    depois = cliente.post("/deteccoes/ingest", json={
        "tipo": "FACIAL", "camera_codigo": codigo,
        "ocorrido_em": (agora + timedelta(
            seconds=settings.UNMATCHED_FACE_WINDOW_S + 30)).isoformat(),
        "embedding": vetor(70003), "qualidade_face": 0.77,
    })
    assert depois.status_code == 201, depois.text
    d3 = depois.json()
    assert not d3.get("descartada"), "fora da janela é outra passagem"
    assert d3["codigo"] != primeira.json()["codigo"]


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


# ----------------------------------------------------------------------
# Série dos códigos de registro
# ----------------------------------------------------------------------
def test_codigo_nao_e_reemitido_depois_de_apagar(cliente, camera):
    """O caso real: apagar um registro travava toda a ingestão.

    O código saía de ``COUNT(*) + 1``, que presume que registro nunca é
    apagado. Apagados cinco, a contagem ficou abaixo do maior código já
    emitido e cada ingestão passou a tentar um código existente: o UNIQUE
    recusava, a contagem não subia, e a tentativa seguinte repetia o mesmo
    código. A ingestão parou de vez — com o worker detectando normalmente e
    todo registro sendo descartado com 500.
    """
    def ingerir() -> str:
        r = cliente.post("/deteccoes/ingest", json={
            "tipo": "OBJETO", "camera_codigo": camera,
            "ocorrido_em": datetime.now(timezone.utc).isoformat(),
            "classe": "carro", "confianca": 0.8, "bbox": [0.1, 0.1, 0.2, 0.2],
        })
        assert r.status_code == 201, r.text
        return r.json()["codigo"]

    primeiro, segundo = ingerir(), ingerir()
    assert primeiro != segundo

    for codigo in (segundo, primeiro):
        r = cliente.delete(f"/deteccoes/{codigo}", params={"motivo": "teste de regressão"})
        assert r.status_code == 204, r.text

    # Sem contador, estes dois voltariam aos códigos apagados e o 201 daria 500.
    terceiro, quarto = ingerir(), ingerir()
    assert terceiro not in (primeiro, segundo), "código de registro apagado foi reemitido"
    assert quarto not in (primeiro, segundo, terceiro)


def test_deteccao_presa_a_alerta_nao_pode_ser_apagada(cliente, camera):
    """Apagar por baixo de um alerta o deixaria apontando para o vazio: o
    vínculo é ``ON DELETE SET NULL``, então nem daria erro."""
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "ACAO", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "invasao", "confianca": 0.95, "duracao_s": 4,
    })
    assert r.status_code == 201, r.text
    codigo = r.json()["codigo"]
    assert r.json()["severidade"] >= 3, "invasão deve gerar alerta"

    negado = cliente.delete(f"/deteccoes/{codigo}", params={"motivo": "tentativa indevida"})
    assert negado.status_code == 409
    assert cliente.get(f"/deteccoes/{codigo}").status_code == 200, "o registro continua lá"
