"""Testes de ponta a ponta contra a API, com banco SQLite em memória.

    pytest -q

Exercitam o caminho real: autenticação, emissão de identificador, correção de
sexo, ingestão de detecção com busca 1:N, alerta e trilha de auditoria.
"""
from __future__ import annotations

import asyncio
import io
import json
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
            "nome": f"Pessoa Teste {sexo} Sobrenome", "sexo": sexo, "status": "VISITANTE"
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
        "nome": "Alguem Que Sera Corrigido", "sexo": "F", "status": "VISITANTE"
    }).json()
    id_original = criada["id_biometrico"]
    assert id_original.startswith("BIO-F")

    corrigida = cliente.patch(f"/pessoas/{id_original}", json={"sexo": "M"}).json()

    assert corrigida["id_biometrico"] == id_original, "o identificador é permanente"
    assert corrigida["sexo"] == "M", "o cadastro passa a constar como masculino"
    assert corrigida["sexo_emissao"] == "F", "o prefixo registra o que constava na emissão"


def test_bio_n_sai_de_nao_identificado_ao_ser_cadastrado(cliente):
    """A única exceção à permanência do identificador.

    BIO-N não afirma nada sobre a pessoa: diz que ainda não se sabe quem ela
    é. Quando alguém cadastra e informa o sexo, deixa de ser verdade, e manter
    o prefixo seria deixar no identificador uma afirmação que o próprio
    cadastro contradiz. Por isso aqui o ID é reemitido — e só aqui.

    O caso de cima, BIO-F corrigido para M, continua preservando o ID: lá
    houve uma declaração no ato da emissão, e corrigir o cadastro depois não
    desfaz o que constava então.
    """
    criada = cliente.post("/pessoas", json={
        "nome": "Ainda Sem Identificacao", "sexo": "N", "status": "VISITANTE"
    }).json()
    id_n = criada["id_biometrico"]
    assert id_n.startswith("BIO-N")

    promovida = cliente.patch(f"/pessoas/{id_n}", json={
        "sexo": "M", "nome": "Agora Com Nome"
    }).json()

    assert promovida["id_biometrico"].startswith("BIO-M"), "saiu de não identificado"
    assert promovida["id_biometrico"] != id_n, "o identificador foi reemitido"
    assert promovida["sexo_emissao"] == "M", "o prefixo novo passa a ser o de emissão"

    # Uma vez só, e de mão única: já promovido, volta a valer a permanência.
    id_m = promovida["id_biometrico"]
    de_novo = cliente.patch(f"/pessoas/{id_m}", json={"sexo": "F"}).json()
    assert de_novo["id_biometrico"] == id_m, "promoção não se repete"
    assert de_novo["sexo"] == "F" and de_novo["sexo_emissao"] == "M"

    # O registro antigo não fica ocupando a sequência nem responde mais.
    assert cliente.get(f"/pessoas/{id_n}").status_code == 404


def test_fila_de_desconhecidos_so_traz_quem_nao_tem_candidato(cliente, camera):
    """A outra metade da triagem: rosto que não se parece com ninguém.

    A fila de conferência pergunta "é ela?" sobre quem o motor apontou. Esta
    pergunta "quem é?", e por isso exclui quem tem candidato — mandar os dois
    para a mesma fila faria o operador decidir coisas diferentes no mesmo
    gesto.
    """
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "FACIAL", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "embedding": vetor(4242), "qualidade_face": 0.77,
    })
    assert r.status_code == 201
    assert r.json()["match"] is False, "vetor aleatório não casa com ninguém"

    fila = cliente.get("/pessoas/desconhecidos", params={"horas": 24}).json()
    assert fila["janela_horas"] == 24
    for item in fila["itens"]:
        assert item["snapshot_key"], "sem recorte não há o que conferir"

    # Capturas já vinculadas a alguém não voltam para a fila.
    vinculadas = {i["codigo"] for i in fila["itens"]}
    pessoas = cliente.get("/pessoas", params={"por_pagina": 5}).json()["itens"]
    if pessoas:
        trilha = cliente.get(f"/pessoas/{pessoas[0]['id_biometrico']}/capturas",
                             params={"tipo": "rosto", "limite": 20}).json()
        assert not ({d["codigo"] for d in trilha} & vinculadas), \
            "captura de alguém identificado não é desconhecida"


def test_abrir_nao_identificado_exige_captura(cliente):
    """Registro sem amostra nenhuma nasceria sem template — e sem reconhecer.

    Medido nesta base: duas capturas da mesma pessoa só se reconhecem em 12%
    dos pares. Um BIO-N aberto sem amostra, ou com uma só, não reconheceria a
    própria pessoa no encontro seguinte.
    """
    assert cliente.post("/pessoas/nao-identificado", json=[]).status_code == 422
    r = cliente.post("/pessoas/nao-identificado", json=["FR-0000000000"])
    assert r.status_code == 422, "captura inexistente não abre registro"


def test_proximo_id_previsto_bate_com_o_emitido(cliente):
    previsto = cliente.get("/pessoas/proximo-id", params={"sexo": "N"}).json()["proximo_id"]
    emitido = cliente.post("/pessoas", json={"nome": "Pessoa Sem Sexo Informado", "sexo": "N"}).json()
    assert emitido["id_biometrico"] == previsto


def test_restrito_exige_motivo(cliente):
    r = cliente.post("/pessoas", json={
        "nome": "Sem Motivo Declarado", "sexo": "M", "status": "RESTRITO"
    })
    assert r.status_code == 422
    assert "restri" in r.text.lower()


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
    # A sobreposição do mosaico rotula a caixa com este nome; sem ele o rosto
    # reconhecido volta a aparecer no vídeo apenas como "rosto".
    assert d["nome"] == "Rosto Conhecido Silva"


def test_angulo_novo_nao_enfraquece_os_ja_cadastrados(cliente):
    """Cadastrar mais um ângulo não pode derrubar os que já funcionavam.

    A busca compara contra cada amostra e fica com a melhor. Comparar contra
    a MÉDIA delas fazia o contrário: medido nesta base, o pior ângulo caía de
    0,858 com 2 amostras para 0,739 com 3 e 0,688 com 4 — cada câmera
    acrescentada corroía as anteriores, e era questão de tempo até cruzarem o
    limiar. É isto que permite cobrir câmera por câmera.
    """
    from sqlalchemy import select
    from app.database import db_context
    from app.models import AmostraFacial, Pessoa
    from app.services import face_engine

    pessoa = cliente.post("/pessoas", json={
        "nome": "Pessoa De Varios Angulos", "sexo": "F"}).json()
    frente, perfil = vetor(11), vetor(22)

    async def montar(vetores):
        async with db_context() as db:
            p = (await db.execute(select(Pessoa).where(
                Pessoa.id_biometrico == pessoa["id_biometrico"]))).scalar_one()
            for a in (await db.execute(select(AmostraFacial).where(
                    AmostraFacial.pessoa_id == p.id))).scalars().all():
                await db.delete(a)
            await db.flush()
            for v in vetores:
                db.add(AmostraFacial(pessoa_id=p.id, embedding=v, qualidade=0.8,
                                     origem="teste"))
            await db.flush()
            await face_engine.recalcular_template(db, p)

    async def similaridade(probe):
        async with db_context() as db:
            return (await face_engine.buscar_pessoa(db, probe, 0.50)).similaridade

    cliente.portal.call(montar, [frente])
    so_frente = cliente.portal.call(similaridade, frente)
    assert so_frente > 0.99

    # Chega o perfil, de outra câmera. Os dois ângulos são bem diferentes.
    cliente.portal.call(montar, [frente, perfil])
    assert cliente.portal.call(similaridade, frente) > 0.99, "a frente não pode enfraquecer"
    assert cliente.portal.call(similaridade, perfil) > 0.99, "o perfil novo também vale"


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
                     json={"resolucao": "Equipe acionada e ocorrência atendida."})
    assert r.status_code == 200
    assert r.json()["status"] == "RESOLVIDO"


def test_alerta_aponta_para_a_captura_que_o_gerou(cliente, camera):
    """Sem o código da captura, a ficha descreve o que foi visto mas não mostra.

    É o que a tela usa para buscar o recorte — quem tria decide olhando a
    imagem, não só o texto "classe 'arma' com 93% de confiança".
    """
    d = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "arma", "confianca": 0.91,
    }).json()

    a = cliente.get(f"/alertas/{d['alerta']}").json()
    assert a["deteccao_codigo"] == d["codigo"]

    # E o mesmo código aparece na listagem, que é onde a miniatura é pintada.
    lista = cliente.get("/alertas", params={"por_pagina": 50}).json()["itens"]
    linha = next(x for x in lista if x["codigo"] == d["alerta"])
    assert linha["deteccao_codigo"] == d["codigo"]


def test_alerta_nao_se_exclui_sem_justificativa(cliente, camera):
    """A obrigação é do contrato, não da tela.

    Se o motivo fosse exigido só pelo modal, um `curl` — ou uma tela futura
    escrita com pressa — apagaria alerta sem deixar por quê. O servidor
    recusa antes de chegar ao handler.
    """
    d = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "arma", "confianca": 0.95,
    }).json()
    codigo = d["alerta"]

    assert cliente.delete(f"/alertas/{codigo}").status_code == 422, "sem motivo"
    assert cliente.delete(f"/alertas/{codigo}", params={"motivo": "curto"}).status_code == 422
    assert cliente.get(f"/alertas/{codigo}").status_code == 200, "nada foi apagado"

    r = cliente.delete(f"/alertas/{codigo}",
                       params={"motivo": "Regra descontinuada; alerta sem uso operacional."})
    assert r.status_code == 204, r.text
    assert cliente.get(f"/alertas/{codigo}").status_code == 404

    # A detecção que originou o alerta continua: ela é o registro do que foi
    # visto, e apagá-la junto esconderia o evento em vez da pendência.
    assert cliente.get(f"/deteccoes/{d['codigo']}").status_code == 200

    trilha = cliente.get("/auditoria", params={"acao": "ALERTA_EXCLUSAO", "por_pagina": 5}).json()
    assert trilha["itens"], "a exclusão precisa constar na trilha"
    assert "Regra descontinuada" in trilha["itens"][0]["finalidade"]


# ----------------------------------------------------------------------
# Auditoria
# ----------------------------------------------------------------------
def test_cadeia_de_auditoria_permanece_integra(cliente):
    r = cliente.get("/auditoria/verificar")
    assert r.status_code == 200
    d = r.json()
    assert d["integra"] is True, d["mensagem"]
    assert d["linhas_verificadas"] > 0


def test_descricao_longa_nao_nasce_impossivel_de_verificar(cliente):
    """O caso real: uma linha de 380 caracteres que nunca verificaria.

    `registrar` hasheava o texto inteiro e gravava só os 300 primeiros, e
    `verificar_cadeia` recalcula a partir do que está gravado — a linha
    nascia acusando adulteração sem ninguém ter tocado nela. O corte precisa
    acontecer antes do hash.
    """
    from app import audit
    from app.database import db_context

    async def gravar_longa():
        async with db_context() as db:
            await audit.registrar(
                db, acao="TESTE_LONGO", descricao="x" * 380,
                alvo="y" * 200, finalidade="z" * 300,
            )
    cliente.portal.call(gravar_longa)

    d = cliente.get("/auditoria/verificar").json()
    assert d["integra"] is True, d["mensagem"]


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


# ----------------------------------------------------------------------
# Bloqueio e exclusão de usuários
# ----------------------------------------------------------------------
SENHA_NOVA = "Teste@2026Forte"


def _criar_usuario(cliente, sufixo: str) -> dict:
    r = cliente.post("/usuarios", json={
        "nome": f"Usuario Numero {sufixo}", "email": f"u{sufixo}@pm.gov.br",
        "matricula": f"PA-{sufixo}", "perfil": "OPERADOR", "senha": SENHA_NOVA,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _logar(email: str) -> int:
    # Sem `with`: abrir o cliente como contexto roda o lifespan de novo e o
    # gerenciador de WebSocket passa a esperar um loop que não é o da suíte.
    from fastapi.testclient import TestClient as TC
    return TC(app).post("/auth/login",
                        json={"identificador": email, "senha": SENHA_NOVA}).status_code


def test_bloqueio_tira_o_acesso_e_desbloqueio_devolve(cliente):
    u = _criar_usuario(cliente, "900001")
    assert _logar(u["email"]) == 200

    r = cliente.post(f"/usuarios/{u['id']}/bloqueio", params={"motivo": "afastamento temporario"})
    assert r.status_code == 200, r.text
    assert r.json()["ativo"] is False
    assert _logar(u["email"]) == 403, "conta bloqueada não autentica"

    # Bloquear de novo não faz sentido e não deve passar por cima do registro.
    assert cliente.post(f"/usuarios/{u['id']}/bloqueio",
                        params={"motivo": "repetindo a dose"}).status_code == 409

    r = cliente.delete(f"/usuarios/{u['id']}/bloqueio")
    assert r.status_code == 200, r.text
    assert r.json()["ativo"] is True
    assert _logar(u["email"]) == 200, "desbloquear devolve o acesso"


def test_exclusao_sai_da_lista_mas_nao_apaga_a_linha(cliente):
    """A conta precisa continuar no banco: `auditoria.usuario_id` é ON DELETE
    SET NULL e zerá-lo faria a cadeia de hash acusar adulteração."""
    u = _criar_usuario(cliente, "900002")

    assert cliente.delete(f"/usuarios/{u['id']}",
                          params={"motivo": "desligamento definitivo"}).status_code == 204
    # 401, e não 403: o endereço foi liberado com a exclusão, então não
    # pertence mais a ninguém. Antes a conta era encontrada e recusada por
    # inativa — e um 403 ali contava ao curioso que aquele e-mail já foi
    # cadastrado. O acesso continua negado, e agora sem revelar nada.
    assert _logar(u["email"]) == 401

    emails = [x["email"] for x in cliente.get("/usuarios").json()]
    assert u["email"] not in emails, "some da lista padrão"

    # Procurada pelo `id`, e não pelo e-mail: a exclusão libera e-mail e
    # matrícula para quem vier depois — segurá-los para sempre travava o
    # cadastro de quem assume o endereço funcional de um colega que saiu.
    # A linha continua lá, que é o que esta garantia exige.
    com_excluidos = cliente.get("/usuarios", params={"incluir_excluidos": "true"}).json()
    alvo = next(x for x in com_excluidos if x["id"] == u["id"])
    assert alvo["excluido_em"] is not None
    assert alvo["email"] != u["email"], "o endereço tem de ter sido liberado"

    integra = cliente.get("/auditoria/verificar").json()
    assert integra["integra"] is True, integra["mensagem"]


def test_link_do_administrador_redefine_a_senha(cliente):
    """O link emitido pelo admin tem de servir, não só existir.

    É o caminho de quem está sem envio de e-mail configurado: se ele não
    levar até a troca de senha, a instalação fica sem nenhuma recuperação.
    """
    u = _criar_usuario(cliente, "900010")

    r = cliente.post(f"/usuarios/{u['id']}/link-redefinicao",
                     params={"motivo": "operador esqueceu a senha"})
    assert r.status_code == 200, r.text
    link = r.json()["link"]
    assert "#redefinir=" in link
    segredo = link.split("#redefinir=")[1]

    # O link precisa identificar a pessoa certa antes de mostrar o formulário.
    v = cliente.post("/auth/verificar-link", json={"token": segredo})
    assert v.status_code == 200, v.text
    assert v.json()["email"] == u["email"]

    nova = "Trocada@2026x"
    assert cliente.post("/auth/redefinir-senha",
                        json={"token": segredo, "nova_senha": nova}).status_code == 200

    from fastapi.testclient import TestClient as TC
    assert TC(app).post("/auth/login",
                        json={"identificador": u["email"], "senha": nova}).status_code == 200
    assert _logar(u["email"]) == 401, "a senha antiga deixa de valer"


def test_link_novo_derruba_o_anterior(cliente):
    """Dois links vivos dobram a janela de quem interceptar um deles."""
    u = _criar_usuario(cliente, "900011")
    pedir = lambda: cliente.post(            # noqa: E731
        f"/usuarios/{u['id']}/link-redefinicao",
        params={"motivo": "reemissao por perda do primeiro"},
    ).json()["link"].split("#redefinir=")[1]

    primeiro = pedir()
    segundo = pedir()
    assert primeiro != segundo

    assert cliente.post("/auth/verificar-link", json={"token": primeiro}).status_code == 400
    assert cliente.post("/auth/verificar-link", json={"token": segundo}).status_code == 200


def test_link_do_administrador_exige_justificativa_e_conta_ativa(cliente):
    u = _criar_usuario(cliente, "900012")
    rota = f"/usuarios/{u['id']}/link-redefinicao"

    assert cliente.post(rota).status_code == 422, "sem motivo não passa"
    assert cliente.post(rota, params={"motivo": "curto"}).status_code == 422

    # Conta bloqueada não entraria nem com a senha nova — emitir ali só geraria
    # um link que não leva a lugar nenhum.
    cliente.post(f"/usuarios/{u['id']}/bloqueio", params={"motivo": "afastamento temporario"})
    r = cliente.post(rota, params={"motivo": "operador esqueceu a senha"})
    assert r.status_code == 409
    assert "bloqueado" in r.json()["detail"].lower()


def test_link_do_administrador_fica_na_trilha_sem_o_segredo(cliente):
    """Quem lê a trilha é auditor: um link vivo ali entregaria a conta."""
    u = _criar_usuario(cliente, "900013")
    link = cliente.post(f"/usuarios/{u['id']}/link-redefinicao",
                        params={"motivo": "operador esqueceu a senha"}).json()["link"]
    segredo = link.split("#redefinir=")[1]

    trilha = cliente.get("/auditoria", params={"acao": "SENHA_RESET"}).json()
    registros = trilha["itens"] if isinstance(trilha, dict) else trilha
    assert registros, "a emissão precisa aparecer na trilha"
    assert segredo not in json.dumps(registros), "o segredo não pode estar registrado"
    assert any(u["email"] in json.dumps(r) for r in registros)


def test_conta_excluida_nao_volta(cliente):
    u = _criar_usuario(cliente, "900003")
    cliente.delete(f"/usuarios/{u['id']}", params={"motivo": "desligamento definitivo"})

    assert cliente.delete(f"/usuarios/{u['id']}/bloqueio").status_code == 409
    assert cliente.patch(f"/usuarios/{u['id']}", json={"unidade": "Portaria"}).status_code == 409


def test_lugar_de_passagem_nao_vira_lotacao(cliente):
    """O elevador tem câmera, mas ninguém é lotado nele.

    A lista de setores sai das câmeras, e sem este corte o cadastro de pessoa
    ofereceria "Elevador" e "Descanso" como lotação — que não quer dizer nada.
    A ficha da câmera continua precisando dos dois para dizer onde ela está.
    """
    # As duas câmeras são deste teste: depender de outro para existir deixaria
    # o resultado na mão da ordem em que a suíte roda.
    for nome, setor, ip in [("Câmera do Elevador", "Elevador", "10.0.0.77"),
                            ("Câmera do Arquivo", "Arquivo", "10.0.0.78")]:
        r = cliente.post("/cameras", json={
            "nome": nome, "setor": setor, "cena": "corredor",
            "rtsp_url": f"rtsp://{ip}:554/s1",
        })
        assert r.status_code == 201, r.text

    todos = cliente.get("/cameras/setores").json()["setores"]
    assert "Elevador" in todos, "a ficha da câmera precisa poder apontar para lá"

    lotacao = cliente.get("/cameras/setores", params={"lotacao": "true"}).json()["setores"]
    assert "Elevador" not in lotacao
    assert "Descanso" not in lotacao
    # O corte é cirúrgico: setor de trabalho continua oferecido.
    assert set(lotacao) <= set(todos)
    assert "Arquivo" in lotacao


def test_camera_escolhe_quais_acoes_vigia(cliente):
    """Cada câmera vigia o que faz sentido no lugar onde está.

    Aglomeração num corredor de passagem é ruído; queda num arquivo com
    escada é o que importa. Antes havia um interruptor só para as dez.
    """
    codigo = cliente.post("/cameras", json={
        "nome": "Câmera do Arquivo Morto", "setor": "Arquivo", "cena": "corredor",
        "rtsp_url": "rtsp://10.0.0.91:554/s1",
        "analitico_acao": True, "acoes_ativas": ["queda", "permanencia"],
    }).json()["codigo"]

    ficha = cliente.get(f"/cameras/{codigo}").json()
    assert ficha["acoes_ativas"] == ["queda", "permanencia"]

    r = cliente.patch(f"/cameras/{codigo}", json={"acoes_ativas": ["aglomeracao"]})
    assert r.status_code == 200, r.text
    assert r.json()["acoes_ativas"] == ["aglomeracao"]

    # Desmarcar tudo é escolha legítima, e precisa persistir como tal.
    r = cliente.patch(f"/cameras/{codigo}", json={"acoes_ativas": []})
    assert r.status_code == 200 and r.json()["acoes_ativas"] == []


def test_erro_de_validacao_diz_qual_campo_e_em_portugues(cliente):
    """Recusa que não nomeia o campo é recusa que o operador não resolve.

    A tela mostrava só "Dados inválidos na requisição." — quem estava
    configurando a câmera via que não deu, nunca o quê. O servidor já montava
    a lista campo a campo; ela precisa chegar legível.
    """
    codigo = cliente.post("/cameras", json={
        "nome": "Câmera da Escada Oeste", "setor": "Recepção", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.95:554/s1",
    }).json()["codigo"]

    r = cliente.patch(f"/cameras/{codigo}", json={"fps": 999})
    assert r.status_code == 422
    problemas = r.json()["problemas"]
    assert [p["campo"] for p in problemas] == ["fps"]
    assert problemas[0]["problema"] == "o máximo é 120"

    # Mensagem que o próprio sistema escreveu chega inteira, sem o envelope
    # "Value error," que o Pydantic põe na frente.
    r = cliente.patch(f"/cameras/{codigo}", json={"acoes_ativas": ["vandalismo"]})
    assert r.status_code == 422
    problema = r.json()["problemas"][0]
    assert problema["campo"] == "acoes_ativas"
    assert problema["problema"].startswith("ação sem detector")


def test_campo_desconhecido_e_nomeado(cliente):
    """`extra=forbid` recusando em silêncio é o pior caso: o campo errado não
    aparece em lugar nenhum da tela."""
    codigo = cliente.post("/cameras", json={
        "nome": "Câmera da Escada Leste", "setor": "Recepção", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.96:554/s1",
    }).json()["codigo"]

    r = cliente.patch(f"/cameras/{codigo}", json={"inventado": 1})
    assert r.status_code == 422
    problemas = r.json()["problemas"]
    assert problemas[0]["campo"] == "inventado"
    assert problemas[0]["problema"] == "campo não reconhecido"


def test_lista_guardada_sobrevive_a_analise_desligada(cliente):
    """Desligar a análise não joga fora a seleção.

    Quem montou "só aglomeração" nesta câmera e desligou a análise para
    conferir outra coisa precisa reencontrar a escolha, não um formulário em
    branco. O que manda é `analitico_acao` — o analisador só é consultado com
    ele ligado, e a lista fica guardada para o religar.
    """
    codigo = cliente.post("/cameras", json={
        "nome": "Câmera do Saguão Norte", "setor": "Recepção", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.94:554/s1",
        "analitico_acao": True, "acoes_ativas": ["aglomeracao"],
    }).json()["codigo"]

    r = cliente.patch(f"/cameras/{codigo}",
                      json={"analitico_acao": False, "acoes_ativas": ["aglomeracao"]})
    assert r.status_code == 200, r.text
    ficha = r.json()
    assert ficha["analitico_acao"] is False
    assert ficha["acoes_ativas"] == ["aglomeracao"]


def test_api_recusa_acao_que_o_sistema_nao_detecta(cliente):
    """Fogo está na lista da interface e não tem detector.

    Gravar o pedido quieto deixaria a ficha da câmera dizendo que ela vigia
    incêndio. A recusa é o que impede a tela de mentir.
    """
    codigo = cliente.post("/cameras", json={
        "nome": "Câmera da Subestação", "setor": "Garagem", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.92:554/s1",
    }).json()["codigo"]

    r = cliente.patch(f"/cameras/{codigo}", json={"acoes_ativas": ["vandalismo"]})
    assert r.status_code == 422
    assert "detector" in r.text

    r = cliente.patch(f"/cameras/{codigo}", json={"acoes_ativas": ["sambar"]})
    assert r.status_code == 422


def test_acao_ligada_sem_a_zona_que_ela_exige_aparece_no_diagnostico(cliente):
    """Invasão de perímetro sem perímetro desenhado não dispara nunca.

    A configuração aceita — a zona pode ser desenhada depois — mas a saúde da
    câmera precisa dizer que, do jeito que está, aquilo não vigia nada.
    """
    codigo = cliente.post("/cameras", json={
        "nome": "Câmera da Doca de Carga", "setor": "Garagem", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.93:554/s1",
        "analitico_acao": True, "acoes_ativas": ["invasao"],
    }).json()["codigo"]

    pendencias = cliente.get(f"/cameras/{codigo}/saude").json()["acoes_sem_efeito"]
    assert [p["acao"] for p in pendencias] == ["invasao"]
    assert "zona" in pendencias[0]["motivo"].lower()

    cliente.patch(f"/cameras/{codigo}", json={"zonas": [
        {"nome": "Linha da doca", "tipo": "LINHA", "pontos": [[0.1, 0.5], [0.9, 0.5]]},
    ]})
    assert cliente.get(f"/cameras/{codigo}/saude").json()["acoes_sem_efeito"] == []


def test_perimetro_desenhado_volta_inteiro_ao_reabrir(cliente, camera):
    """O que o editor grava é o que a tela lê de volta.

    A usuária desenhou um perímetro, salvou, reabriu a câmera e não achou
    nada. Este teste manda exatamente o que o editor manda — linha, cor,
    pontos, regra de invasão e lista de autorizados — e confere que tudo
    volta, inclusive `regras`, que é um JSON solto e poderia ser engolido
    sem ninguém notar.
    """
    perimetro = {
        "nome": "Portão dos fundos", "tipo": "LINHA", "cor": "#8E4EC6",
        "pontos": [[0.5, 0.0], [0.5, 1.0]],
        "regras": {"invasao": True, "autorizados": ["BIO-M-000012"]},
    }
    r = cliente.patch(f"/cameras/{camera}", json={"zonas": [perimetro]})
    assert r.status_code == 200, r.text

    lidas = cliente.get(f"/cameras/{camera}").json()["zonas"]
    assert len(lidas) == 1, "a zona tem de sobreviver ao salvamento"
    z = lidas[0]
    assert z["tipo"] == "LINHA" and z["cor"] == "#8E4EC6"
    assert z["pontos"] == [[0.5, 0.0], [0.5, 1.0]]
    assert z["regras"] == {"invasao": True, "autorizados": ["BIO-M-000012"]}, (
        "sem as regras a zona volta muda, e o desenho não vigia nada")


def test_catalogo_de_acoes_chega_a_interface(cliente):
    """A tela desenha o seletor a partir desta lista; sem ela, não desenha."""
    acoes = cliente.get("/api/info").json()["acoes"]
    from app.services.vision import CATALOGO_ACOES
    assert len(acoes) == len(CATALOGO_ACOES)
    vand = next(a for a in acoes if a["codigo"] == "vandalismo")
    assert vand["disponivel"] is False and vand["indisponivel"]
    # Fogo ganhou detector em 15/10 e precisa chegar à tela como disponível.
    fogo = next(a for a in acoes if a["codigo"] == "fogo")
    assert fogo["disponivel"] is True
    invasao = next(a for a in acoes if a["codigo"] == "invasao")
    assert invasao["disponivel"] is True and invasao["exige_zona"] == "LINHA"


def test_placa_lida_chega_ao_registro(cliente, camera):
    """O caminho inteiro: a leitura entra em `DeteccaoIn` e sai na consulta.

    O serviço de placa é exercitado à parte, em tests/test_worker.py; aqui o
    que se prova é que o texto não se perde entre o worker e a ficha.
    """
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "carro", "confianca": 0.93, "placa": "SZQ3B16",
    })
    assert r.status_code == 201, r.text
    codigo = r.json()["codigo"]

    ficha = cliente.get(f"/deteccoes/{codigo}").json()
    assert ficha["placa"] == "SZQ3B16"


# ----------------------------------------------------------------------
# Sala vigiada: alerta em quem ENTRA e em quem SAI
# ----------------------------------------------------------------------
def _sala(cliente, nome, ip, vigiada=True):
    return cliente.post("/cameras", json={
        "nome": nome, "setor": "Reunião", "cena": "praca",
        "rtsp_url": f"rtsp://{ip}:554/s1", "alerta_presenca": vigiada,
    }).json()["codigo"]


def _ocupar(inferencia, codigo, quantas):
    """Uma volta da inferência com `quantas` pessoas firmes no quadro."""
    from app.services.vision import ObjetoDetectado
    # Caixa grande e confiança alta: o que o registro aceita (0,70 e 44px).
    return [ObjetoDetectado("pessoa", 0.9, (100 + i * 200, 100, 220 + i * 200, 500))
            for i in range(quantas)]


def test_sala_vigiada_alerta_quem_entra(cliente):
    """O pedido: avisar quando alguém entra na sala, autorizado ou não."""
    from sqlalchemy import select

    from app.database import db_context
    from app.models import Camera
    from app.services.inferencia import Inferencia

    codigo = _sala(cliente, "Câmera da Sala de Reunião B", "10.0.0.97")

    async def rodar():
        inf = Inferencia()
        vistos = []
        async with db_context() as db:
            cam = (await db.execute(
                select(Camera).where(Camera.codigo == codigo))).scalar_one()
            # sala vazia -> primeira volta só estabelece a referência
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 0), None)
            vistos.append(inf._ocupacao[cam.codigo])
            # entra uma
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 1), None)
            vistos.append(inf._ocupacao[cam.codigo])
        return vistos

    vistos = cliente.portal.call(rodar)
    assert vistos == [0, 1]

    alertas = cliente.get("/alertas", params={"camera": codigo}).json()["itens"]
    titulos = [a["titulo"] for a in alertas]
    assert any("Entrada em" in t for t in titulos), titulos


def test_saida_baixa_a_ocupacao_sem_gerar_alerta(cliente):
    """Saída não é ocorrência — "a sala ficou vazia" é o fim de uma.

    Mas a queda **precisa** ser contabilizada: sem baixar a referência, a
    sala esvaziando e enchendo de novo iria de 1 para 0 e de volta para 1, e
    a entrada seguinte não seria vista como aumento. O silêncio é do alerta,
    não da contagem.
    """
    from sqlalchemy import func, select

    from app.database import db_context
    from app.models import Alerta, Camera
    from app.services.inferencia import Inferencia

    codigo = _sala(cliente, "Câmera da Sala de Reunião C", "10.0.0.96")

    async def rodar():
        inf = Inferencia()
        async with db_context() as db:
            cam = (await db.execute(
                select(Camera).where(Camera.codigo == codigo))).scalar_one()
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 1), None)
            antes = (await db.execute(select(func.count()).select_from(Alerta))).scalar_one()

            # esvazia: duas voltas, porque a queda exige confirmação
            for _ in range(2):
                await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 0), None)
            vazia = inf._ocupacao[cam.codigo]
            sem_alerta = (await db.execute(select(func.count()).select_from(Alerta))).scalar_one()

            # entra alguém de novo: tem de ser visto como entrada
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 1), None)
            depois = (await db.execute(select(func.count()).select_from(Alerta))).scalar_one()
        return antes, vazia, sem_alerta, depois

    antes, vazia, sem_alerta, depois = cliente.portal.call(rodar)
    assert vazia == 0, "a contagem precisa acompanhar a sala esvaziando"
    assert sem_alerta == antes, "esvaziar não pode gerar alerta"
    assert depois == antes + 1, "a entrada seguinte precisa ser vista"

    titulos = [a["titulo"] for a in
               cliente.get("/alertas", params={"camera": codigo}).json()["itens"]]
    assert all("Saída" not in t for t in titulos), titulos


def test_presenca_continua_nao_gera_alerta(cliente):
    """O defeito que isto conserta: sete alertas em seis minutos para as
    mesmas duas pessoas. Presença que dura não é notícia; a mudança é."""
    from sqlalchemy import func, select

    from app.database import db_context
    from app.models import Alerta, Camera
    from app.services.inferencia import Inferencia

    codigo = _sala(cliente, "Câmera da Sala de Reunião D", "10.0.0.95")

    async def rodar():
        inf = Inferencia()
        async with db_context() as db:
            cam = (await db.execute(
                select(Camera).where(Camera.codigo == codigo))).scalar_one()
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 3), None)
            antes = (await db.execute(select(func.count()).select_from(Alerta))).scalar_one()
            for _ in range(6):        # seis voltas com a sala do mesmo jeito
                await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 3), None)
            depois = (await db.execute(select(func.count()).select_from(Alerta))).scalar_one()
        return antes, depois

    antes, depois = cliente.portal.call(rodar)
    assert depois == antes, "sala parada não pode gerar alerta nenhum"


def test_alerta_de_sala_sempre_carrega_imagem(cliente):
    """Alerta sem imagem obriga quem revisa a ir procurar a captura.

    A saída não tem detecção para anexar — ninguém é fotografado indo embora —
    e a entrada tem a detecção deduplicada por 30 minutos, então a mesma
    pessoa voltando à sala gerava alerta com a coluna CAPTURA em branco. Por
    isso o alerta guarda o próprio quadro.
    """
    import numpy as np
    from sqlalchemy import select

    from app.database import db_context
    from app.models import Camera
    from app.services.inferencia import Inferencia

    codigo = _sala(cliente, "Câmera da Sala de Reunião F", "10.0.0.93")
    quadro = np.full((180, 320, 3), 40, dtype=np.uint8)   # quadro qualquer

    async def rodar():
        inf = Inferencia()
        async with db_context() as db:
            cam = (await db.execute(
                select(Camera).where(Camera.codigo == codigo))).scalar_one()
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 0), None, quadro)
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 1), None, quadro)
            # esvazia (sem alerta) e enche de novo: a segunda entrada também
            # precisa carregar imagem, e ela não tem detecção nenhuma porque
            # o registro é deduplicado por 30 minutos.
            for _ in range(2):
                await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 0), None, quadro)
            await inf._vigiar_sala(db, cam, _ocupar(inf, codigo, 1), None, quadro)

    cliente.portal.call(rodar)

    alertas = cliente.get("/alertas", params={"camera": codigo}).json()["itens"]
    assert len(alertas) >= 2, [a["titulo"] for a in alertas]
    for a in alertas:
        assert a["tem_imagem"] is True, f"{a['titulo']} nasceu sem imagem"
        r = cliente.get(f"/alertas/{a['codigo']}/snapshot",
                        params={"token": cliente.headers["Authorization"].split()[1]})
        assert r.status_code == 200, r.text
        assert r.headers["content-type"] == "image/jpeg"
        assert len(r.content) > 0


def test_todo_alerta_publica_evento_para_a_tela(cliente):
    """O alerta precisa chegar à tela sozinho, sem ninguém abrir a lista.

    Antes, só o alerta de sala publicava evento próprio; o nascido de uma
    detecção viajava escondido dentro do evento `deteccao` e a tela só reagia
    na severidade 4. Num sistema integrado a presídio, alerta que depende de
    alguém estar na tela certa é alerta perdido.
    """
    from app.services import events

    publicados = []
    original = events.publicar

    async def espiao(tipo, dados):
        publicados.append((tipo, dados))
        await original(tipo, dados)

    # a rota importa `publicar` por nome; o espião entra nos dois lugares
    from app.routers import deteccoes as rota
    events.publicar = espiao
    rota.publicar = espiao
    try:
        pessoa = cliente.post("/pessoas", json={
            "nome": "Pessoa Com Acesso Restrito", "sexo": "M",
            "status": "RESTRITO", "restricao": "Mandado em aberto",
            "tipificacao": "Art. 157", "orgao_expedidor": "TJ",
        }).json()
        emb = vetor(777)

        from sqlalchemy import select
        from app.models import Pessoa

        async def gravar():
            async with db_context() as db:
                p = (await db.execute(select(Pessoa).where(
                    Pessoa.id_biometrico == pessoa["id_biometrico"]))).scalar_one()
                p.embedding = emb
        cliente.portal.call(gravar)

        camera = _sala(cliente, "Câmera do Pátio Interno", "10.0.2.5", vigiada=False)
        r = cliente.post("/deteccoes/ingest", json={
            "tipo": "FACIAL", "camera_codigo": camera,
            "ocorrido_em": datetime.now(timezone.utc).isoformat(),
            "embedding": emb, "qualidade_face": 0.9,
        })
        assert r.status_code == 201, r.text
        assert r.json()["alerta"], "pessoa restrita precisa gerar alerta"
    finally:
        events.publicar = original
        rota.publicar = original

    eventos = [d for t, d in publicados if t == "alerta"]
    assert eventos, f"nenhum evento 'alerta' publicado; vieram {[t for t, _ in publicados]}"
    e = eventos[-1]
    # O que o painel da tela precisa para se desenhar sozinho.
    for campo in ("codigo", "titulo", "detalhe", "severidade",
                  "camera_codigo", "camera_nome", "deteccao_codigo"):
        assert campo in e, f"falta '{campo}' no evento — o painel não se desenha sem ele"
    assert e["severidade"] >= 3


def test_sala_comum_nao_vigia_presenca(cliente):
    """Sem o interruptor, pessoa continua só registro — é o que impede as
    outras 25 câmeras de virarem fonte de alerta."""
    codigo = _sala(cliente, "Câmera do Corredor Norte", "10.0.0.98", vigiada=False)
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": codigo,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "pessoa", "confianca": 0.93,
    })
    assert r.status_code == 201, r.text
    assert r.json()["alerta"] is None


def test_deteccao_de_pessoa_sozinha_nao_alerta_mais(cliente):
    """Nem numa sala vigiada: o alerta agora vem da transição, não da
    detecção. Sem isto, a ingestão por API voltaria a inundar a lista."""
    codigo = _sala(cliente, "Câmera da Sala de Reunião E", "10.0.0.94")
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": codigo,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "pessoa", "confianca": 0.95,
    })
    assert r.status_code == 201, r.text
    assert r.json()["alerta"] is None


def test_interruptor_de_presenca_vai_e_volta_pela_api(cliente):
    sala = _sala(cliente, "Câmera da Sala de Perícia", "10.0.1.11", vigiada=False)
    assert cliente.get(f"/cameras/{sala}").json()["alerta_presenca"] is False
    r = cliente.patch(f"/cameras/{sala}", json={"alerta_presenca": True})
    assert r.status_code == 200 and r.json()["alerta_presenca"] is True
    r = cliente.patch(f"/cameras/{sala}", json={"alerta_presenca": False})
    assert r.json()["alerta_presenca"] is False


# ----------------------------------------------------------------------
# Configuração do sistema
# ----------------------------------------------------------------------
def test_configuracao_so_oferece_o_que_passa_a_valer(cliente):
    """A tela não pode gravar parâmetro que o sistema relê só na subida."""
    d = cliente.get("/configuracao").json()
    chaves = {p["chave"] for p in d["parametros"]}

    assert "RETENTION_DAYS_UNMATCHED" in chaves
    assert "INFERENCIA_CONFIANCA_MIN" in chaves
    # Lidos uma vez, quando o processo sobe: gravá-los mostraria um número
    # que o sistema não está usando.
    assert chaves.isdisjoint({"DEVICE", "FACE_MODEL", "EMBEDDING_DIM"})
    assert {f["variavel"] for f in d["fixos"]} >= {"DEVICE", "FACE_MODEL"}

    for p in d["parametros"]:
        if p["tipo"] != "perfis":
            assert p["minimo"] is not None and p["maximo"] is not None, p["chave"]


def test_ajuste_passa_a_valer_sem_reiniciar(cliente):
    from app.config import settings

    original = settings.RETENTION_DAYS_UNMATCHED
    try:
        r = cliente.put("/configuracao", json={"valores": {"RETENTION_DAYS_UNMATCHED": 45}})
        assert r.status_code == 200, r.text
        assert r.json()["alterados"] == ["RETENTION_DAYS_UNMATCHED"]
        assert settings.RETENTION_DAYS_UNMATCHED == 45, "o processo precisa já operar com o novo valor"

        trilha = cliente.get("/auditoria", params={"acao": "CONFIG_EDIT", "por_pagina": 5}).json()
        assert trilha["itens"][0]["alvo"] == "RETENTION_DAYS_UNMATCHED"

        # O antes/depois fica só na linha gravada: `AuditoriaOut` não publica
        # `metadados`, e é ele que diz de quanto para quanto foi.
        from sqlalchemy import select
        from app.models import Auditoria

        async def meta():
            async with db_context() as db:
                linha = (await db.execute(
                    select(Auditoria).where(Auditoria.acao == "CONFIG_EDIT")
                    .order_by(Auditoria.id.desc()).limit(1)
                )).scalar_one()
                return linha.metadados
        assert cliente.portal.call(meta)["RETENTION_DAYS_UNMATCHED"] == {"de": original, "para": 45}
    finally:
        settings.RETENTION_DAYS_UNMATCHED = original


def test_valor_fora_da_faixa_nao_encosta_em_settings(cliente):
    from app.config import settings

    antes = settings.INFERENCIA_CONFIANCA_MIN
    # O piso vai de 0,50 a 0,95; 0,05 encheria o registro de falso positivo.
    assert cliente.put("/configuracao",
                       json={"valores": {"INFERENCIA_CONFIANCA_MIN": 0.05}}).status_code == 422
    assert cliente.put("/configuracao",
                       json={"valores": {"SECRET_KEY": "x"}}).status_code == 422
    assert settings.INFERENCIA_CONFIANCA_MIN == antes


def test_lote_invalido_nao_aplica_metade(cliente):
    """Sucesso parcial deixaria a tela mostrando um estado que não existe."""
    from app.config import settings

    antes = settings.MAX_LOGIN_ATTEMPTS
    r = cliente.put("/configuracao", json={"valores": {
        "MAX_LOGIN_ATTEMPTS": 7,            # válido
        "PASSWORD_EXPIRE_DAYS": 9000,       # fora da faixa
    }})
    assert r.status_code == 422
    assert settings.MAX_LOGIN_ATTEMPTS == antes, "o campo válido do lote não pode ter sido aplicado"


def test_administrador_nao_se_bloqueia_nem_se_exclui(cliente):
    eu = cliente.get("/auth/eu").json()
    assert cliente.post(f"/usuarios/{eu['id']}/bloqueio",
                        params={"motivo": "tiro no pe"}).status_code == 409
    assert cliente.delete(f"/usuarios/{eu['id']}",
                          params={"motivo": "tiro no pe"}).status_code == 409


# ----------------------------------------------------------------------
# Exportação da trilha de auditoria
# ----------------------------------------------------------------------
# Exportação da trilha de auditoria
# ----------------------------------------------------------------------
def test_exportacao_da_trilha_sai_em_pdf(cliente):
    """O arquivo é documento: tem de abrir num leitor de PDF e dizer de onde veio."""
    r = cliente.get("/auditoria/exportar",
                    params={"finalidade": "conferencia de rotina"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert "attachment" in r.headers["content-disposition"]
    assert ".pdf" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF-"), "sem a assinatura nenhum leitor abre"
    assert r.content.rstrip().endswith(b"%%EOF"), "documento truncado"


def test_pdf_tras_a_capa_com_quem_exportou_e_por_que(cliente):
    """Sem a capa o documento é uma lista de linhas sem procedência — e é a
    procedência que um auditor confere antes de ler o conteúdo."""
    pdfium = pytest.importorskip("pypdfium2")

    r = cliente.get("/auditoria/exportar",
                    params={"finalidade": "apuracao disciplinar 42"})
    doc = pdfium.PdfDocument(io.BytesIO(r.content))
    capa = doc[0].get_textpage().get_text_range()

    assert "Trilha de auditoria" in capa
    assert "apuracao disciplinar 42" in capa          # finalidade declarada
    assert "Teste Admin" in capa                      # quem exportou
    assert "trilha completa, sem filtro" in capa      # recorte
    assert "INTEGRIDADE DA CADEIA" in capa            # selo
    # A marcação do Paragraph não pode vazar como texto no documento.
    assert "<br/>" not in capa and "&amp;" not in capa


def test_exportacao_leva_o_mesmo_recorte_que_a_tela_mostra(cliente):
    """A tela filtra e o botão fica ao lado dela. Exportar outra coisa entrega
    à corregedoria um documento que ninguém pediu."""
    pdfium = pytest.importorskip("pypdfium2")

    total_tela = cliente.get("/auditoria",
                             params={"acao": "LOGIN", "por_pagina": 1}).json()["total"]
    assert total_tela > 0, "o login do fixture já deveria estar na trilha"

    r = cliente.get("/auditoria/exportar",
                    params={"acao": "LOGIN", "finalidade": "apurar acessos"})
    assert r.status_code == 200
    doc = pdfium.PdfDocument(io.BytesIO(r.content))
    capa = doc[0].get_textpage().get_text_range()
    assert "ação=LOGIN" in capa
    assert str(total_tela) in capa

    inteiro = "".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    assert "CONSULTA_BIO" not in inteiro, "o recorte vazou para fora do filtro"


def test_exportacao_fica_registrada_com_a_finalidade_e_o_recorte(cliente):
    """Quem exportou, por quê e *o quê* — a terceira é a que faltava: sem o
    recorte, a trilha dizia "87 linhas" sem dizer quais 87."""
    cliente.get("/auditoria/exportar",
                params={"acao": "LOGIN", "finalidade": "apuracao disciplinar 42"})
    ultima = cliente.get("/auditoria", params={"acao": "EXPORTACAO"}).json()["itens"][0]
    assert ultima["finalidade"] == "apuracao disciplinar 42"
    assert "ação=LOGIN" in ultima["descricao"]

    cliente.get("/auditoria/exportar", params={"finalidade": "copia integral"})
    sem_filtro = cliente.get("/auditoria", params={"acao": "EXPORTACAO"}).json()["itens"][0]
    assert "sem filtro" in sem_filtro["descricao"]


def test_recorte_grande_demais_e_recusado_antes_da_espera(cliente, monkeypatch):
    """Montar o documento leva segundos por milhar de linhas e o navegador
    desiste em 45s. Recusar na entrada, dizendo como estreitar, evita uma
    espera longa que terminaria em erro de rede."""
    from app.routers import auditoria

    monkeypatch.setattr(auditoria, "MAX_LINHAS_PDF", 1)
    r = cliente.get("/auditoria/exportar", params={"finalidade": "tudo de uma vez"})
    assert r.status_code == 413
    detalhe = r.json()["detail"]
    assert "Estreite" in detalhe, "a recusa tem de dizer o que fazer em seguida"


def test_exportacao_exige_sessao_e_justificativa(cliente):
    """A rota só responde autenticada — foi por isso que abrir a URL numa aba
    nova nunca entregou arquivo: aba nova não leva o cabeçalho."""
    semcabecalho = TestClient(app)
    assert semcabecalho.get("/auditoria/exportar",
                            params={"finalidade": "sem sessao"}).status_code == 401

    assert cliente.get("/auditoria/exportar", params={"finalidade": "curta"}).status_code == 422
    assert cliente.get("/auditoria/exportar").status_code == 422


def test_filtro_de_placa_encontra_as_placas_lidas(cliente, camera):
    """O caso real: havia 8 placas lidas no banco e o filtro devolvia zero.

    "Placa veicular" era comparada contra a coluna `classe`, onde nunca houve
    uma detecção de classe "placa" — a leitura mora no campo `placa` da
    detecção do veículo. A opção existia na tela e não podia funcionar.
    """
    agora = datetime.now(timezone.utc).isoformat()
    r = cliente.post("/deteccoes/ingest", json={
        "camera_codigo": camera, "tipo": "OBJETO", "classe": "carro",
        "confianca": 0.9, "bbox": [0.1, 0.1, 0.2, 0.2], "placa": "ABC1D23",
        "ocorrido_em": agora,
    })
    assert r.status_code in (200, 201), r.text

    cliente.post("/deteccoes/ingest", json={
        "camera_codigo": camera, "tipo": "OBJETO", "classe": "carro",
        "confianca": 0.9, "bbox": [0.3, 0.3, 0.2, 0.2], "ocorrido_em": agora,
    })

    com_placa = cliente.get("/deteccoes", params={"classe": "placa", "por_pagina": 50}).json()
    assert com_placa["total"] >= 1
    assert all(i.get("placa") for i in com_placa["itens"]), \
        "o filtro de placa não pode trazer detecção sem placa"

    # E não vira um filtro de classe: carro sem placa continua sendo carro.
    todos_carros = cliente.get("/deteccoes", params={"classe": "carro", "por_pagina": 1}).json()
    assert todos_carros["total"] > com_placa["total"]


# ----------------------------------------------------------------------
# Exclusão de detecção: o impedimento é dito antes, não depois
# ----------------------------------------------------------------------
def test_ficha_da_deteccao_diz_quantos_alertas_ela_gerou(cliente, camera):
    """O caso real: o operador escrevia o motivo, confirmava, e só então ouvia
    que não podia apagar — com duas saídas sugeridas e nenhuma ao alcance.

    A regra não muda: apagar por baixo de um alerta o deixaria apontando para
    o vazio. O que muda é a tela saber disso quando ainda dá para escolher
    outro caminho.
    """
    agora = datetime.now(timezone.utc).isoformat()
    # Severidade alta o bastante para a detecção gerar alerta.
    r = cliente.post("/deteccoes/ingest", json={
        "camera_codigo": camera, "tipo": "ACAO", "classe": "corrida",
        "confianca": 0.95, "bbox": [0.2, 0.2, 0.2, 0.3], "ocorrido_em": agora,
    })
    assert r.status_code in (200, 201), r.text
    codigo = r.json()["codigo"]

    ficha = cliente.get(f"/deteccoes/{codigo}").json()
    assert "alertas" in ficha, "a ficha precisa dizer o que trava a exclusão"
    assert isinstance(ficha["alertas"], list), "são os códigos, para a tela poder agir"

    if ficha["alertas"]:
        # Com alerta preso, a exclusão é recusada — e a recusa diz o caminho.
        r = cliente.delete(f"/deteccoes/{codigo}", params={"motivo": "teste de bloqueio"})
        assert r.status_code == 409
        assert "falso positivo" in r.json()["detail"].lower()

        # E o caminho funciona: marcar falso positivo é sempre possível.
        rev = cliente.post(f"/deteccoes/{codigo}/revisao",
                           json={"veredito": "Falso positivo"})
        assert rev.status_code == 200
        assert rev.json()["veredito"] == "Falso positivo"


def test_deteccao_sem_alerta_nem_pessoa_pode_ser_apagada(cliente, camera):
    """O bloqueio não pode virar uma parede para tudo: registro solto, que
    nasceu errado por defeito do detector, continua removível."""
    agora = datetime.now(timezone.utc).isoformat()
    r = cliente.post("/deteccoes/ingest", json={
        "camera_codigo": camera, "tipo": "OBJETO", "classe": "carro",
        "confianca": 0.4, "bbox": [0.6, 0.6, 0.1, 0.1], "ocorrido_em": agora,
    })
    codigo = r.json()["codigo"]
    ficha = cliente.get(f"/deteccoes/{codigo}").json()
    assert ficha["alertas"] == []

    assert cliente.delete(f"/deteccoes/{codigo}",
                          params={"motivo": "rotulo inexistente"}).status_code == 204
    assert cliente.get(f"/deteccoes/{codigo}").status_code == 404


def test_encerrar_alerta_nao_libera_a_exclusao_mas_apaga_lo_libera(cliente, camera):
    """O defeito que a mensagem escondia, encontrado em 16/10/2026.

    A recusa dizia "Feche o alerta antes". Medido na API: encerrar põe o
    alerta em RESOLVIDO e **mantém** `deteccao_id`, então a contagem que
    bloqueia continua a mesma e a exclusão é recusada de novo. O operador
    fazia exatamente o que o sistema mandava e batia na mesma parede.

    O caminho que funciona é excluir o alerta. Este teste guarda os dois
    lados: encerrar não basta, apagar resolve.
    """
    agora = datetime.now(timezone.utc).isoformat()
    r = cliente.post("/deteccoes/ingest", json={
        "camera_codigo": camera, "tipo": "ACAO", "classe": "invasao",
        "confianca": 0.95, "bbox": [0.3, 0.3, 0.2, 0.3], "ocorrido_em": agora,
    })
    codigo = r.json()["codigo"]
    ficha = cliente.get(f"/deteccoes/{codigo}").json()
    if not ficha["alertas"]:
        pytest.skip("esta classe não gerou alerta nesta configuração")

    alerta = ficha["alertas"][0]

    # 1. Com o alerta de pé, a exclusão é recusada.
    assert cliente.delete(f"/deteccoes/{codigo}",
                          params={"motivo": "tentativa um"}).status_code == 409

    # 2. Encerrar o alerta — e a exclusão CONTINUA recusada.
    assert cliente.post(f"/alertas/{alerta}/encerrar",
                        json={"resolucao": "conferido no local"}).status_code == 200
    r = cliente.delete(f"/deteccoes/{codigo}", params={"motivo": "tentativa dois"})
    assert r.status_code == 409, "encerrar não desfaz o vínculo"
    assert "encerrar não basta" in r.json()["detail"], (
        "a recusa tem de dizer que encerrar não resolve — era o que faltava")

    # 3. Excluir o alerta libera.
    assert cliente.delete(f"/alertas/{alerta}",
                          params={"motivo": "registro incorreto"}).status_code in (200, 204)
    assert cliente.get(f"/deteccoes/{codigo}").json()["alertas"] == []
    assert cliente.delete(f"/deteccoes/{codigo}",
                          params={"motivo": "lixo do detector"}).status_code == 204


# ----------------------------------------------------------------------
# Recuperação de senha por link
# ----------------------------------------------------------------------
def _capturar_emails(monkeypatch) -> list:
    """Troca a entrega SMTP por uma lista — nada sai da máquina no teste.

    É também o único jeito de o teste obter o link: ele não volta mais na
    resposta, em ambiente nenhum.
    """
    from app.services import notificacoes

    enviados = []

    async def _registrar(destino, assunto, corpo, corpo_html=None):
        enviados.append((destino, assunto, corpo))

    monkeypatch.setattr(notificacoes, "_enviar_email", _registrar)
    return enviados


def _pedir_link(cliente, monkeypatch, email="t@pm.gov.br") -> str:
    """Pede a redefinição e devolve o segredo que chegou por e-mail."""
    import re

    enviados = _capturar_emails(monkeypatch)
    r = cliente.post("/auth/recuperar", json={"email": email})
    assert r.status_code == 200, r.text
    assert len(enviados) == 1, f"esperava um e-mail, saíram {len(enviados)}"
    return re.search(r"#redefinir=([\w-]+)", enviados[-1][2]).group(1)


def test_recuperacao_envia_link_e_redefine_em_uma_etapa(cliente, monkeypatch):
    """O fluxo inteiro: pede o link, abre com ele, grava a senha nova.

    Antes eram três etapas e um código de seis dígitos digitado à mão. O link
    identifica o token e, por ele, o usuário — então a tela não precisa pedir
    o e-mail de novo nem contar tentativas.
    """
    token = _pedir_link(cliente, monkeypatch)
    assert len(token) >= 20, "o segredo do link precisa de entropia, não 6 dígitos"

    # O link diz de quem é, antes de a pessoa digitar qualquer coisa.
    v = cliente.post("/auth/verificar-link", json={"token": token})
    assert v.status_code == 200
    assert v.json()["email"] == "t@pm.gov.br"

    nova = "Trocada@2026Forte"
    g = cliente.post("/auth/redefinir-senha", json={"token": token, "nova_senha": nova})
    assert g.status_code == 200, g.text

    # E a senha nova vale de verdade.
    login = cliente.post("/auth/login", json={"identificador": "t@pm.gov.br", "senha": nova})
    assert login.status_code == 200
    cliente.headers["Authorization"] = "Bearer " + login.json()["access_token"]


def test_link_so_funciona_uma_vez(cliente, monkeypatch):
    """Senão, quem pegasse o e-mail depois entraria de novo a qualquer momento."""
    token = _pedir_link(cliente, monkeypatch)
    senha = "Primeira@2026Vez"
    assert cliente.post("/auth/redefinir-senha",
                        json={"token": token, "nova_senha": senha}).status_code == 200

    r = cliente.post("/auth/redefinir-senha",
                     json={"token": token, "nova_senha": "Segunda@2026Vez"})
    assert r.status_code == 400
    assert "inválido ou expirado" in r.json()["detail"]

    login = cliente.post("/auth/login", json={"identificador": "t@pm.gov.br", "senha": senha})
    cliente.headers["Authorization"] = "Bearer " + login.json()["access_token"]


def test_link_inventado_e_recusado_sem_dizer_o_motivo(cliente):
    """A recusa é a mesma para link falso, vencido e já usado.

    Distinguir contaria a um estranho que aquele endereço tem conta — a mesma
    razão de a etapa 1 responder igual exista ou não o e-mail.
    """
    r = cliente.post("/auth/verificar-link", json={"token": "x" * 40})
    assert r.status_code == 400
    assert "inválido ou expirado" in r.json()["detail"]


def test_pedir_link_invalida_o_anterior(cliente, monkeypatch):
    """Dois links vivos ao mesmo tempo dobrariam a janela de quem interceptasse."""
    t1 = _pedir_link(cliente, monkeypatch)
    t2 = _pedir_link(cliente, monkeypatch)
    assert t1 != t2

    assert cliente.post("/auth/verificar-link", json={"token": t1}).status_code == 400
    assert cliente.post("/auth/verificar-link", json={"token": t2}).status_code == 200

    senha = "Terceira@2026Vez"
    assert cliente.post("/auth/redefinir-senha",
                        json={"token": t2, "nova_senha": senha}).status_code == 200
    login = cliente.post("/auth/login", json={"identificador": "t@pm.gov.br", "senha": senha})
    cliente.headers["Authorization"] = "Bearer " + login.json()["access_token"]


def test_resposta_e_identica_com_e_sem_conta(cliente, monkeypatch):
    """Quem pede não pode saber, pela resposta, se o e-mail tem cadastro.

    Houve uma versão que dizia "não há conta" na tela, e outra que devolvia o
    link como atalho de desenvolvimento. A primeira entregava a lista de quem
    tem cadastro a quem testasse um por um; a segunda, a própria conta. O
    corpo tem de ser o mesmo, byte a byte.
    """
    _zerar_limite_de_taxa()
    _capturar_emails(monkeypatch)

    com = cliente.post("/auth/recuperar", json={"email": "t@pm.gov.br"})
    sem = cliente.post("/auth/recuperar", json={"email": "ninguem@pm.gov.br"})

    assert com.status_code == sem.status_code == 200
    assert com.json() == sem.json()
    assert "redefinir=" not in com.text, "o link vazou na resposta"
    assert "se houver" in com.json()["mensagem"].lower()


def test_senha_vazia_e_falta_de_configuracao_nao_falha_de_envio(cliente, monkeypatch):
    """Host e usuário preenchidos com a senha em branco não é "configurado".

    Esse era o estado que fazia a tela anunciar falha de entrega a cada
    pedido: `email_configurado` olhava só host e usuário, o envio era
    tentado, e a recusa do servidor virava erro — quando o que havia era
    uma instalação que ainda não terminou de configurar.
    """
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setattr(cfg, "SMTP_USER", "alguem@gmail.com")
    monkeypatch.setattr(cfg, "SMTP_PASSWORD", "")
    assert cfg.email_configurado is False
    assert "SMTP_PASSWORD" in cfg.falta_para_email

    # IP próprio: o limitador conta por origem, e os testes anteriores já
    # gastaram a cota do endereço padrão do cliente de teste.
    r = cliente.post("/auth/recuperar", json={"email": "t@pm.gov.br"},
                     headers={"x-forwarded-for": "10.9.0.1"})
    assert r.status_code == 200
    # A tela não diz nada sobre o envio — isso contaria que a conta existe.
    assert set(r.json()) == {"mensagem", "expira_em_segundos"}
    # Quem opera vê pela trilha, e vê como falta de configuração.
    trilha = cliente.get("/auditoria", params={"acao": "SENHA_RESET_FALHA_ENVIO"}).json()
    assert any("não configurado" in l["descricao"] for l in trilha["itens"])


def test_relay_interno_dispensa_senha(cliente, monkeypatch):
    """Exigir senha de quem não a cobra inventaria um impedimento.

    Relay interno na mesma rede costuma aceitar sem credencial, e tratá-lo
    como não configurado mandaria preencher uma senha que não existe.
    """
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "SMTP_HOST", "relay.interno.pm.gov.br")
    monkeypatch.setattr(cfg, "SMTP_USER", "seguranca@pm.gov.br")
    monkeypatch.setattr(cfg, "SMTP_PASSWORD", "")
    assert cfg.email_configurado is True
    assert cfg.falta_para_email == ""


def test_envio_que_quebra_continua_sendo_falha(cliente, monkeypatch):
    """A classificação nova não pode engolir defeito de verdade."""
    from app.config import settings as cfg
    from app.services import notificacoes

    monkeypatch.setattr(cfg, "SMTP_HOST", "relay.interno.pm.gov.br")
    monkeypatch.setattr(cfg, "SMTP_USER", "seguranca@pm.gov.br")
    monkeypatch.setattr(cfg, "SMTP_PASSWORD", "")

    def _recusa(*a, **k):
        raise OSError("conexão recusada")

    monkeypatch.setattr(notificacoes, "_entregar", _recusa)

    r = cliente.post("/auth/recuperar", json={"email": "t@pm.gov.br"},
                     headers={"x-forwarded-for": "10.9.0.2"})
    assert r.status_code == 200
    assert set(r.json()) == {"mensagem", "expira_em_segundos"}
    # A falha vai para quem opera, pela trilha — e como falha, não como
    # falta de configuração.
    trilha = cliente.get("/auditoria", params={"acao": "SENHA_RESET_FALHA_ENVIO"}).json()
    assert any("e-mail falhou" in l["descricao"] for l in trilha["itens"])


def test_tela_que_abre_recebe_a_sobreposicao_corrente(cliente):
    """O mosaico não pode abrir cego.

    O WebSocket só entrega o que acontecer daqui para a frente, e o worker
    leva perto de cem segundos para voltar a cada câmera. Sem uma leitura do
    estado atual, abrir a tela dá um minuto e meio de vídeo sem caixa
    nenhuma — com gente em cena o tempo todo.
    """
    r = cliente.get("/stream/caixas")
    assert r.status_code == 200, r.text
    assert "itens" in r.json()


def test_sobreposicao_vencida_nao_semeia_caixa_fantasma():
    """Carga velha descreve cena que já passou.

    Semear a tela com ela criaria exatamente a caixa fantasma que a validade
    existe para apagar — pior que abrir sem marcação nenhuma.
    """
    import time as _t

    from app.services.inferencia import Inferencia

    motor = Inferencia()
    motor.ciclo_real = 10.0
    agora = _t.monotonic()
    motor._ultimas_caixas = {
        "CAM-NOVA": {"camera_codigo": "CAM-NOVA", "caixas": [{"classe": "pessoa"}],
                     "validade_s": 20.0, "em": agora - 2},
        "CAM-VELHA": {"camera_codigo": "CAM-VELHA", "caixas": [{"classe": "pessoa"}],
                      "validade_s": 20.0, "em": agora - 90},
    }
    vivas = motor.caixas_correntes()
    assert [c["camera_codigo"] for c in vivas] == ["CAM-NOVA"]
    # A tela que abre herda o relógio já correndo, não um prazo cheio.
    assert vivas[0]["validade_s"] < 20.0


def test_senha_da_camera_nao_sai_na_api(cliente, camera):
    """As 26 câmeras deste parque têm a senha de administrador embutida no
    endereço RTSP, e a leitura entregava o endereço inteiro a quem tivesse a
    permissão `live` — ADMIN, SUPER, OPERADOR e ANALISTA. O navegador
    recebia as senhas e não fazia nada com elas: `rtsp_url` não aparece uma
    única vez no JavaScript.
    """
    cliente.patch(f"/cameras/{camera}", json={
        "rtsp_url": "rtsp://admin:SenhaSecreta123@10.0.0.7:554/stream",
    })

    corpo = cliente.get(f"/cameras/{camera}").text
    assert "SenhaSecreta123" not in corpo, "a senha da câmera vazou na resposta"
    assert "admin:" not in corpo

    lista = cliente.get("/cameras").text
    assert "SenhaSecreta123" not in lista, "a senha vazou na listagem"

    # O que resta ainda serve para conferir host, porta e caminho.
    cam = cliente.get(f"/cameras/{camera}").json()
    assert "10.0.0.7:554/stream" in cam["rtsp_url"]


def test_endereco_mascarado_nao_apaga_o_verdadeiro(cliente, camera):
    """Quem buscar a câmera, mudar um campo e devolver o objeto inteiro
    gravaria a máscara por cima do endereço real — e a câmera pararia de
    conectar sem erro nenhum, porque a máscara é uma string válida."""
    cliente.patch(f"/cameras/{camera}", json={
        "rtsp_url": "rtsp://admin:SenhaSecreta123@10.0.0.7:554/stream",
    })
    mascarado = cliente.get(f"/cameras/{camera}").json()["rtsp_url"]

    cliente.patch(f"/cameras/{camera}", json={"rtsp_url": mascarado, "nome": "Renomeada"})

    import asyncio

    from sqlalchemy import select

    from app.database import db_context
    from app.models import Camera

    async def ler():
        async with db_context() as db:
            cam = (await db.execute(
                select(Camera).where(Camera.codigo == camera))).scalar_one()
            return cam.rtsp_url, cam.nome

    url, nome = asyncio.run(ler())
    assert "SenhaSecreta123" in url, "o endereço real foi sobrescrito pela máscara"
    assert nome == "Renomeada", "o resto da edição tem de passar"


def test_cadastro_emite_link_de_primeiro_acesso(cliente, monkeypatch):
    """A conta nasce com um link, não com uma senha na tela do administrador.

    Senha por e-mail chega legível na caixa de entrada e fica lá para
    sempre. O link guarda só o hash no banco, vale meia hora, serve uma vez
    — e a senha que a pessoa escolhe não é conhecida por mais ninguém.
    """
    enviados = []

    async def _falso(email, nome, link, minutos):
        enviados.append((email, nome, link, minutos))

    import app.routers.usuarios as mod

    monkeypatch.setattr(mod, "enviar_primeiro_acesso", _falso)

    r = cliente.post("/usuarios", json={
        "nome": "Camila Souza da Silva", "matricula": "PA-009901",
        "email": "camila.teste@pm.gov.br", "perfil": "OPERADOR",
    })
    assert r.status_code == 201, r.text
    corpo = r.json()

    assert corpo["primeiro_acesso"]["envio"] == "enviado"
    # Com o envio funcionando, o segredo não precisa passar pela tela.
    assert corpo["primeiro_acesso"]["link"] == ""
    assert "senha_provisoria" not in corpo, "a senha não pode mais sair na resposta"

    assert len(enviados) == 1
    email, nome, link, _ = enviados[0]
    assert email == "camila.teste@pm.gov.br"
    assert "#redefinir=" in link


def test_link_acompanha_a_resposta_quando_o_email_falha(cliente, monkeypatch):
    """A conta já existe: deixar o administrador sem caminho seria pior que
    o e-mail não ter saído."""
    import app.routers.usuarios as mod

    async def _falha(*a, **k):
        raise mod.FalhaNoEnvio("servidor recusou")

    monkeypatch.setattr(mod, "enviar_primeiro_acesso", _falha)

    r = cliente.post("/usuarios", json={
        "nome": "Outro Alguém", "matricula": "PA-009902",
        "email": "outro.teste@pm.gov.br", "perfil": "ANALISTA",
    })
    assert r.status_code == 201, r.text
    pa = r.json()["primeiro_acesso"]
    assert pa["envio"] == "falhou"
    assert "#redefinir=" in pa["link"], "sem o link a conta nasceria inacessível"


def test_conflito_diz_qual_campo_e_se_a_conta_esta_inativa(cliente, monkeypatch):
    """"E-mail ou matrícula" obriga a adivinhar — e a conta pode estar só
    inativa, caso em que recriar é o caminho errado: o certo é reativar,
    que preserva trilha, sessões e histórico."""
    import app.routers.usuarios as mod

    async def _nada(*a, **k):
        return None

    monkeypatch.setattr(mod, "enviar_primeiro_acesso", _nada)

    base = {"nome": "Fulana Dupla", "matricula": "PA-009903",
            "email": "dupla.teste@pm.gov.br", "perfil": "OPERADOR"}
    assert cliente.post("/usuarios", json=base).status_code == 201

    r = cliente.post("/usuarios", json={**base, "nome": "Outra Pessoa"})
    assert r.status_code == 409
    detalhe = r.json()["detail"]
    assert "e-mail" in detalhe and "matrícula" in detalhe
    assert "Fulana Dupla" in detalhe, "precisa dizer de quem é a conta"


def test_excluir_libera_o_email_para_quem_vier_depois(cliente, monkeypatch):
    """Quando alguém assume o e-mail funcional de quem saiu.

    A linha da conta excluída fica — `auditoria.usuario_id` é ON DELETE SET
    NULL e o hash de cada linha da trilha é calculado sobre ela, então
    apagar de verdade faria a cadeia acusar adulteração. Mas segurar o
    endereço para sempre travava o cadastro com um erro sem saída.
    """
    import app.routers.usuarios as mod

    async def _nada(*a, **k):
        return None

    monkeypatch.setattr(mod, "enviar_primeiro_acesso", _nada)

    email, matricula = "cadeira.unica@pm.gov.br", "PA-007777"
    r = cliente.post("/usuarios", json={
        "nome": "Primeira Titular", "matricula": matricula,
        "email": email, "perfil": "OPERADOR"})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    # Enquanto existe, o endereço é dela.
    assert cliente.post("/usuarios", json={
        "nome": "Segunda", "matricula": matricula,
        "email": email, "perfil": "OPERADOR"}).status_code == 409

    assert cliente.request(
        "DELETE", f"/usuarios/{uid}",
        params={"motivo": "Desligamento do servidor"}).status_code in (200, 204)

    # Depois da exclusão, o endereço fica livre.
    r2 = cliente.post("/usuarios", json={
        "nome": "Segunda Titular", "matricula": matricula,
        "email": email, "perfil": "OPERADOR"})
    assert r2.status_code == 201, r2.text
    assert r2.json()["id"] != uid, "tem de ser outra conta, com outra identidade"


def test_trilha_guarda_o_titular_anterior(cliente, monkeypatch):
    """O que some do cadastro segue na auditoria — é lá que o histórico mora.

    A identidade da conta antiga nunca foi o e-mail: é o `usuario_id`, que
    não muda. Mas quem audita procura pelo endereço, e ele tem de estar
    registrado no momento da exclusão.
    """
    import app.routers.usuarios as mod

    async def _nada(*a, **k):
        return None

    monkeypatch.setattr(mod, "enviar_primeiro_acesso", _nada)

    email = "some.daqui@pm.gov.br"
    uid = cliente.post("/usuarios", json={
        "nome": "Vai Sair", "matricula": "PA-007778",
        "email": email, "perfil": "ANALISTA"}).json()["id"]
    cliente.request("DELETE", f"/usuarios/{uid}",
                    params={"motivo": "Fim de contrato do terceirizado"})

    trilha = cliente.get("/auditoria", params={"acao": "USR_DEL"}).json()
    alvos = [x["alvo"] for x in trilha["itens"]]
    assert email in alvos, "a trilha precisa guardar o endereço do titular"


def test_alerta_assumido_diz_quem_assumiu(cliente, camera):
    """A tela mostrava o estado "Em curso" sem dizer de quem.

    Saber quem já está cuidando é o que evita duas guarnições no mesmo
    chamado — e o identificador do usuário, que era tudo o que a resposta
    trazia, não diz nada a quem está operando.
    """
    import uuid as _uuid

    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "ACAO", "camera_codigo": camera, "classe": "queda",
        "confianca": 0.95, "severidade": 4,
        "ocorrido_em": "2026-10-28T12:00:00Z",
    })
    assert r.status_code == 201, r.text
    codigo = r.json().get("alerta")
    assert codigo, "a queda precisa gerar alerta para este teste valer"

    antes = cliente.get(f"/alertas/{codigo}").json()
    assert antes["status"] == "ABERTO"
    assert antes["atribuido_nome"] == "", "ninguém assumiu ainda"

    assert cliente.post(f"/alertas/{codigo}/assumir").status_code == 200

    depois = cliente.get(f"/alertas/{codigo}").json()
    assert depois["status"] == "EM_ANDAMENTO"
    assert depois["atribuido_nome"], "o nome de quem assumiu tem de vir"
    assert _uuid.UUID(depois["atribuido_a"])


# ----------------------------------------------------------------------
# A trilha sob concorrência, e o diagnóstico que ela dá
# ----------------------------------------------------------------------
def test_gravacoes_simultaneas_nao_bifurcam_a_cadeia():
    """A corrida que fazia a trilha acusar adulteração sem ninguém tocar nela.

    `registrar` lê a última linha e usa o hash dela como elo. Sem serializar,
    duas requisições concorrentes leem a *mesma* última linha e a segunda
    grava um elo apontando para antes da primeira.

    Não é hipótese: na trilha deste sistema são 79 colisões envolvendo 162 das
    2.152 linhas, com pares gravados a 26 ms de distância compartilhando o
    mesmo `hash_anterior`. Reproduzido em banco limpo antes da correção: 12
    escritores simultâneos por 6 rodadas renderam 66 elos quebrados em 72
    linhas. Depois dela, 1 em 432 ao longo de seis execuções.
    """
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import audit
    from app.models import Auditoria, Base

    async def ensaiar():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Sessao = async_sessionmaker(engine, expire_on_commit=False)

        async def escrever(n: int):
            async with Sessao() as db:
                await audit.registrar(db, acao="TESTE", descricao=f"linha {n}")
                await db.commit()

        await asyncio.gather(*(escrever(i) for i in range(12)))

        async with Sessao() as db:
            linhas = (await db.execute(
                select(Auditoria).order_by(Auditoria.id.asc()))).scalars().all()
        await engine.dispose()
        return linhas

    linhas = asyncio.run(ensaiar())
    assert len(linhas) == 12

    # O conteúdo de cada linha tem de bater com o próprio hash. Esta é a
    # propriedade que sustenta a trilha como evidência, e ela não pode
    # depender de quantas requisições chegaram juntas.
    for linha in linhas:
        corpo = audit._corpo_da_linha(linha)
        assert linha.hash_atual == audit.calcular_hash(linha.hash_anterior, corpo), (
            f"linha {linha.id} não confere com o próprio hash")

    # E o encadeamento não pode bifurcar: duas linhas com o mesmo elo anterior
    # significam que as duas leram a mesma "última linha".
    elos = [linha.hash_anterior for linha in linhas]
    assert len(set(elos)) == len(elos), (
        "duas linhas nasceram com o mesmo elo anterior — a corrida voltou")


def test_verificacao_separa_adulteracao_de_defeito_conhecido():
    """Um alarme que não separa o grave do benigno acaba ignorado.

    A verificação dizia sempre a mesma frase — "a trilha pode ter sido
    alterada fora da aplicação" — para três causas de gravidade muito
    diferente. Nesta instalação as três conviviam, e a única que exige
    investigação (conteúdo reescrito) ficava indistinguível das outras duas.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app import audit
    from app.models import Base

    async def ensaiar():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Sessao = async_sessionmaker(engine, expire_on_commit=False)

        async with Sessao() as db:
            for i in range(3):
                await audit.registrar(db, acao="TESTE", descricao=f"linha {i}")
            await db.commit()

        # Alguém reescreve o texto de uma linha direto no banco.
        async with Sessao() as db:
            from sqlalchemy import select

            from app.models import Auditoria
            alvo = (await db.execute(
                select(Auditoria).order_by(Auditoria.id.asc()))).scalars().all()[1]
            alvo.descricao = "texto trocado por fora"
            await db.commit()

        async with Sessao() as db:
            saida = await audit.verificar_cadeia(db)
        await engine.dispose()
        return saida

    v = asyncio.run(ensaiar())

    assert v.integra is False
    assert v.conteudo_divergente == 1, "a linha reescrita tem de ser apontada"
    assert v.vinculo_de_usuario_perdido == 0, (
        "isto não é vínculo zerado — é texto trocado, e a diferença importa")
    assert "investigar" in v.mensagem, (
        f"a mensagem precisa dizer que exige investigação: {v.mensagem}")


# ----------------------------------------------------------------------
# Retenção: o que pode ser apagado, e sobretudo o que não pode
# ----------------------------------------------------------------------
def _cenario_retencao():
    """Quatro capturas vencidas, três delas protegidas por motivos distintos."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, Camera, Deteccao, TipoDeteccao
    from app.services import retencao

    async def ensaiar():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Sessao = async_sessionmaker(engine, expire_on_commit=False)

        ontem = datetime.now(timezone.utc) - timedelta(days=1)
        amanha = datetime.now(timezone.utc) + timedelta(days=1)
        import uuid as _uuid
        pessoa_id = _uuid.uuid4()

        async with Sessao() as db:
            cam = Camera(codigo="CAM-900", nome="Teste", setor="T", ip="1.1.1.1",
                         rtsp_url="rtsp://x/y")
            db.add(cam)
            await db.flush()

            def captura(codigo, **extra):
                return Deteccao(codigo=codigo, tipo=TipoDeteccao.FACIAL,
                                ocorrido_em=ontem, camera_id=cam.id, **extra)

            db.add_all([
                # Vencida, anônima, não revisada: esta pode sair.
                captura("FR-1", expurgo_em=ontem),
                # Vencida, mas vinculada a uma pessoa: pode integrar inquérito.
                captura("FR-2", expurgo_em=ontem, pessoa_id=pessoa_id),
                # Vencida, mas revisada por um operador: idem.
                captura("FR-3", expurgo_em=ontem, revisado=True),
                # Prazo ainda não venceu.
                captura("FR-4", expurgo_em=amanha),
                # Sem prazo nenhum: promovida a registro identificado.
                captura("FR-5", expurgo_em=None),
            ])
            await db.commit()

        async with Sessao() as db:
            antes = await retencao.vencidas(db)
            saida = await retencao.expurgar(db, origem="teste")
            await db.commit()

        async with Sessao() as db:
            from sqlalchemy import select
            sobraram = sorted(
                (await db.execute(select(Deteccao.codigo))).scalars().all())
            depois = await retencao.vencidas(db)
        await engine.dispose()
        return antes, saida, sobraram, depois

    return asyncio.run(ensaiar())


def test_expurgo_apaga_so_a_captura_anonima_vencida():
    """O que o expurgo **não** toca é o que importa aqui.

    Captura vinculada a uma pessoa ou já revisada por um operador pode
    integrar inquérito; apagá-la por prazo seria destruir prova. E prazo que
    ainda não venceu é, por definição, prazo.
    """
    antes, saida, sobraram, depois = _cenario_retencao()

    assert antes == 1, f"só uma captura estava elegível, contou {antes}"
    assert saida["removidos"] == 1
    assert sobraram == ["FR-2", "FR-3", "FR-4", "FR-5"], (
        f"o expurgo tocou no que não podia: sobraram {sobraram}")
    assert depois == 0


def test_expurgo_deixa_rastro_na_trilha():
    """Apagar sem registrar é apagar sem prestar contas — e a LGPD cobra as
    duas coisas: eliminar no prazo, e poder demonstrar que eliminou."""
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Auditoria, Base
    from app.services import retencao

    async def ensaiar():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Sessao = async_sessionmaker(engine, expire_on_commit=False)
        async with Sessao() as db:
            # Sem nada vencido: não pode nascer linha de auditoria dizendo
            # que houve expurgo quando não houve.
            await retencao.expurgar(db, origem="teste")
            await db.commit()
        async with Sessao() as db:
            linhas = (await db.execute(select(Auditoria))).scalars().all()
        await engine.dispose()
        return linhas

    assert asyncio.run(ensaiar()) == [], (
        "expurgo sem nada a apagar não pode registrar que apagou")


def test_retencao_automatica_vem_desligada():
    """Apagar é irreversível. Quem decide quando a política passa a valer de
    fato é quem responde por ela — o padrão só avisa."""
    from app.config import settings

    assert settings.RETENCAO_INTERVALO_HORAS == 0, (
        "o expurgo automático não pode vir ligado por padrão")


# ----------------------------------------------------------------------
# O link que sai por e-mail precisa funcionar na máquina de quem recebe
# ----------------------------------------------------------------------
def test_link_de_redefinicao_usa_o_endereco_publico(monkeypatch):
    """O e-mail chegava com `http://localhost:8000/#redefinir=...`.

    `localhost` na caixa de entrada de outra pessoa é a máquina *dela*, onde
    não há sistema nenhum. O envio dava certo, o link chegava, e não abria —
    falha silenciosa, porque nada no servidor tem como saber disso.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "APP_URL", "https://seguranca.pp.gov.br")
    assert settings.url_publica == "https://seguranca.pp.gov.br"
    assert "localhost" not in settings.url_publica

    # Barra final copiada da barra de endereço não pode virar `//#redefinir`.
    monkeypatch.setattr(settings, "APP_URL", "http://10.0.0.5:8000/")
    assert f"{settings.url_publica}/#redefinir=x" == \
        "http://10.0.0.5:8000/#redefinir=x"

    # Sem APP_URL, cai no endereço local — que é o certo numa máquina só.
    monkeypatch.setattr(settings, "APP_URL", "")
    assert settings.url_publica == settings.url_local.rstrip("/")


def test_nenhum_link_enviado_monta_endereco_pelo_url_local():
    """A guarda contra a regressão: `url_local` descreve onde o servidor
    escuta e serve ao console; quem viaja é `url_publica`."""
    import re
    from pathlib import Path

    raiz = Path(__file__).resolve().parent.parent
    suspeitas = []
    for arquivo in sorted((raiz / "app" / "routers").glob("*.py")):
        for n, linha in enumerate(
                arquivo.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"url_local.*#redefinir", linha):
                suspeitas.append(f"{arquivo.name}:{n}: {linha.strip()}")

    assert not suspeitas, (
        "link de redefinição montado com o endereço de escuta:\n"
        + "\n".join(suspeitas))


def test_avisa_quando_o_link_vai_sair_inutil(monkeypatch):
    """Sem APP_URL e com e-mail configurado, o arranque tem de dizer."""
    from app.config import settings

    monkeypatch.setattr(settings, "APP_URL", "")
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    monkeypatch.setattr(type(settings), "falta_para_email",
                        property(lambda self: ""))
    assert "APP_URL" in settings.falta_url_publica

    # Com APP_URL definida, nada a avisar.
    monkeypatch.setattr(settings, "APP_URL", "http://10.0.0.5:8000")
    assert settings.falta_url_publica == ""


# ----------------------------------------------------------------------
# Aviso de "não há conta": pela tela, nunca por e-mail
# ----------------------------------------------------------------------
def _zerar_limite_de_taxa():
    """Apaga os contadores de tentativa entre um ensaio e outro.

    `/auth/recuperar` aceita 5 pedidos por IP a cada 15 minutos, e na suíte
    todos os testes chegam do mesmo cliente. Somados, estouram o teto — e o
    429 aparece como falha de quem testa, não do que está sob teste.
    """
    from app.database import get_redis

    volatil = get_redis()
    dados = getattr(volatil, "_dados", None)
    if dados is None:                   # Redis de verdade: nada a limpar aqui
        return
    expira = getattr(volatil, "_expira", {})
    for chave in [k for k in dados if k.startswith("rl:")]:
        dados.pop(chave, None)
        expira.pop(chave, None)


def test_e_mail_sem_conta_nao_dispara_mensagem_nenhuma(cliente, monkeypatch):
    """Endereço sem conta: nada sai por e-mail.

    Houve uma versão que mandava "não há conta vinculada" para o endereço
    digitado. Quem errava uma letra do próprio e-mail fazia o sistema
    escrever para um estranho — e o envio, feito pela conta de quem opera,
    ainda aparecia na caixa dessa conta como se fosse dela.
    """
    _zerar_limite_de_taxa()
    enviados = _capturar_emails(monkeypatch)

    r = cliente.post("/auth/recuperar", json={"email": "naoexiste@pm.gov.br"})

    assert r.status_code == 200
    assert enviados == [], f"endereço sem conta recebeu e-mail: {enviados}"


def test_conta_existente_recebe_so_o_link(cliente, monkeypatch):
    """Ela recebe uma mensagem só: a do link, no endereço cadastrado."""
    _zerar_limite_de_taxa()
    enviados = _capturar_emails(monkeypatch)

    r = cliente.post("/auth/recuperar", json={"email": "t@pm.gov.br"})

    assert r.status_code == 200
    assert [d for d, *_ in enviados] == ["t@pm.gov.br"]
    assert "redefinição de senha" in enviados[0][1]


def test_a_sondagem_de_enderecos_fica_na_trilha(cliente):
    """A contrapartida de a tela contar que não há conta.

    Dizer na tela resolve o erro de digitação na hora e, em troca, dá a quem
    já está na rede interna uma forma de descobrir quais endereços têm conta.
    A troca só se sustenta se a sondagem deixar rastro: sem registro, ela é
    silenciosa e ninguém nunca saberia que aconteceu.

    Uma tentativa é rotina e entra como `info`. A repetição vinda do mesmo
    lugar é o que denuncia varredura, e sobe para `warn` — é por aí que quem
    audita acha, sem ler linha por linha.
    """
    _zerar_limite_de_taxa()

    for _ in range(4):
        cliente.post("/auth/recuperar", json={"email": "sondando@pm.gov.br"})

    trilha = cliente.get("/auditoria", params={"acao": "SENHA_RESET_SEM_CONTA"})
    assert trilha.status_code == 200
    # Só as deste ensaio: a ação é a mesma de outros testes da sessão, e o
    # endereço é o que separa uma sondagem de outra — que é justamente a
    # informação que o registro precisa carregar.
    linhas = [l for l in trilha.json()["itens"]
              if l["alvo"] == "sondando@pm.gov.br"]

    assert len(linhas) >= 4, (
        f"a sondagem não ficou registrada: {len(linhas)} linha(s) para este "
        "endereço")
    assert any(l["severidade"] == "warn" for l in linhas), (
        "a repetição vinda do mesmo lugar tem de subir de severidade")
    assert any(l["severidade"] == "info" for l in linhas), (
        "a primeira tentativa é rotina e não pode nascer como alarme")


def test_endereco_com_conta_nao_vira_linha_de_sondagem(cliente):
    """Quem tem conta e pede redefinição não é sondagem.

    Registrar os dois iguais encheria a trilha de rotina e afogaria o sinal
    que ela existe para dar.
    """
    _zerar_limite_de_taxa()

    antes = cliente.get("/auditoria", params={"acao": "SENHA_RESET_SEM_CONTA"})
    cliente.post("/auth/recuperar", json={"email": "t@pm.gov.br"})
    depois = cliente.get("/auditoria", params={"acao": "SENHA_RESET_SEM_CONTA"})

    assert depois.json()["total"] == antes.json()["total"], (
        "pedido de conta existente virou linha de sondagem")


def test_link_nao_volta_na_resposta_nem_em_desenvolvimento(cliente, monkeypatch):
    """Com o servidor aberto na rede, link na resposta é a conta na mão.

    Bastava saber o e-mail de alguém para pedir, receber o link na tela e
    trocar a senha dessa pessoa. O link só pode sair por e-mail.
    """
    from app.config import settings as cfg

    _zerar_limite_de_taxa()
    monkeypatch.setattr(cfg, "ENV", "development")
    _capturar_emails(monkeypatch)

    r = cliente.post("/auth/recuperar", json={"email": "t@pm.gov.br"})

    assert "redefinir=" not in r.text
    assert "link_debug" not in r.json()


def test_repetir_o_endereco_errado_continua_sem_e_mail(cliente, monkeypatch):
    """Insistir no endereço errado não muda nada: nenhuma mensagem sai.

    É o que impede o formulário de virar relé de spam assinado pelo
    SEGURANÇA — quem não está autenticado não escolhe destinatário.
    """
    _zerar_limite_de_taxa()
    enviados = _capturar_emails(monkeypatch)

    respostas = [cliente.post("/auth/recuperar",
                              json={"email": "alvo-de-spam@pm.gov.br"})
                 for _ in range(4)]

    assert all(r.status_code == 200 for r in respostas)
    assert len({r.json()["mensagem"] for r in respostas}) == 1
    assert enviados == []


def test_o_modelo_de_sem_conta_nao_existe_mais():
    """O texto antigo não pode voltar por descuido."""
    from app.services import notificacoes

    assert not hasattr(notificacoes, "MODELO_SEM_CONTA")
    assert not hasattr(notificacoes, "enviar_aviso_sem_conta")


def test_e_mail_de_redefinicao_sai_em_texto_e_html(monkeypatch):
    """Botão para quem lê HTML, endereço por extenso para quem não lê.

    O e-mail saía só em texto, e o Gmail mostrava o link cru — com cara de
    mensagem improvisada, que é a cara do golpe que se quer evitar.
    """
    import asyncio

    from app.services import notificacoes

    capturadas = []
    monkeypatch.setattr(notificacoes.settings, "SMTP_HOST", "relay.interno")
    monkeypatch.setattr(notificacoes.settings, "SMTP_USER", "seguranca@pm.gov.br")
    monkeypatch.setattr(notificacoes, "_entregar", capturadas.append)

    link = "http://10.0.0.5:8000/#redefinir=abc_DEF-123"
    asyncio.run(notificacoes.enviar_link_recuperacao(
        "t@pm.gov.br", "Ana <b>Teste</b>", link, 30))

    msg = capturadas[0]
    assert msg.get_content_type() == "multipart/alternative"
    texto = msg.get_body(("plain",)).get_content()
    pagina = msg.get_body(("html",)).get_content()

    assert link in texto, "o texto puro precisa do endereço inteiro"
    assert f'href="{link}"' in pagina, "o botão precisa apontar para o link"
    assert "Redefinir senha" in pagina
    assert "30 minutos" in texto and "30 minutos" in pagina
    # Nome vindo do cadastro não pode virar marcação.
    assert "<b>Teste</b>" not in pagina
    assert "Ana &lt;b&gt;Teste&lt;/b&gt;" in pagina


# ----------------------------------------------------------------------
# Central de alertas: período e triagem em lote (2026-11-04)
# ----------------------------------------------------------------------
def _alerta_novo(cliente, camera):
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "arma", "confianca": 0.93,
    })
    assert r.status_code == 201, r.text
    return r.json()["alerta"]["codigo"] if isinstance(r.json()["alerta"], dict) else r.json()["alerta"]


def test_filtro_de_periodo_da_central_de_alertas_filtra(cliente, camera):
    """A tela mandava o período desde sempre, e a rota não o lia."""
    _alerta_novo(cliente, camera)
    futuro = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    r = cliente.get("/alertas", params={"desde": futuro})
    assert r.status_code == 200
    assert r.json()["total"] == 0, "alerta de hoje não pode entrar num período futuro"

    passado = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert cliente.get("/alertas", params={"desde": passado}).json()["total"] >= 1


def test_encerrar_em_lote_fecha_todos_e_audita_cada_um(cliente, camera):
    """Mesmas regras do individual: desfecho obrigatório e uma linha de
    trilha por alerta — o lote não apaga quem encerrou o quê."""
    codigos = [_alerta_novo(cliente, camera) for _ in range(3)]

    curto = cliente.post("/alertas/encerrar-lote", json={"codigos": codigos, "resolucao": "curto"})
    assert curto.status_code == 422, "desfecho curto demais deve ser recusado"

    antes = cliente.get("/auditoria", params={"acao": "ALERTA_FECHA"}).json()["total"]
    r = cliente.post("/alertas/encerrar-lote", json={
        "codigos": codigos + ["ALR-NAO-EXISTE"],
        "resolucao": "Falso positivo: o operador conferiu a imagem e não houve a ocorrência."})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["encerrados"] == 3
    assert corpo["nao_encontrados"] == ["ALR-NAO-EXISTE"]
    for c in codigos:
        assert cliente.get(f"/alertas/{c}").json()["status"] == "RESOLVIDO"
    depois = cliente.get("/auditoria", params={"acao": "ALERTA_FECHA"}).json()["total"]
    assert depois - antes == 3, "cada alerta do lote precisa da sua linha na trilha"

    # De novo: os três já estão encerrados — o lote não falha, só informa.
    r2 = cliente.post("/alertas/encerrar-lote", json={
        "codigos": codigos, "resolucao": "Segunda tentativa sobre os mesmos alertas."})
    assert r2.json()["encerrados"] == 0
    assert sorted(r2.json()["ja_encerrados"]) == sorted(codigos)


def test_filtro_bio_n_lista_os_bio_n_e_a_lista_padrao_nao(cliente):
    """O cartão BIO-N da base filtra por eles e abria uma tabela vazia: a rota
    tirava os BIO-N da lista até quando eram pedidos."""
    criada = cliente.post("/pessoas", json={
        "nome": "Nao Identificado Para Listar", "sexo": "N", "status": "VISITANTE"
    }).json()
    id_n = criada["id_biometrico"]

    so_n = cliente.get("/pessoas", params={"sexo": "N", "por_pagina": 200}).json()
    assert id_n in [p["id_biometrico"] for p in so_n["itens"]], "pedido, o BIO-N aparece"
    assert all(p["sexo"] == "N" for p in so_n["itens"])

    padrao = cliente.get("/pessoas", params={"por_pagina": 200}).json()
    assert id_n not in [p["id_biometrico"] for p in padrao["itens"]], (
        "a lista padrão continua sendo de quem tem cadastro")

    # A quantidade de capturas esperando triagem vai junto do esquema.
    esquema = cliente.get("/pessoas/esquema-ids").json()
    assert isinstance(esquema.get("aguardando_triagem"), int)


def test_recorte_da_placa_e_guardado_e_servido(cliente, camera):
    """A placa lida vem com o próprio recorte, que a ficha mostra ampliado."""
    import base64

    import cv2
    import numpy as np

    placa_img = np.full((120, 360, 3), 230, np.uint8)
    cv2.putText(placa_img, "TVM8I52", (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
    jpeg = base64.b64encode(cv2.imencode(".jpg", placa_img)[1].tobytes()).decode()

    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "OBJETO", "camera_codigo": camera,
        "ocorrido_em": datetime.now(timezone.utc).isoformat(),
        "classe": "carro", "confianca": 0.9, "placa": "TVM8I52",
        "snapshot_jpeg": jpeg, "placa_jpeg": jpeg,
    })
    assert r.status_code == 201, r.text
    codigo = r.json()["codigo"]
    det = cliente.get(f"/deteccoes/{codigo}").json()
    assert det["placa"] == "TVM8I52"
    assert det["placa_snapshot_key"], "o recorte da placa precisa ficar guardado"

    token = cliente.headers["Authorization"].split()[-1]
    img = cliente.get(f"/deteccoes/{codigo}/placa", params={"token": token})
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"


# ----------------------------------------------------------------------
# Alerta do vídeo de teste
# ----------------------------------------------------------------------
def _disparo_de_teste():
    import base64
    import cv2
    import numpy as np
    ok, jpg = cv2.imencode(".jpg", np.full((90, 160, 3), 120, np.uint8))
    return {"video": "04.avi", "acao": "corrida", "segundo": 16.56, "tempo_video": "00:16.6",
            "quadro": 414, "confianca": 0.75, "pessoa_id": 42, "velocidade_corpos_s": 2.41,
            "gabarito": "acerto", "bbox_normalizado": [0.2, 0.3, 0.3, 0.9],
            "print_jpeg": base64.b64encode(jpg.tobytes()).decode()}


def test_video_teste_vira_alerta_marcado_com_o_print(cliente):
    """Corrida tem severidade 2 e normalmente não alerta; a do teste alerta, marcada."""
    from fastapi.testclient import TestClient as TC
    # Sem `with`: não dispara a partida do servidor de novo (a inferência já
    # está no laço do cliente principal).
    local = TC(app, client=("127.0.0.1", 50123))
    r = local.post("/deteccoes/video-teste", json=_disparo_de_teste())
    assert r.status_code == 201, r.text
    alerta = r.json()["alerta"]
    assert alerta
    a = cliente.get(f"/alertas/{alerta}").json()
    assert a["titulo"] == "Corrida súbita (vídeo de teste)"
    assert "00:16.6" in a["detalhe"] and "não é ocorrência real" in a["detalhe"]
    assert a["deteccao_codigo"]
    token = cliente.headers["Authorization"].split()[1]
    img = cliente.get(f"/deteccoes/{a['deteccao_codigo']}/snapshot?token={token}")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    # A câmera técnica não entra na lista de câmeras do parque.
    assert all(c["codigo"] != "VIDEO-TESTE" for c in cliente.get("/cameras").json())


def test_video_teste_so_aceita_chamada_desta_maquina(cliente):
    r = cliente.post("/deteccoes/video-teste", json=_disparo_de_teste())
    assert r.status_code == 403


def test_alerta_de_sala_nao_repete_o_nome_quando_setor_e_igual(cliente):
    """CAM-015: câmera e setor "Reunião" — o texto saía "em Reunião (Reunião)"."""
    import asyncio
    from app.database import db_context, engine
    from app.models import Camera, StatusCamera
    from app.routers.deteccoes import alertar_presenca

    async def rodar(codigo, nome, setor):
        async with db_context() as db:
            cam = Camera(codigo=codigo, nome=nome, setor=setor,
                         status=StatusCamera.MANUTENCAO, rtsp_url="")
            db.add(cam)
            await db.flush()
            alerta = await alertar_presenca(db, cam, 1, 2)
            texto = alerta.detalhe
        await engine.dispose()
        return texto

    igual = asyncio.run(rodar("CAM-SALA1", "Reunião", "Reunião"))
    assert "em Reunião. Agora há 2" in igual and "(Reunião)" not in igual
    diferente = asyncio.run(rodar("CAM-SALA2", "Sala 3", "Diretoria"))
    assert "em Sala 3 (Diretoria)." in diferente


def test_vincular_a_ficha_nao_e_recusado_pela_ia(cliente, monkeypatch):
    """O operador confirmou que é a pessoa: sem rosto extraível, vincula mesmo assim.

    Abrir um registro novo (BIO-N) continua exigindo amostra, porque registro
    sem nenhuma amostra não reconheceria ninguém.
    """
    import asyncio
    from datetime import datetime, timezone

    from fastapi import HTTPException

    from app.database import db_context, engine
    from app.models import Camera, Deteccao, Pessoa, StatusCamera, TipoDeteccao
    from app.routers import pessoas as rota
    from app.services import evidencias, face_engine

    monkeypatch.setattr(evidencias, "ler", lambda chave: b"jpeg")
    monkeypatch.setattr(face_engine, "extrair_confirmado", lambda jpeg: None)

    async def rodar(exigir):
        async with db_context() as db:
            cam = Camera(codigo=f"CAM-V{int(exigir)}", nome="V", setor="V",
                         status=StatusCamera.MANUTENCAO, rtsp_url="")
            p = Pessoa(nome=f"Pessoa Vinculo {int(exigir)}", id_biometrico=f"BIO-M-99990{int(exigir)}",
                       sexo="M", sexo_emissao="M", status="FUNCIONARIO")
            db.add_all([cam, p])
            await db.flush()
            det = Deteccao(codigo=f"FR-TESTE-V{int(exigir)}", tipo=TipoDeteccao.FACIAL,
                           ocorrido_em=datetime.now(timezone.utc), camera_id=cam.id,
                           snapshot_key="x.jpg", severidade=1)
            db.add(det)
            await db.flush()
            try:
                aceitas, recusadas = await rota._capturas_viram_amostras(
                    db, p, [det.codigo], exigir_amostra=exigir)
                resultado = (aceitas, recusadas, det.pessoa_id == p.id)
            except HTTPException as exc:
                resultado = exc.status_code
        await engine.dispose()
        return resultado

    aceitas, recusadas, vinculada = asyncio.run(rodar(False))
    assert aceitas == ["FR-TESTE-V0"] and vinculada
    assert recusadas and recusadas[0]["vinculada"]
    assert asyncio.run(rodar(True)) == 422


def test_falso_positivo_facial_sai_da_ficha_da_pessoa(cliente):
    """Rosto que não é da pessoa: marcado falso positivo, sai da trajetória dela."""
    import asyncio
    from datetime import datetime, timezone

    from app.database import db_context, engine
    from app.models import Camera, Deteccao, Pessoa, StatusCamera, TipoDeteccao

    async def preparar():
        async with db_context() as db:
            cam = Camera(codigo="CAM-FP", nome="FP", setor="FP",
                         status=StatusCamera.MANUTENCAO, rtsp_url="")
            p = Pessoa(nome="Pessoa Falso Positivo", id_biometrico="BIO-F-999991",
                       sexo="F", sexo_emissao="F", status="FUNCIONARIO", total_deteccoes=1)
            db.add_all([cam, p])
            await db.flush()
            db.add(Deteccao(codigo="FR-TESTE-FP", tipo=TipoDeteccao.FACIAL,
                            ocorrido_em=datetime.now(timezone.utc), camera_id=cam.id,
                            pessoa_id=p.id, similaridade=0.52, limiar_aplicado=0.5,
                            severidade=1))
        await engine.dispose()

    asyncio.run(preparar())
    r = cliente.post("/deteccoes/FR-TESTE-FP/revisao", json={"veredito": "Falso positivo"})
    assert r.status_code == 200, r.text
    assert r.json()["pessoa"] is None
    d = cliente.get("/deteccoes/FR-TESTE-FP").json()
    assert d["pessoa"] is None and d["revisado"] and d["veredito"] == "Falso positivo"


def test_video_teste_de_multidao_diz_quantas_pessoas_correm(cliente):
    from fastapi.testclient import TestClient as TC
    corpo = _disparo_de_teste()
    corpo.update({"video": "UMN video data.avi", "pessoas": 12})
    r = TC(app, client=("127.0.0.1", 50125)).post("/deteccoes/video-teste", json=corpo)
    assert r.status_code == 201, r.text
    a = cliente.get(f"/alertas/{r.json()['alerta']}").json()
    assert "12 pessoas correndo" in a["detalhe"] and "pessoa 42" not in a["detalhe"]
    # "Envolvidos" da ficha da detecção é a contagem, não 1.
    assert cliente.get(f"/deteccoes/{a['deteccao_codigo']}").json()["envolvidos"] == 12


def test_captura_sem_rosto_confirmado_nao_entra_na_ficha(cliente, monkeypatch):
    """Nuca, luva, objeto: o reconhecimento apontou alguém, mas no recorte não há
    rosto. Vai para a conferência com a sugestão, não para a ficha."""
    import base64
    from datetime import datetime, timezone

    import cv2
    import numpy as np
    from fastapi.testclient import TestClient as TC

    from app.services import face_engine

    pessoa = cliente.post("/pessoas", json={"nome": "Pessoa Rosto Confirmado Teste",
                                            "sexo": "M", "status": "FUNCIONARIO"}).json()
    pid = pessoa["id"]

    async def aponta(db, emb, limiar=None):
        return face_engine.ResultadoBusca(pid, pessoa["id_biometrico"], 0.62, limiar or 0.5,
                                          pid, pessoa["id_biometrico"])
    monkeypatch.setattr(face_engine, "buscar_pessoa", aponta)
    monkeypatch.setattr(face_engine, "extrair_de_recorte", lambda jpeg: None)

    import asyncio
    from app.database import db_context, engine
    from app.models import Camera, StatusCamera

    async def criar_camera():
        async with db_context() as db:
            db.add(Camera(codigo="CAM-RC", nome="RC", setor="RC", status=StatusCamera.ONLINE,
                          rtsp_url="", analitico_facial=True))
        await engine.dispose()
    asyncio.run(criar_camera())

    ok, jpg = cv2.imencode(".jpg", np.full((120, 100, 3), 90, np.uint8))
    cam = "CAM-RC"
    corpo = {"tipo": "FACIAL", "camera_codigo": cam, "embedding": [0.01] * 512,
             "qualidade_face": 0.6, "ocorrido_em": datetime.now(timezone.utc).isoformat(),
             "snapshot_jpeg": base64.b64encode(jpg.tobytes()).decode()}
    r = TC(app, client=("127.0.0.1", 50126)).post("/deteccoes/ingest", json=corpo)
    assert r.status_code == 201, r.text
    assert r.json()["match"] is False
    d = cliente.get(f"/deteccoes/{r.json()['codigo']}").json()
    assert d["pessoa"] is None


def test_aprovacao_da_triagem_cria_ficha_com_sexo_escolhido(cliente, monkeypatch, tmp_path):
    """Grupo da triagem só vira ficha aprovado, com o sexo que o operador escolheu."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone

    from app.database import db_context, engine
    from app.models import Camera, Deteccao, StatusCamera, TipoDeteccao
    from app.routers import pessoas as rota, triagem_cadastro as tc
    from app.services import face_engine

    monkeypatch.setattr(tc, "PASTA", tmp_path)
    cods = [f"FR-TRI-{i}" for i in range(5)]
    (tmp_path / "grupos.json").write_text(json.dumps([{
        "grupo": 1, "nome": "x", "sexo": "M", "sexo_conf": 0.85, "capturas": 5,
        "coesao": 0.8, "cameras": ["TRI"], "codigos": cods, "parecidos_com": []}]))

    async def preparar():
        async with db_context() as db:
            cam = Camera(codigo="CAM-TRI", nome="TRI", setor="TRI",
                         status=StatusCamera.MANUTENCAO, rtsp_url="")
            db.add(cam); await db.flush()
            agora = datetime.now(timezone.utc)
            for i, c in enumerate(cods):
                db.add(Deteccao(codigo=c, tipo=TipoDeteccao.FACIAL, camera_id=cam.id,
                                ocorrido_em=agora - timedelta(minutes=i), snapshot_key="x.jpg",
                                severidade=1))
        await engine.dispose()
    asyncio.run(preparar())

    import numpy as np
    from types import SimpleNamespace
    monkeypatch.setattr(rota.evidencias, "ler", lambda k: b"jpeg")
    monkeypatch.setattr(face_engine, "extrair_confirmado",
                        lambda j: SimpleNamespace(embedding=np.ones(512, np.float32) / 22.6, qualidade=0.8))

    g = cliente.get("/pessoas/triagem-novas").json()["grupos"]
    assert len(g) == 1 and g[0]["nome_m"] != g[0]["nome_f"]
    # Sem escolher sexo não passa; com uma foto desmarcada, ela fica na triagem.
    r = cliente.post("/pessoas/triagem-novas/aprovar",
                     json={"chave": g[0]["chave"], "sexo": "N", "nome": g[0]["nome_f"], "codigos": cods})
    assert r.status_code == 422
    r = cliente.post("/pessoas/triagem-novas/aprovar",
                     json={"chave": g[0]["chave"], "sexo": "F", "nome": g[0]["nome_f"], "codigos": cods[:4]})
    assert r.status_code == 201, r.text
    assert r.json()["id_biometrico"].startswith("BIO-F") and r.json()["capturas"] == 4
    assert cliente.get("/pessoas/triagem-novas").json()["total"] == 0
    p = cliente.get(f"/pessoas/{r.json()['id_biometrico']}").json()
    assert p["sexo"] == "F" and p["status"] == "FUNCIONARIO"


def test_amostra_errada_nao_se_multiplica_e_sai_com_o_falso_positivo(cliente):
    """O caso da Thais (06/11/2026): uma amostra que era de um homem ficou na
    ficha dela, puxou o mesmo homem na Produção 02, a captura virou amostra
    automática e o erro se repetiu todo dia. Marcar "Falso positivo" tirava a
    captura da ficha, mas o rosto seguia no template.

    Duas travas: amostra automática precisa se parecer com DUAS amostras da
    pessoa, e o "Falso positivo" apaga a amostra que nasceu da captura.
    """
    from sqlalchemy import func, select
    from app.models import AmostraFacial, Pessoa
    from app.services import face_engine

    cam = cliente.post("/cameras", json={
        "nome": "Câmera Amostras", "setor": "Centro", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.9:554/s1",
        "limiar_facial": 0.60, "analitico_facial": True,
    }).json()["codigo"]
    pessoa = cliente.post("/pessoas", json={"nome": "Dona Das Amostras", "sexo": "F"}).json()
    dela, errada = np.array(vetor(301)), np.array(vetor(302))

    async def montar():
        async with db_context() as db:
            p = (await db.execute(select(Pessoa).where(
                Pessoa.id_biometrico == pessoa["id_biometrico"]))).scalar_one()
            for v in (dela, errada):
                db.add(AmostraFacial(pessoa_id=p.id, embedding=v.tolist(),
                                     qualidade=0.8, origem="teste"))
            await db.flush()
            await face_engine.recalcular_template(db, p)
            return p.id

    pid = cliente.portal.call(montar)

    async def contar():
        async with db_context() as db:
            return (await db.execute(select(func.count()).select_from(AmostraFacial)
                                     .where(AmostraFacial.pessoa_id == pid))).scalar_one()

    def ingerir(v):
        r = cliente.post("/deteccoes/ingest", json={
            "tipo": "FACIAL", "camera_codigo": cam,
            "ocorrido_em": datetime.now(timezone.utc).isoformat(),
            "embedding": (v / np.linalg.norm(v)).tolist(), "qualidade_face": 0.8,
        })
        assert r.status_code == 201, r.text
        return r.json()

    # Quem só se parece com a amostra errada: ainda casa, mas não vira amostra.
    d = ingerir(errada + 0.3 * np.array(vetor(303)))
    assert d["match"] is True
    assert cliente.portal.call(contar) == 2, "sem segunda amostra parecida, não acolhe"

    # Parecida com as duas: vira amostra, ligada à captura de origem.
    d = ingerir(dela + errada)
    assert d["match"] is True
    assert cliente.portal.call(contar) == 3

    # O operador diz que não é ela: a captura e a amostra saem juntas.
    rev = cliente.post(f"/deteccoes/{d['codigo']}/revisao", json={"veredito": "Falso positivo"})
    assert rev.status_code == 200, rev.text
    assert cliente.portal.call(contar) == 2, "a amostra da captura errada fica no template"


def test_grupo_da_triagem_se_vincula_a_ficha_existente(cliente, monkeypatch, tmp_path):
    """Rosto que já tem ficha, mas não foi reconhecido naquele ângulo: vincular
    junta as capturas à ficha, e só as compatíveis com ela viram amostra."""
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace

    import numpy as np
    from app.database import db_context, engine
    from app.models import Camera, Deteccao, StatusCamera, TipoDeteccao
    from app.routers import pessoas as rota, triagem_cadastro as tc
    from app.services import face_engine

    e = lambda k: np.eye(512, dtype=np.float32)[k]
    monkeypatch.setattr(tc, "PASTA", tmp_path)
    tc._cache_vetores.clear()
    monkeypatch.setattr(rota.evidencias, "ler", lambda k: b"jpeg")
    monkeypatch.setattr(face_engine, "extrair_confirmado",
                        lambda j: SimpleNamespace(embedding=e(7), qualidade=0.8))
    a = [f"FR-VA-{i}" for i in range(3)]
    b = [f"FR-VB-{i}" for i in range(5)]
    (tmp_path / "grupos.json").write_text(json.dumps([
        {"sexo": "M", "codigos": a}, {"sexo": "M", "codigos": b}]))
    np.savez(tmp_path / "vetores.npz", codigos=np.array(a + b),
             vet=np.stack([e(7)] * 3 + [e(7)] * 3 + [e(300)] * 2))

    async def preparar():
        async with db_context() as db:
            cam = Camera(codigo="CAM-VIN", nome="VIN", setor="VIN",
                         status=StatusCamera.MANUTENCAO, rtsp_url="")
            db.add(cam); await db.flush()
            agora = datetime.now(timezone.utc)
            for i, c in enumerate(a + b):
                db.add(Deteccao(codigo=c, tipo=TipoDeteccao.FACIAL, camera_id=cam.id,
                                ocorrido_em=agora - timedelta(minutes=i), snapshot_key="x.jpg",
                                severidade=1))
        await engine.dispose()
    asyncio.run(preparar())

    grupos = cliente.get("/pessoas/triagem-novas").json()["grupos"]
    ga = next(g for g in grupos if g["chave"] == a[0])
    criada = cliente.post("/pessoas/triagem-novas/aprovar", json={
        "chave": a[0], "sexo": "M", "nome": ga["nome_m"], "codigos": a}).json()
    amostras_antes = cliente.get(f"/pessoas/{criada['id_biometrico']}").json()["amostras"]

    sug = cliente.get(f"/pessoas/triagem-novas/{b[0]}/parecidas").json()["sugestoes"]
    assert sug[0]["id_biometrico"] == criada["id_biometrico"], "a ficha da pessoa vem primeiro"

    r = cliente.post("/pessoas/triagem-novas/vincular", json={
        "chave": b[0], "id_biometrico": criada["id_biometrico"], "codigos": b, "incluir_amostras": True})
    assert r.status_code == 200, r.text
    assert r.json()["capturas"] == 5
    assert r.json()["amostras_novas"] == 3, "só as 3 compatíveis viram amostra"
    p = cliente.get(f"/pessoas/{criada['id_biometrico']}").json()
    assert p["amostras"] == amostras_antes + 3
    assert cliente.get("/pessoas/triagem-novas").json()["total"] == 0, "o grupo sai da fila"
    r = cliente.post("/pessoas/triagem-novas/vincular", json={
        "chave": b[0], "id_biometrico": criada["id_biometrico"], "codigos": b})
    assert r.status_code == 404
