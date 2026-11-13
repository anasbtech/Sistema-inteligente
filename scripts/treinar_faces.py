"""Treina a galeria de reconhecimento com os rostos que uma pessoa confirmou.

O modelo que tira o vetor do rosto (InsightFace) não aprende pessoas: quem
decide "é fulano" é a comparação com as **amostras** de cada ficha. Até aqui
elas eram poucas (1.157) diante dos 13 mil rostos já confirmados por quem
opera — quase todos os ângulos que a operação conferiu não ensinavam nada.

Este script, para cada ficha:

1. **Limpa**: amostra que se parece mais com o rosto típico de outra pessoa
   do que com o da própria ficha sai da galeria (é o que fazia a ficha de nuca
   do Márcio Luiz Miranda "reconhecer" qualquer um).
2. **Amplia**: escolhe até `NOVAS_MAX` rostos confirmados, espalhados pelos
   ângulos e câmeras (o mais diferente do que já existe, por vez), e os torna
   amostras. Só entra rosto confirmado por uma pessoa e coerente com a ficha.
3. **Mede antes de gravar**: separa 20% dos confirmados como prova e compara
   a galeria de hoje com a nova — acertos, erros sobre outras fichas e erros
   sobre rostos que alguém já recusou.

    py -3.13 -m scripts.treinar_faces            # só mede
    py -3.13 -m scripts.treinar_faces --aplicar  # grava e recalcula as fichas
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

NOVAS_MAX = 40            # amostras novas por ficha, por rodada
GALERIA_MAX = 120         # teto de amostras por ficha: rodando sempre, não cresce sem fim
DUPLICADA = 0.85          # tão parecida com uma amostra que não ensina nada
COERENTE_MIN = 0.30       # rosto confirmado precisa lembrar o rosto típico da ficha
CONTAMINADA_FOLGA = 0.05  # amostra mais parecida com outra pessoa por esta folga sai
# Regra do automático (a mesma da conciliação), para medir.
# Medido em 12/11/2026 com a galeria treinada: folga 0,15 e 3 amostras deram
# 0 erro automático em 2.331 rostos de prova (com 0,10 e 2 foram 9).
AUTO_SIM, AUTO_FOLGA, AUTO_AMOSTRAS, APOIO = 0.60, 0.15, 3, 0.50


def _norm(M):
    M = np.asarray(M, np.float32)
    return M / np.linalg.norm(M, axis=-1, keepdims=True)


def avaliar(G, donos, testes, impostores):
    """Quantos rostos de prova a regra do automático acertaria e erraria."""
    pessoas = sorted(set(donos), key=str); col = {p: i for i, p in enumerate(pessoas)}
    cols = np.array([col[p] for p in donos])
    r = Counter()

    def decide(v):
        s = G @ v
        m = np.full(len(pessoas), -1.0, np.float32); np.maximum.at(m, cols, s)
        ap = np.bincount(cols, weights=(s >= APOIO), minlength=len(pessoas))
        o = np.argsort(-m); b1 = m[o[0]]; b2 = m[o[1]] if len(o) > 1 else -1
        if b1 >= AUTO_SIM and b1 - b2 >= AUTO_FOLGA and ap[o[0]] >= AUTO_AMOSTRAS:
            return pessoas[o[0]], "auto"
        return pessoas[o[0]], ("sugestao" if b1 >= 0.45 else "nada")

    for v, dono in testes:
        p, modo = decide(v)
        if modo == "auto":
            r["acerto automatico" if p == dono else "ERRO automatico (outra ficha)"] += 1
        elif modo == "sugestao":
            r["sugestao certa" if p == dono else "sugestao errada"] += 1
        else:
            r["nao reconhecido"] += 1
    for v, recusada in impostores:
        p, modo = decide(v)
        if modo == "auto" and p == recusada:
            r["ERRO: vinculou rosto que voce recusou"] += 1
    return r


async def main(aplicar: bool) -> None:
    from sqlalchemy import delete, select

    from app import audit
    from app.database import db_context, engine
    from app.models import AmostraFacial, Deteccao, Pessoa, TipoDeteccao
    from app.services import face_engine

    z = np.load(RAIZ / "data" / "cadastro_triagem" / "vetores.npz", allow_pickle=True)
    vet = {str(k): (v, float(q)) for k, v, q in zip(z["codigos"], z["vet"], z["qual"]) if np.linalg.norm(v) > 0.5}

    async with db_context() as db:
        amostras = (await db.execute(select(AmostraFacial.id, AmostraFacial.pessoa_id, AmostraFacial.embedding)
                                     .join(Pessoa, Pessoa.id == AmostraFacial.pessoa_id)
                                     .where(Pessoa.ativo.is_(True)))).all()
        confirmadas = (await db.execute(
            select(Deteccao.id, Deteccao.codigo, Deteccao.pessoa_id, Deteccao.camera_id)
            .join(Pessoa, Pessoa.id == Deteccao.pessoa_id)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL, Pessoa.ativo.is_(True),
                   (Deteccao.veredito == "Confirmado") | (Deteccao.similaridade >= 0.999)))).all()
        recusadas = (await db.execute(
            select(Deteccao.codigo, Deteccao.pessoa_sugerida_id)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL, Deteccao.veredito == "Falso positivo",
                   Deteccao.pessoa_sugerida_id.isnot(None)))).all()
        nomes = {p.id: p.nome for p in (await db.execute(select(Pessoa))).scalars()}
        ja_amostra = {d for (d,) in (await db.execute(select(AmostraFacial.deteccao_id)
                                                      .where(AmostraFacial.deteccao_id.isnot(None)))).all()}

    A = _norm([json.loads(e) if isinstance(e, str) else e for _, _, e in amostras])
    a_ids = [i for i, _, _ in amostras]; a_donos = [p for _, p, _ in amostras]

    # Rostos confirmados com vetor, por ficha.
    por_pessoa = defaultdict(list)
    for did, cod, pid, cam in confirmadas:
        if cod in vet and did not in ja_amostra:
            v, q = vet[cod]
            por_pessoa[pid].append({"det": did, "cod": cod, "cam": cam, "v": _norm(v), "q": q})
    # Rosto típico de cada ficha: média dos confirmados (ou das amostras, se não houver).
    tipico = {}
    for p in set(a_donos) | set(por_pessoa):
        base = [x["v"] for x in por_pessoa.get(p, [])] or [A[i] for i, d in enumerate(a_donos) if d == p]
        if base:
            c = _norm(np.mean(base, axis=0))
            # Uma passada robusta: refaz sem os que nem lembram a média.
            perto = [b for b in base if float(b @ c) >= COERENTE_MIN] or base
            tipico[p] = _norm(np.mean(perto, axis=0))
    pessoas = sorted(tipico, key=str); T = np.vstack([tipico[p] for p in pessoas])
    idx = {p: i for i, p in enumerate(pessoas)}

    def mais_parecida_com_outra(v, p):
        s = T @ v; proprio = s[idx[p]]; s[idx[p]] = -1
        return float(s.max()) > proprio + CONTAMINADA_FOLGA

    # 1. Amostras contaminadas.
    sair = [k for k, (v, p) in enumerate(zip(A, a_donos)) if p in idx and mais_parecida_com_outra(v.copy(), p)]
    # 2. Rostos confirmados coerentes, separados em treino (80%) e prova (20%).
    rng = np.random.default_rng(13)
    treino, prova = defaultdict(list), []
    for p, lista in por_pessoa.items():
        boas = [x for x in lista if p in idx and float(x["v"] @ tipico[p]) >= COERENTE_MIN
                and not mais_parecida_com_outra(x["v"].copy(), p)]
        rng.shuffle(boas)
        corte = max(1, int(len(boas) * 0.2)) if len(boas) >= 5 else 0
        prova += [(x["v"], p) for x in boas[:corte]]
        treino[p] = boas[corte:]

    def escolher(p, lista, existentes):
        """Até NOVAS_MAX, cada uma o mais diferente possível do que já há."""
        escolhidas = []
        atual = list(existentes)
        cand = sorted(lista, key=lambda x: -x["q"])
        while cand and len(escolhidas) < NOVAS_MAX:
            if atual:
                M = np.vstack(atual)
                prox = np.array([float((M @ x["v"]).max()) for x in cand])
            else:
                prox = np.zeros(len(cand))
            k = int(np.argmin(prox))
            if prox[k] >= DUPLICADA:
                break
            x = cand.pop(k); escolhidas.append(x); atual.append(x["v"])
        return escolhidas

    manter = [k for k in range(len(A)) if k not in set(sair)]
    G0, d0 = A, a_donos
    novas = {}
    for p in pessoas:
        existentes = [A[k] for k in manter if a_donos[k] == p]
        novas[p] = escolher(p, treino.get(p, []), existentes)[:max(0, GALERIA_MAX - len(existentes))]
    G1 = np.vstack([A[manter]] + [np.vstack([x["v"] for x in l]) for l in novas.values() if l])
    d1 = [a_donos[k] for k in manter] + [p for p, l in novas.items() for _ in l]
    impost = [(_norm(vet[c][0]), p) for c, p in recusadas if c in vet]

    print(f"galeria hoje: {len(A)} amostras · contaminadas a remover: {len(sair)} · novas: {sum(len(l) for l in novas.values())}")
    print(f"prova: {len(prova)} rostos confirmados separados · {len(impost)} rostos recusados")
    antes, depois = avaliar(G0, d0, prova, impost), avaliar(G1, d1, prova, impost)
    print(f"\n{'':40s}{'hoje':>8s}{'treinada':>10s}")
    for k in sorted(set(antes) | set(depois)):
        print(f"{k:40s}{antes.get(k, 0):8d}{depois.get(k, 0):10d}")
    sai_por = Counter(nomes.get(a_donos[k], "?") for k in sair)
    if sai_por:
        print("\namostras contaminadas por ficha:", ", ".join(f"{n} {q}" for n, q in sai_por.most_common(12)))

    if not aplicar:
        print("\n(nada gravado; rode com --aplicar)"); return
    # Na gravação o treino usa também os 20% de prova: nada de bom fica de fora.
    for p in pessoas:
        existentes = [A[k] for k in manter if a_donos[k] == p] + [x["v"] for x in novas[p]]
        resto = [x for x in por_pessoa.get(p, []) if x not in novas[p]
                 and float(x["v"] @ tipico[p]) >= COERENTE_MIN and not mais_parecida_com_outra(x["v"].copy(), p)]
        cabe = max(0, min(NOVAS_MAX - len(novas[p]), GALERIA_MAX - len(existentes)))
        novas[p] += escolher(p, resto, existentes)[:cabe]
    async with db_context() as db:
        if sair:
            await db.execute(delete(AmostraFacial).where(AmostraFacial.id.in_([a_ids[k] for k in sair])))
        total = 0
        for p, lista in novas.items():
            for x in lista:
                db.add(AmostraFacial(pessoa_id=p, embedding=x["v"].tolist(), qualidade=x["q"],
                                     origem="captura", camera_id=x["cam"], deteccao_id=x["det"]))
                total += 1
        await db.flush()
        tocadas = set(novas) | {a_donos[k] for k in sair}
        for pessoa in (await db.execute(select(Pessoa).where(Pessoa.id.in_(list(tocadas))))).scalars():
            await face_engine.recalcular_template(db, pessoa)
        await audit.registrar(db, acao="BIO_AMOSTRA", severidade="warn",
                              descricao=f"Treino da galeria: {total} amostra(s) de rostos confirmados, "
                                        f"{len(sair)} contaminada(s) removida(s)",
                              metadados={"novas": total, "removidas": len(sair),
                                         "antes": dict(antes), "depois": dict(depois)})
    await engine.dispose()
    print(f"\ngravado: {total} amostra(s) nova(s), {len(sair)} removida(s), {len(tocadas)} ficha(s) recalculada(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--aplicar", action="store_true")
    ap.add_argument("--folga", type=float, default=AUTO_FOLGA)
    ap.add_argument("--apoio", type=int, default=AUTO_AMOSTRAS)
    ap.add_argument("--sim", type=float, default=AUTO_SIM)
    a = ap.parse_args()
    AUTO_FOLGA, AUTO_AMOSTRAS, AUTO_SIM = a.folga, a.apoio, a.sim
    asyncio.run(main(a.aplicar))
