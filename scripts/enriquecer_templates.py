"""Leva capturas já confirmadas para o template de quem elas são.

    python scripts/enriquecer_templates.py [--limite-por-camera N] [--aplicar]

Sem `--aplicar` é um ensaio: diz o que faria e não grava nada.

O sistema aprende sozinho desde que `acolher_amostra` passou a rodar a cada
reconhecimento — mas só para quem passa de agora em diante. As capturas que
já estavam no banco nunca passaram por lá, e o resultado se vê na ficha:
André Luiz Cardoso tem 1.538 capturas confirmadas em 3 câmeras e 8 amostras
de uma câmera só; Tereza Cristina Martins tem 1.310 capturas e nenhuma
amostra com câmera registrada. Célia Regina Fernandes, com 21 amostras em 10
câmeras, é a que o parque inteiro reconhece.

Reconhecer em outra câmera é cobertura de ângulos, não limiar — e é isso que
este script vai buscar no histórico.

**Só entra o que já é da pessoa.** A seleção parte de capturas com
`pessoa_id` preenchido, que no banco deste parque significa similaridade de
0,50 para cima: correspondência que o motor afirmou, não palpite. Nada aqui
atribui identidade nova, e a fila de conferência segue sendo o caminho de
quem está abaixo do limiar.

A guarda que torna isto seguro
------------------------------
O recorte guardado não devolve o mesmo vetor de quando a pessoa passou —
`preencher_sugestoes.py` mediu uma similaridade de 0,44 voltar como 0,12. Um
vetor degradado é *pior* que inútil aqui: `acolher_amostra` recusa o que é
parecido demais com o que já existe, então justamente o vetor ruim passaria
no teste de distinção e entraria no template como se fosse ângulo novo.

Por isso, toda captura reextraída é submetida de volta à busca 1:N: só vira
amostra se o motor ainda a reconhecer **como a mesma pessoa** e acima do
limiar. O que não se reconhece a si mesmo não entra.

Cota, distinção e recálculo do template são os de sempre: quem decide se
cabe é `acolher_amostra`, a mesma função do caminho ao vivo.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlalchemy import func, select                              # noqa: E402

from app.config import settings                                 # noqa: E402
from app.database import db_context                             # noqa: E402
from app.models import (                                        # noqa: E402
    AmostraFacial, Camera, Deteccao, Pessoa, TipoDeteccao,
)
from app.services import biometrics, evidencias, face_engine    # noqa: E402

# Quantas capturas tentar por câmera antes de desistir dela. A cota é de
# poucas amostras, e insistir em quinhentos recortes da mesma câmera para
# arrancar a terceira custa minutos e não acrescenta ângulo.
TENTATIVAS_POR_CAMERA = 12


async def _alvos(db, limite_por_camera: int):
    """(pessoa, câmera, capturas) onde o template ainda tem vaga."""
    ocupadas = {
        (p, c): n for p, c, n in (
            await db.execute(
                select(AmostraFacial.pessoa_id, AmostraFacial.camera_id,
                       func.count())
                .where(AmostraFacial.camera_id.isnot(None))
                .group_by(AmostraFacial.pessoa_id, AmostraFacial.camera_id)
            )
        ).all()
    }

    pares = (
        await db.execute(
            select(Deteccao.pessoa_id, Deteccao.camera_id, func.count())
            .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.pessoa_id.isnot(None),
                   Deteccao.snapshot_key != "")
            .group_by(Deteccao.pessoa_id, Deteccao.camera_id)
        )
    ).all()

    alvos = []
    for pessoa_id, camera_id, total in pares:
        if camera_id is None:
            continue
        vaga = limite_por_camera - ocupadas.get((pessoa_id, camera_id), 0)
        if vaga <= 0:
            continue
        pessoa = (await db.execute(
            select(Pessoa).where(Pessoa.id == pessoa_id))).scalar_one_or_none()
        camera = (await db.execute(
            select(Camera).where(Camera.id == camera_id))).scalar_one_or_none()
        if pessoa is None or camera is None:
            continue
        # BIO-N fica de fora. É o registro provisório de quem o sistema não
        # sabe quem é, e engordar o template dele faz o oposto do que se
        # quer: um molde mais abrangente passa a atrair rostos de estranhos
        # distintos para o mesmo identificador, e a triagem herda a mistura.
        if biometrics.sem_nome(pessoa.id_biometrico):
            continue
        # As de melhor qualidade primeiro: é o recorte nítido que sobrevive à
        # reextração, e cada tentativa custa perto de um segundo.
        capturas = (
            await db.execute(
                select(Deteccao)
                .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                       Deteccao.pessoa_id == pessoa_id,
                       Deteccao.camera_id == camera_id,
                       Deteccao.snapshot_key != "")
                .order_by(Deteccao.qualidade_face.desc().nullslast(),
                          Deteccao.similaridade.desc())
                .limit(TENTATIVAS_POR_CAMERA)
            )
        ).scalars().all()
        alvos.append((pessoa, camera, vaga, total, capturas))
    return alvos


async def principal(limite_por_camera: int, aplicar: bool) -> int:
    if not face_engine.modelos_disponiveis():
        print("  Modelos faciais indisponíveis — nada a fazer.")
        return 1

    async with db_context() as db:
        alvos = await _alvos(db, limite_por_camera)

    if not alvos:
        print("\n  Nenhum template com vaga. Nada a fazer.\n")
        return 0

    print(f"\n  {len(alvos)} par(es) pessoa×câmera com vaga no template"
          f" (cota {limite_por_camera}/câmera)")
    print(f"  {'ENSAIO — nada será gravado' if not aplicar else 'APLICANDO'}\n")

    entraram = recusadas = sem_recorte = sem_rosto = nao_se_reconheceu = 0
    por_pessoa: dict[str, int] = {}

    for pessoa, camera, vaga, total, capturas in alvos:
        cabem = vaga
        for det in capturas:
            if cabem <= 0:
                break
            jpeg = evidencias.ler(det.snapshot_key)
            if jpeg is None:
                sem_recorte += 1
                continue
            face = await asyncio.to_thread(face_engine.extrair_de_recorte, jpeg)
            if face is None:
                sem_rosto += 1
                continue

            async with db_context() as db:
                # A guarda: o vetor reextraído tem de se reconhecer como a
                # mesma pessoa. Sem isto, um recorte degradado entraria
                # justamente por *não* se parecer com o que já existe.
                conf = await face_engine.buscar_pessoa(
                    db, face.embedding, camera.limiar_facial)
                if not conf.match or conf.pessoa_id != str(pessoa.id):
                    nao_se_reconheceu += 1
                    continue
                if not aplicar:
                    entraram += 1
                    cabem -= 1
                    por_pessoa[pessoa.nome] = por_pessoa.get(pessoa.nome, 0) + 1
                    continue
                alvo = (await db.execute(
                    select(Pessoa).where(Pessoa.id == pessoa.id))).scalar_one()
                if await face_engine.acolher_amostra(
                        db, alvo, face.embedding, face.qualidade, camera.id):
                    entraram += 1
                    cabem -= 1
                    por_pessoa[pessoa.nome] = por_pessoa.get(pessoa.nome, 0) + 1
                else:
                    recusadas += 1

        if por_pessoa.get(pessoa.nome):
            print(f"    {pessoa.nome[:30]:32} {camera.codigo}  "
                  f"+{vaga - cabem} amostra(s)  (de {total} capturas)")

    print(f"\n  {entraram} amostra(s) {'entrariam' if not aplicar else 'adicionadas'}")
    if por_pessoa:
        for nome, n in sorted(por_pessoa.items(), key=lambda p: -p[1]):
            print(f"    {nome[:34]:36} +{n}")
    print(f"\n  recusadas pela cota/distinção: {recusadas}")
    print(f"  não se reconheceram ao reextrair: {nao_se_reconheceu}")
    print(f"  sem recorte no armazenamento: {sem_recorte}")
    print(f"  recorte sem rosto legível: {sem_rosto}")
    if not aplicar:
        print("\n  Ensaio. Repita com --aplicar para gravar.\n")
    else:
        print()
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--limite-por-camera", type=int,
                   default=settings.FACE_SAMPLES_PER_CAMERA,
                   help="cota de amostras por câmera (padrão: a do sistema)")
    p.add_argument("--aplicar", action="store_true",
                   help="grava de fato; sem isto é só ensaio")
    args = p.parse_args()
    raise SystemExit(asyncio.run(principal(args.limite_por_camera, args.aplicar)))
