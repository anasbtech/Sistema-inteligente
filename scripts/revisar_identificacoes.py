"""Reaplica a dupla checagem facial a tudo que o reconhecimento já vinculou.

A partir de 05/11/2026 uma captura só entra na ficha de alguém se o próprio
recorte tiver rosto e esse rosto apontar a mesma pessoa acima do limiar
(`deteccoes._rosto_confirmado`). Este script aplica a mesma regra ao que já
estava vinculado antes dela: o que não passa sai da ficha e vai para a
conferência humana, com a pessoa como sugestão.

Capturas confirmadas por uma pessoa (similaridade 1,0, vindas da triagem)
não são tocadas: a regra existe para o automático, não para quem olhou.

    py -3.13 -m scripts.revisar_identificacoes            # só conta
    py -3.13 -m scripts.revisar_identificacoes --aplicar  # desvincula
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import evidencias, face_engine  # noqa: E402


def _vetor(bruto):
    v = json.loads(bruto) if isinstance(bruto, (str, bytes)) else bruto
    return face_engine.normalizar(v)


def carregar_referencias(c):
    """Amostras de quem está ativo (e o template de quem não tem amostra)."""
    ids, vetores = [], []
    com_amostra = set()
    for pid, emb in c.execute("select a.pessoa_id, a.embedding from amostras_faciais a "
                              "join pessoas p on p.id=a.pessoa_id where p.ativo=1"):
        ids.append(pid); vetores.append(_vetor(emb)); com_amostra.add(pid)
    for pid, emb in c.execute("select id, embedding from pessoas where ativo=1 and embedding is not null"):
        if pid not in com_amostra:
            ids.append(pid); vetores.append(_vetor(emb))
    return np.array(ids), np.vstack(vetores).astype(np.float32)


def melhor_pessoa(ids, matriz, vetor):
    sims = matriz @ face_engine.normalizar(vetor)
    melhor = {}
    for pid, s in zip(ids, sims):
        if s > melhor.get(pid, -1):
            melhor[pid] = float(s)
    pid = max(melhor, key=melhor.get)
    return pid, melhor[pid]


def verificar(caminho_banco: str):
    c = sqlite3.connect(caminho_banco)
    ids, matriz = carregar_referencias(c)
    linhas = c.execute("""select d.id, d.codigo, d.pessoa_id, d.similaridade, d.limiar_aplicado,
                                 d.snapshot_key from deteccoes d
                          where d.tipo='FACIAL' and d.pessoa_id is not null
                            and d.similaridade < 0.999""").fetchall()
    sair, motivos = [], Counter()
    t0 = time.time()
    for n, (did, cod, pid, sim, lim, chave) in enumerate(linhas, 1):
        jpeg = evidencias.ler(chave) if chave else None
        motivo = None
        if not jpeg:
            motivo = "sem imagem guardada"
        else:
            rosto = face_engine.extrair_de_recorte(jpeg)
            if rosto is None:
                motivo = "sem rosto no recorte"
            else:
                outro, s = melhor_pessoa(ids, matriz, rosto.embedding)
                if s < (lim or settings.FACE_THRESHOLD):
                    motivo = "rosto abaixo do limiar"
                elif outro != pid:
                    motivo = "rosto aponta outra pessoa"
        if motivo:
            sair.append((did, cod, pid, motivo))
            motivos[motivo] += 1
        if n % 250 == 0:
            print(f"  {n}/{len(linhas)} verificadas, {len(sair)} a sair ({time.time() - t0:.0f}s)",
                  flush=True)
    c.close()
    return linhas, sair, motivos


async def aplicar(sair):
    from sqlalchemy import func, select

    from app import audit
    from app.database import db_context
    from app.models import Camera, Deteccao, Pessoa

    por_pessoa = defaultdict(list)
    for did, cod, pid, motivo in sair:
        por_pessoa[pid].append((did, cod, motivo))
    from app.models import Usuario
    async with db_context() as db:
        # Quem pediu a revisão responde por ela na trilha.
        quem = (await db.execute(select(Usuario).where(Usuario.email == "admin@pa.com.br"))
                ).scalars().first()
        for pid, itens in por_pessoa.items():
            pessoa = await db.get(Pessoa, pid if not isinstance(pid, str) else __import__("uuid").UUID(pid))
            for did, cod, motivo in itens:
                det = await db.get(Deteccao, __import__("uuid").UUID(did) if isinstance(did, str) else did)
                det.pessoa_sugerida_id = det.pessoa_id
                det.pessoa_id = None
            await db.flush()
            # Total e última captura da pessoa, pelo que ficou.
            total = (await db.execute(select(func.count()).select_from(Deteccao)
                                      .where(Deteccao.pessoa_id == pessoa.id))).scalar_one()
            ultima = (await db.execute(select(Deteccao).where(Deteccao.pessoa_id == pessoa.id)
                                       .order_by(Deteccao.ocorrido_em.desc()).limit(1))
                      ).scalars().first()
            pessoa.total_deteccoes = total
            pessoa.ultima_deteccao = ultima.ocorrido_em if ultima else None
            if ultima is not None:
                cam = await db.get(Camera, ultima.camera_id)
                pessoa.ultima_camera = cam.nome if cam else ""
            else:
                pessoa.ultima_camera = ""
            await audit.registrar(
                db, acao="BIO_REVISAO", severidade="warn", usuario=quem,
                alvo=pessoa.id_biometrico,
                descricao=f"{len(itens)} captura(s) automática(s) desvinculada(s): sem rosto "
                          "confirmado no recorte ou rosto de outra pessoa",
                finalidade="Revisão das identificações automáticas (dupla checagem)",
                metadados={"motivos": dict(Counter(m for _, _, m in itens)),
                           "capturas": [c for _, c, _ in itens][:500]},
            )
            print(f"  {pessoa.id_biometrico} {pessoa.nome}: {len(itens)} desvinculada(s), "
                  f"ficam {total}", flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--aplicar", action="store_true")
    args = p.parse_args()
    banco = settings.DATABASE_URL.split(":///")[-1]
    linhas, sair, motivos = verificar(banco)
    print(f"{len(linhas)} identificações automáticas; {len(sair)} não passam: {dict(motivos)}")
    if args.aplicar and sair:
        asyncio.run(aplicar(sair))


if __name__ == "__main__":
    main()
