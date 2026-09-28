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
log = logging.getLogger("seguranca.worker.run")


# Renova com folga: o relogio do worker e o da API nao sao o mesmo, e chegar
# raspando no vencimento e o mesmo que nao renovar.
MARGEM_RENOVACAO_S = 300


class Sessao:
    """Mantem o `Authorization` do cliente valido enquanto o worker roda.

    O access token vale uma hora. Sem renovacao, o worker seguia lendo as
    cameras e rodando os modelos normalmente, mas cada envio voltava 401 e a
    deteccao era descartada em silencio - de fora parecia que o sistema tinha
    parado de detectar, sem nada ter caido.
    """

    def __init__(self, cliente: httpx.AsyncClient) -> None:
        self.cliente = cliente
        self.refresh_token = ""
        self.expira_em_s = 0

    def aplicar(self, dados: dict) -> None:
        self.cliente.headers["Authorization"] = f"Bearer {dados['access_token']}"
        self.refresh_token = dados.get("refresh_token", "") or self.refresh_token
        self.expira_em_s = int(dados.get("expires_in", 3600))

    async def entrar(self, identificador: str, senha: str) -> None:
        r = await self.cliente.post(f"{API}/auth/login",
                                    json={"identificador": identificador, "senha": senha},
                                    timeout=15.0)
        r.raise_for_status()
        self.aplicar(r.json())
        log.info("Autenticado como %s - o token passa a se renovar sozinho", identificador)

    async def renovar(self) -> bool:
        if not self.refresh_token:
            return False
        try:
            r = await self.cliente.post(f"{API}/auth/refresh",
                                        json={"refresh_token": self.refresh_token},
                                        timeout=15.0)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("Falha ao renovar o token: %s", exc)
            return False
        self.aplicar(r.json())
        log.info("Token renovado (vale %d min)", self.expira_em_s // 60)
        return True

    async def manter(self, parar: asyncio.Event) -> None:
        """Renova pouco antes de vencer, ate mandarem parar."""
        while not parar.is_set():
            espera = max(60, self.expira_em_s - MARGEM_RENOVACAO_S)
            try:
                await asyncio.wait_for(parar.wait(), timeout=espera)
                return                      # pediram para parar
            except asyncio.TimeoutError:
                pass
            if not await self.renovar():
                # Insistir a cada minuto e melhor que desistir: a API pode
                # estar reiniciando.
                self.expira_em_s = 120


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


async def principal(codigos: List[str] | None, token: str, intervalo: int,
                    usuario: str = "", senha: str = "", refresh: str = "") -> None:
    cabecalhos = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(headers=cabecalhos) as cliente:
        sessao = Sessao(cliente)
        if usuario and senha:
            try:
                await sessao.entrar(usuario, senha)
            except httpx.HTTPError as exc:
                log.error("Não foi possível autenticar: %s", exc)
                sys.exit(1)
        elif refresh:
            sessao.refresh_token = refresh
            if not await sessao.renovar():
                log.error("Refresh token inválido ou expirado.")
                sys.exit(1)
        elif token:
            log.warning("Token fixo: vence em até 1h e a partir daí todo envio é "
                        "recusado com 401, sem o worker parar. Prefira --usuario/--senha.")

        try:
            cameras = await carregar_cameras(cliente, codigos)
        except httpx.HTTPStatusError as exc:
            log.error("Não foi possível carregar câmeras (%s). Verifique as credenciais.",
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
        if sessao.refresh_token:
            tarefas.append(asyncio.create_task(sessao.manter(parar)))

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
    p = argparse.ArgumentParser(description="Worker de inferência do SEGURANÇA")
    grupo = p.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--camera", action="append", help="Código da câmera (repetível)")
    grupo.add_argument("--todas", action="store_true", help="Todas as câmeras on-line")
    p.add_argument("--token", default=os.getenv("seguranca_SERVICE_TOKEN", ""),
                   help="Access token de serviço (ou seguranca_SERVICE_TOKEN). Vence em 1h.")
    p.add_argument("--usuario", default=os.getenv("seguranca_SERVICE_USER", ""),
                   help="E-mail ou matrícula do usuário de serviço (ou seguranca_SERVICE_USER)")
    p.add_argument("--senha", default=os.getenv("seguranca_SERVICE_SENHA", ""),
                   help="Senha do usuário de serviço (ou seguranca_SERVICE_SENHA)")
    p.add_argument("--refresh-token", dest="refresh", default=os.getenv("seguranca_REFRESH_TOKEN", ""),
                   help="Refresh token (vale 7 dias), alternativa a usuário e senha")
    p.add_argument("--intervalo", type=int, default=5,
                   help="Analisar 1 a cada N quadros (padrão: 5)")
    args = p.parse_args()

    if not (args.token or args.refresh or (args.usuario and args.senha)):
        p.error("informe --usuario e --senha, ou --refresh-token, ou --token")

    asyncio.run(principal(None if args.todas else args.camera, args.token,
                          args.intervalo, args.usuario, args.senha, args.refresh))


if __name__ == "__main__":
    cli()

