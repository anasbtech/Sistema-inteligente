"""Mapa de calor histórico: acumulador de ocupação e análise por período.

O acumulador recebe as caixas como a análise as publica e grava segundos de
presença por célula; a análise devolve grade, série, focos, aglomerações e o
comparativo com o período anterior — tudo a partir do que foi gravado.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.database import db_context, engine
from app.main import app
from app.models import Base


async def _preparar():
    from app.models import Perfil, SequenciaBiometrica, Sexo, Usuario
    from app.security import hash_senha
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    async with db_context() as db:
        for s in Sexo:
            db.add(SequenciaBiometrica(sexo=s, proximo=1))
        db.add(Usuario(nome="Teste Admin", email="t@pm.gov.br", matricula="T-1",
                       senha_hash=hash_senha("Teste@2026Forte"), perfil=Perfil.ADMIN,
                       unidade="Teste", senha_alterada_em=datetime.now(timezone.utc)))
    await engine.dispose()


@pytest.fixture(scope="module")
def cliente():
    asyncio.run(_preparar())
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"identificador": "t@pm.gov.br", "senha": "Teste@2026Forte"})
        c.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        yield c


def _pessoa(x, y, tid):
    return {"classe": "pessoa", "bbox": [x - .03, y - .2, .06, .2], "track_id": tid}


def test_maior_grupo_conta_quem_esta_junto():
    from app.services.ocupacao import maior_grupo
    juntos = [(.50, .50), (.52, .51), (.49, .53), (.51, .49)]
    longe = [(.05, .9), (.95, .1)]
    n, centro = maior_grupo(juntos + longe, 0.18)
    assert n == 4 and abs(centro[0] - .505) < .02


def test_acumulador_soma_presenca_pico_e_grupo_sem_contar_duas_vezes():
    from app.services.ocupacao import COLUNAS, LINHAS, Acumulador
    a = Acumulador()
    t0 = datetime(2026, 11, 13, 10, 5, tzinfo=timezone.utc)
    caixas = [_pessoa(.5, .5, 1), _pessoa(.52, .5, 2), _pessoa(.5, .52, 3), _pessoa(.1, .9, 4)]
    a.registrar("CAM-X", caixas, agora=0.0, quando=t0)          # só marca o relógio
    for i in range(1, 11):                                        # 10 voltas de 2 s
        a.registrar("CAM-X", caixas, agora=2.0 * i, quando=t0 + timedelta(seconds=2 * i))
    h = next(iter(a._horas.values()))
    assert h.observado_s == pytest.approx(20.0)
    assert h.presenca_s == pytest.approx(80.0)                    # 4 pessoas × 20 s
    assert h.pico == 4 and h.maior_grupo == 3
    assert h.trilhas == {1, 2, 3, 4}                              # distintas, não 40
    cel = min(LINHAS - 1, int(.9 * LINHAS * 1.0)) * COLUNAS + int(.1 * COLUNAS)
    assert sum(h.pessoas.values()) == pytest.approx(80.0)
    assert h.grupo_s == pytest.approx(20.0)
    # Volta com um vão enorme (câmera caiu): conta no máximo o teto, não o vão.
    a.registrar("CAM-X", [], agora=10_000.0, quando=t0 + timedelta(minutes=30))
    assert h.observado_s <= 20.0 + 30.0


def test_analise_devolve_ocupacao_focos_e_comparativo(cliente):
    from app.services.ocupacao import Acumulador
    cam = cliente.post("/cameras", json={"nome": "Salão", "setor": "Centro", "cena": "rua",
                                         "rtsp_url": "rtsp://10.0.0.9:554/s1"}).json()["codigo"]
    a = Acumulador()
    agora = datetime.now(timezone.utc).replace(minute=10, second=0, microsecond=0) - timedelta(hours=2)
    caixas = [_pessoa(.3, .7, 1), _pessoa(.31, .7, 2), _pessoa(.3, .72, 3)]
    a.registrar(cam, caixas, agora=0.0, quando=agora)
    for i in range(1, 31):
        a.registrar(cam, caixas, agora=5.0 * i, quando=agora + timedelta(seconds=5 * i))
    assert asyncio.run(a.gravar()) == 1
    # Gravar de novo a mesma hora soma, não sobrescreve.
    a.registrar(cam, caixas, agora=155.0, quando=agora + timedelta(seconds=155))
    asyncio.run(a.gravar())

    desde = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    r = cliente.get("/mapa-calor/analise", params={"camera": cam, "desde": desde})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["base"] == "ocupacao" and d["granularidade"] == "hora"
    assert d["kpis"]["presenca_s"] == pytest.approx(3 * 155.0, rel=.01)
    assert d["kpis"]["pico"] == 3 and d["kpis"]["maior_grupo"] == 3
    assert d["kpis"]["ocupacao_media"] == pytest.approx(3.0, rel=.01)
    assert d["focos"] and d["focos"][0]["regiao"].startswith("embaixo")
    assert max(max(l) for l in d["grade"]["valores"]) == 1000
    assert "anterior" in d and d["anterior"]["kpis"]["presenca_s"] == 0
    assert sum(d["zonas"]) == pytest.approx(1.0, abs=.01)

    c = cliente.get("/mapa-calor/cameras", params={"desde": desde}).json()
    linha = next(l for l in c["cameras"] if l["codigo"] == cam)
    assert linha["pico"] == 3 and linha["presenca_s"] > 0
