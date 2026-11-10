"""Inteligência veicular: passagem, rastreio, sondas, gravação e API.

Sem modelo nenhum carregado: o detector, o CLIP e a segmentação são caros e
opcionais. O que se testa aqui é a lógica que decide o que vira registro e o
que é afirmado — e é ela que não pode errar calada.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import db_context, engine
from app.main import app
from app.models import Base


def _quadro():
    q = np.full((720, 1280, 3), 90, np.uint8)
    q[200:420, 300:700] = (40, 40, 200)      # um "carro" vermelho, com textura
    q[200:420:8, 300:700] = 255
    return q


def _trilha(tid, bbox, conf=0.9, classe="carro"):
    from app.services.veiculos.rastreio import Trilha
    return Trilha(tid, bbox, conf, classe)


# ----------------------------------------------------------------------
# Passagem
# ----------------------------------------------------------------------
def test_um_veiculo_vira_uma_passagem_quando_sai_de_cena():
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    t0 = datetime.now(timezone.utc)
    prontas = []
    for k in range(6):                      # seis quadros com o mesmo carro
        prontas += p.atualizar(_quadro(), [_trilha(7, (300 + k * 5, 200, 700 + k * 5, 420))],
                               t0 + timedelta(seconds=k * .5), k * .5)
    assert prontas == [], "enquanto está em cena, não registra"
    prontas += p.atualizar(_quadro(), [], t0 + timedelta(seconds=10), 10.0)
    assert len(prontas) == 1, "saiu de cena: uma passagem, não seis registros"
    r = prontas[0]
    assert r.trilha == 7 and r.quadros == 6 and r.categoria == "carro"
    assert r.melhor_quadro is not None and r.recortes_placa


def test_carro_parado_registra_uma_vez_e_segue_rastreado():
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    t0 = datetime.now(timezone.utc)
    prontas = []
    passo = 1.0
    n = int(settings.VEICULO_PASSAGEM_MAX_S / passo) + 30
    for k in range(n):
        prontas += p.atualizar(_quadro(), [_trilha(3, (300, 200, 700, 420))],
                               t0 + timedelta(seconds=k * passo), k * passo)
    assert len(prontas) == 1, "parado por minutos: um registro só"
    assert 3 in p.abertas, "e continua rastreado"
    assert p.encerrar_todas() == [], "já registrado não volta ao encerrar"


def test_veiculo_visto_num_quadro_so_nao_vira_registro():
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    p.atualizar(_quadro(), [_trilha(1, (300, 200, 700, 420))], None, 0.0)
    assert p.atualizar(_quadro(), [], None, 30.0) == [], "um quadro só é ruído, não passagem"


def test_categoria_sai_da_votacao_dos_quadros():
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    for k, cl in enumerate(["caminhao", "carro", "carro", "carro"]):
        p.atualizar(_quadro(), [_trilha(2, (300, 200, 700, 420), 0.8, cl)], None, float(k))
    pr = p.atualizar(_quadro(), [], None, 60.0)[0]
    assert pr.categoria == "carro"


def test_carro_estacionado_que_o_rastreio_perde_nao_vira_varios_registros():
    """A Kombi parada na borda da Portaria: o detector oscila, o rastreio
    perde e recria a trilha. Medido: 12 passagens em 4 minutos. É uma."""
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    prontas, mono, tid = [], 0.0, 1
    for _ in range(5):                 # cinco "vidas" da mesma trilha, no mesmo lugar
        for _ in range(6):
            prontas += p.atualizar(_quadro(), [_trilha(tid, (300, 200, 700, 420), 0.6)], None, mono)
            mono += 0.5
        prontas += p.atualizar(_quadro(), [], None, mono + 10)   # some
        mono += 11
        tid += 1
    assert len(prontas) == 1


def test_carro_que_passa_onde_outro_esteve_parado_nao_e_engolido():
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    for k in range(6):
        p.atualizar(_quadro(), [_trilha(1, (300, 200, 700, 420))], None, k * .5)
    assert len(p.atualizar(_quadro(), [], None, 20.0)) == 1
    prontas = []
    for k in range(6):                 # outro carro, chegando de longe e andando
        x = 900 - k * 120
        prontas += p.atualizar(_quadro(), [_trilha(2, (x, 200, x + 380, 420))], None, 30 + k * .5)
    prontas += p.atualizar(_quadro(), [], None, 60.0)
    assert len(prontas) == 1, "nasceu longe do parado: é outro veículo"


def test_furgao_hesitante_sai_veiculo_e_onibus_convicto_sai_onibus():
    """A Kombi saía "ônibus 49%" na tarja (09/11/2026). Sem convicção, o
    detector não afirma porte nenhum: "veículo"."""
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    for k in range(4):
        p.atualizar(_quadro(), [_trilha(1, (300, 200, 700, 420), 0.55, "onibus")], None, float(k))
    assert p.atualizar(_quadro(), [], None, 30.0)[0].categoria == "veiculo"
    for k in range(4):
        p.atualizar(_quadro(), [_trilha(2, (100 + 90 * k, 200, 500 + 90 * k, 420), 0.97, "onibus")], None, 40.0 + k)
    assert p.atualizar(_quadro(), [], None, 90.0)[0].categoria == "onibus"


# ----------------------------------------------------------------------
# Rastreio
# ----------------------------------------------------------------------
def test_bytetrack_mantem_o_id_do_veiculo_em_movimento():
    from app.services.veiculos.rastreio import RastreadorCamera
    r = RastreadorCamera(2.0)
    ids = set()
    for k in range(8):
        dets = np.array([[100 + 25 * k, 300, 400 + 25 * k, 520, 0.9, 2],
                         [900, 100, 1000, 220, 0.8, 3]], np.float32)
        tr = r.atualizar(dets, (720, 1280, 3))
        ids |= {(t.id, t.classe) for t in tr}
    assert ids == {(1, "carro"), (2, "motocicleta")}, "dois veículos, dois ids estáveis"


def test_rastreadores_de_cameras_diferentes_nao_se_misturam():
    """O defeito do laço geral: um rastreador para todas as câmeras."""
    from app.services.veiculos.rastreio import RastreadorCamera
    a, b = RastreadorCamera(2.0), RastreadorCamera(2.0)
    det = np.array([[100, 100, 300, 250, 0.9, 2]], np.float32)
    for _ in range(3):
        ta = a.atualizar(det, (720, 1280, 3))
        tb = b.atualizar(np.zeros((0, 6), np.float32), (720, 1280, 3))
    assert ta and not tb


# ----------------------------------------------------------------------
# Sondas: nunca afirmar o que não foi validado
# ----------------------------------------------------------------------
def _sonda(pisos):
    W = np.eye(4, 2, dtype=np.float32) * 5
    return {"W": W, "b": np.zeros(2), "escala": 1.0, "classes": ["branco", "preto"],
            "pisos": np.array(pisos, float)}


def test_classe_sem_validacao_nunca_e_afirmada():
    from app.services.veiculos.identificacao import aplicar_sonda
    cls, conf, motivo = aplicar_sonda(_sonda([np.inf, 0.5]), np.array([1, 0, 0, 0], np.float32))
    assert cls == "" and conf > 0.9 and "sem exemplos validados" in motivo


def test_abaixo_do_piso_fica_nao_identificado_com_o_motivo():
    from app.services.veiculos.identificacao import aplicar_sonda
    cls, _, motivo = aplicar_sonda(_sonda([0.999, 0.999]), np.array([.2, .1, 0, 0], np.float32))
    assert cls == "" and "abaixo do piso" in motivo


def test_acima_do_piso_afirma():
    from app.services.veiculos.identificacao import aplicar_sonda
    cls, conf, motivo = aplicar_sonda(_sonda([0.6, 0.6]), np.array([1, 0, 0, 0], np.float32))
    assert cls == "branco" and not motivo


def test_treino_so_libera_classe_que_alcanca_a_precisao_alvo():
    """"a" e "c" vêm da mesma distribuição (não há como separá-las); "b" é
    distinta. Só "b" pode sair liberada, e o que sair tem de ter precisão."""
    from scripts.veiculos_treinar import pisos, validar
    rng = np.random.default_rng(0)
    ca, cb = rng.normal(size=512), rng.normal(size=512)
    X, y = [], []
    for k, centro in ((0, ca), (1, cb), (2, ca)):
        for _ in range(15):
            v = centro + rng.normal(scale=0.6, size=512)
            X.append(v / np.linalg.norm(v)); y.append(k)
    X, y = np.array(X, np.float32), np.array(y)
    probs = validar(X, y, 3)
    p, rel = pisos(probs, y, ["a", "b", "c"], 0.95)
    assert rel["b"]["liberada"], rel
    assert not (rel["a"]["liberada"] and rel["c"]["liberada"]), rel
    for r in rel.values():
        if r["liberada"]:
            assert r["precisao"] >= 0.95


# ----------------------------------------------------------------------
# Gravação e API
# ----------------------------------------------------------------------
async def _preparar_banco():
    from app.models import Perfil, SequenciaBiometrica, Sexo, Usuario
    from app.security import hash_senha

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with db_context() as db:
        for s in Sexo:
            db.add(SequenciaBiometrica(sexo=s, proximo=1))
        db.add(Usuario(nome="Teste Admin", email="v@pm.gov.br", matricula="V-1",
                       senha_hash=hash_senha("Teste@2026Forte"), perfil=Perfil.ADMIN,
                       unidade="Teste", senha_alterada_em=datetime.now(timezone.utc)))
    await engine.dispose()


@pytest.fixture(scope="module")
def cliente(tmp_path_factory):
    from app.services import evidencias
    raiz_antiga = evidencias.RAIZ
    evidencias.RAIZ = tmp_path_factory.mktemp("evid").resolve()   # nunca em evidencias/ real
    asyncio.run(_preparar_banco())
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"identificador": "v@pm.gov.br", "senha": "Teste@2026Forte"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        c.token = r.json()["access_token"]
        yield c
    evidencias.RAIZ = raiz_antiga


@pytest.fixture(scope="module")
def camera(cliente):
    r = cliente.post("/cameras", json={
        "nome": "Portaria Teste", "setor": "Entrada", "cena": "rua",
        "rtsp_url": "rtsp://10.0.0.7:554/s1", "analitico_objeto": True,
        "detecta_veiculo": True,
    })
    assert r.status_code == 201, r.text
    codigo = r.json()["codigo"]
    r = cliente.patch(f"/cameras/{codigo}", json={"inteligencia_veicular": True})
    assert r.status_code == 200, r.text
    assert r.json()["inteligencia_veicular"] is True
    return codigo


def _gravar(cliente, camera, **attrs):
    from app.services.veiculos import identificacao, persistencia
    from app.services.veiculos.servico import Retrato
    agora = datetime.now(timezone.utc)
    q = _quadro()
    r = Retrato(camera, 11, agora, agora + timedelta(seconds=6), 12, "carro", 0.81, 0.88,
                q, (300, 200, 700, 420), [q[200:420, 300:700]])
    res = identificacao.Resultado(**attrs)
    return cliente.portal.call(persistencia.gravar, r, res)


def test_passagem_gravada_aparece_no_historico_com_as_imagens(cliente, camera):
    info = _gravar(cliente, camera, cor="vermelho", cor_conf=0.97,
                   observacoes={"marca_modelo": "modelo de marca/modelo ainda não treinado",
                                "placa": "nenhuma leitura com qualidade suficiente"})
    assert info["codigo"].startswith("OD-")
    r = cliente.get("/veiculos/passagens", params={"camera": camera}).json()
    assert r["total"] == 1
    p = r["itens"][0]
    assert p["categoria"] == "carro" and p["cor"] == "vermelho" and p["deteccao"] == info["codigo"]
    assert p["marca"] == "" and "não treinado" in p["observacoes"]["marca_modelo"], \
        "sem modelo, marca vazia e o motivo — nunca um palpite"
    assert p["tem_quadro"] is True
    for tipo in ("recorte", "quadro"):
        img = cliente.get(f"/veiculos/passagens/{p['id']}/imagem/{tipo}", params={"token": cliente.token})
        assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"
    # E o registro geral continua existindo, como sempre.
    det = cliente.get(f"/deteccoes/{info['codigo']}").json()
    assert det["classe"] == "carro"


def test_filtros_do_historico(cliente, camera):
    _gravar(cliente, camera, cor="branco", cor_conf=0.9, placa="ABC1D23", placa_conf=0.95)
    assert cliente.get("/veiculos/passagens", params={"cor": "branco"}).json()["total"] == 1
    assert cliente.get("/veiculos/passagens", params={"placa": "abc-1d"}).json()["total"] == 1
    assert cliente.get("/veiculos/passagens", params={"categoria": "onibus"}).json()["total"] == 0


def test_rotulo_valida_e_sai_da_fila_de_rotulagem(cliente, camera):
    itens = cliente.get("/veiculos/passagens", params={"rotulagem": True}).json()["itens"]
    alvo = itens[0]["id"]
    r = cliente.post(f"/veiculos/passagens/{alvo}/rotulo", json={"cor": "roxo-neon"})
    assert r.status_code == 422, "cor fora da lista não entra"
    r = cliente.post(f"/veiculos/passagens/{alvo}/rotulo", json={"marca": "Fiat"})
    assert r.status_code == 422, "marca sem modelo não entra"
    r = cliente.post(f"/veiculos/passagens/{alvo}/rotulo",
                     json={"cor": "prata/cinza", "marca": "Fiat", "modelo": "Strada"})
    assert r.status_code == 200, r.text
    restantes = cliente.get("/veiculos/passagens", params={"rotulagem": True}).json()["itens"]
    assert alvo not in [x["id"] for x in restantes]
    assert cliente.get(f"/veiculos/passagens/{alvo}").json()["rotulo"]["modelo"] == "Strada"
    r = cliente.post(f"/veiculos/passagens/{alvo}/rotulo", json={"tipo": "veiculo"})
    assert r.status_code == 422, "'veículo' é falta de resposta, não rótulo"
    r = cliente.post(f"/veiculos/passagens/{alvo}/rotulo", json={"tipo": "caminhao_pequeno"})
    assert r.status_code == 200, r.text
    assert cliente.get(f"/veiculos/passagens/{alvo}").json()["rotulo"]["tipo"] == "caminhao_pequeno"


def test_estado_mostra_camera_e_modelos(cliente, camera):
    e = cliente.get("/veiculos/estado").json()
    cam = next(c for c in e["cameras"] if c["codigo"] == camera)
    assert cam["inteligencia_veicular"] is True
    assert set(e["modelos"]) == {"tipo", "cor", "marca_modelo"}
    assert e["rotulados"] >= 1


def test_imagem_exige_token(cliente, camera):
    pid = cliente.get("/veiculos/passagens").json()["itens"][0]["id"]
    r = cliente.get(f"/veiculos/passagens/{pid}/imagem/recorte", params={"token": "x"})
    assert r.status_code == 401


def test_cor_que_o_modelo_nao_conhece_nao_vira_neutra():
    """Carro amarelo (zero exemplos) saiu "branco 98%" em 09/11/2026."""
    from app.services.veiculos.identificacao import conferir_tom
    amarelo = np.zeros((200, 300, 3), np.uint8); amarelo[:] = (0, 220, 230)
    assert "cor viva" in conferir_tom(amarelo, None, "branco")
    branco = np.full((200, 300, 3), 235, np.uint8)
    assert conferir_tom(branco, None, "branco") == ""


def test_tom_diferente_da_cor_afirmada_nao_passa():
    """Moto azul saiu "vermelho"."""
    from app.services.veiculos.identificacao import conferir_tom
    azul = np.zeros((200, 300, 3), np.uint8); azul[:] = (200, 60, 20)
    assert "não confere" in conferir_tom(azul, None, "vermelho")
    vermelho = np.zeros((200, 300, 3), np.uint8); vermelho[:] = (30, 30, 210)
    assert conferir_tom(vermelho, None, "vermelho") == ""


# ----------------------------------------------------------------------
# Tipo: carro, picape, van, caminhão pequeno/grande, ônibus, moto
# ----------------------------------------------------------------------
def _sonda_falsa(classes, vencedora, pisos=None):
    """Sonda que sempre responde `vencedora` com ~99%."""
    k = len(classes)
    W = np.zeros((4, k), np.float32)
    b = np.zeros(k, np.float32); b[classes.index(vencedora)] = 6.0
    return {"W": W, "b": b, "escala": 1.0, "classes": classes,
            "pisos": np.array(pisos if pisos is not None else [0.5] * k, float), "_mtime": 0}


def test_sonda_de_tipo_separa_van_do_onibus(monkeypatch):
    from app.services.veiculos import identificacao as idf
    classes = ["caminhao_grande", "caminhao_pequeno", "carro", "onibus", "van"]
    monkeypatch.setattr(idf, "sonda", lambda nome: _sonda_falsa(classes, "van"))
    tipo, conf, motivo = idf.decidir_tipo(np.ones(4, np.float32), "onibus", 0.49)
    assert tipo == "van" and conf > 0.9 and motivo == ""


def test_sem_sonda_caminhao_nao_ganha_porte_inventado(monkeypatch):
    from app.services.veiculos import identificacao as idf
    monkeypatch.setattr(idf, "sonda", lambda nome: None)
    assert idf.decidir_tipo(np.ones(4, np.float32), "caminhao", 0.95)[0] == "veiculo"
    assert idf.decidir_tipo(np.ones(4, np.float32), "carro", 0.9)[0] == "carro"
    assert idf.decidir_tipo(np.ones(4, np.float32), "veiculo", 0.5)[0] == "veiculo"


def test_tipo_travado_na_sonda_nao_e_afirmado(monkeypatch):
    """Classe sem exemplos validados (piso infinito) não sai — vale o detector."""
    from app.services.veiculos import identificacao as idf
    classes = ["caminhao_pequeno", "carro"]
    monkeypatch.setattr(idf, "sonda", lambda nome: _sonda_falsa(classes, "caminhao_pequeno",
                                                                [np.inf, 0.5]))
    tipo, _, motivo = idf.decidir_tipo(np.ones(4, np.float32), "caminhao", 0.95)
    assert tipo == "veiculo" and "sem exemplos validados" in motivo


def test_tarja_ao_vivo_usa_o_tipo_da_trilha_e_nao_o_quadro_cru():
    from app.services.veiculos.passagem import Passagens
    from app.services.veiculos.servico import ServicoVeicular
    p = Passagens("CAM-T")
    t = _trilha(1, (300, 200, 700, 420), 0.49, "onibus")
    for k in range(3):
        p.atualizar(_quadro(), [t], None, float(k))
    assert ServicoVeicular._classe_ao_vivo(t, p)[0] == "veiculo", "ônibus a 49% não vai para a tela"
    p.abertas[1].tipo = ("van", 0.97)
    assert ServicoVeicular._classe_ao_vivo(t, p) == ("van", 0.97)


def test_kombi_parada_que_volta_a_ser_rastreada_herda_o_tipo():
    from app.services.veiculos.passagem import Passagens
    p = Passagens("CAM-T")
    for k in range(6):
        p.atualizar(_quadro(), [_trilha(1, (300, 200, 700, 420), 0.5, "onibus")], None, k * .5)
    p.abertas[1].tipo = ("van", 0.96)
    p.atualizar(_quadro(), [], None, 20.0)
    p.atualizar(_quadro(), [_trilha(2, (305, 202, 702, 421), 0.5, "onibus")], None, 25.0)
    assert p.abertas[2].tipo == ("van", 0.96) and p.abertas[2].previa_pedida


def test_passagem_de_van_vai_como_carro_no_registro_geral(cliente, camera):
    info = _gravar(cliente, camera, tipo="caminhao_grande", tipo_conf=0.97)
    det = cliente.get(f"/deteccoes/{info['codigo']}").json()
    assert det["classe"] == "caminhao", "o registro geral fala o vocabulário do detector"
    r = cliente.get("/veiculos/passagens", params={"categoria": "caminhao_grande"}).json()
    assert r["total"] == 1 and r["itens"][0]["categoria_conf"] == 0.97


def test_filtro_usa_o_tipo_e_a_cor_conferidos(cliente, camera):
    """A Ducato que a IA chamou de carro e a pessoa rotulou van aparece em "Van"."""
    _gravar(cliente, camera, tipo="carro", tipo_conf=0.03, cor="prata/cinza", cor_conf=0.7)
    alvo = cliente.get("/veiculos/passagens", params={"categoria": "carro"}).json()["itens"][0]["id"]
    r = cliente.post(f"/veiculos/passagens/{alvo}/rotulo", json={"tipo": "van", "cor": "branco"})
    assert r.status_code == 200, r.text
    vans = cliente.get("/veiculos/passagens", params={"categoria": "van"}).json()["itens"]
    assert alvo in [x["id"] for x in vans]
    carros = cliente.get("/veiculos/passagens", params={"categoria": "carro"}).json()["itens"]
    assert alvo not in [x["id"] for x in carros]
    brancos = cliente.get("/veiculos/passagens", params={"cor": "branco"}).json()["itens"]
    assert alvo in [x["id"] for x in brancos]


def test_kombi_e_tipo_proprio_e_vai_como_carro_no_registro_geral(cliente, camera):
    info = _gravar(cliente, camera, tipo="kombi", tipo_conf=0.96)
    assert cliente.get(f"/deteccoes/{info['codigo']}").json()["classe"] == "carro"
    pid = cliente.get("/veiculos/passagens", params={"categoria": "kombi"}).json()["itens"][0]["id"]
    r = cliente.post(f"/veiculos/passagens/{pid}/rotulo", json={"tipo": "kombi"})
    assert r.status_code == 200, r.text
    tipos = [t["valor"] for t in cliente.get("/veiculos/catalogo").json()["tipos"]]
    assert "kombi" in tipos and "van" in tipos


def test_excluir_rotulo_devolve_a_passagem_para_a_fila(cliente, camera):
    _gravar(cliente, camera, tipo="carro", tipo_conf=0.9)
    pid = cliente.get("/veiculos/passagens", params={"rotulagem": True}).json()["itens"][0]["id"]
    assert cliente.post(f"/veiculos/passagens/{pid}/rotulo", json={"tipo": "kombi"}).status_code == 200
    r = cliente.delete(f"/veiculos/passagens/{pid}/rotulo")
    assert r.status_code == 200 and r.json()["excluido"] is True
    assert cliente.get(f"/veiculos/passagens/{pid}").json()["rotulo"] is None
    fila = cliente.get("/veiculos/passagens", params={"rotulagem": True}).json()["itens"]
    assert pid in [x["id"] for x in fila]
    assert cliente.delete(f"/veiculos/passagens/{pid}/rotulo").json()["excluido"] is False


def test_codigos_reservados_ao_mesmo_tempo_nao_se_repetem(cliente):
    """09/11/2026: o módulo de veículos e o laço geral pegaram o mesmo código
    OD, e o recorte de uma pessoa foi gravado por cima do de um carro."""
    import asyncio
    from app.database import db_context, engine
    from app.services import sequencias

    async def reservar():
        async with db_context() as db:
            n = await sequencias.proximo_numero(db, "TESTE-CORRIDA")
            await asyncio.sleep(0.01)          # outra sessão entra aqui
            return n

    async def rodar():
        try:
            return await asyncio.gather(*[reservar() for _ in range(12)])
        finally:
            await engine.dispose()
    numeros = asyncio.run(rodar())
    assert len(set(numeros)) == len(numeros), numeros


def test_recorte_de_um_codigo_nao_e_sobrescrito(tmp_path, monkeypatch):
    from datetime import datetime
    from app.services import evidencias
    monkeypatch.setattr(evidencias, "RAIZ", tmp_path)
    quando = datetime(2026, 11, 9)
    evidencias.gravar("OD-1", quando, b"carro", sobrescrever=False)
    with pytest.raises(FileExistsError):
        evidencias.gravar("OD-1", quando, b"pessoa", sobrescrever=False)
    assert evidencias.ler(evidencias.chave_de("OD-1", quando)) == b"carro"


def test_excluir_passagem_tira_do_historico_com_o_rotulo(cliente, camera):
    _gravar(cliente, camera, tipo="carro", tipo_conf=0.9)
    pid = cliente.get("/veiculos/passagens", params={"rotulagem": True}).json()["itens"][0]["id"]
    assert cliente.post(f"/veiculos/passagens/{pid}/rotulo", json={"tipo": "carro"}).status_code == 200
    r = cliente.delete(f"/veiculos/passagens/{pid}")
    assert r.status_code == 200 and r.json()["excluida"] is True
    assert cliente.get(f"/veiculos/passagens/{pid}").status_code == 404
    assert cliente.delete(f"/veiculos/passagens/{pid}").status_code == 404


# ----------------------------------------------------------------------
# Marca/modelo: só carros, só afirmado pela sonda validada
# ----------------------------------------------------------------------
def test_marca_modelo_so_sai_da_sonda_e_confere_a_dimensao(monkeypatch):
    from app.services.veiculos import identificacao as idf, modelo_fino
    classes = ["Honda|Fit", "Volkswagen|Kombi"]
    monkeypatch.setattr(idf, "sonda", lambda nome: _sonda_falsa(classes, "Honda|Fit"))
    assert modelo_fino.afirmar(np.ones(4, np.float32))[:2] == ("Honda", "Fit")
    assert "outra versão" in modelo_fino.afirmar(np.ones(768, np.float32))[3]
    monkeypatch.setattr(idf, "sonda", lambda nome: None)
    marca, modelo, _, motivo = modelo_fino.afirmar(np.ones(4, np.float32))
    assert marca == "" and modelo == "" and motivo


def test_analisador_preenche_sugestoes_e_nao_afirma_sem_sonda(cliente, camera, monkeypatch):
    """Sem modelo validado, o carro ganha só as sugestões; moto fica de fora."""
    import json, sqlite3
    from app.config import settings
    from app.services.veiculos import identificacao as idf, modelo_fino
    from scripts import veiculos_modelo_rodar as rodar
    _gravar(cliente, camera, tipo="carro", tipo_conf=0.9)
    _gravar(cliente, camera, tipo="motocicleta", tipo_conf=0.9)
    monkeypatch.setattr(modelo_fino, "vetor", lambda img: np.ones(4, np.float32) / 2)
    monkeypatch.setattr(modelo_fino, "sugestoes",
                        lambda v, k=3: [{"marca": "Honda", "modelo": "Fit", "conf": 0.98}])
    monkeypatch.setattr(idf, "sonda", lambda nome: None)
    monkeypatch.setattr(rodar, "LOG", __import__("pathlib").Path(settings.SNAPSHOT_DIR) / "vm.log")
    c = rodar.conectar()
    rodar.analisar_pendentes(c, 500)
    linhas = c.execute("select categoria, marca, observacoes, vetor_modelo from passagens_veiculo").fetchall()
    carros = [l for l in linhas if l[0] != "motocicleta"]
    motos = [l for l in linhas if l[0] == "motocicleta"]
    assert carros and all(l[3] for l in carros)
    assert all(l[1] == "" for l in carros), "sem sonda validada, nenhum modelo é afirmado"
    assert all(json.loads(l[2])["sugestoes_modelo"][0]["modelo"] == "Fit" for l in carros)
    assert motos and all(l[3] in (None, "null") for l in motos), "moto não é analisada"
    c.close()


# ----------------------------------------------------------------------
# Mapa de calor
# ----------------------------------------------------------------------
def test_mapa_de_calor_conta_pelo_ponto_de_apoio(cliente, camera):
    from datetime import datetime, timedelta, timezone
    agora = datetime.now(timezone.utc)
    for k in range(5):
        r = cliente.post("/deteccoes/ingest", json={
            "tipo": "OBJETO", "camera_codigo": camera, "classe": "pessoa", "confianca": 0.9,
            "ocorrido_em": (agora - timedelta(minutes=k)).isoformat(),
            "bbox": [0.10, 0.20, 0.10, 0.50]})          # pés em x=0,15 y≈0,685
        assert r.status_code == 201, r.text
    m = cliente.get("/mapa-calor", params={"camera": camera, "camada": "pessoas"}).json()
    assert m["destaques"]["total"] == 5
    lin, col = int((0.2 + 0.5 * 0.97) * m["linhas"]), int(0.15 * m["colunas"])
    assert m["grade"][lin][col] == 5, "conta onde a pessoa pisa, não no peito"
    assert sum(map(sum, m["semana"])) == 5
    assert m["destaques"]["area_pico"]["regiao"].endswith("à esquerda")
    rk = cliente.get("/mapa-calor/ranking", params={"camada": "pessoas"}).json()
    assert any(c["codigo"] == camera and c["total"] >= 5 for c in rk["cameras"])
    assert cliente.get("/mapa-calor", params={"camera": camera, "camada": "xyz"}).status_code == 422
    assert cliente.get("/mapa-calor", params={"camera": "CAM-NAO", "camada": "pessoas"}).status_code == 404
