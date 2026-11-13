"""Sinais → ocorrência com prioridade.

Cada sinal sozinho é fraco: abraço efusivo e brincadeira dão os mesmos
sinais de trajetória que uma agressão; alguém que tropeça dá queda. O que pesa
é a coincidência — na mesma câmera, em poucos minutos. Sinais dentro de
`GUARDIAN_JANELA_S` do último entram na mesma ocorrência, e a prioridade é
recalculada a cada sinal novo.

Prioridade (o que o operador vê primeiro):

* **CRÍTICA** — gesto de socorro (sozinho já basta: é alguém pedindo ajuda);
  ou possível agressão somada a queda ou a contato com fuga; ou agressão
  repetida.
* **ALTA** — possível agressão; contato seguido de fuga; queda seguida de
  corrida.
* **MÉDIA** — queda.
* **BAIXA** — corrida súbita sozinha.

A ocorrência nunca diz que houve crime nem aponta pessoa: lista os sinais e
quanto a IA confiou em cada um. Quem conclui é a avaliação humana.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app.config import settings

log = logging.getLogger("seguranca.guardian")

SINAIS = {
    "sinal_socorro": ("Gesto de socorro (Signal for Help)", 10.0),
    "briga": ("Possível agressão física", 4.0),
    "roubo": ("Contato seguido de fuga", 4.0),
    "queda": ("Queda de pessoa", 3.0),
    "corrida": ("Corrida súbita", 1.0),
}
PRIORIDADES = ("CRITICA", "ALTA", "MEDIA", "BAIXA")
ROTULO_PRIORIDADE = {"CRITICA": "crítica", "ALTA": "alta", "MEDIA": "média", "BAIXA": "baixa"}
ABERTAS = ("NOVA", "EM_ANALISE")
STATUS = ("NOVA", "EM_ANALISE", "RISCO_CONFIRMADO", "FALSO_ALARME", "ENCERRADA")


def avaliar(tipos: List[str]) -> Tuple[str, float, str]:
    """(prioridade, pontuação, motivo legível) de uma lista de tipos de sinal."""
    n = {t: tipos.count(t) for t in SINAIS}
    pontos = sum(SINAIS[t][1] for t in tipos if t in SINAIS)
    if n["sinal_socorro"]:
        return "CRITICA", pontos, "gesto de pedido de socorro"
    if n["briga"] and (n["queda"] or n["roubo"]):
        return "CRITICA", pontos, ("possível agressão seguida de queda" if n["queda"]
                                   else "possível agressão com contato e fuga")
    if n["briga"] >= 2:
        return "CRITICA", pontos, "possível agressão repetida"
    if n["briga"]:
        return "ALTA", pontos, "possível agressão física"
    if n["roubo"]:
        return "ALTA", pontos, "contato seguido de fuga"
    if n["queda"] and n["corrida"]:
        return "ALTA", pontos, "queda e corrida na mesma cena"
    if n["queda"]:
        return "MEDIA", pontos, "queda de pessoa"
    return "BAIXA", pontos, "corrida súbita"


def _iso(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


async def registrar_sinal(db, det, cam, alerta=None, detalhe: str = ""):
    """Junta a detecção `det` (ação) à ocorrência aberta da câmera, ou abre
    uma nova. Só para câmera com o Guardian ligado e sinal conhecido."""
    from sqlalchemy import func, select

    from app.models import OcorrenciaGuardian
    from app.services import sequencias

    if not getattr(cam, "guardian", False) or det.classe not in SINAIS:
        return None
    quando = det.ocorrido_em if det.ocorrido_em.tzinfo else det.ocorrido_em.replace(tzinfo=timezone.utc)
    limite = quando - timedelta(seconds=settings.GUARDIAN_JANELA_S)
    oc = (await db.execute(
        select(OcorrenciaGuardian)
        .where(OcorrenciaGuardian.camera_id == cam.id, OcorrenciaGuardian.status.in_(ABERTAS),
               OcorrenciaGuardian.ultimo_sinal >= limite)
        .order_by(OcorrenciaGuardian.ultimo_sinal.desc()))).scalars().unique().first()
    nova = oc is None
    if nova:
        async def maior() -> int:
            cod = (await db.execute(select(func.max(OcorrenciaGuardian.codigo)))).scalar_one_or_none()
            try:
                return int((cod or "GRD-0").split("-")[1])
            except (IndexError, ValueError):
                return 0
        numero = await sequencias.proximo_numero(db, "GRD", maior)
        oc = OcorrenciaGuardian(codigo=f"GRD-{numero:05d}", camera_id=cam.id, inicio=quando,
                                ultimo_sinal=quando, prioridade="BAIXA", sinais=[], historico=[],
                                status="NOVA")
        db.add(oc)
    rotulo = SINAIS[det.classe][0]
    sinais = list(oc.sinais or [])
    sinais.append({"tipo": det.classe, "rotulo": rotulo, "em": _iso(quando),
                   "confianca": round(float(det.confianca or 0), 3), "deteccao": det.codigo,
                   "detalhe": detalhe})
    prioridade, pontos, motivo = avaliar([s["tipo"] for s in sinais])
    anterior = oc.prioridade
    oc.sinais = sinais
    oc.ultimo_sinal = max(quando, oc.ultimo_sinal if oc.ultimo_sinal.tzinfo
                          else oc.ultimo_sinal.replace(tzinfo=timezone.utc))
    oc.prioridade, oc.pontuacao = prioridade, pontos
    oc.confianca = round(max(s["confianca"] for s in sinais), 3)
    # A imagem da ocorrência é a do sinal mais grave: o gesto, se houver.
    if det.snapshot_key and (not oc.imagem_key or det.classe == "sinal_socorro"):
        oc.imagem_key = det.snapshot_key
    if alerta is not None and not oc.alerta_codigo:
        oc.alerta_codigo = alerta.codigo
    hist = list(oc.historico or [])
    hist.append({"em": _iso(datetime.now(timezone.utc)), "por": "IA", "evento": "sinal",
                 "texto": f"{rotulo} ({round(float(det.confianca or 0) * 100)}%) — prioridade "
                          f"{ROTULO_PRIORIDADE[prioridade]}: {motivo}"})
    oc.historico = hist
    await db.flush()
    log.info("Guardian %s: %s em %s → %s (%s)", oc.codigo, det.classe, cam.codigo, prioridade, motivo)
    try:
        from app.services.events import publicar
        await publicar("guardian", {
            "codigo": oc.codigo, "prioridade": prioridade, "motivo": motivo, "nova": nova,
            "subiu": anterior != prioridade and not nova, "camera_codigo": cam.codigo,
            "camera_nome": cam.nome, "sinal": rotulo, "em": _iso(quando)})
    except Exception as exc:  # noqa: BLE001
        log.debug("Guardian: evento não publicado: %s", exc)
    # O vídeo: o serviço grava os segundos antes e depois, se a câmera estiver
    # no laço dele (o passado só existe na memória do laço).
    if not oc.clipe_key:
        try:
            from app.services.guardian.servico import servico
            servico.pedir_clipe(cam.codigo, oc.codigo, quando)
        except Exception as exc:  # noqa: BLE001
            log.debug("Guardian: clipe não pedido: %s", exc)
    return oc
