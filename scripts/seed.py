"""Popula o banco com o estado inicial do sistema.

    python -m scripts.seed

Cria os usuários das cinco funções e o registro biométrico da responsável
pelo sistema. Não cria câmeras nem histórico de detecções: o parque de
câmeras é cadastrado a partir dos equipamentos reais, e as detecções nascem
do worker de inferência.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.database import db_context, engine
from app.models import (
    Perfil, Pessoa, SequenciaBiometrica, Sexo, StatusPessoa, Usuario,
)
from app.security import hash_senha
from app.services import biometrics

log = logging.getLogger("seed")

USUARIOS = [
    ("Ana Caroline",            "admin@pm.gov.br",      "Admin@2026", "PA-000001", Perfil.ADMIN,    "Comando Geral"),
    ("Ten. Rafael Bittencourt", "supervisor@pm.gov.br", "Super@2026", "PM-118203", Perfil.SUPER,    "1º BPM"),
    ("Sgt. Priscila Tavares",   "operador@pm.gov.br",   "Oper@2026",  "PM-220741", Perfil.OPERADOR, "COPOM"),
    ("Dra. Helena Nakamura",    "analista@pm.gov.br",   "Anal@2026",  "CV-009317", Perfil.ANALISTA, "Inteligência"),
    ("Dr. Enzo Vilela",         "auditoria@mp.gov.br",  "Audi@2026",  "MP-004412", Perfil.AUDITOR,  "Corregedoria"),
]

# Registro biométrico inicial. Sem embedding: o template só existe depois que
# uma foto é enviada a POST /pessoas/{id}/amostras, com os modelos instalados.
PESSOA_INICIAL = {
    "nome": "Ana Caroline",
    "sexo": Sexo.F,
    "status": StatusPessoa.FUNCIONARIO,
    "unidade": "Comando Geral",
    "base_legal": "Termo assinado",
}


async def semear(silencioso: bool = False) -> None:
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

        # --- registro biométrico inicial ---
        sexo = PESSOA_INICIAL["sexo"]
        # É AQUI que o identificador nasce — uma única vez, pelo sexo do cadastro.
        id_bio = await biometrics.emitir_id_biometrico(db, sexo)
        db.add(Pessoa(
            id_biometrico=id_bio, sexo=sexo, sexo_emissao=sexo,
            nome=PESSOA_INICIAL["nome"],
            status=PESSOA_INICIAL["status"],
            unidade=PESSOA_INICIAL["unidade"],
            base_legal=PESSOA_INICIAL["base_legal"],
            cadastrado_por=admin.id,
        ))
        await db.flush()
        p(f"  Registro biométrico {id_bio} criado (sem template — envie uma foto).")

        esquema = await biometrics.contagem_por_sexo(db)
        for _sexo, dados in esquema.items():
            p(f"    {dados['prefixo']} ({dados['rotulo']}): {dados['registros']} registros"
              f" — próximo {dados['proximo']}")


async def _principal() -> None:
    logging.basicConfig(level=logging.WARNING)
    await semear()
    await engine.dispose()
    print("\n" + "=" * 62)
    print("  Banco populado. Credenciais:")
    print("=" * 62)
    for nome, email, senha, _m, perfil, _u in USUARIOS:
        print(f"  {email:<26} {senha:<12} {perfil.value}")
    print("=" * 62)
    print("  Suba o sistema:  python run.py\n")


if __name__ == "__main__":
    asyncio.run(_principal())
