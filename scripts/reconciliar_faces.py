"""Concilia as capturas faciais não identificadas com as fichas de hoje.

Uma captura fica "não identificada" quando, no instante em que foi gravada, o
rosto não bateu com nenhuma ficha. Muitas dessas pessoas ganharam ficha ou
amostras depois (triagem, conferência), e o rosto antigo continua solto. Este
script compara cada um deles, de novo, com as amostras atuais e decide:

* **Entra na ficha sozinho** só quando não há dúvida: similaridade alta, folga
  clara sobre a segunda pessoa mais parecida, mais de uma amostra da ficha
  apontando para o mesmo rosto e rosto com pixels suficientes.
* **Vai para aprovação** quando há alguém parecido mas não o bastante: a
  captura recebe a pessoa sugerida e entra na fila de conferência.
* **Continua não identificada** quando ninguém cadastrado se parece: é quem
  ainda não tem ficha, e a triagem de pessoas novas cuida disso.

Captura já revisada por alguém não é tocada — inclusive a marcada falso
positivo, que não pode voltar para a mesma ficha por este caminho. Nenhuma
amostra é criada: o template só aprende com o que uma pessoa confirmou.

Os vetores vêm da extração da triagem (data/cadastro_triagem/vetores.npz).

    py -3.13 -m scripts.reconciliar_faces            # só conta
    py -3.13 -m scripts.reconciliar_faces --aplicar  # grava
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

# Critérios do automático. Medidos em 12/11/2026 sobre 12.527 rostos soltos,
# conferidos em mosaico lado a lado com a foto da ficha: de 0,60 para cima,
# com folga e duas amostras, os erros vistos sumiram; entre 0,55 e 0,60
# ainda aparecia rosto de outra pessoa — essa faixa vai para aprovação.
AUTO_SIM = 0.60
AUTO_FOLGA = 0.15           # sobre a 2ª pessoa mais parecida (ver scripts/treinar_faces.py)
AUTO_AMOSTRAS = 3           # amostras da ficha com similaridade >= APOIO
APOIO = 0.50
ROSTO_MIN_PX = 48           # menor que isto o vetor sai de rosto esticado
DUVIDA_SIM = 0.45           # abaixo disto, ninguém cadastrado se parece
DETECTOR_MIN = 0.40         # rosto visto de verdade: nuca e perfil saem abaixo disto
# Ficha confiável: as amostras se parecem entre si e não com outra pessoa. A
# do Márcio Luiz Miranda (12/11/2026) tinha 24 amostras quase todas de nuca,
# coesão 0,26 e 21% delas mais parecidas com outra pessoa — e "reconhecia" a
# nuca de qualquer um. Ficha assim não recebe nada sozinha.
FICHA_COESAO_MIN = 0.30
FICHA_CRUZADA_MAX = 0.10    # fração de amostras >= 0,5 com amostra de outra pessoa


def carregar_vetores():
    z = np.load(RAIZ / "data" / "cadastro_triagem" / "vetores.npz", allow_pickle=True)
    V = z["vet"].astype(np.float32)
    normas = np.linalg.norm(V, axis=1)
    return {str(k): (V[i] / normas[i], float(z["alt"][i]), float(z["det"][i]))
            for i, k in enumerate(z["codigos"]) if normas[i] > 0.5}


async def main(aplicar: bool) -> None:
    from sqlalchemy import select, update

    from app.database import db_context, engine
    from app.models import AmostraFacial, Camera, Deteccao, Pessoa, TipoDeteccao
    from app import audit

    vetores = carregar_vetores()
    async with db_context() as db:
        soltas = (await db.execute(
            select(Deteccao.codigo, Deteccao.ocorrido_em, Deteccao.camera_id)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL, Deteccao.pessoa_id.is_(None),
                   Deteccao.revisado.is_(False))
        )).all()
        amostras = (await db.execute(
            select(AmostraFacial.pessoa_id, AmostraFacial.embedding)
            .join(Pessoa, Pessoa.id == AmostraFacial.pessoa_id).where(Pessoa.ativo.is_(True))
        )).all()
        cameras = {cid: nome for cid, nome in (await db.execute(select(Camera.id, Camera.nome))).all()}

    if not amostras:
        print("nenhuma amostra facial: nada a conciliar"); return
    ids = [pid for pid, _ in amostras]
    A = np.vstack([np.asarray(json.loads(e) if isinstance(e, str) else e, np.float32) for _, e in amostras])
    A /= np.linalg.norm(A, axis=1, keepdims=True)
    pessoas = sorted(set(ids), key=str)
    col = {p: i for i, p in enumerate(pessoas)}
    colunas = np.array([col[p] for p in ids])

    # Quais fichas são confiáveis para o automático.
    S = A @ A.T
    duvidosas = set()
    for p in pessoas:
        m = colunas == col[p]
        if m.sum() < 2:
            duvidosas.add(p); continue
        coesao = S[np.ix_(m, m)][~np.eye(int(m.sum()), dtype=bool)].mean()
        cruzada = (S[np.ix_(m, ~m)].max(axis=1) >= APOIO).mean() if (~m).any() else 0.0
        if coesao < FICHA_COESAO_MIN or cruzada > FICHA_CRUZADA_MAX:
            duvidosas.add(p)

    motivos = Counter()
    auto, duvida = [], []
    for codigo, quando, cam_id in soltas:
        item = vetores.get(codigo)
        if item is None:
            motivos["sem rosto aproveitável"] += 1; continue
        v, alt, det = item
        s = A @ v
        melhor = np.full(len(pessoas), -1.0, np.float32)
        np.maximum.at(melhor, colunas, s)
        apoio = np.bincount(colunas, weights=(s >= APOIO), minlength=len(pessoas))
        ordem = np.argsort(-melhor)
        b1, b2 = float(melhor[ordem[0]]), float(melhor[ordem[1]]) if len(ordem) > 1 else -1.0
        pid = pessoas[ordem[0]]
        if b1 < DUVIDA_SIM:
            motivos["ninguém parecido (fica para a triagem)"] += 1; continue
        if (b1 >= AUTO_SIM and b1 - b2 >= AUTO_FOLGA and apoio[ordem[0]] >= AUTO_AMOSTRAS
                and alt >= ROSTO_MIN_PX and det >= DETECTOR_MIN and pid not in duvidosas):
            auto.append((codigo, pid, b1, quando, cam_id)); motivos["entra na ficha"] += 1
        else:
            duvida.append((codigo, pid, b1)); motivos["vai para aprovação"] += 1

    print(f"{len(soltas)} capturas soltas e não revisadas")
    for k, n in motivos.most_common():
        print(f"  {k}: {n}")
    por_pessoa = Counter(p for _, p, *_ in auto)
    async with db_context() as db:
        nomes = {p.id: p.nome for p in (await db.execute(select(Pessoa).where(Pessoa.id.in_(list(set(por_pessoa) | duvidosas))))).scalars()}
    print("  fichas com amostras incoerentes (só por aprovação):", ", ".join(sorted(nomes.get(p, str(p)) for p in duvidosas)) or "nenhuma")
    for pid, n in por_pessoa.most_common(15):
        print(f"    {nomes.get(pid, pid)}: +{n}")
    import os
    if os.environ.get("RECONCILIAR_LISTA"):     # para conferência em mosaico
        Path(os.environ["RECONCILIAR_LISTA"]).write_text(json.dumps(
            [{"codigo": c, "pessoa": nomes.get(p, str(p)), "sim": round(s, 3)} for c, p, s, _, _ in auto],
            ensure_ascii=False), encoding="utf-8")
    if not aplicar:
        print("\n(nada gravado; rode com --aplicar)"); return

    async with db_context() as db:
        for codigo, pid, sim, _, _ in auto:
            await db.execute(update(Deteccao)
                             .where(Deteccao.codigo == codigo, Deteccao.pessoa_id.is_(None))
                             .values(pessoa_id=pid, similaridade=round(sim, 4), pessoa_sugerida_id=None))
        for codigo, pid, sim in duvida:
            await db.execute(update(Deteccao)
                             .where(Deteccao.codigo == codigo, Deteccao.pessoa_id.is_(None))
                             .values(pessoa_sugerida_id=pid, similaridade=round(sim, 4)))
        # Contador e última passagem de cada ficha que ganhou rostos.
        novos = defaultdict(list)
        for codigo, pid, sim, quando, cam_id in auto:
            novos[pid].append((quando, cam_id))
        for p in (await db.execute(select(Pessoa).where(Pessoa.id.in_(list(novos))))).scalars():
            lista = novos[p.id]
            p.total_deteccoes = (p.total_deteccoes or 0) + len(lista)
            quando, cam_id = max(lista, key=lambda t: t[0])
            if p.ultima_deteccao is None or quando > p.ultima_deteccao:
                p.ultima_deteccao = quando
                p.ultima_camera = cameras.get(cam_id, p.ultima_camera)
        await audit.registrar(
            db, acao="BIO_TRIAGEM", severidade="info",
            descricao=f"Conciliação facial: {len(auto)} captura(s) vinculada(s) às fichas, "
                      f"{len(duvida)} para aprovação",
            metadados={"vinculadas": len(auto), "para_aprovacao": len(duvida),
                       "criterio": {"sim": AUTO_SIM, "folga": AUTO_FOLGA, "amostras": AUTO_AMOSTRAS,
                                    "rosto_px": ROSTO_MIN_PX, "duvida": DUVIDA_SIM,
                                    "detector": DETECTOR_MIN, "coesao": FICHA_COESAO_MIN,
                                    "cruzada": FICHA_CRUZADA_MAX}})
    await engine.dispose()
    print(f"\ngravado: {len(auto)} vinculada(s), {len(duvida)} para aprovação")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    asyncio.run(main(ap.parse_args().aplicar))
