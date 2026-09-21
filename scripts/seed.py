"""Popula o banco com dados de demonstração.

    python -m scripts.seed

Cria usuários das cinco funções, um parque de câmeras com zonas, registros
biométricos com embeddings **sintéticos** (vetores aleatórios normalizados) e
um histórico de detecções para o painel abrir com gráficos preenchidos.

Os embeddings não correspondem a nenhuma pessoa real — servem para exercitar a
busca vetorial e o esquema de identificadores, e são inúteis para reconhecer
alguém.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import date, datetime, timezone

import numpy as np
from sqlalchemy import func, select

from app.config import settings
from app.database import db_context, engine
from app.models import (
    Camera, Perfil, Pessoa, SequenciaBiometrica, Sexo, StatusCamera,
    StatusPessoa, TipoZona, Usuario, Zona,
)
from app.security import hash_senha
from app.services import biometrics

log = logging.getLogger("seed")

rng = random.Random(20260920)
nprng = np.random.default_rng(20260920)

USUARIOS = [
    ("Cel. Marcos Andrade",     "admin@pm.gov.br",      "Admin@2026", "PM-104882", Perfil.ADMIN,    "Comando Geral"),
    ("Ten. Rafael Bittencourt", "supervisor@pm.gov.br", "Super@2026", "PM-118203", Perfil.SUPER,    "1º BPM"),
    ("Sgt. Priscila Tavares",   "operador@pm.gov.br",   "Oper@2026",  "PM-220741", Perfil.OPERADOR, "COPOM"),
    ("Dra. Helena Nakamura",    "analista@pm.gov.br",   "Anal@2026",  "CV-009317", Perfil.ANALISTA, "Inteligência"),
    ("Dr. Enzo Vilela",         "auditoria@mp.gov.br",  "Audi@2026",  "MP-004412", Perfil.AUDITOR,  "Corregedoria"),
]

CAMERAS = [
    # nome, cena, setor, facial, ação, status
    ("Av. Brasil × R. XV — Cruzamento",    "avenida",   "Centro",              True,  True,  "ONLINE"),
    ("Praça da Matriz — Coreto",           "praca",     "Centro",              True,  True,  "ONLINE"),
    ("Terminal Central — Plataforma A",    "terminal",  "Terminal Rodoviário", True,  True,  "ONLINE"),
    ("Terminal Central — Bilheteria",      "terminal",  "Terminal Rodoviário", True,  False, "ONLINE"),
    ("Portaria Batalhão — Guarita 1",      "portaria",  "Centro",              True,  False, "ONLINE"),
    ("Estacionamento Fórum — Setor B",     "estac",     "Centro",              False, False, "MANUTENCAO"),
    ("R. das Palmeiras — 400",             "rua",       "Zona Norte",          False, True,  "ONLINE"),
    ("Perímetro Depósito — Face Leste",    "perimetro", "Distrito Industrial", True,  True,  "ONLINE"),
    ("Av. Litorânea — km 3",               "avenida",   "Zona Sul",            False, True,  "ONLINE"),
    ("Corredor Administrativo — 2º andar", "corredor",  "Centro",              True,  False, "ONLINE"),
    ("Praça do Mercado — Entrada",         "praca",     "Zona Leste",          True,  True,  "ONLINE"),
    ("Viaduto Norte — Acesso",             "avenida",   "Zona Norte",          True,  False, "ONLINE"),
    ("Portaria Delegacia 3ª DP",           "portaria",  "Zona Oeste",          True,  False, "ONLINE"),
    ("R. Sete de Setembro — 120",          "rua",       "Centro",              False, False, "OFFLINE"),
    ("Perímetro Portuário — Portão 4",     "perimetro", "Área Portuária",      True,  True,  "ONLINE"),
    ("Terminal Sul — Desembarque",         "terminal",  "Zona Sul",            True,  True,  "ONLINE"),
]

NOMES_M = ["Adriano","Alexandre","Anderson","Bruno","Caio","Carlos","Daniel","Diego","Eduardo",
           "Fábio","Felipe","Gabriel","Gustavo","Henrique","Igor","João","Leandro","Leonardo",
           "Lucas","Marcelo","Marcos","Mateus","Paulo","Pedro","Rafael","Ricardo","Rodrigo",
           "Samuel","Thiago","Vinícius","Wesley"]
NOMES_F = ["Adriana","Amanda","Ana","Beatriz","Bianca","Camila","Carla","Carolina","Daniela",
           "Débora","Fabiana","Fernanda","Gabriela","Helena","Isabela","Jaqueline","Juliana",
           "Larissa","Letícia","Luciana","Mariana","Natália","Patrícia","Priscila","Renata",
           "Sabrina","Tatiane","Vanessa","Viviane"]
SOBRENOMES = ["Almeida","Alves","Araújo","Barbosa","Barros","Cardoso","Carvalho","Castro","Costa",
              "Cunha","Dias","Duarte","Ferreira","Fernandes","Freitas","Gomes","Gonçalves","Lima",
              "Lopes","Machado","Marques","Martins","Melo","Mendes","Moraes","Moreira","Nascimento",
              "Nogueira","Nunes","Oliveira","Pereira","Pinheiro","Ramos","Ribeiro","Rocha",
              "Rodrigues","Santos","Silva","Soares","Souza","Teixeira","Vieira"]
ALCUNHAS = ['"Magrão"', '"Tiquinho"', '"Loira"', '"Zé"', '"Bia"', '"Preta"', '"Gordo"', '"Nego"']
TIPIFICACOES = ["Art. 155 CP — Furto", "Art. 157 CP — Roubo", "Art. 171 CP — Estelionato",
                "Art. 33 Lei 11.343 — Tráfico", "Art. 180 CP — Receptação"]


def embedding_sintetico() -> list[float]:
    """Vetor 512-D L2-normalizado. NÃO corresponde a nenhuma pessoa real."""
    v = nprng.normal(size=settings.EMBEDDING_DIM).astype(np.float32)
    return (v / np.linalg.norm(v)).tolist()


def zonas_da_cena(cena: str) -> list[Zona]:
    z = []
    if cena in ("perimetro", "portaria", "estac"):
        z.append(Zona(nome="Linha de intrusão", tipo=TipoZona.LINHA, cor="#E8474D",
                      pontos=[[0.08, 0.62], [0.92, 0.55]], regras={"invasao": True}))
    z.append(Zona(nome="Área de interesse", tipo=TipoZona.POLIGONO, cor="#35A7DE",
                  pontos=[[0.12, 0.42], [0.86, 0.38], [0.92, 0.90], [0.06, 0.94]],
                  regras={"contagem": True}))
    if cena in ("avenida", "rua", "terminal"):
        z.append(Zona(nome="Faixa de contagem", tipo=TipoZona.LINHA, cor="#F0A02A",
                      pontos=[[0.05, 0.78], [0.95, 0.72]], regras={"contagem": True}))
    if cena in ("praca", "terminal"):
        z.append(Zona(nome="Zona de aglomeração", tipo=TipoZona.POLIGONO, cor="#8C6FE0",
                      pontos=[[0.30, 0.50], [0.75, 0.48], [0.80, 0.82], [0.25, 0.85]],
                      regras={"aglomeracao": True}))
    return z


PLANO_PESSOAS = [
    (Sexo.M, StatusPessoa.PROCURADO, 5),    (Sexo.F, StatusPessoa.PROCURADO, 2),
    (Sexo.M, StatusPessoa.MONITORADO, 4),   (Sexo.F, StatusPessoa.MONITORADO, 3),
    (Sexo.M, StatusPessoa.DESAPARECIDO, 2), (Sexo.F, StatusPessoa.DESAPARECIDO, 3),
    (Sexo.N, StatusPessoa.DESAPARECIDO, 1),
    (Sexo.M, StatusPessoa.FUNCIONARIO, 7),  (Sexo.F, StatusPessoa.FUNCIONARIO, 6),
    (Sexo.M, StatusPessoa.CIDADAO, 12),     (Sexo.F, StatusPessoa.CIDADAO, 10),
    (Sexo.N, StatusPessoa.CIDADAO, 2),
]


async def semear(silencioso: bool = False, historico: int = 650) -> None:
    def p(msg: str) -> None:
        if not silencioso:
            print(msg)

    async with db_context() as db:
        ja_tem = int((await db.execute(select(func.count()).select_from(Usuario))).scalar_one())
        if ja_tem:
            p(f"O banco já contém {ja_tem} usuário(s). Nada a fazer.")
            p("Para recomeçar: python run.py --reset")
            return

        # --- contadores de identificador ---
        for sexo in Sexo:
            db.add(SequenciaBiometrica(sexo=sexo, proximo=1))
        await db.flush()

        # --- usuários ---
        agora = datetime.now(timezone.utc)
        for nome, email, senha, matricula, perfil, unidade in USUARIOS:
            db.add(Usuario(
                nome=nome, email=email, matricula=matricula,
                senha_hash=hash_senha(senha), perfil=perfil, unidade=unidade,
                ativo=True, mfa_ativo=perfil in (Perfil.ADMIN, Perfil.SUPER),
                senha_alterada_em=agora,
            ))
        await db.flush()
        p(f"  {len(USUARIOS)} usuários criados.")

        admin = (await db.execute(
            select(Usuario).where(Usuario.email == "admin@pm.gov.br")
        )).scalar_one()

        # --- câmeras ---
        for i, (nome, cena, setor, facial, acao, status) in enumerate(CAMERAS, start=1):
            cam = Camera(
                codigo=f"CAM-{i:03d}", nome=nome, cena=cena, setor=setor,
                status=StatusCamera[status],
                ip=f"10.30.{i}.10",
                rtsp_url=f"rtsp://10.30.{i}.10:554/stream1",
                onvif_url=f"http://10.30.{i}.10:80/onvif/device_service",
                fabricante=rng.choice(["Intelbras", "Hikvision", "Axis", "Dahua", "Bosch"]),
                modelo=rng.choice(["VIP-5280", "DS-2CD2T87", "P3265-LVE", "IPC-HFW5541", "NDE-8503"]),
                resolucao=rng.choice(["1920x1080", "2560x1440", "3840x2160"]),
                fps=rng.choice([15, 20, 25, 30]),
                codec=rng.choice(["H.265", "H.264"]),
                bitrate_kbps=rng.randint(2048, 8192),
                analitico_facial=facial, analitico_objeto=True, analitico_acao=acao,
                ptz=cena in ("avenida", "praca", "perimetro", "terminal") and rng.random() > 0.4,
                limiar_facial=round(rng.uniform(0.85, 0.92), 2),
                limiar_objeto=round(rng.uniform(0.55, 0.75), 2),
                retencao_dias=rng.choice([30, 60, 90, 180]),
                latitude=-23.55 + rng.uniform(-0.06, 0.06),
                longitude=-46.63 + rng.uniform(-0.06, 0.06),
                latencia_ms=rng.randint(38, 180) if status == "ONLINE" else 0,
                ultimo_heartbeat=agora if status == "ONLINE" else None,
                instalada_em=date(2024, rng.randint(1, 12), rng.randint(1, 28)),
            )
            for z in zonas_da_cena(cena):
                cam.zonas.append(z)
            db.add(cam)
        await db.flush()
        p(f"  {len(CAMERAS)} câmeras criadas.")

        # --- pessoas ---
        total = 0
        for sexo, status, quantidade in PLANO_PESSOAS:
            for _ in range(quantidade):
                primeiro = rng.choice(NOMES_F if sexo == Sexo.F else NOMES_M)
                nome = f"{primeiro} {rng.choice(SOBRENOMES)} {rng.choice(SOBRENOMES)}"

                # É AQUI que o identificador nasce — uma única vez, pelo sexo do cadastro.
                id_bio = await biometrics.emitir_id_biometrico(db, sexo)

                nascimento = date(rng.randint(1965, 2006), rng.randint(1, 12), rng.randint(1, 28))
                procurado = status == StatusPessoa.PROCURADO
                db.add(Pessoa(
                    id_biometrico=id_bio, sexo=sexo, sexo_emissao=sexo,
                    nome=nome,
                    alcunha=rng.choice(ALCUNHAS) if rng.random() > 0.82 else "",
                    nascimento=nascimento,
                    cpf=f"{rng.randint(100,999)}.{rng.randint(100,999)}.{rng.randint(100,999)}-{rng.randint(10,99)}",
                    rg=f"{rng.randint(10,49)}.{rng.randint(100,999)}.{rng.randint(100,999)}-{rng.randint(0,9)}",
                    status=status,
                    mandado=f"MP-2026/{rng.randint(1000, 9999)}" if procurado else "",
                    tipificacao=rng.choice(TIPIFICACOES) if procurado else "",
                    orgao_expedidor=rng.choice(["1ª Vara Criminal", "2ª Vara Criminal",
                                                "Vara de Execuções Penais"]) if procurado else "",
                    unidade=rng.choice(["1º BPM", "2º BPM", "3º BPM", "DHPP", "Delegacia Central"]),
                    embedding=embedding_sintetico(),
                    qualidade=round(rng.uniform(0.72, 0.98), 3),
                    amostras=rng.randint(1, 8),
                    base_legal=("Termo assinado" if status == StatusPessoa.FUNCIONARIO
                                else "Segurança pública"),
                    cadastrado_por=admin.id,
                ))
                total += 1

        await db.flush()
        p(f"  {total} registros biométricos criados.")

        esquema = await biometrics.contagem_por_sexo(db)
        for _sexo, dados in esquema.items():
            p(f"    {dados['prefixo']} ({dados['rotulo']}): {dados['registros']} registros"
              f" — próximo {dados['proximo']}")

    # --- histórico de detecções ---
    if historico:
        from app.services.simulador import popular_historico
        criados = await popular_historico(horas=26, quantidade=historico)
        p(f"  {criados} detecções históricas geradas.")


async def _principal() -> None:
    logging.basicConfig(level=logging.WARNING)
    await semear()
    await engine.dispose()
    print("\n" + "=" * 62)
    print("  Banco populado. Credenciais de demonstração:")
    print("=" * 62)
    for nome, email, senha, _m, perfil, _u in USUARIOS:
        print(f"  {email:<26} {senha:<12} {perfil.value}")
    print("=" * 62)
    print("  Suba o sistema:  python run.py\n")


if __name__ == "__main__":
    asyncio.run(_principal())
