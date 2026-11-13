"""Auditoria das fichas biométricas: fichas repetidas e rostos fora do lugar.

Duas verificações, ficha por ficha:

1. **Fichas repetidas** — duas fichas que são a mesma pessoa. Compara o rosto
   típico de cada uma e quantos rostos de uma batem melhor com a outra. Só
   relata: juntar fichas é decisão de quem opera.
2. **Rostos que não são da ficha** — cada rosto vinculado é comparado com as
   amostras da própria ficha (sem a amostra que nasceu dele mesmo) e com as
   das outras. Sai da ficha:
   * o rosto claramente de OUTRA pessoa cadastrada: desvinculado e posto em
     "Rostos para confirmar" daquela outra ficha;
   * o rosto nítido que não lembra ninguém da ficha: marcado "não é esta
     pessoa" (falso positivo), como a remoção feita à mão na ficha.
   Nos dois casos a amostra que nasceu desse rosto sai do template, a foto
   de perfil deixa de usá-lo e o contador é refeito.

Rosto borrado, pequeno ou de perfil não é julgado: vetor ruim parece "outra
pessoa" sem ser. Os vetores vêm da extração da triagem.

    py -3.13 -m scripts.auditar_fichas            # só relata
    py -3.13 -m scripts.auditar_fichas --aplicar  # grava
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

# Julgável: rosto com pixels e detector suficientes.
ROSTO_MIN_PX = 40
DETECTOR_MIN = 0.50
# De outra pessoa: a outra ficha bate forte e com folga sobre a própria.
OUTRA_SIM = 0.50
OUTRA_FOLGA = 0.12
OUTRA_APOIO = 2            # amostras da outra ficha com >= 0,45
# Não lembra a ficha: fraco contra as amostras E contra o rosto típico.
PROPRIA_MAX = 0.28
TIPICO_MAX = 0.22
# Fichas repetidas.
REPETIDA_TIPICO = 0.55


def _norm(M):
    M = np.asarray(M, np.float32)
    return M / np.linalg.norm(M, axis=-1, keepdims=True)


async def main(aplicar: bool) -> None:
    from sqlalchemy import func, select, update

    from app import audit
    from app.database import db_context, engine
    from app.models import AmostraFacial, Deteccao, Pessoa, TipoDeteccao
    from app.services import face_engine

    z = np.load(RAIZ / "data" / "cadastro_triagem" / "vetores.npz", allow_pickle=True)
    vet = {str(k): (v, float(a), float(d)) for k, v, a, d in zip(z["codigos"], z["vet"], z["alt"], z["det"])
           if np.linalg.norm(v) > 0.5}

    async with db_context() as db:
        pessoas = {p.id: p for p in (await db.execute(select(Pessoa).where(Pessoa.ativo.is_(True)))).scalars()}
        amostras = (await db.execute(select(AmostraFacial.pessoa_id, AmostraFacial.embedding, AmostraFacial.deteccao_id)
                                     .where(AmostraFacial.pessoa_id.in_(list(pessoas))))).all()
        vinculadas = (await db.execute(
            select(Deteccao.id, Deteccao.codigo, Deteccao.pessoa_id, Deteccao.veredito)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL, Deteccao.pessoa_id.in_(list(pessoas))))).all()

    A = _norm([json.loads(e) if isinstance(e, str) else e for _, e, _ in amostras])
    donos = np.array([str(p) for p, _, _ in amostras]); origem = [d for _, _, d in amostras]
    ids = sorted(set(donos)); col = {p: i for i, p in enumerate(ids)}
    cols = np.array([col[p] for p in donos])

    # Rostos de cada ficha com vetor.
    faces = defaultdict(list)
    sem_vetor = Counter()
    for did, cod, pid, ver in vinculadas:
        if cod in vet:
            v, alt, det = vet[cod]
            faces[str(pid)].append({"id": did, "cod": cod, "v": _norm(v), "alt": alt, "det": det, "ver": ver})
        else:
            sem_vetor[str(pid)] += 1

    # Rosto típico robusto: média dos rostos nítidos, refeita sem os que não lembram a média.
    tipico = {}
    for p in ids:
        base = [f["v"] for f in faces.get(p, []) if f["alt"] >= ROSTO_MIN_PX and f["det"] >= DETECTOR_MIN]
        base = base or [A[k] for k in np.where(donos == p)[0]]
        c = _norm(np.mean(base, axis=0))
        for _ in range(2):
            perto = [b for b in base if float(b @ c) >= 0.30] or base
            c = _norm(np.mean(perto, axis=0))
        tipico[p] = c
    T = np.vstack([tipico[p] for p in ids])
    nome = lambda p: pessoas[uuid_de[p]].nome if p in uuid_de else p
    uuid_de = {str(k): k for k in pessoas}

    # ---- 1. fichas repetidas ----
    S = T @ T.T
    np.fill_diagonal(S, -1)
    repetidas = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if S[i, j] >= REPETIDA_TIPICO:
                # Quantos rostos de cada uma batem melhor com a outra.
                def migram(a, b):
                    fs = [f for f in faces.get(ids[a], []) if f["alt"] >= ROSTO_MIN_PX]
                    if not fs: return 0, 0
                    V = np.vstack([f["v"] for f in fs])
                    return int(((V @ T[b]) > (V @ T[a])).sum()), len(fs)
                repetidas.append((float(S[i, j]), ids[i], ids[j], migram(i, j), migram(j, i)))
    repetidas.sort(reverse=True)
    print(f"{len(pessoas)} fichas · {len(A)} amostras · {sum(len(l) for l in faces.values())} rostos vinculados com vetor"
          f" · {sum(sem_vetor.values())} sem rosto aproveitável (não julgados)")
    print(f"\n1. FICHAS POSSIVELMENTE REPETIDAS (rosto típico >= {REPETIDA_TIPICO}): {len(repetidas)}")
    for s, a, b, (ma, na), (mb, nb) in repetidas:
        print(f"   {s:.2f}  {nome(a)} ({pessoas[uuid_de[a]].id_biometrico})  <->  {nome(b)} ({pessoas[uuid_de[b]].id_biometrico})"
              f"   rostos que batem mais com a outra: {ma}/{na} e {mb}/{nb}")

    # ---- 2. rostos fora do lugar ----
    outra, nao_parece = [], []
    julgados = 0
    por_ficha = defaultdict(Counter)
    for p, lista in faces.items():
        if p not in col: continue
        for f in lista:
            if f["alt"] < ROSTO_MIN_PX or f["det"] < DETECTOR_MIN:
                continue
            julgados += 1
            s = A @ f["v"]
            proprio = (cols == col[p]) & np.array([o != f["id"] for o in origem])
            own = float(s[proprio].max()) if proprio.any() else -1.0
            m = np.full(len(ids), -1.0, np.float32); np.maximum.at(m, cols, s)
            ap = np.bincount(cols, weights=(s >= 0.45), minlength=len(ids))
            m[col[p]] = -1
            q = int(np.argmax(m)); other = float(m[q])
            tip = float(f["v"] @ tipico[p])
            item = {"cod": f["cod"], "id": f["id"], "pessoa": p, "propria": round(own, 3), "tipico": round(tip, 3),
                    "outra": ids[q], "outra_sim": round(other, 3), "confirmado": f["ver"] == "Confirmado"}
            if other >= OUTRA_SIM and other - max(own, tip) >= OUTRA_FOLGA and ap[q] >= OUTRA_APOIO:
                outra.append(item); por_ficha[p]["outra pessoa"] += 1
            elif own < PROPRIA_MAX and tip < TIPICO_MAX:
                nao_parece.append(item); por_ficha[p]["não lembra a ficha"] += 1

    print(f"\n2. ROSTOS FORA DO LUGAR ({julgados} rostos nítidos julgados)")
    print(f"   de outra pessoa cadastrada: {len(outra)} ({sum(i['confirmado'] for i in outra)} tinham sido confirmados)")
    print(f"   não lembram a ficha:        {len(nao_parece)} ({sum(i['confirmado'] for i in nao_parece)} tinham sido confirmados)")
    for p, c in sorted(por_ficha.items(), key=lambda t: -sum(t[1].values()))[:25]:
        print(f"     {nome(p)}: " + ", ".join(f"{k} {n}" for k, n in c.items()))
    if os.environ.get("AUDITAR_LISTA"):
        Path(os.environ["AUDITAR_LISTA"]).write_text(json.dumps(
            {"outra": [{**i, "id": str(i["id"]), "nome": nome(i["pessoa"]), "nome_outra": nome(i["outra"])} for i in outra],
             "nao_parece": [{**i, "id": str(i["id"]), "nome": nome(i["pessoa"])} for i in nao_parece],
             "repetidas": [[s, a, b] for s, a, b, *_ in repetidas]}, ensure_ascii=False), encoding="utf-8")
    if not aplicar:
        print("\n(nada gravado; rode com --aplicar)"); await engine.dispose(); return

    from datetime import datetime, timezone
    agora = datetime.now(timezone.utc)
    async with db_context() as db:
        tocadas = set()
        for i in outra:
            # Sai desta ficha e vai para a conferência da outra: quem decide é uma pessoa.
            await db.execute(update(Deteccao).where(Deteccao.id == i["id"]).values(
                pessoa_id=None, pessoa_sugerida_id=uuid_de[i["outra"]], similaridade=i["outra_sim"],
                revisado=False, veredito=None))
            await face_engine.remover_amostras_da_captura(db, i["id"]); tocadas.add(i["pessoa"])
        for i in nao_parece:
            await db.execute(update(Deteccao).where(Deteccao.id == i["id"]).values(
                pessoa_id=None, pessoa_sugerida_id=uuid_de[i["pessoa"]], revisado=True,
                veredito="Falso positivo", revisado_em=agora))
            await face_engine.remover_amostras_da_captura(db, i["id"]); tocadas.add(i["pessoa"])
        await db.flush()
        saiu = {i["cod"] for i in outra + nao_parece}
        for p in tocadas:
            pessoa = await db.get(Pessoa, uuid_de[p])
            if (pessoa.foto_deteccao or "") in saiu:
                pessoa.foto_deteccao = ""
            pessoa.total_deteccoes = (await db.execute(select(func.count()).select_from(Deteccao)
                                      .where(Deteccao.pessoa_id == pessoa.id))).scalar_one()
            await face_engine.recalcular_template(db, pessoa)
        await audit.registrar(db, acao="BIO_TRIAGEM", severidade="warn",
                              descricao=f"Auditoria das fichas: {len(outra)} rosto(s) de outra pessoa para conferência, "
                                        f"{len(nao_parece)} marcado(s) como não sendo da ficha",
                              metadados={"outra_pessoa": len(outra), "nao_lembra": len(nao_parece),
                                         "fichas": len(tocadas), "repetidas": len(repetidas)})
    await engine.dispose()
    print(f"\ngravado: {len(outra)} para conferência da outra ficha, {len(nao_parece)} fora da ficha, {len(tocadas)} ficha(s) recalculada(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    asyncio.run(main(ap.parse_args().aplicar))
