"""Agrupa rostos sem cadastro e abre um BIO-N para cada pessoa encontrada.

    python scripts/agrupar_desconhecidos.py                 # simula
    python scripts/agrupar_desconhecidos.py --aplicar       # cria os BIO-N
    python scripts/agrupar_desconhecidos.py --limite 2000   # por partes

POR QUE EM LOTE, E NÃO NA HORA DA CAPTURA
-----------------------------------------
O reconhecimento normal compara uma captura contra um template já formado.
Para um desconhecido não existe template, e comparar captura contra captura
não funciona nesta instalação: medido sobre as amostras reais da base, dois
quadros da mesma pessoa só passam do limiar em 12% dos pares (mediana 0,366
contra limiar de 0,50). Um BIO-N aberto na hora, com uma amostra, não
reconheceria a própria pessoa no quadro seguinte — abriria outro, e outro.

Olhando o conjunto inteiro de uma vez o problema é outro. A mesma pessoa
aparece em dezenas de quadros, e basta que *alguns* pares se reconheçam para
que todos caiam no mesmo grupo por ligação. Medido numa amostra de 138
capturas: 44 grupos no limiar 0,40, sendo o maior com 40 quadros — quadros que
par a par jamais teriam se encontrado.

O grupo vira um BIO-N com todas as suas capturas como amostras, e aí o
reconhecimento normal passa a funcionar para ele, como funciona para os
cadastrados: a busca compara contra cada amostra e fica com a melhor.

AS DUAS ESCOLHAS QUE IMPORTAM
-----------------------------
`--limiar` decide o que é a mesma pessoa. Baixo demais funde gente distinta;
alto demais parte a mesma pessoa em vários registros. O padrão é conservador
de propósito: fragmentar é recuperável — alguém funde os registros depois —,
enquanto fundir duas pessoas num identificador só contamina a base e aparece
como alguém sendo reconhecido onde nunca esteve.

`--min-amostras` descarta grupo pequeno demais. Um grupo de uma captura é
exatamente o registro inútil que esta abordagem existe para evitar: ele
nasceria sem reconhecer ninguém e ficaria ocupando um identificador.

CUSTO
-----
O embedding não fica guardado na detecção, só o recorte, então cada captura
exige uma extração — medidos 754 ms por recorte nesta máquina, cerca de três
horas para 13 mil. Como isso disputa CPU com a inferência das câmeras, rode
fora do horário de movimento, ou por partes com `--limite`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import db_context  # noqa: E402
from app.models import AmostraFacial, Deteccao, TipoDeteccao  # noqa: E402
from app.services import biometrics, evidencias, face_engine  # noqa: E402


def agrupar_por_ligacao(matriz: np.ndarray, limiar: float) -> dict[int, list[int]]:
    """Une quem se parece, direta ou indiretamente.

    É o que resolve o caso difícil: A não se parece com C, mas A se parece com
    B e B com C, e os três são a mesma pessoa vista de ângulos que mudam aos
    poucos. Exigir que todos se parecessem entre si partiria esse grupo.

    O mesmo encadeamento é o risco da escolha do limiar: baixo demais, uma
    única ponte errada funde dois grupos inteiros.
    """
    pai = list(range(len(matriz)))

    def raiz(i: int) -> int:
        while pai[i] != i:
            pai[i] = pai[pai[i]]
            i = pai[i]
        return i

    semelhantes = np.argwhere(np.triu(matriz >= limiar, k=1))
    for i, j in semelhantes:
        a, b = raiz(int(i)), raiz(int(j))
        if a != b:
            pai[a] = b

    grupos: dict[int, list[int]] = defaultdict(list)
    for i in range(len(matriz)):
        grupos[raiz(i)].append(i)
    return dict(grupos)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true",
                    help="cria os registros; sem isto apenas simula")
    ap.add_argument("--limiar", type=float, default=0.45,
                    help="semelhança mínima para unir duas capturas (padrão: 0,45)")
    ap.add_argument("--min-amostras", type=int, default=3,
                    help="grupos menores que isto são ignorados (padrão: 3)")
    ap.add_argument("--limite", type=int, default=0,
                    help="processa no máximo N capturas (0 = todas)")
    args = ap.parse_args()

    async with db_context() as db:
        consulta = (
            select(Deteccao)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.pessoa_id.is_(None),
                   Deteccao.pessoa_sugerida_id.is_(None),
                   Deteccao.snapshot_key != "")
            .order_by(Deteccao.ocorrido_em.desc())
        )
        if args.limite:
            consulta = consulta.limit(args.limite)
        capturas = (await db.execute(consulta)).scalars().all()

    print(f"capturas sem cadastro: {len(capturas)}")
    if not capturas:
        return 0

    print("extraindo embeddings dos recortes…")
    inicio = time.time()
    vetores, origens, sem_rosto = [], [], 0
    for n, det in enumerate(capturas, 1):
        jpeg = evidencias.ler(det.snapshot_key)
        if jpeg is None:
            sem_rosto += 1
            continue
        face = await asyncio.to_thread(face_engine.extrair_de_recorte, jpeg)
        if face is None:
            sem_rosto += 1
            continue
        vetores.append(np.asarray(face.embedding, dtype=np.float32))
        origens.append((det, float(face.qualidade)))
        if n % 200 == 0:
            decorrido = time.time() - inicio
            print(f"  {n}/{len(capturas)} — {decorrido/n*1000:.0f} ms/recorte, "
                  f"restam ~{decorrido/n*(len(capturas)-n)/60:.0f} min")

    print(f"  {len(vetores)} com rosto legível, {sem_rosto} descartadas "
          f"({sem_rosto/len(capturas)*100:.0f}% dos recortes)")
    if len(vetores) < args.min_amostras:
        print("nada a agrupar.")
        return 0

    matriz = np.vstack([v / np.linalg.norm(v) for v in vetores])
    grupos = agrupar_por_ligacao(matriz @ matriz.T, args.limiar)
    aproveitaveis = sorted((g for g in grupos.values() if len(g) >= args.min_amostras),
                           key=len, reverse=True)
    cobertas = sum(len(g) for g in aproveitaveis)

    print(f"\nlimiar {args.limiar} · mínimo {args.min_amostras} amostras")
    print(f"  {len(grupos)} grupos, {len(aproveitaveis)} com tamanho suficiente")
    print(f"  cobrem {cobertas} capturas; {len(vetores)-cobertas} ficam sem registro")
    print(f"\n  os maiores: {[len(g) for g in aproveitaveis[:12]]}")

    if not args.aplicar:
        print("\nsimulação — nada foi criado. Use --aplicar para abrir os registros.")
        return 0

    print(f"\nabrindo {len(aproveitaveis)} registro(s)…")
    async with db_context() as db:
        for grupo in aproveitaveis:
            pessoa = await biometrics.abrir_registro_nao_identificado(
                db, None,
                f"Agrupado automaticamente a partir de {len(grupo)} capturas "
                f"sem correspondência (semelhança mínima {args.limiar}). "
                "Ao cadastrar a pessoa e informar o sexo, o identificador "
                "passa a BIO-M ou BIO-F.",
            )
            for i in grupo:
                det, qualidade = origens[i]
                db.add(AmostraFacial(
                    pessoa_id=pessoa.id, embedding=matriz[i].tolist(),
                    qualidade=round(qualidade, 4), origem="captura",
                    camera_id=det.camera_id,
                ))
                # A captura passa a pertencer ao registro e sai da fila.
                atual = await db.get(Deteccao, det.id)
                atual.pessoa_id = pessoa.id
                atual.expurgo_em = None
            await db.flush()
            await face_engine.recalcular_template(db, pessoa)
            print(f"  {pessoa.id_biometrico}: {len(grupo)} amostras, "
                  f"qualidade {pessoa.qualidade:.2f}")

    print(f"\n{len(aproveitaveis)} registro(s) abertos. "
          "Complete o cadastro de cada um para sair de BIO-N.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
