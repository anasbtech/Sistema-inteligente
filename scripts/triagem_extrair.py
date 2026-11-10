"""Etapa 1 do cadastro da triagem: rosto, vetor e sexo estimado de cada captura.

Lê as capturas faciais sem pessoa (as da triagem), reanalisa o recorte gravado
e guarda, por captura: o vetor do rosto (512-D), a qualidade, a confiança do
detector e o sexo estimado com a sua margem. Salva aos poucos em
data/cadastro_triagem/vetores.npz, e retoma de onde parou.

    py -3.13 -m scripts.triagem_extrair [--limite N]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import evidencias  # noqa: E402

PASTA = RAIZ / "data" / "cadastro_triagem"
ARQ = PASTA / "vetores.npz"


def modelo():
    from insightface.app import FaceAnalysis
    # Dois detectores pequenos: o recorte já é o rosto (73-115 px nas capturas
    # antigas), e o de 640 px perdia a maioria deles. Medido em 60 capturas:
    # 256 px sem moldura acha 41; o de 320 px com moldura acha alguns dos que
    # sobram. Limiar 0,3: o que for duvidoso cai depois, no agrupamento.
    apps = []
    for tam in (256, 320):
        app = FaceAnalysis(name=settings.FACE_MODEL, providers=["CPUExecutionProvider"],
                           allowed_modules=["detection", "recognition", "genderage"])
        app.prepare(ctx_id=-1, det_size=(tam, tam))
        app.det_model.det_thresh = 0.3
        apps.append(app)
    return apps


def analisar(apps, jpeg: bytes):
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    a, l = img.shape[:2]
    faces = apps[0].get(img)
    if not faces:
        img = cv2.copyMakeBorder(img, int(a * .4), int(a * .4), int(l * .4), int(l * .4),
                                 cv2.BORDER_REPLICATE)
        faces = apps[1].get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: x.det_score * (x.bbox[3] - x.bbox[1]))
    altura = float(f.bbox[3] - f.bbox[1])
    # genderage: 1 = masculino, 0 = feminino.
    return (np.asarray(f.normed_embedding, np.float32), float(f.det_score), altura,
            int(f.gender), float(getattr(f, "age", 0) or 0))


def trocar(tmp: Path, destino: Path) -> None:
    """Substitui o arquivo, insistindo se outro processo o tiver aberto naquele
    instante (o agrupamento lendo, o antivírus olhando). No Windows a troca
    de arquivo aberto falha com "Acesso negado"."""
    for tentativa in range(30):
        try:
            tmp.replace(destino)
            return
        except PermissionError:
            time.sleep(2)
    tmp.replace(destino)


def manter_acordado() -> None:
    """Pede ao Windows para não suspender por inatividade enquanto este
    processo vive. Em 06/11/2026 o notebook suspendeu 15 min depois do último
    uso e a análise ficou parada o fim de semana inteiro. Tampa fechada e
    bateria acabando ainda suspendem."""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception:
        pass


def _trava():
    """Uma análise por vez: duas gravando vetores.npz se atropelariam."""
    import msvcrt
    PASTA.mkdir(parents=True, exist_ok=True)
    f = open(PASTA / "extrair.lock", "w")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("Outra análise já está rodando; esta não começa.", flush=True)
        sys.exit(0)
    return f


def main():
    trava = _trava()  # noqa: F841 — mantém a trava até o fim
    manter_acordado()
    p = argparse.ArgumentParser()
    p.add_argument("--limite", type=int, default=0)
    p.add_argument("--prioridade", choices=["baixa", "normal"], default="baixa",
                   help="normal: disputa a CPU com o sistema ao vivo (mais rápido)")
    args = p.parse_args()
    PASTA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.DATABASE_URL.split(":///")[-1])
    linhas = c.execute("""select codigo, snapshot_key, camera_id, ocorrido_em, qualidade_face
                          from deteccoes where tipo='FACIAL' and pessoa_id is null
                            and snapshot_key != '' and coalesce(veredito,'') != 'Falso positivo'
                          order by (coalesce(qualidade_face, 0) >= 0.60) desc,
                                   ocorrido_em desc""").fetchall()
    # Boas primeiro: qualidade >= 0,60 rende rosto aproveitável em 58% das
    # capturas, contra 28% abaixo disso (medido em 09/11/2026). É o que faz os
    # grupos aparecerem cedo; dentro de cada faixa, as recentes primeiro.
    feito = {}
    if ARQ.exists():
        # Fechado logo após ler: no Windows, arquivo aberto não pode ser
        # substituído, e o primeiro salvamento falhava com "Acesso negado"
        # (06/11/2026, a análise parou nas primeiras 500).
        with np.load(ARQ, allow_pickle=True) as d:
            feito = {k: True for k in d["codigos"]}
    pend = [l for l in linhas if l[0] not in feito]
    if args.limite:
        pend = pend[:args.limite]
    print(f"{len(linhas)} capturas na triagem; {len(feito)} já analisadas; {len(pend)} a fazer",
          flush=True)
    app = modelo()
    # Prioridade abaixo do normal: o sistema ao vivo vem primeiro — a não ser
    # que se peça `--prioridade normal`.
    try:
        if args.prioridade == "normal":
            raise RuntimeError("prioridade normal pedida")
        import ctypes
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)   # abaixo do normal (IDLE ficava parado)
    except Exception:
        pass
    novos = {"codigos": [], "vet": [], "det": [], "alt": [], "sexo": [], "idade": [],
             "camera": [], "quando": [], "qual": []}
    t0 = time.time()

    def salvar():
        antigos = None
        if ARQ.exists():
            with np.load(ARQ, allow_pickle=True) as d:
                antigos = {k: d[k] for k in d.files}
        lote = {k: np.array(v) for k, v in novos.items()}
        if antigos is not None and len(lote["codigos"]):
            lote = {k: np.concatenate([antigos[k], lote[k]]) for k in lote}
        elif antigos is not None:
            lote = antigos
        tmp = PASTA / "vetores.tmp.npz"
        np.savez(tmp, **lote)
        trocar(tmp, ARQ)
        for v in novos.values():
            v.clear()

    sem = 0
    for n, (cod, chave, cam, quando, qual) in enumerate(pend, 1):
        jpeg = evidencias.ler(chave)
        r = analisar(app, jpeg) if jpeg else None
        if r is None:
            sem += 1
            r = (np.zeros(512, np.float32), 0.0, 0.0, -1, 0.0)   # sem rosto: marcado
        vet, det, alt, sexo, idade = r
        for k, v in zip(novos, (cod, vet, det, alt, sexo, idade, cam, str(quando), qual or 0.0)):
            novos[k].append(v)
        if n % 100 == 0 or n == len(pend):
            salvar()
            ritmo = (time.time() - t0) / n
            print(f"  {n}/{len(pend)}  {ritmo:.2f}s cada  sem rosto {sem}  "
                  f"faltam ~{ritmo * (len(pend) - n) / 60:.0f} min", flush=True)
    if any(novos.values()):
        salvar()


if __name__ == "__main__":
    main()
