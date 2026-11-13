"""Ocupação contínua das câmeras, para o mapa de calor histórico.

A detecção grava uma linha por *passagem*: diz que alguém apareceu, e onde,
mas não por quanto tempo ficou nem por onde andou. Um mapa de calor feito só
disso marca a porta e esquece a mesa onde a pessoa passou a tarde.

Aqui cada volta da análise que publica as caixas de uma câmera
(`inferencia._publicar_caixas`, e o módulo veicular para os veículos) soma:

* ao pé de cada pessoa, os segundos desde a volta anterior daquela câmera —
  é o tempo de presença naquele ponto do chão;
* o pico de pessoas ao mesmo tempo e o maior grupo de pessoas juntas (dentro
  do raio que a análise de aglomeração usa), com o instante;
* os `track_id` vistos, para contar pessoas distintas na hora sem contar duas
  vezes quem ficou parado várias voltas.

Tudo em memória, por câmera e hora cheia, e gravado no banco a cada minuto
(`OcupacaoHora`), somando ao que a hora já tinha — um reinício no meio da
hora perde no máximo o último minuto. Nenhuma imagem e nenhuma identidade:
segundos por célula e contagens.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

log = logging.getLogger("seguranca.ocupacao")

COLUNAS, LINHAS = 64, 36          # 16:9; o histórico é suavizado na tela
INTERVALO_MAX_S = 30.0            # volta mais espaçada que isto não conta o vão inteiro
GRUPO_MIN = 3                     # a partir de quantas pessoas juntas o lugar entra no mapa de aglomeração
GRAVAR_A_CADA_S = 60.0
VEICULOS = {"carro", "motocicleta", "caminhao", "onibus", "bicicleta", "picape", "van",
            "kombi", "caminhao_pequeno", "caminhao_grande", "veiculo"}


def _hora_cheia(t: datetime) -> datetime:
    return t.replace(minute=0, second=0, microsecond=0)


def _celula(bbox) -> Optional[int]:
    """Célula do pé da caixa: o ponto em que a pessoa toca o chão."""
    try:
        x, y, w, h = (float(v) for v in bbox[:4])
    except (TypeError, ValueError):
        return None
    px, py = x + w / 2, y + h * 0.97
    if not (0.0 <= px <= 1.0 and 0.0 <= py <= 1.0):
        return None
    return min(LINHAS - 1, int(py * LINHAS)) * COLUNAS + min(COLUNAS - 1, int(px * COLUNAS))


def maior_grupo(pontos: list[tuple[float, float]], raio: float) -> tuple[int, Optional[tuple[float, float]]]:
    """Maior grupo de pontos que cabem num círculo de `raio` em torno de um
    deles (o mesmo critério da ação de aglomeração), e o centro do grupo.

    `raio` é fração da largura; a altura é corrigida pela proporção 16:9 para
    o círculo ser redondo no chão da imagem, não esticado."""
    melhor, centro = 0, None
    for ax, ay in pontos:
        perto = [(bx, by) for bx, by in pontos if math.hypot(bx - ax, (by - ay) * 9 / 16) <= raio]
        if len(perto) > melhor:
            melhor = len(perto)
            centro = (sum(p[0] for p in perto) / len(perto), sum(p[1] for p in perto) / len(perto))
    return melhor, centro


@dataclass
class _Hora:
    observado_s: float = 0.0
    presenca_s: float = 0.0
    veiculos_s: float = 0.0
    pico: int = 0
    pico_em: Optional[datetime] = None
    maior_grupo: int = 0
    maior_grupo_em: Optional[datetime] = None
    grupo_s: float = 0.0
    trilhas: set = field(default_factory=set)
    pessoas: dict = field(default_factory=dict)
    veiculos: dict = field(default_factory=dict)
    grupos: dict = field(default_factory=dict)


class Acumulador:
    def __init__(self) -> None:
        self._horas: dict[tuple[str, datetime], _Hora] = {}
        self._ultima: dict[tuple[str, str], float] = {}
        self._task: Optional[asyncio.Task] = None

    # ---- coleta (síncrona e barata: chamada a cada volta da análise) ----
    def registrar(self, codigo: str, caixas: list, fonte: str = "geral",
                  agora: Optional[float] = None, quando: Optional[datetime] = None) -> None:
        """Soma uma volta da análise de `codigo`.

        `fonte` separa o laço geral (pessoas e veículos) do módulo veicular
        (só veículos): cada um tem o seu ritmo, e o intervalo de um não pode
        pesar as caixas do outro."""
        import time
        agora = time.monotonic() if agora is None else agora
        quando = quando or datetime.now(timezone.utc)
        chave = (codigo, fonte)
        anterior = self._ultima.get(chave)
        self._ultima[chave] = agora
        if anterior is None:
            return                    # a primeira volta só marca o relógio
        dt = min(INTERVALO_MAX_S, max(0.0, agora - anterior))
        if dt <= 0:
            return
        h = self._horas.setdefault((codigo, _hora_cheia(quando)), _Hora())
        pontos = []
        n_pessoas = 0
        for c in caixas or ():
            bbox, classe = c.get("bbox"), c.get("classe") or ""
            if not bbox:
                continue
            cel = _celula(bbox)
            if classe == "pessoa" and fonte == "geral":
                n_pessoas += 1
                if cel is not None:
                    h.pessoas[cel] = h.pessoas.get(cel, 0.0) + dt
                    x, y, w, hh = (float(v) for v in bbox[:4])
                    pontos.append((x + w / 2, y + hh * 0.97))
                if c.get("track_id") is not None:
                    h.trilhas.add(int(c["track_id"]))
            elif classe in VEICULOS or fonte == "veic":
                h.veiculos_s += dt
                if cel is not None:
                    h.veiculos[cel] = h.veiculos.get(cel, 0.0) + dt
        if fonte != "geral":
            return
        h.observado_s += dt
        h.presenca_s += dt * n_pessoas
        if n_pessoas > h.pico:
            h.pico, h.pico_em = n_pessoas, quando
        if len(pontos) >= GRUPO_MIN:
            n, centro = maior_grupo(pontos, settings.ACAO_AGLOMERACAO_RAIO)
            if n > h.maior_grupo:
                h.maior_grupo, h.maior_grupo_em = n, quando
            if n >= GRUPO_MIN and centro is not None:
                cel = (min(LINHAS - 1, int(centro[1] * LINHAS)) * COLUNAS
                       + min(COLUNAS - 1, int(centro[0] * COLUNAS)))
                h.grupos[cel] = h.grupos.get(cel, 0.0) + dt * n
                h.grupo_s += dt

    def pendentes(self) -> int:
        return len(self._horas)

    # ---- gravação ----
    async def gravar(self) -> int:
        """Soma o que está em memória às linhas do banco e esvazia a memória."""
        if not self._horas:
            return 0
        lote, self._horas = self._horas, {}
        from sqlalchemy import select

        from app.database import db_context
        from app.models import Camera, OcupacaoHora

        def somar(a: dict, b: dict) -> dict:
            out = {str(k): float(v) for k, v in (a or {}).items()}
            for k, v in b.items():
                out[str(k)] = round(out.get(str(k), 0.0) + v, 2)
            return out

        try:
            async with db_context() as db:
                ids = dict((await db.execute(select(Camera.codigo, Camera.id))).all())
                for (codigo, inicio), h in lote.items():
                    cam_id = ids.get(codigo)
                    if cam_id is None:
                        continue
                    linha = (await db.execute(select(OcupacaoHora).where(
                        OcupacaoHora.camera_id == cam_id, OcupacaoHora.inicio == inicio))).scalar_one_or_none()
                    if linha is None:
                        linha = OcupacaoHora(camera_id=cam_id, inicio=inicio, observado_s=0.0, presenca_s=0.0,
                                             veiculos_s=0.0, pico=0, maior_grupo=0, grupo_s=0.0, trilhas=[],
                                             grade_pessoas={}, grade_veiculos={}, grade_grupos={})
                        db.add(linha)
                    linha.observado_s = round((linha.observado_s or 0) + h.observado_s, 2)
                    linha.presenca_s = round((linha.presenca_s or 0) + h.presenca_s, 2)
                    linha.veiculos_s = round((linha.veiculos_s or 0) + h.veiculos_s, 2)
                    if h.pico > (linha.pico or 0):
                        linha.pico, linha.pico_em = h.pico, h.pico_em
                    if h.maior_grupo > (linha.maior_grupo or 0):
                        linha.maior_grupo, linha.maior_grupo_em = h.maior_grupo, h.maior_grupo_em
                    linha.grupo_s = round((linha.grupo_s or 0) + h.grupo_s, 2)
                    linha.trilhas = sorted(set(linha.trilhas or []) | h.trilhas)
                    linha.grade_pessoas = somar(linha.grade_pessoas, h.pessoas)
                    linha.grade_veiculos = somar(linha.grade_veiculos, h.veiculos)
                    linha.grade_grupos = somar(linha.grade_grupos, h.grupos)
                    linha.atualizado_em = datetime.now(timezone.utc)
        except Exception as exc:  # noqa: BLE001
            # Devolve à memória: o minuto seguinte tenta de novo com tudo somado.
            log.warning("Ocupação não gravada (%s); nova tentativa no próximo minuto", exc)
            for chave, h in lote.items():
                atual = self._horas.get(chave)
                if atual is None:
                    self._horas[chave] = h
                else:
                    _juntar(atual, h)
            return 0
        return len(lote)

    async def _laco(self) -> None:
        while True:
            await asyncio.sleep(GRAVAR_A_CADA_S)
            try:
                await self.gravar()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha no laço da ocupação: %s", exc)

    async def iniciar(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._laco())

    async def parar(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        await self.gravar()           # o último minuto não se perde no desligamento


def _juntar(a: _Hora, b: _Hora) -> None:
    a.observado_s += b.observado_s; a.presenca_s += b.presenca_s; a.veiculos_s += b.veiculos_s
    if b.pico > a.pico:
        a.pico, a.pico_em = b.pico, b.pico_em
    if b.maior_grupo > a.maior_grupo:
        a.maior_grupo, a.maior_grupo_em = b.maior_grupo, b.maior_grupo_em
    a.grupo_s += b.grupo_s
    a.trilhas |= b.trilhas
    for dst, src in ((a.pessoas, b.pessoas), (a.veiculos, b.veiculos), (a.grupos, b.grupos)):
        for k, v in src.items():
            dst[k] = dst.get(k, 0.0) + v


acumulador = Acumulador()
