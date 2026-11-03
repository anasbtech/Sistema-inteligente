"""Preenche o candidato das capturas antigas que ficaram perto do limiar.

    python scripts/preencher_sugestoes.py [--limite N] [--incluir-cobertas]

A fila de conferência mostra quem o motor apontou quando a similaridade não
alcançou o limiar. Esse apontamento passou a ser gravado no momento da
detecção, mas as capturas que já estavam no banco têm o campo vazio — e são
justamente elas que carregam o histórico de quem passou por onde.

A captura não guarda o vetor, só o recorte. Então aqui ele é extraído de
novo do JPEG e a busca 1:N é refeita. É caro — perto de um segundo por
captura —, e por isso a ordem importa: vêm primeiro as câmeras que ainda não
reconhecem ninguém, que são as que a conferência tem como abrir. Capturas em
câmera já coberta não entram, a menos que se peça: confirmá-las não
acrescenta ângulo nenhum ao template.

Idempotente: quem já tem candidato gravado é pulado.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlalchemy import func, select                              # noqa: E402

from app.config import settings                                  # noqa: E402
from app.database import db_context                              # noqa: E402
from app.models import AmostraFacial, Camera, Deteccao, TipoDeteccao  # noqa: E402
from app.services import evidencias, face_engine                 # noqa: E402


async def principal(limite: int, incluir_cobertas: bool) -> int:
    if not face_engine.modelos_disponiveis():
        print("  Modelos faciais indisponíveis — nada a fazer.")
        return 1

    async with db_context() as db:
        # Câmera coberta é aquela de que alguma pessoa já tem amostra: ali a
        # conferência não abre ângulo novo, só confirma o que já responde.
        cobertas = {
            cid for (cid,) in (
                await db.execute(
                    select(AmostraFacial.camera_id).where(AmostraFacial.camera_id.isnot(None))
                )
            ).all()
        }
        # Câmera que nunca identificou ninguém é onde a conferência rende
        # mais: é lá que a pessoa passa e o sistema não a reconhece.
        identificam = {
            cid for (cid,) in (
                await db.execute(
                    select(Deteccao.camera_id)
                    .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                           Deteccao.pessoa_id.isnot(None))
                    .group_by(Deteccao.camera_id)
                    .having(func.count() > 0)
                )
            ).all()
        }

        linhas = (
            await db.execute(
                select(Deteccao, Camera).join(Camera, Camera.id == Deteccao.camera_id)
                .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                       Deteccao.pessoa_id.is_(None),
                       Deteccao.pessoa_sugerida_id.is_(None),
                       Deteccao.snapshot_key != "",
                       Deteccao.similaridade >= settings.FACE_REVIEW_MIN,
                       Deteccao.similaridade < settings.FACE_THRESHOLD)
                .order_by(Deteccao.similaridade.desc())
            )
        ).all()

    if not incluir_cobertas:
        linhas = [(d, c) for d, c in linhas if c.id not in cobertas]
    # Câmera muda primeiro, similaridade depois: o que interessa é abrir
    # câmera nova, não confirmar de novo onde já se reconhece.
    linhas.sort(key=lambda par: (par[1].id in identificam, -(par[0].similaridade or 0)))
    linhas = linhas[:limite]

    print(f"\n  {len(linhas)} captura(s) a examinar"
          f" (faixa {settings.FACE_REVIEW_MIN:.2f}–{settings.FACE_THRESHOLD:.2f})\n")
    if not linhas:
        print("  Nada pendente.\n")
        return 0

    marcadas = sem_rosto = sem_recorte = sem_candidato = fora_da_faixa = 0
    por_camera: dict[str, int] = {}

    for i, (det, cam) in enumerate(linhas, 1):
        jpeg = evidencias.ler(det.snapshot_key)
        if jpeg is None:
            sem_recorte += 1
            continue
        face = await asyncio.to_thread(face_engine.extrair_de_recorte, jpeg)
        if face is None:
            sem_rosto += 1
            continue
        async with db_context() as db:
            resultado = await face_engine.buscar_pessoa(db, face.embedding, cam.limiar_facial)
            # O vetor reextraído do recorte não é idêntico ao do momento da
            # captura — medido aqui, uma similaridade de 0,44 voltou como
            # 0,12. Fora da faixa, o apontamento deixa de ser quase-acerto e
            # vira palpite ruim: mostrá-lo faria o operador julgar um rosto
            # que o motor nem considera parecido.
            #
            # E a similaridade gravada não é tocada: ela registra o que o
            # sistema concluiu quando a pessoa passou. Reescrevê-la agora
            # apagaria esse registro e, num sistema com trilha de auditoria,
            # histórico não se corrige para caber em tela.
            if not resultado.candidato_id:
                sem_candidato += 1
                continue
            if resultado.similaridade < settings.FACE_REVIEW_MIN:
                fora_da_faixa += 1
                continue
            atual = (
                await db.execute(select(Deteccao).where(Deteccao.id == det.id))
            ).scalar_one()
            atual.pessoa_sugerida_id = uuid.UUID(resultado.candidato_id)
            marcadas += 1
            por_camera[cam.codigo] = por_camera.get(cam.codigo, 0) + 1
        if i % 20 == 0 or i == len(linhas):
            print(f"    {i}/{len(linhas)} examinadas, {marcadas} com candidato")

    print(f"\n  {marcadas} captura(s) ganharam candidato")
    if por_camera:
        print("  por câmera: " + ", ".join(f"{c}={n}" for c, n in sorted(por_camera.items())))
    if sem_recorte:
        print(f"  {sem_recorte} sem recorte em disco")
    if sem_rosto:
        print(f"  {sem_rosto} sem rosto legível no recorte")
    if sem_candidato:
        print(f"  {sem_candidato} sem nenhum cadastro para apontar")
    if fora_da_faixa:
        print(f"  {fora_da_faixa} com o vetor reextraído fora da faixa")
    print()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limite", type=int, default=200,
                   help="quantas capturas examinar (padrão 200)")
    p.add_argument("--incluir-cobertas", action="store_true",
                   help="examinar também câmeras que já têm amostra")
    a = p.parse_args()
    raise SystemExit(asyncio.run(principal(a.limite, a.incluir_cobertas)))
