"""Etapa 2 do cadastro da triagem: grupos de capturas que são a mesma pessoa.

Conservador por desenho: o que não for claramente uma pessoa só fica na
triagem. Um grupo só nasce se:

* cada captura tem rosto bom (detector >= 0,60 e rosto >= 40 px);
* as capturas se ligam por semelhança >= LIGA (0,55), e depois cada uma fica
  a >= CENTRO (0,50) da média do grupo — quem não fica sai;
* o grupo tem >= MIN_GRUPO (5) capturas;
* o sexo é sugerido pela maioria e marcado "confirmar" abaixo de 80%;
* o grupo não se parece (>= 0,45) com nenhuma amostra de quem já está
  cadastrado — esse é quase-acerto, e a conferência humana resolve.

Gera data/cadastro_triagem/grupos.json e a prévia em HTML, com um nome
fictício completo e único por grupo.

    py -3.13 -m scripts.triagem_agrupar
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

from app.config import settings  # noqa: E402
from app import banco_direto  # noqa: E402

PASTA = RAIZ / "data" / "cadastro_triagem"
# Ligação por MÉDIA (09/11/2026). A ligação por um único par parecido, com
# piso 0,55, quase não juntava ninguém: nestas câmeras duas fotos da mesma
# pessoa passam de 0,55 em só 8% dos pares. Baixar o piso dela emendava todo
# mundo numa corrente (268 capturas num grupo só). Pela média, um par parecido
# por acaso não emenda duas pessoas. Calibrado nas 205 amostras das 20 pessoas
# cadastradas: média >= 0,30 dá 25 grupos, 23 de uma pessoa só, 3 capturas de
# outra pessoa no total, 19 das 20 pessoas encontradas. Pessoa repartida em
# dois grupos é inofensiva: aprovado um, o outro passa a "parecer cadastrado".
MEDIA, CENTRO, MIN_GRUPO, DET_MIN, ALT_MIN = 0.30, 0.40, 3, 0.60, 40.0
JA_CADASTRADO = 0.45

NOMES_M = ["Adriano", "Alexandre", "Anderson", "Antônio", "Arthur", "Bruno", "Carlos", "César",
           "Cláudio", "Daniel", "Diego", "Douglas", "Eduardo", "Emerson", "Fábio", "Fernando",
           "Flávio", "Francisco", "Guilherme", "Gustavo", "Hélio", "Henrique", "Igor", "Jefferson",
           "João", "Jorge", "Júlio", "Leandro", "Leonardo", "Luciano", "Marcelo", "Márcio",
           "Marcos", "Mário", "Maurício", "Murilo", "Otávio", "Paulo", "Rafael", "Renato",
           "Ricardo", "Roberto", "Rodrigo", "Sérgio", "Thiago", "Valter", "Vinícius", "Wagner"]
NOMES_F = ["Adriana", "Alessandra", "Aline", "Amanda", "Ana Paula", "Andreia", "Beatriz",
           "Bianca", "Bruna", "Camila", "Carla", "Cristiane", "Daniela", "Débora", "Eliane",
           "Fabiana", "Fernanda", "Flávia", "Gabriela", "Isabela", "Jaqueline", "Joana",
           "Juliana", "Larissa", "Letícia", "Lúcia", "Mariana", "Marina", "Mônica", "Natália",
           "Patrícia", "Priscila", "Raquel", "Renata", "Rosana", "Sabrina", "Sandra", "Simone",
           "Sônia", "Tatiana", "Vanessa", "Viviane"]
MEIOS = ["Aparecido", "Augusto", "Cristiano", "Henrique", "Luiz", "Roberto", "Vieira",
         "da Silva", "de Souza", "dos Santos", "Ferreira", "Gomes", "Lima", "Moraes"]
MEIOS_F = ["Aparecida", "Cristina", "Helena", "Luiza", "Regina", "Vitória", "da Silva",
           "de Souza", "dos Santos", "Ferreira", "Gomes", "Lima", "Moraes"]
SOBRENOMES = ["Albuquerque", "Almeida", "Andrade", "Araújo", "Barbosa", "Barros", "Batista",
              "Borges", "Campos", "Cardoso", "Carvalho", "Castro", "Correia", "Costa", "Cunha",
              "Dias", "Duarte", "Farias", "Fonseca", "Freitas", "Lopes", "Machado", "Martins",
              "Medeiros", "Melo", "Mendes", "Miranda", "Monteiro", "Nascimento", "Nogueira",
              "Oliveira", "Pereira", "Pinto", "Ramos", "Reis", "Ribeiro", "Rocha", "Santana",
              "Siqueira", "Soares", "Teixeira", "Vasconcelos"]


def nomes_ficticios(sexos, existentes):
    rnd = random.Random(20261106)
    usados = {n.lower() for n in existentes}
    primeiros_usados = {n.split()[0].lower() for n in existentes}
    saida = []
    for sexo in sexos:
        base, meios = (NOMES_M, MEIOS) if sexo == "M" else (NOMES_F, MEIOS_F)
        for _ in range(10000):
            nome = f"{rnd.choice(base)} {rnd.choice(meios)} {rnd.choice(SOBRENOMES)}"
            # Nome completo único, e de preferência primeiro nome também — dois
            # "Marcelo" na mesma lista confundem quem lê o alerta.
            if nome.lower() in usados:
                continue
            if nome.split()[0].lower() in primeiros_usados and _ < 3000:
                continue
            usados.add(nome.lower())
            primeiros_usados.add(nome.split()[0].lower())
            saida.append(nome)
            break
        else:
            raise RuntimeError("nomes esgotados")
    return saida


def raiz(pai, i):
    while pai[i] != i:
        pai[i] = pai[pai[i]]
        i = pai[i]
    return i


def main():
    # Fechado logo após ler: a extração pode estar rodando e precisa trocar
    # este arquivo; aberto, o Windows recusa a troca e a extração cai.
    with np.load(PASTA / "vetores.npz", allow_pickle=True) as arq:
        d = {k: arq[k] for k in arq.files}
    ok = (d["sexo"] >= 0) & (d["det"] >= DET_MIN) & (d["alt"] >= ALT_MIN)
    idx = np.where(ok)[0]
    V = d["vet"][idx].astype(np.float32)
    V /= np.linalg.norm(V, axis=1, keepdims=True)
    n = len(idx)
    print(f"{len(d['codigos'])} analisadas; {n} com rosto bom")

    # Agrupamento hierárquico por ligação média (ver MEDIA acima).
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist
    brutos = {}
    if n >= 2:
        rotulo = fcluster(linkage(pdist(V, "cosine"), "average"), 1 - MEDIA, "distance")
        for i, k in enumerate(rotulo):
            brutos.setdefault(int(k), []).append(i)
    elif n == 1:
        brutos[0] = [0]

    # Cadastrados: amostras de quem já tem ficha.
    c = banco_direto.conectar()
    linhas_a = c.execute("select a.embedding, p.id_biometrico, p.nome from amostras_faciais a "
                         "join pessoas p on p.id=a.pessoa_id where p.ativo").fetchall()
    A = np.array([json.loads(e) if isinstance(e, str) else e for (e, _, _) in linhas_a], np.float32)
    dono_a = [(i, n) for (_, i, n) in linhas_a]
    if len(A):
        A /= np.linalg.norm(A, axis=1, keepdims=True)
    existentes = [n for (n,) in c.execute("select nome from pessoas")]

    grupos, motivos = [], Counter()
    for membros in brutos.values():
        if len(membros) < MIN_GRUPO:
            motivos["poucas capturas"] += 1
            continue
        m = np.array(membros)
        # Encolhe até todos ficarem perto do centro (corrente de ligações
        # pode juntar duas pessoas por um elo fraco).
        for _ in range(10):
            centro = V[m].mean(0); centro /= np.linalg.norm(centro)
            perto = (V[m] @ centro) >= CENTRO
            if perto.all():
                break
            m = m[perto]
        if len(m) < MIN_GRUPO:
            motivos["grupo disperso"] += 1
            continue
        # Sexo é sugestão, não filtro: o modelo acerta 89% dos homens e só
        # 64% das mulheres nestas câmeras (medido nas 12 fichas conhecidas).
        # Quem garante que o grupo é uma pessoa é a semelhança do rosto; o
        # sexo vai para a prévia, marcado "confirmar" quando não é claro.
        sexos = d["sexo"][idx[m]]
        maioria = Counter(sexos.tolist()).most_common(1)[0]
        centro = V[m].mean(0); centro /= np.linalg.norm(centro)
        ja = None
        if len(A):
            sims = A @ centro
            k = int(sims.argmax())
            if float(sims[k]) >= JA_CADASTRADO:
                motivos["parece cadastrado (vai para vínculo)"] += 1
                ja = {"id_biometrico": dono_a[k][0], "nome": dono_a[k][1], "similaridade": round(float(sims[k]), 3)}
        coesao = float((V[m] @ centro).mean())
        grupos.append({"membros": m, "sexo": "M" if maioria[0] == 1 else "F",
                       "sexo_conf": maioria[1] / len(m),
                       "coesao": coesao, "centro": centro, "ja": ja})

    # Dois grupos muito parecidos podem ser a mesma pessoa: fica um só aviso,
    # e não se juntam sozinhos (a prévia mostra os dois lado a lado).
    for g in grupos:
        g["parecidos"] = []
    for i, g in enumerate(grupos):
        for j in range(i + 1, len(grupos)):
            if float(g["centro"] @ grupos[j]["centro"]) >= 0.50:
                g["parecidos"].append(j); grupos[j]["parecidos"].append(i)

    grupos.sort(key=lambda g: -len(g["membros"]))
    # Pessoas novas primeiro; os vínculos a fichas existentes depois.
    grupos.sort(key=lambda g: (g["ja"] is not None, -len(g["membros"])))
    nomes = nomes_ficticios([g["sexo"] for g in grupos], existentes)
    cams = dict(c.execute("select id, codigo from cameras"))
    saida = []
    for k, (g, nome) in enumerate(zip(grupos, nomes), 1):
        ii = idx[g["membros"]]
        qual = d["det"][ii] * np.minimum(d["alt"][ii] / 112, 1)
        ordem = ii[np.argsort(-qual)]
        saida.append({
            "grupo": k, "nome": nome, "sexo": g["sexo"], "capturas": len(ii),
            "sexo_conf": round(g["sexo_conf"], 2),
            "confirmar_sexo": g["sexo_conf"] < 0.80,
            "coesao": round(g["coesao"], 3),
            "cameras": sorted({cams.get(x, "?") for x in d["camera"][ii]}),
            "codigos": [str(x) for x in d["codigos"][ordem]],
            "parecidos_com": g["parecidos"],
            "ja_cadastrado": g["ja"],
        })
    # Troca atômica: a tela de aprovação lê este arquivo a qualquer momento, e
    # gravá-lo por cima deixava uma janela em que ela leria metade.
    from scripts.triagem_extrair import trocar
    tmp = PASTA / "grupos.tmp.json"
    tmp.write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
    trocar(tmp, PASTA / "grupos.json")
    print(f"{len(saida)} grupos; {sum(g['capturas'] for g in saida)} capturas; "
          f"M {sum(g['sexo'] == 'M' for g in saida)} / F {sum(g['sexo'] == 'F' for g in saida)}")
    print("descartados:", dict(motivos))


if __name__ == "__main__":
    main()
