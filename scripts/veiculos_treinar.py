"""Treina e valida as sondas de tipo, cor e marca/modelo da inteligência veicular.

Uma sonda é uma camada linear sobre o vetor CLIP do veículo (o vetor fica
gravado em cada passagem). Treina em segundos, em CPU, e aprende com poucos
exemplos — que precisam ser **desta instalação**: a câmera, a luz e a frota
daqui.

De onde vêm os exemplos:
* rótulos dados na tela (Veículos → Rotular), tabela `rotulos_veiculo`;
* a base inicial em models/veiculos/<atributo>/base_inicial.npz, se houver
  (a de cor tem 94 recortes da Portaria e da Garagem rotulados à mão em
  06/11/2026; a de tipo, os recortes do histórico conferidos um a um em
  09/11/2026 — carro, picape, van, caminhão pequeno e grande, ônibus, moto).

Como decide o que afirmar: cada exemplo é julgado por uma sonda treinada sem
ele (validação deixa-um-de-fora, ou 10 partes acima de 150 exemplos). Para
cada classe procura-se o menor piso de confiança em que a precisão medida
alcança VEICULO_PRECISAO_ALVO, com pelo menos 3 acertos acima dele. Classe que
não alcança fica com piso infinito: o sistema nunca a afirma, e a passagem
sai "não identificada" com o motivo.

    py -3.13 -m scripts.veiculos_treinar              # treina tipo, cor e marca/modelo
    py -3.13 -m scripts.veiculos_treinar --so tipo
    py -3.13 -m scripts.veiculos_treinar --simular    # mede sem gravar a sonda
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import numpy as np  # noqa: E402

from app.config import settings  # noqa: E402
from app import banco_direto  # noqa: E402

PASTA = Path(settings.VEICULO_MARCA_MODELO_DIR).parent


def _motos() -> set:
    from app.services.veiculos.catalogo_br import MODELOS
    return {(m, n) for m, n, c in MODELOS if c == "moto"}


MOTOS = _motos()
ESCALA = 20.0


def exemplos(atributo: str) -> tuple[np.ndarray, list, list]:
    vetores, rotulos, origens = [], [], []
    base = PASTA / atributo / "base_inicial.npz"
    if base.exists():
        with np.load(base, allow_pickle=False) as d:
            vetores += list(d["vetores"].astype(np.float32))
            rotulos += [str(r) for r in d["rotulos"]]
            origens += [f"base:{o}" for o in d["origens"]]
    c = banco_direto.conectar()
    linhas = []
    # Banco ainda sem a coluna `tipo` (servidor não reiniciado desde a
    # migração): os rótulos de cor e modelo continuam valendo.
    # Marca/modelo treina sobre o vetor do CLIP grande (modelo_fino); tipo e
    # cor, sobre o do pequeno, que é o que a identificação ao vivo calcula.
    col_vetor = "p.vetor_modelo" if atributo == "modelo_carro" else "p.vetor"
    for col_tipo in ("r.tipo", "'' as tipo"):
        try:
            linhas = c.execute(f"""select p.id, {col_vetor}, r.cor, r.marca, r.modelo, {col_tipo}
                                   from rotulos_veiculo r join passagens_veiculo p on p.id = r.passagem_id
                                   where {col_vetor} is not null and {col_vetor} not in ('[]', 'null')""").fetchall()
            break
        except Exception:  # coluna `tipo` ausente: sem migração ainda
            continue
    for pid, vet, cor, marca, modelo, tipo in linhas:
        if atributo == "tipo":
            rot = tipo or ""
        elif atributo == "cor":
            rot = cor
        else:
            # Só carros: moto não tem marca/modelo por enquanto.
            rot = f"{marca}|{modelo}" if marca and modelo and (marca, modelo) not in MOTOS else ""
        if rot:
            vetores.append(np.asarray(json.loads(vet), np.float32))
            rotulos.append(rot)
            origens.append(f"passagem:{pid}")
    X = np.stack(vetores) if vetores else np.zeros((0, 512), np.float32)
    if len(X):
        X /= np.linalg.norm(X, axis=1, keepdims=True)
    return X, rotulos, origens


def treinar(X: np.ndarray, y: np.ndarray, k: int, lam: float = 0.01):
    import torch
    W = torch.zeros(X.shape[1], k, requires_grad=True)
    b = torch.zeros(k, requires_grad=True)
    opt = torch.optim.LBFGS([W, b], max_iter=200)
    Xt, yt = torch.tensor(X) * ESCALA, torch.tensor(y)

    def passo():
        opt.zero_grad()
        perda = torch.nn.functional.cross_entropy(Xt @ W + b, yt) + lam * (W ** 2).sum()
        perda.backward()
        return perda
    opt.step(passo)
    return W.detach().numpy(), b.detach().numpy()


def prever(W, b, X):
    lg = (X * ESCALA) @ W + b
    p = np.exp(lg - lg.max(1, keepdims=True))
    return p / p.sum(1, keepdims=True)


def validar(X, y, k) -> np.ndarray:
    n = len(y)
    # Deixa-um-de-fora até 150 exemplos; acima disso, 10 partes (300 treinos
    # em CPU levavam mais de meia hora, com o mesmo resultado estatístico).
    partes = np.arange(n) if n <= 150 else np.arange(n) % 10
    probs = np.zeros((n, k))
    for parte in np.unique(partes):
        teste = partes == parte
        W, b = treinar(X[~teste], y[~teste], k)
        probs[teste] = prever(W, b, X[teste])
    return probs


def pisos(probs, y, classes, alvo) -> tuple[np.ndarray, dict]:
    pred, conf = probs.argmax(1), probs.max(1)
    out = np.full(len(classes), np.inf)
    rel = {}
    for k, nome in enumerate(classes):
        sel = pred == k
        escolhido = None
        for t in sorted(set(conf[sel].tolist())):
            acima = sel & (conf >= t)
            acertos = int((y[acima] == k).sum())
            if acima.sum() and acertos >= 3 and acertos / acima.sum() >= alvo:
                # Arredondado para baixo: arredondar 0,99996 para 1,000 deixava
                # a própria previsão abaixo do piso, e a classe nunca saía.
                escolhido = float(np.floor(t * 1000) / 1000)
                break
        total = int((y == k).sum())
        if escolhido is None:
            rel[nome] = {"exemplos": total, "liberada": False,
                         "motivo": f"não alcança {alvo:.0%} de precisão na validação"}
            continue
        out[k] = escolhido
        acima = sel & (conf >= escolhido)
        rel[nome] = {"exemplos": total, "liberada": True, "piso": round(escolhido, 3),
                     "precisao": round(float((y[acima] == k).mean()), 3),
                     "cobertura": round(float((y[acima] == k).sum() / max(1, total)), 3)}
    return out, rel


def rodar(atributo: str, simular: bool) -> dict:
    X, rot, orig = exemplos(atributo)
    cont = Counter(rot)
    # Toda classe com exemplo entra no modelo, mesmo abaixo do mínimo — só não
    # é liberada. Deixá-la de fora empurrava o veículo dela para a classe
    # liberada mais parecida: em 09/11/2026 uma moto azul saiu "vermelho" a
    # 69% porque azul (3 exemplos) não existia no modelo.
    classes = sorted(c for c, n in cont.items() if n >= 2)
    poucos = {c: n for c, n in cont.items() if n < settings.VEICULO_EXEMPLOS_MIN}
    suficientes = [c for c in classes if cont[c] >= settings.VEICULO_EXEMPLOS_MIN]
    print(f"\n[{atributo}] {len(rot)} exemplos; classes com o mínimo de "
          f"{settings.VEICULO_EXEMPLOS_MIN}: {len(suficientes)}; abaixo do mínimo (travadas): "
          f"{poucos or 'nenhuma'}")
    if len(suficientes) < 2:
        print("  Menos de duas classes com exemplos suficientes: nada a treinar ainda.")
        return {"treinado": False, "exemplos": len(rot), "por_classe": dict(cont)}
    sel = np.array([r in classes for r in rot])
    X, y = X[sel], np.array([classes.index(r) for r, s in zip(rot, sel) if s])
    probs = validar(X, y, len(classes))
    p, rel = pisos(probs, y, classes, settings.VEICULO_PRECISAO_ALVO)
    for k, c in enumerate(classes):
        if cont[c] < settings.VEICULO_EXEMPLOS_MIN:
            p[k] = np.inf
            rel[c] = {"exemplos": cont[c], "liberada": False,
                      "motivo": f"menos de {settings.VEICULO_EXEMPLOS_MIN} exemplos"}
    pred, conf = probs.argmax(1), probs.max(1)
    afirmados = np.array([np.isfinite(p[k]) and c >= p[k] for k, c in zip(pred, conf)])
    precisao = float((pred[afirmados] == y[afirmados]).mean()) if afirmados.any() else None
    cobertura = float(afirmados.mean())
    print(f"  Validação: sem piso, acerto {float((pred == y).mean()):.1%}; com os pisos, "
          f"precisão {precisao if precisao is None else f'{precisao:.1%}'} "
          f"respondendo {cobertura:.1%} dos veículos ({int(afirmados.sum())}/{len(y)})")
    for nome, r in rel.items():
        if r["liberada"]:
            print(f"    {nome:28s} liberada  piso {r['piso']:.2f}  precisão {r['precisao']:.0%}  "
                  f"cobre {r['cobertura']:.0%} de {r['exemplos']}")
        else:
            print(f"    {nome:28s} TRAVADA   ({r['exemplos']} exemplos; {r['motivo']})")
    relatorio = {"atributo": atributo, "treinado_em": datetime.now().isoformat(timespec="seconds"),
                 "exemplos": int(len(y)), "classes": rel, "precisao_validacao": precisao,
                 "cobertura_validacao": round(cobertura, 3),
                 "alvo": settings.VEICULO_PRECISAO_ALVO, "abaixo_do_minimo": poucos}
    if simular:
        print("  (--simular: sonda não gravada)")
        return relatorio
    W, b = treinar(X, y, len(classes))
    destino = PASTA / atributo
    destino.mkdir(parents=True, exist_ok=True)
    tmp = destino / "sonda.tmp.npz"
    np.savez(tmp, W=W.astype(np.float32), b=b.astype(np.float32), escala=np.float32(ESCALA),
             classes=np.array(classes), pisos=p.astype(np.float64))
    tmp.replace(destino / "sonda.npz")
    (destino / "relatorio.json").write_text(json.dumps(relatorio, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    print(f"  Sonda gravada em {destino / 'sonda.npz'}")
    return relatorio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--so", choices=["tipo", "cor", "modelo_carro"])
    ap.add_argument("--simular", action="store_true")
    a = ap.parse_args()
    for atributo in ([a.so] if a.so else ["tipo", "cor", "modelo_carro"]):
        rodar(atributo, a.simular)


if __name__ == "__main__":
    main()
