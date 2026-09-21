"""Testa a conexão RTSP das câmeras cadastradas.

    python -m scripts.testar_camera              todas as câmeras do banco
    python -m scripts.testar_camera CAM-001      uma câmera
    python -m scripts.testar_camera rtsp://…     uma URL avulsa

Usa o mesmo leitor que o servidor usa para alimentar o mosaico, então o que
funcionar aqui funciona lá — e o que falhar aqui falharia lá do mesmo jeito,
só que sem mensagem de erro visível.

A senha nunca é impressa.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time

from sqlalchemy import select

from app.database import db_context, engine
from app.models import Camera
from app.services import video

TEMPO_LIMITE_S = 15.0


def mascarar(url: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def testar(codigo: str, url: str, resolucao_ficha: str = "") -> bool:
    print(f"\n{codigo}")
    print(f"  url        {mascarar(url)}")

    inicio = time.monotonic()
    try:
        leitor = video.obter(codigo, url)
    except video.LeitorIndisponivel as exc:
        print(f"  RESULTADO  {exc}")
        return False

    leitor.pronto.wait(TEMPO_LIMITE_S)
    decorrido = time.monotonic() - inicio

    if leitor.erro:
        print(f"  RESULTADO  FALHOU — {leitor.erro}  ({decorrido:.1f}s)")
        leitor.parar()
        return False
    if leitor.quadro is None:
        print(f"  RESULTADO  FALHOU — nenhum quadro em {TEMPO_LIMITE_S:.0f}s")
        leitor.parar()
        return False

    import cv2
    import numpy as np

    imagem = cv2.imdecode(np.frombuffer(leitor.quadro, np.uint8), cv2.IMREAD_COLOR)
    altura, largura = imagem.shape[:2]
    print(f"  primeiro quadro em {decorrido:.1f}s")
    print(f"  entregue   {largura}x{altura}  ({len(leitor.quadro)/1024:.0f} KiB em JPEG)")
    if resolucao_ficha:
        print(f"  na ficha   {resolucao_ficha}  (o leitor reduz para {video.LARGURA_SAIDA}px de largura)")
    print("  RESULTADO  OK")
    leitor.parar()
    return True


async def _do_banco(filtro: str | None) -> list[tuple[str, str, str]]:
    async with db_context() as db:
        consulta = select(Camera).order_by(Camera.codigo)
        if filtro:
            consulta = consulta.where(Camera.codigo == filtro)
        cams = (await db.execute(consulta)).scalars().unique().all()
    return [(c.codigo, c.rtsp_url, c.resolucao) for c in cams if c.rtsp_url]


async def _principal() -> None:
    argumento = sys.argv[1] if len(sys.argv) > 1 else None

    if argumento and argumento.startswith("rtsp://"):
        alvos = [("URL avulsa", argumento, "")]
    else:
        alvos = await _do_banco(argumento)
        await engine.dispose()
        if not alvos:
            alvo = f" com o código {argumento}" if argumento else ""
            print(f"Nenhuma câmera com RTSP cadastrado{alvo}.")
            return

    print(f"Testando {len(alvos)} câmera(s). Limite de {TEMPO_LIMITE_S:.0f}s cada.")
    ok = sum(testar(codigo, url, res) for codigo, url, res in alvos)
    print(f"\n{ok} de {len(alvos)} responderam.")
    sys.exit(0 if ok == len(alvos) else 1)


if __name__ == "__main__":
    asyncio.run(_principal())
