"""Aizon Guardian: gesto Signal for Help, correlação de sinais, evidência e API.

O gesto é testado com mãos sintéticas nos 21 pontos do MediaPipe, desenhadas
como a mão de verdade fica em cada tempo do gesto — e como ficam as mãos
parecidas que não são o gesto (mão aberta com o polegar para fora, punho com
o polegar por fora). Sem modelo carregado: o que se testa é a decisão.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.database import db_context, engine
from app.main import app
from app.models import Base


# ----------------------------------------------------------------------
# Mãos sintéticas (pixels; palma "para cima" na imagem)
# ----------------------------------------------------------------------
PUNHO = (100, 200)
BASES = {5: (80, 140), 9: (100, 135), 13: (118, 140), 17: (134, 150)}


def _mao(dedos: str, polegar: str, escala: float = 1.0):
    """dedos: 'abertos' | 'fechados'; polegar: 'dentro' | 'fora'."""
    p = {0: PUNHO, **BASES}
    for base in (5, 9, 13, 17):
        bx, by = BASES[base]
        if dedos == "abertos":
            p[base + 1], p[base + 2], p[base + 3] = (bx - 2, by - 25), (bx - 3, by - 37), (bx - 4, by - 48)
        else:
            p[base + 1], p[base + 2], p[base + 3] = (bx, by - 22), (bx + 3, by - 12), (bx + 4, by + 10)
    if polegar == "dentro":
        p[1], p[2], p[3], p[4] = (80, 190), (75, 175), (85, 165), (102, 160)
    else:
        p[1], p[2], p[3], p[4] = (78, 192), (62, 180), (50, 168), (40, 158)
    pts = np.array([p[i] for i in range(21)], float)
    centro = pts.mean(axis=0)
    return (centro + (pts - centro) * escala).tolist()


ABERTA_POLEGAR = _mao("abertos", "dentro")
PUNHO_POLEGAR = _mao("fechados", "dentro")
MAO_ABERTA = _mao("abertos", "fora")
PUNHO_COMUM = _mao("fechados", "fora")


def test_cada_tempo_do_gesto_e_reconhecido():
    from app.services.guardian import gesto
    assert gesto.classificar(ABERTA_POLEGAR)[0] == gesto.ABERTA_POLEGAR
    assert gesto.classificar(PUNHO_POLEGAR)[0] == gesto.PUNHO_POLEGAR


def test_maos_parecidas_nao_sao_o_gesto():
    from app.services.guardian import gesto
    assert gesto.classificar(MAO_ABERTA)[0] == gesto.OUTRO, "mão aberta comum (polegar para fora)"
    assert gesto.classificar(PUNHO_COMUM)[0] == gesto.OUTRO, "punho comum (polegar por fora)"


def test_mao_pequena_demais_nao_e_avaliada():
    """Nas câmeras de sala as mãos tinham 18–32 px: os dedos não se separam."""
    from app.services.guardian import gesto
    pequena = _mao("abertos", "dentro", escala=0.3)
    assert gesto.tamanho(pequena) < gesto.MAO_MIN_PX
    assert gesto.classificar(pequena)[0] == gesto.OUTRO


def test_a_mesma_geometria_vale_perto_e_longe():
    from app.services.guardian import gesto
    grande = _mao("fechados", "dentro", escala=2.5)
    assert gesto.classificar(grande)[0] == gesto.PUNHO_POLEGAR


def _passar(seq, quadros, t0=0.0, passo=0.25):
    sinais = []
    for i, m in enumerate(quadros):
        sinais += seq.observar([(m, 0.95)], t0 + i * passo)
    return sinais


def test_sequencia_completa_vira_sinal():
    from app.services.guardian.gesto import SequenciaGesto
    sinais = _passar(SequenciaGesto(), [ABERTA_POLEGAR] * 3 + [PUNHO_POLEGAR] * 3)
    assert len(sinais) == 1
    assert sinais[0].confianca > 0.5 and sinais[0].repeticoes == 1


def test_um_tempo_sozinho_ou_ordem_trocada_nao_e_sinal():
    from app.services.guardian.gesto import SequenciaGesto
    assert _passar(SequenciaGesto(), [ABERTA_POLEGAR] * 8) == [], "só a mão aberta (contando 'quatro')"
    assert _passar(SequenciaGesto(), [PUNHO_POLEGAR] * 8) == [], "só o punho (segurando algo)"
    assert _passar(SequenciaGesto(), [PUNHO_POLEGAR] * 3 + [ABERTA_POLEGAR] * 3) == [], "ordem trocada"


def test_sequencia_lenta_demais_nao_fecha():
    from app.services.guardian.gesto import SequenciaGesto
    seq = SequenciaGesto()
    sinais = _passar(seq, [ABERTA_POLEGAR] * 2, 0.0) + _passar(seq, [PUNHO_POLEGAR] * 3, 5.0)
    assert sinais == []


def test_um_aviso_por_mao_no_intervalo():
    from app.services.guardian.gesto import SequenciaGesto
    seq = SequenciaGesto()
    repetido = ([ABERTA_POLEGAR] * 3 + [PUNHO_POLEGAR] * 3) * 3
    assert len(_passar(seq, repetido)) == 1, "a mesma mão repetindo o gesto não vira três alertas"


# ----------------------------------------------------------------------
# Prioridade
# ----------------------------------------------------------------------
def test_prioridade_dos_sinais():
    from app.services.guardian.correlacao import avaliar
    assert avaliar(["sinal_socorro"])[0] == "CRITICA"
    assert avaliar(["briga", "queda"])[0] == "CRITICA"
    assert avaliar(["briga", "roubo"])[0] == "CRITICA"
    assert avaliar(["briga", "briga"])[0] == "CRITICA"
    assert avaliar(["briga"])[0] == "ALTA"
    assert avaliar(["roubo"])[0] == "ALTA"
    assert avaliar(["queda", "corrida"])[0] == "ALTA"
    assert avaliar(["queda"])[0] == "MEDIA"
    assert avaliar(["corrida"])[0] == "BAIXA"


# ----------------------------------------------------------------------
# Evidência cifrada
# ----------------------------------------------------------------------
def test_video_de_evidencia_fica_cifrado_no_disco(tmp_path, monkeypatch):
    import cv2
    from app.config import settings
    from app.services import evidencias
    from app.services.guardian import evidencia
    monkeypatch.setattr(evidencias, "RAIZ", tmp_path)
    monkeypatch.setattr(settings, "GUARDIAN_CHAVE", str(tmp_path / "chave.key"))
    mem = evidencia.Memoria(10)
    for i in range(5):
        q = np.full((360, 640, 3), 40 * i, np.uint8)
        cv2.putText(q, str(i), (300, 200), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 4)
        mem.guardar(q, agora=100 + i * 0.25)
    chave = evidencia.gravar_clipe("GRD-T", datetime(2026, 11, 10), mem.desde(0))
    bruto = (tmp_path / chave).read_bytes()
    assert b"\xff\xd8" not in bruto, "nenhum JPEG legível no arquivo gravado"
    lido = evidencia.ler_clipe(chave)
    assert len(lido) == 5 and lido[0][1][:2] == b"\xff\xd8"
    assert [round(t, 2) for t, _ in lido] == [0.0, 0.25, 0.5, 0.75, 1.0]


# ----------------------------------------------------------------------
# Fluxo pela API
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
        db.add(Usuario(nome="Operadora", email="g@pm.gov.br", matricula="G-1",
                       senha_hash=hash_senha("Teste@2026Forte"), perfil=Perfil.ADMIN,
                       unidade="Teste", senha_alterada_em=datetime.now(timezone.utc)))
    await engine.dispose()


@pytest.fixture(scope="module")
def cliente(tmp_path_factory):
    asyncio.run(_preparar_banco())
    with TestClient(app) as c:
        r = c.post("/auth/login", json={"identificador": "g@pm.gov.br", "senha": "Teste@2026Forte"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = "Bearer " + r.json()["access_token"]
        c.token = r.json()["access_token"]
        yield c


_IP = iter(range(10, 250))


def _camera(cliente, nome, guardian=True):
    r = cliente.post("/cameras", json={"nome": nome, "setor": "Recepção", "cena": "interno",
                                       "rtsp_url": f"rtsp://10.0.9.{next(_IP)}:554/s1",
                                       "analitico_acao": True})
    assert r.status_code == 201, r.text
    cod = r.json()["codigo"]
    r = cliente.patch(f"/cameras/{cod}", json={"guardian": guardian})
    assert r.status_code == 200 and r.json()["guardian"] is guardian, r.text
    return cod


def _acao(cliente, cam, classe, conf=0.8, minutos=0):
    r = cliente.post("/deteccoes/ingest", json={
        "tipo": "ACAO", "camera_codigo": cam, "classe": classe, "confianca": conf,
        "ocorrido_em": (datetime.now(timezone.utc) - timedelta(minutes=minutos)).isoformat(),
        "bbox": [0.4, 0.3, 0.2, 0.4], "envolvidos": 2, "duracao_s": 4})
    assert r.status_code == 201, r.text
    return r.json()


def test_sinais_na_mesma_camera_viram_uma_ocorrencia_que_sobe_de_prioridade(cliente):
    cam = _camera(cliente, "Guardian Hall")
    _acao(cliente, cam, "briga")
    ocs = cliente.get("/guardian/ocorrencias", params={"camera": cam}).json()["itens"]
    assert len(ocs) == 1 and ocs[0]["prioridade"] == "ALTA"
    _acao(cliente, cam, "queda")
    ocs = cliente.get("/guardian/ocorrencias", params={"camera": cam}).json()["itens"]
    assert len(ocs) == 1, "mesma janela, mesma ocorrência"
    assert ocs[0]["prioridade"] == "CRITICA" and ocs[0]["n_sinais"] == 2
    d = cliente.get(f"/guardian/ocorrencias/{ocs[0]['codigo']}").json()
    assert [s["tipo"] for s in d["sinais"]] == ["briga", "queda"]
    assert any("crítica" in h["texto"] for h in d["historico"])


def test_camera_sem_guardian_nao_abre_ocorrencia(cliente):
    cam = _camera(cliente, "Sem Guardian", guardian=False)
    _acao(cliente, cam, "briga")
    assert cliente.get("/guardian/ocorrencias", params={"camera": cam}).json()["total"] == 0


def test_gesto_de_socorro_e_critico_e_vira_alerta(cliente):
    cam = _camera(cliente, "Guardian Balcão")
    info = _acao(cliente, cam, "sinal_socorro", conf=0.82)
    assert info["alerta"], "gesto de socorro sempre vira alerta"
    oc = cliente.get("/guardian/ocorrencias", params={"camera": cam}).json()["itens"][0]
    assert oc["prioridade"] == "CRITICA" and oc["alerta"]
    al = cliente.get("/alertas", params={"camera": cam}).json()
    itens = al["itens"] if isinstance(al, dict) else al
    alvo = next(a for a in itens if a["codigo"] == oc["alerta"])
    assert alvo["severidade"] == 4 and "Signal for Help" in alvo["detalhe"]
    assert "não confirma" in alvo["detalhe"], "o alerta não afirma o fato"


def test_avaliacao_humana_exige_justificativa_e_fica_na_auditoria(cliente):
    cam = _camera(cliente, "Guardian Avaliação")
    _acao(cliente, cam, "briga")
    cod = cliente.get("/guardian/ocorrencias", params={"camera": cam}).json()["itens"][0]["codigo"]
    r = cliente.post(f"/guardian/ocorrencias/{cod}/avaliacao", json={"status": "FALSO_ALARME", "nota": "não"})
    assert r.status_code == 422, "justificativa curta demais"
    r = cliente.post(f"/guardian/ocorrencias/{cod}/avaliacao", json={"status": "NOVA", "nota": "x" * 20})
    assert r.status_code == 422
    r = cliente.post(f"/guardian/ocorrencias/{cod}/avaliacao",
                     json={"status": "FALSO_ALARME", "nota": "Dois colegas brincando no corredor, conferido no vídeo."})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "FALSO_ALARME" and r.json()["historico"][-1]["por"] == "Operadora"
    trilha = cliente.get("/auditoria", params={"acao": "GUARDIAN_AVALIACAO"}).json()
    itens = trilha["itens"] if isinstance(trilha, dict) else trilha
    assert any(t["alvo"] == cod for t in itens)
    # Avaliada como falso alarme, a ocorrência fecha: sinal novo abre outra.
    _acao(cliente, cam, "briga")
    assert cliente.get("/guardian/ocorrencias", params={"camera": cam}).json()["total"] == 2


def test_estado_e_estatisticas(cliente):
    e = cliente.get("/guardian/estado").json()
    assert e["abertas"]["CRITICA"] >= 2 and any(c["nome"] == "Guardian Hall" for c in e["cameras"])
    s = cliente.get("/guardian/estatisticas", params={"dias": 7}).json()
    assert s["total"] >= 4 and s["por_prioridade"]["CRITICA"] >= 2
    assert s["taxa_falso_alarme"] is not None
    assert next(x for x in s["por_sinal"] if x["tipo"] == "briga")["total"] >= 3
