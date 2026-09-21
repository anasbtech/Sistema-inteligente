"""Lançador do worker de inferência.

    python -m worker.run --camera CAM-001
    python -m worker.run --todas --token <ACCESS_TOKEN>

O worker busca a configuração na API (limiares, zonas, analíticos ativos), de
modo que mudar a configuração pela interface muda o comportamento do worker no
próximo reinício — sem editar código.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import List

import httpx

from worker.pipeline import API, ConfigCamera, PipelineCamera

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
)
log = logging.getLogger("vigia.worker.run")


async def carregar_cameras(cliente: httpx.AsyncClient, codigos: List[str] | None) -> List[ConfigCamera]:
    if codigos:
        saida = []
        for c in codigos:
            r = await cliente.get(f"{API}/cameras/{c}")
            r.raise_for_status()
            saida.append(ConfigCamera.de_api(r.json()))
        return saida

    r = await cliente.get(f"{API}/cameras", params={"status": "ONLINE"})
    r.raise_for_status()
    return [ConfigCamera.de_api(d) for d in r.json()]


async def principal(codigos: List[str] | None, token: str, intervalo: int) -> None:
    cabecalhos = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=cabecalhos) as cliente:
        try:
            cameras = await carregar_cameras(cliente, codigos)
        except httpx.HTTPStatusError as exc:
            log.error("Não foi possível carregar câmeras (%s). Verifique o token de serviço.",
                      exc.response.status_code)
            sys.exit(1)

        if not cameras:
            log.error("Nenhuma câmera on-line para processar.")
            sys.exit(1)

        log.info("Processando %d câmera(s): %s", len(cameras),
                 ", ".join(c.codigo for c in cameras))

        pipelines = [PipelineCamera(c, cliente) for c in cameras]
        tarefas = [asyncio.create_task(p.rodar(intervalo)) for p in pipelines]

        parar = asyncio.Event()

        def _encerrar(*_):
            log.info("Sinal recebido — encerrando…")
            parar.set()

        loop = asyncio.get_running_loop()
        for sinal in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sinal, _encerrar)
            except NotImplementedError:  # Windows
                signal.signal(sinal, _encerrar)

        await parar.wait()
        for t in tarefas:
            t.cancel()
        await asyncio.gather(*tarefas, return_exceptions=True)
        for p in pipelines:
            p.fechar()
        log.info("Worker encerrado.")


def cli() -> None:
    p = argparse.ArgumentParser(description="Worker de inferência do VIGIA")
    grupo = p.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--camera", action="append", help="Código da câmera (repetível)")
    grupo.add_argument("--todas", action="store_true", help="Todas as câmeras on-line")
    p.add_argument("--token", default=os.getenv("VIGIA_SERVICE_TOKEN", ""),
                   help="Access token de serviço (ou VIGIA_SERVICE_TOKEN)")
    p.add_argument("--intervalo", type=int, default=5,
                   help="Analisar 1 a cada N quadros (padrão: 5)")
    args = p.parse_args()

    asyncio.run(principal(None if args.todas else args.camera, args.token, args.intervalo))


if __name__ == "__main__":
    cli()
