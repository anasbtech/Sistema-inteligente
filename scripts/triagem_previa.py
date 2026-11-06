"""Etapa 3: página de prévia dos grupos, para aprovação antes de gravar.

Gera data/cadastro_triagem/previa.html (local, fora do git: são rostos reais).
Cada grupo mostra o nome fictício, o sexo sugerido (com o aviso quando não é
claro), as câmeras e até 12 capturas, da melhor para a pior.

    py -3.13 -m scripts.triagem_previa
"""
from __future__ import annotations

import base64
import html
import json
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import evidencias  # noqa: E402

PASTA = RAIZ / "data" / "cadastro_triagem"


def miniatura(jpeg: bytes) -> str:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    img = cv2.resize(img, (84, int(84 * img.shape[0] / img.shape[1])), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


def main():
    grupos = json.loads((PASTA / "grupos.json").read_text(encoding="utf-8"))
    c = sqlite3.connect(settings.DATABASE_URL.split(":///")[-1])
    chave = dict(c.execute("select codigo, snapshot_key from deteccoes where tipo='FACIAL' and pessoa_id is null"))
    blocos = []
    for g in grupos:
        fotos = "".join(f'<img src="{miniatura(evidencias.ler(chave[cod]))}" title="{cod}">'
                        for cod in g["codigos"][:12] if cod in chave and evidencias.ler(chave[cod]))
        sexo = "Masculino" if g["sexo"] == "M" else "Feminino"
        aviso = '<span class="av">confirmar sexo</span>' if g["confirmar_sexo"] else ""
        par = (f'<span class="av2">parecido com o grupo {", ".join(str(grupos[j]["grupo"]) for j in g["parecidos_com"])}</span>'
               if g["parecidos_com"] else "")
        blocos.append(f'''<section><header><b>#{g["grupo"]} {html.escape(g["nome"])}</b>
<span>{sexo} ({g["sexo_conf"]:.0%})</span>{aviso}{par}
<span class="mut">{g["capturas"]} capturas · {", ".join(g["cameras"])} · coesão {g["coesao"]:.2f}</span></header>
<div class="fotos">{fotos}</div></section>''')
    pagina = f'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><title>Prévia do cadastro da triagem</title>
<style>body{{background:#0B1118;color:#E6EEF6;font:14px system-ui;margin:24px}}
section{{border:1px solid #25394B;border-radius:8px;padding:12px 14px;margin:0 0 12px}}
header{{display:flex;gap:12px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}}
.mut{{color:#7F95A9;font-size:12px}}.av{{background:#F0A02A;color:#111;border-radius:4px;padding:1px 6px;font-size:12px}}
.av2{{background:#8C6FE0;color:#fff;border-radius:4px;padding:1px 6px;font-size:12px}}
.fotos img{{height:96px;margin:0 4px 4px 0;border-radius:4px}}</style>
<h1>Prévia: {len(grupos)} pessoas novas, {sum(g["capturas"] for g in grupos)} capturas</h1>
{"".join(blocos)}</html>'''
    (PASTA / "previa.html").write_text(pagina, encoding="utf-8")
    print(PASTA / "previa.html")


if __name__ == "__main__":
    main()
