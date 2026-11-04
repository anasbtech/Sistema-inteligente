"""Ingestão e consulta de detecções, e geração automática de alertas.

O worker de inferência chama `POST /deteccoes/ingest`. É aqui — e não no
worker — que a busca 1:N acontece, para que o limiar, a auditoria e a regra
de alerta fiquem num lugar só.
"""
from __future__ import annotations

import base64
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit
from app.config import settings
from app.database import get_db
from app.deps import exige, usuario_de_query
from app.models import (
    Alerta, Camera, Deteccao, Pessoa, StatusAlerta, StatusPessoa,
    TipoDeteccao, Usuario, Zona,
)
from app.schemas import DeteccaoIn, DeteccaoOut, Pagina, QuadroAoVivoIn, RevisaoIn
from app.services import biometrics, evidencias, face_engine, retencao, sequencias
from app.services.events import publicar
from app.services.notificacoes import notificar_alerta_critico
from app.services.vision import (
    ROTULO_ACAO, SEVERIDADE_ACAO, janela_horario, severidade_objeto,
)
from app.tipos import como_utc

log = logging.getLogger("seguranca.deteccoes")
router = APIRouter(prefix="/deteccoes", tags=["Detecções"])

PREFIXO_CODIGO = {
    TipoDeteccao.FACIAL: "FR",
    TipoDeteccao.OBJETO: "OD",
    TipoDeteccao.ACAO: "AR",
}

# Só quem está com acesso restrito vira ocorrência ao ser reconhecido.
# Funcionário, visitante e terceirizado entram como passagem registrada.
SEVERIDADE_STATUS = {
    StatusPessoa.RESTRITO: 4,
    StatusPessoa.FUNCIONARIO: 1,
    StatusPessoa.VISITANTE: 1,
    StatusPessoa.TERCEIRIZADO: 1,
}


def _sequencial_do_codigo(codigo: str, digitos: int) -> int:
    """Últimos ``digitos`` do código, como número. 0 quando não dá para ler."""
    if not codigo:
        return 0
    try:
        return int(codigo.rsplit("-", 1)[1][-digitos:])
    except (IndexError, ValueError):
        return 0


async def _proximo_codigo(db: AsyncSession, tipo: TipoDeteccao) -> str:
    ano = datetime.now(timezone.utc).year
    pfx = PREFIXO_CODIGO[tipo]

    async def maior_emitido() -> int:
        # Lexicográfico serve como numérico aqui: o sequencial tem largura fixa.
        codigo = (
            await db.execute(
                select(func.max(Deteccao.codigo)).where(
                    Deteccao.codigo.like(f"{pfx}-{ano}%")
                )
            )
        ).scalar_one_or_none()
        return _sequencial_do_codigo(codigo or "", 6)

    async def em_uso(numero: int) -> bool:
        return (
            await db.execute(
                select(Deteccao.id)
                .where(Deteccao.codigo == f"{pfx}-{ano}{numero:06d}")
                .limit(1)
            )
        ).scalar_one_or_none() is not None

    numero = await sequencias.proximo_numero(
        db, f"{pfx}-{ano}", maior_emitido, em_uso
    )
    return f"{pfx}-{ano}{numero:06d}"


async def _criar_alerta(db: AsyncSession, det: Deteccao, cam: Camera,
                        pessoa: Optional[Pessoa],
                        notificar: bool = True,
                        zona_nome: str = "") -> Optional[Alerta]:
    """Severidade 3+ vira alerta. Abaixo disso fica só no registro."""
    if det.severidade < 3:
        return None

    async def maior_alerta() -> int:
        codigo = (
            await db.execute(select(func.max(Alerta.codigo)))
        ).scalar_one_or_none()
        return _sequencial_do_codigo(codigo or "", 5)

    numero_alerta = await sequencias.proximo_numero(db, "ALR", maior_alerta)

    if det.tipo == TipoDeteccao.FACIAL and pessoa is not None:
        # Chega aqui só com severidade 3+, que hoje é o acesso restrito.
        rotulo = ("Acesso restrito" if pessoa.status == StatusPessoa.RESTRITO
                  else "Pessoa")
        titulo = f"{rotulo} identificado"
        detalhe = (
            f"{pessoa.nome} ({pessoa.id_biometrico}) reconhecido com "
            f"{det.similaridade * 100:.1f}% de similaridade em {cam.nome}. "
            "Confirmação humana obrigatória antes de qualquer ação."
        )
    elif det.tipo == TipoDeteccao.OBJETO:
        titulo = f"{det.classe} detectado"
        detalhe = f"Classe '{det.classe}' com {(det.confianca or 0) * 100:.1f}% de confiança em {cam.nome}."
    else:
        titulo = ROTULO_ACAO.get(det.classe, det.classe)
        if det.classe == "fora_de_horario":
            # A frase genérica aqui era "evento comportamental por 0s
            # envolvendo 1 pessoa(s)" — verdadeira e inútil. Quem recebe este
            # alerta às 3h precisa das duas coisas que o justificam: a hora em
            # que a pessoa foi vista e a janela que ela furou.
            ini, fim = janela_horario(cam.horario_inicio, cam.horario_fim)
            visto = como_utc(det.ocorrido_em).astimezone()
            detalhe = (
                f"Pessoa vista às {visto:%H:%M} em {cam.nome}, fora da janela "
                f"permitida ({ini} às {fim})."
            )
        elif det.classe in ("invasao", "area_restrita"):
            # Qual perímetro, e por que isto é ocorrência: a zona pode ter
            # lista de autorizados, e o alerta só nasce para quem não está
            # nela — inclusive para quem o reconhecimento não identificou.
            onde = f" ({zona_nome})" if zona_nome else ""
            o_que = ("cruzou o perímetro" if det.classe == "invasao"
                     else "entrou na área restrita")
            detalhe = (
                f"Pessoa sem autorização {o_que}{onde} em {cam.nome}. Quem o "
                "reconhecimento facial não identificou conta como não "
                "autorizado — confirme na imagem."
            )
        elif det.classe == "contrafluxo":
            onde = f" ({zona_nome})" if zona_nome else ""
            detalhe = (
                f"Pessoa andando contra o sentido permitido{onde} em "
                f"{cam.nome}, por {det.duracao_s or 0}s seguidos."
            )
        elif det.classe in ("permanencia", "abandono"):
            # "por 1327s" é verdadeiro e ilegível: quem recebe o alerta
            # precisa saber de cara se é um quarto de hora ou três, porque é
            # disso que depende ir até lá. A duração é o fato aqui — sem ela
            # o alerta diz apenas que havia alguém parado, que é o normal.
            minutos = round((det.duracao_s or 0) / 60)
            quanto = f"{minutos} min" if minutos else f"{det.duracao_s or 0}s"
            quem = "Pessoa" if det.classe == "permanencia" else "Volume"
            detalhe = (
                f"{quem} na mesma área de {cam.nome} há {quanto}, sem sair "
                "do lugar."
            )
        elif det.classe == "briga":
            # A frase genérica dizia "evento comportamental envolvendo 2
            # pessoas", sob um título que afirmava agressão. Aqui o sistema
            # não conclui intenção a partir de vídeo — o que ele tem são três
            # sinais de trajetória que coincidiram, e abraço efusivo e
            # brincadeira os produzem. Quem recebe isto precisa saber que vai
            # olhar a imagem para decidir, e sobre o que decidir: um alerta
            # que afirma agressão entre duas pessoas identificadas tem
            # consequência real para elas.
            detalhe = (
                f"Indício de agressão entre {det.envolvidos or 2} pessoas em "
                f"{cam.nome}, por {det.duracao_s or 0}s: corpos sobrepostos, "
                "ambos em movimento rápido e errático. O sistema não conclui "
                "agressão a partir do vídeo — confirme na imagem antes de "
                "qualquer ação."
            )
        elif det.classe == "roubo":
            # Mesma cautela da briga: o vídeo mostra contato seguido de fuga,
            # não o objeto mudando de mão. Dizer o que foi visto é o que
            # permite a quem atende decidir — e o que protege quem só
            # esbarrou em alguém e saiu apressado.
            detalhe = (
                f"Indício de roubo em {cam.nome}: duas pessoas em contato e, "
                f"em seguida, uma delas saiu em disparada enquanto a outra "
                f"ficou ({det.duracao_s or 0}s do contato à fuga). O sistema "
                "não vê o objeto mudar de mão — confirme na imagem antes de "
                "qualquer ação."
            )
        else:
            detalhe = (
                f"Evento comportamental por {det.duracao_s or 0}s envolvendo "
                f"{det.envolvidos or 1} pessoa(s) em {cam.nome}."
            )

    alerta = Alerta(
        codigo=f"ALR-{numero_alerta:05d}",
        deteccao_id=det.id,
        camera_id=cam.id,
        pessoa_id=pessoa.id if pessoa else None,
        tipo=det.tipo,
        titulo=titulo,
        detalhe=detalhe,
        severidade=det.severidade,
        status=StatusAlerta.ABERTO,
        disparado_em=det.ocorrido_em,
    )
    db.add(alerta)
    await db.flush()

    # Durante a carga histórica do seed não faz sentido acionar ninguém.
    if notificar:
        # Todo alerta sobe pelo mesmo evento, e não só o de presença.
        #
        # Antes o alerta nascido de detecção viajava escondido dentro do
        # evento `deteccao`, e a tela só reagia a ele na severidade 4 — com um
        # toast de sete segundos. Num sistema integrado a presídio isso é
        # pouco: o aviso tem de interromper quem está operando, não disputar
        # atenção com o resto da tela e sumir sozinho.
        await publicar("alerta", {
            "codigo": alerta.codigo,
            "titulo": titulo,
            "detalhe": detalhe,
            "severidade": det.severidade,
            "camera_codigo": cam.codigo,
            "camera_nome": cam.nome,
            "deteccao_codigo": det.codigo,
            "tem_imagem": bool(det.snapshot_key),
            "ocorrido_em": det.ocorrido_em,
        })
        if det.severidade >= 4:
            await notificar_alerta_critico(titulo, detalhe, cam.codigo)
    return alerta


async def alertar_presenca(db: AsyncSession, cam: Camera,
                           quantidade: int, total: int,
                           det: Optional[Deteccao] = None,
                           jpeg: Optional[bytes] = None,
                           nomes: Optional[List[str]] = None) -> Alerta:
    """Alerta de alguém **entrando** numa sala vigiada.

    Antes isto era severidade elevada em cada detecção de pessoa, e o
    resultado foi o que o operador viu na tela: a mesma reunião rendendo um
    alerta por volta do parque — sete em seis minutos, com duas pessoas.
    Presença contínua não é notícia; a entrada é.

    Só entrada, e não saída: "a sala ficou vazia" não é ocorrência, é o fim
    de uma. A queda de ocupação continua sendo acompanhada em
    `inferencia._vigiar_sala` — sem baixar a referência, a entrada seguinte
    não seria vista como aumento —, mas não produz alerta.

    O alerta carrega a **própria imagem**, e não a de uma detecção. Dois
    motivos, os dois medidos na tela: na saída não há detecção nenhuma para
    anexar — ninguém é fotografado indo embora — e na entrada o registro de
    detecção é deduplicado por 30 minutos, então a mesma pessoa voltando à
    sala produzia alerta com a coluna CAPTURA em branco.

    A imagem é o quadro inteiro, não o recorte de uma pessoa: o que o alerta
    afirma é sobre a *sala* ("agora há 2", "ficou vazia"), e é a sala que
    precisa aparecer para quem revisa conferir.
    """
    async def maior_alerta() -> int:
        codigo = (await db.execute(select(func.max(Alerta.codigo)))).scalar_one_or_none()
        return _sequencial_do_codigo(codigo or "", 5)

    numero = await sequencias.proximo_numero(db, "ALR", maior_alerta)
    pessoas = "pessoa" if quantidade == 1 else "pessoas"
    verbo = "entrou" if quantidade == 1 else "entraram"
    nomes = [n for n in (nomes or []) if n]

    # O nome no título só quando ele é inequívoco: uma pessoa entrou e uma
    # pessoa foi reconhecida. Com duas entrando e uma reconhecida, pôr o nome
    # no título afirmaria que foi ela quem entrou — e o que se sabe é que ela
    # está na sala, não que acabou de chegar. Essa diferença decide o que o
    # operador vai procurar na imagem.
    if quantidade == 1 and len(nomes) == 1:
        titulo = f"Entrada de {nomes[0]} em {cam.nome}"
    else:
        titulo = f"Entrada em {cam.nome}"

    detalhe = (f"{quantidade} {pessoas} {verbo} em {cam.nome} ({cam.setor}). "
               f"Agora há {total} na sala. ")
    if nomes:
        # "na sala", e não "entrou": o facial lê quem está em cena no quadro,
        # que não é necessariamente quem acabou de cruzar a porta.
        lista = ", ".join(nomes)
        detalhe += (f"Reconhecida na sala: {lista}." if len(nomes) == 1
                    else f"Reconhecidas na sala: {lista}.")
        if total > len(nomes):
            restantes = total - len(nomes)
            detalhe += (f" {restantes} pessoa não identificada." if restantes == 1
                        else f" {restantes} pessoas não identificadas.")
    else:
        detalhe += ("Esta câmera alerta toda entrada, com ou sem autorização — "
                    "nenhuma das pessoas em cena consta na base biométrica.")

    codigo = f"ALR-{numero:05d}"
    agora = datetime.now(timezone.utc)
    chave = ""
    if jpeg:
        try:
            chave = evidencias.gravar(codigo, agora, jpeg)
        except (ValueError, OSError) as exc:
            log.warning("Imagem do alerta %s descartada: %s", codigo, exc)

    alerta = Alerta(
        codigo=codigo,
        snapshot_key=chave,
        deteccao_id=det.id if det is not None else None,
        camera_id=cam.id,
        tipo=TipoDeteccao.OBJETO,
        titulo=titulo,
        detalhe=detalhe,
        severidade=settings.ALERTA_PRESENCA_SEVERIDADE,
        status=StatusAlerta.ABERTO,
        disparado_em=agora,
    )
    db.add(alerta)
    await db.flush()

    # Vai pelo barramento para a tela avisar na hora, e não só quando alguém
    # abrir a lista de alertas.
    await publicar("alerta", {
        "codigo": alerta.codigo,
        "titulo": titulo,
        "detalhe": detalhe,
        "severidade": alerta.severidade,
        "camera_codigo": cam.codigo,
        "camera_nome": cam.nome,
        # Alerta de sala não nasce de detecção: a imagem é do próprio alerta.
        "deteccao_codigo": det.codigo if det is not None else "",
        "tem_imagem": bool(chave),
        "ocorrido_em": agora,
    })
    if alerta.severidade >= 4:
        await notificar_alerta_critico(titulo, detalhe, cam.codigo)
    return alerta



# ======================================================================
# Ingestão — usada pelo worker de inferência E pelo simulador
# ======================================================================
async def processar_deteccao(db: AsyncSession, dados: DeteccaoIn,
                             origem: str = "worker",
                             publicar_evento: bool = True) -> dict:
    """Caminho único de entrada de qualquer detecção no sistema.

    É aqui — e não no worker — que acontecem a busca 1:N, a aplicação do
    limiar da câmera e a decisão de alerta. Um lugar para mudar, um lugar
    para auditar, e o simulador exercita exatamente o mesmo código que a
    câmera real vai exercitar.
    """
    cam = (
        await db.execute(select(Camera).where(Camera.codigo == dados.camera_codigo))
    ).scalars().unique().one_or_none()
    if cam is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Câmera '{dados.camera_codigo}' não cadastrada.")

    pessoa: Optional[Pessoa] = None
    similaridade: Optional[float] = None
    limiar: Optional[float] = None
    sugerido_id = None
    severidade = 1

    if dados.tipo == TipoDeteccao.FACIAL:
        if not cam.analitico_facial:
            raise HTTPException(status.HTTP_409_CONFLICT,
                                "Análise facial desativada nesta câmera.")
        if not dados.embedding:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Detecção facial exige o campo 'embedding'.")

        limiar = cam.limiar_facial
        resultado = await face_engine.buscar_pessoa(db, dados.embedding, limiar)
        similaridade = round(resultado.similaridade, 4)

        if resultado.match:
            pessoa = (
                await db.execute(
                    select(Pessoa).where(Pessoa.id == uuid.UUID(resultado.pessoa_id))
                )
            ).scalar_one_or_none()
            if pessoa is not None:
                severidade = SEVERIDADE_STATUS.get(pessoa.status, 1)
                pessoa.total_deteccoes += 1
                anterior = como_utc(pessoa.ultima_deteccao)
                if anterior is None or como_utc(dados.ocorrido_em) > anterior:
                    pessoa.ultima_deteccao = dados.ocorrido_em
                    pessoa.ultima_camera = cam.nome
                # Acerto folgado vira amostra daquela câmera. É o que mantém
                # a cobertura crescendo sozinha depois que a primeira amostra
                # de um ângulo entrou: a busca compara contra cada amostra e
                # fica com a melhor, então o ângulo novo passa a responder por
                # si. Quem decide se cabe é `acolher_amostra` — cota por
                # câmera e recusa de parecida demais vivem lá.
                if (origem != "seed" and dados.embedding
                        and similaridade >= settings.FACE_AUTO_SAMPLE_MIN):
                    if await face_engine.acolher_amostra(
                            db, pessoa, dados.embedding,
                            dados.qualidade_face or 0.0, cam.id):
                        log.info("%s: captura de %s virou amostra (%.0f%%)",
                                 cam.codigo, pessoa.nome, similaridade * 100)
        # Sem match: severidade 1 e pessoa_id nulo — nenhuma identidade atribuída.
        # Perto do limiar, porém, guarda-se quem o motor apontou: é o que a
        # fila de conferência mostra para alguém decidir. Continua não sendo
        # identidade — `pessoa_id` segue nulo, e nada no sistema trata este
        # campo como se fosse.
        elif (resultado.candidato_id
                and similaridade >= settings.FACE_REVIEW_MIN):
            sugerido_id = uuid.UUID(resultado.candidato_id)


        # Uma presença, uma linha. Sem correspondência não existe identidade
        # para separar duas capturas, e nesta resolução o embedding também não
        # separa — então quem chega dentro da janela da anterior é tratado como
        # a mesma passagem. Sem isto, alguém que cruza o saguão deixava uma
        # dezena de linhas "não identificada", todas com o mesmo rosto.
        # O seed fica de fora: ele fabrica um histórico espalhado, não uma
        # presença contínua diante da câmera.
        if pessoa is None and origem != "seed" and settings.UNMATCHED_FACE_WINDOW_S > 0:
            desde = como_utc(dados.ocorrido_em) - timedelta(
                seconds=settings.UNMATCHED_FACE_WINDOW_S
            )
            anterior_codigo = (
                await db.execute(
                    select(Deteccao.codigo)
                    .where(
                        Deteccao.camera_id == cam.id,
                        Deteccao.tipo == TipoDeteccao.FACIAL,
                        Deteccao.pessoa_id.is_(None),
                        Deteccao.ocorrido_em >= desde,
                    )
                    .order_by(Deteccao.ocorrido_em.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if anterior_codigo is not None:
                log.info("%s: captura não identificada absorvida por %s",
                         cam.codigo, anterior_codigo)
                return {
                    "codigo": anterior_codigo,
                    "match": False,
                    "id_biometrico": None,
                    "similaridade": similaridade,
                    "severidade": 1,
                    "alerta": None,
                    "descartada": True,
                }

    elif dados.tipo == TipoDeteccao.OBJETO:
        limiar = cam.limiar_objeto
        severidade = severidade_objeto(dados.classe)
    else:
        severidade = SEVERIDADE_ACAO.get(dados.classe, 2)

    zona_id = None
    if dados.zona_nome:
        zona = (
            await db.execute(
                select(Zona).where(Zona.camera_id == cam.id, Zona.nome == dados.zona_nome)
            )
        ).scalar_one_or_none()
        zona_id = zona.id if zona else None

    # Capturas faciais sem correspondência têm prazo de validade curto.
    expurgo = None
    if dados.tipo == TipoDeteccao.FACIAL and pessoa is None:
        expurgo = dados.ocorrido_em + timedelta(days=settings.RETENTION_DAYS_UNMATCHED)

    codigo = await _proximo_codigo(db, dados.tipo)

    # O recorte é gravado sob o código da própria detecção. A chave que o
    # worker sugere não vira caminho — ela chega pela rede.
    chave = ""
    if dados.snapshot_jpeg:
        try:
            chave = evidencias.gravar(
                codigo, dados.ocorrido_em,
                base64.b64decode(dados.snapshot_jpeg, validate=True),
            )
        except (ValueError, OSError) as exc:
            log.warning("Recorte de %s descartado: %s", codigo, exc)
    chave_placa = ""
    if dados.placa_jpeg and dados.placa:
        try:
            chave_placa = evidencias.gravar(
                f"{codigo}-placa", dados.ocorrido_em,
                base64.b64decode(dados.placa_jpeg, validate=True),
            )
        except (ValueError, OSError) as exc:
            log.warning("Recorte da placa de %s descartado: %s", codigo, exc)

    det = Deteccao(
        codigo=codigo,
        tipo=dados.tipo,
        ocorrido_em=dados.ocorrido_em,
        camera_id=cam.id,
        pessoa_id=pessoa.id if pessoa else None,
        pessoa_sugerida_id=sugerido_id,
        similaridade=similaridade,
        limiar_aplicado=limiar,
        qualidade_face=dados.qualidade_face,
        classe=dados.classe,
        confianca=dados.confianca,
        duracao_s=dados.duracao_s,
        envolvidos=dados.envolvidos,
        placa=dados.placa,
        zona_id=zona_id,
        bbox=dados.bbox,
        track_id=dados.track_id,
        severidade=severidade,
        snapshot_key=chave,
        placa_snapshot_key=chave_placa,
        hash_evidencia=dados.hash_evidencia,
        expurgo_em=expurgo,
    )
    db.add(det)
    await db.flush()

    alerta = await _criar_alerta(db, det, cam, pessoa,
                                 notificar=publicar_evento,
                                 zona_nome=dados.zona_nome or "")

    if publicar_evento:
        await publicar("deteccao", {
            "codigo": det.codigo,
            "tipo": det.tipo.value,
            "ocorrido_em": det.ocorrido_em,
            "camera_codigo": cam.codigo,
            "camera_nome": cam.nome,
            "setor": cam.setor,
            "classe": det.classe,
            "classe_rotulo": ROTULO_ACAO.get(det.classe, det.classe),
            "confianca": det.confianca,
            "similaridade": det.similaridade,
            "limiar": det.limiar_aplicado,
            "severidade": det.severidade,
            "bbox": det.bbox,
            "snapshot_key": det.snapshot_key,
            "origem": origem,
            "pessoa": {
                "id_biometrico": pessoa.id_biometrico,
                "nome": pessoa.nome,
                "sexo": pessoa.sexo.value,
                "status": pessoa.status.value,
            } if pessoa else None,
            "alerta": {"codigo": alerta.codigo, "titulo": alerta.titulo,
                       "severidade": alerta.severidade} if alerta else None,
        })

    return {
        "codigo": det.codigo,
        "match": pessoa is not None,
        "id_biometrico": pessoa.id_biometrico if pessoa else None,
        # Cru, e não só o identificador legível: quem liga o corpo ao rosto
        # precisa da chave estrangeira, e buscá-la de novo seria uma consulta
        # a mais por rosto identificado.
        "pessoa_id": str(pessoa.id) if pessoa else None,
        # O nome sobe junto porque quem desenha a sobreposição é o worker, e
        # é aqui que a busca 1:N acontece — sem ele, a caixa do rosto
        # identificado continuaria rotulada apenas como "rosto".
        "nome": pessoa.nome if pessoa else None,
        "similaridade": similaridade,
        "severidade": severidade,
        "alerta": alerta.codigo if alerta else None,
    }


@router.post("/ingest", status_code=status.HTTP_201_CREATED,
             summary="Receber detecção do worker de inferência")
async def ingest(dados: DeteccaoIn, db: AsyncSession = Depends(get_db)):
    """Endpoint de serviço: exponha só na rede interna, ou exija mTLS no gateway."""
    return await processar_deteccao(db, dados, origem="worker")


@router.post("/ao-vivo", status_code=status.HTTP_204_NO_CONTENT,
             summary="Caixas do quadro atual, para a sobreposição do mosaico")
async def ao_vivo(dados: QuadroAoVivoIn) -> None:
    """Publica as caixas do quadro atual sem gravar nada.

    Detectar e registrar são coisas diferentes: a caixa acompanha o objeto
    enquanto ele estiver diante da câmera, mas o registro nasce uma vez por
    passagem. Misturar os dois encheria a base de linhas repetidas, ou faria a
    caixa sumir com o carro ainda parado ali.
    """
    await publicar("quadro_ao_vivo", {
        "camera_codigo": dados.camera_codigo,
        "caixas": [c.model_dump() for c in dados.caixas],
    })


# ======================================================================
# Consulta
# ======================================================================
@router.get("", response_model=Pagina, summary="Listar detecções")
async def listar(
    tipo: Optional[TipoDeteccao] = None,
    camera: Optional[str] = Query(None, description="Código da câmera"),
    classe: Optional[str] = None,
    id_biometrico: Optional[str] = None,
    apenas_match: Optional[bool] = None,
    desde: Optional[datetime] = None,
    ate: Optional[datetime] = None,
    severidade_min: Optional[int] = Query(None, ge=1, le=4),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(25, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("facial")),
):
    filtros = []
    if tipo:
        filtros.append(Deteccao.tipo == tipo)
    if classe == "placa":
        # Placa não é classe de objeto, é leitura feita *sobre* o veículo
        # detectado: fica no campo `placa` da própria detecção. Comparada
        # contra `Deteccao.classe`, a opção do filtro nunca devolvia nada —
        # mesmo havendo placas lidas e gravadas. Aqui ela passa a significar
        # "as detecções em que uma placa foi lida", que é o que quem escolhe
        # "Placa veicular" está procurando.
        filtros.append(Deteccao.placa.isnot(None))
        filtros.append(Deteccao.placa != "")
    elif classe:
        filtros.append(Deteccao.classe == classe)
    if camera:
        cam = (await db.execute(select(Camera).where(Camera.codigo == camera))).scalar_one_or_none()
        if cam is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
        filtros.append(Deteccao.camera_id == cam.id)
    if id_biometrico:
        p = (await db.execute(
            select(Pessoa).where(Pessoa.id_biometrico == id_biometrico)
        )).scalar_one_or_none()
        if p is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Registro biométrico não encontrado.")
        filtros.append(Deteccao.pessoa_id == p.id)
    if apenas_match is not None:
        filtros.append(Deteccao.pessoa_id.isnot(None) if apenas_match
                       else Deteccao.pessoa_id.is_(None))
    if desde:
        filtros.append(Deteccao.ocorrido_em >= desde)
    if ate:
        filtros.append(Deteccao.ocorrido_em <= ate)
    if severidade_min:
        filtros.append(Deteccao.severidade >= severidade_min)

    total = (await db.execute(
        select(func.count()).select_from(Deteccao).where(*filtros)
    )).scalar_one()
    linhas = (
        await db.execute(
            select(Deteccao).where(*filtros)
            .order_by(Deteccao.ocorrido_em.desc())
            .offset((pagina - 1) * por_pagina).limit(por_pagina)
        )
    ).scalars().unique().all()

    return Pagina(total=total, pagina=pagina, por_pagina=por_pagina,
                  itens=[DeteccaoOut.model_validate(d) for d in linhas])


@router.get("/por-classe", summary="Contagem por classe no período")
async def por_classe(
    tipo: TipoDeteccao = Query(..., description="OBJETO, FACIAL ou ACAO"),
    camera: Optional[str] = Query(None, description="Código da câmera"),
    desde: Optional[datetime] = None,
    ate: Optional[datetime] = None,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(exige("facial")),
):
    """Quantas detecções de cada classe no intervalo, numa consulta só.

    O gráfico pedia isso com uma requisição por classe, uma dezena delas a
    cada vez que a data mudava. O navegador abre seis conexões por origem, e
    as excedentes esperavam na fila atrás do próprio vídeo do mosaico: o
    gráfico continuava exibindo o intervalo anterior por segundos, o que
    parecia filtro que não pega.
    """
    filtros = [Deteccao.tipo == tipo]
    if camera:
        cam = (await db.execute(
            select(Camera).where(Camera.codigo == camera)
        )).scalar_one_or_none()
        if cam is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Câmera não encontrada.")
        filtros.append(Deteccao.camera_id == cam.id)
    if desde:
        filtros.append(Deteccao.ocorrido_em >= desde)
    if ate:
        filtros.append(Deteccao.ocorrido_em <= ate)

    linhas = (await db.execute(
        select(Deteccao.classe, func.count())
        .where(*filtros)
        .group_by(Deteccao.classe)
    )).all()

    return {"total": sum(n for _, n in linhas),
            "classes": {c: n for c, n in linhas if c}}


@router.get("/resumo", summary="Agregados para o painel")
async def resumo(horas: int = Query(24, ge=1, le=168),
                 desde: Optional[datetime] = Query(None, description="Início da janela (ISO)"),
                 ate: Optional[datetime] = Query(None, description="Fim da janela (ISO)"),
                 db: AsyncSession = Depends(get_db),
                 usuario: Usuario = Depends(exige("facial"))):
    """Agregados da janela pedida.

    Com `desde`/`ate` a janela é esse intervalo — é o que permite olhar um dia
    fechado no passado. Sem eles, vale `horas` contadas de agora para trás.
    """
    inicio = como_utc(desde) if desde else datetime.now(timezone.utc) - timedelta(hours=horas)
    fim = como_utc(ate) if ate else None
    if fim is not None and fim < inicio:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "A data final é anterior à inicial.")

    janela = [Deteccao.ocorrido_em >= inicio]
    if fim is not None:
        janela.append(Deteccao.ocorrido_em <= fim)

    por_tipo = dict((t, n) for t, n in (
        await db.execute(
            select(Deteccao.tipo, func.count()).where(*janela)
            .group_by(Deteccao.tipo)
        )
    ).all())

    # Agrupamento por hora feito em Python: `date_trunc` é do PostgreSQL e o
    # sistema também roda em SQLite.
    brutos = (
        await db.execute(
            select(Deteccao.ocorrido_em, Deteccao.tipo).where(*janela)
        )
    ).all()
    baldes: dict[tuple, int] = {}
    for quando, tipo in brutos:
        hora = quando.replace(minute=0, second=0, microsecond=0)
        baldes[(hora, tipo)] = baldes.get((hora, tipo), 0) + 1
    por_hora = sorted(((h, t, n) for (h, t), n in baldes.items()), key=lambda r: r[0])

    # Todas as câmeras que registraram algo, não as dez primeiras: o painel
    # soma os setores a partir desta lista, e com ela cortada os setores de
    # fora sumiam do gráfico — o 2º, 3º e 4º andar constavam com zero tendo
    # movimento. O parque é de dezenas de câmeras, então a lista inteira cabe.
    por_camera = (
        await db.execute(
            select(Camera.codigo, Camera.nome, Camera.setor, func.count(Deteccao.id).label("n"))
            .join(Deteccao, Deteccao.camera_id == Camera.id)
            .where(*janela)
            .group_by(Camera.codigo, Camera.nome, Camera.setor)
            .order_by(func.count(Deteccao.id).desc())
        )
    ).all()

    identificacoes = (
        await db.execute(
            select(func.count()).select_from(Deteccao)
            .where(Deteccao.tipo == TipoDeteccao.FACIAL,
                   Deteccao.pessoa_id.isnot(None),
                   *janela)
        )
    ).scalar_one()

    return {
        "desde": inicio,
        "ate": fim,
        "total": sum(por_tipo.values()),
        "por_tipo": {t.value: n for t, n in por_tipo.items()},
        "identificacoes_positivas": identificacoes,
        "serie_horaria": [
            {"hora": h, "tipo": t.value, "total": n} for h, t, n in por_hora
        ],
        "top_cameras": [
            {"codigo": c, "nome": n, "setor": s, "total": q} for c, n, s, q in por_camera
        ],
    }


@router.get("/{codigo}/snapshot", summary="Recorte da captura (JPEG)")
async def snapshot(codigo: str, token: str = Query(..., description="Access token JWT"),
                   db: AsyncSession = Depends(get_db)):
    """A imagem que a revisão humana olha antes de confirmar uma identificação.

    Não gera linha de auditoria: a lista carrega uma miniatura por registro, e
    auditar cada uma encheria a trilha de ruído paralelo. O acesso que importa
    — abrir a ficha da captura — já é registrado em `obter`.
    """
    await usuario_de_query(token, db, "facial")

    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")

    imagem = evidencias.ler(det.snapshot_key)
    if imagem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "Sem recorte guardado para esta captura.")
    return Response(content=imagem, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/{codigo}/placa", summary="Recorte da placa lida (JPEG)")
async def imagem_placa(codigo: str, token: str = Query(..., description="Access token JWT"),
                       db: AsyncSession = Depends(get_db)):
    """A placa recortada e ampliada, para conferir a leitura.

    Sem auditoria pelo mesmo motivo do recorte da captura: a lista carrega uma
    miniatura por linha, e o acesso que importa é abrir a ficha.
    """
    await usuario_de_query(token, db, "facial")
    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")
    imagem = evidencias.ler(det.placa_snapshot_key)
    if imagem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sem recorte de placa para esta detecção.")
    return Response(content=imagem, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/{codigo}", response_model=DeteccaoOut, summary="Detalhe da detecção")
async def obter(codigo: str, request: Request, db: AsyncSession = Depends(get_db),
                usuario: Usuario = Depends(exige("facial"))):
    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")

    if det.pessoa_id:
        await audit.registrar(
            db, acao="CONSULTA_BIO", descricao="Consulta a detecção com identificação",
            severidade="info", usuario=usuario,
            alvo=det.pessoa.id_biometrico if det.pessoa else det.codigo,
            request=request,
        )

    saida = DeteccaoOut.model_validate(det)
    # O que trava a exclusão vai junto da ficha. A regra continua a mesma —
    # apagar por baixo de um alerta o deixaria apontando para o vazio —, mas
    # quem abre a ficha passa a saber disso antes de tentar, e com os códigos
    # em mãos para resolver.
    saida.alertas = list((await db.execute(
        select(Alerta.codigo).where(Alerta.deteccao_id == det.id)
        .order_by(Alerta.disparado_em)
    )).scalars().all())
    return saida


@router.post("/{codigo}/revisao", response_model=DeteccaoOut, summary="Revisar detecção")
async def revisar(codigo: str, dados: RevisaoIn, request: Request,
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("facial", "edit"))):
    """Confirmação humana. Falsos positivos alimentam o lote de reavaliação do
    limiar — e não devem ser apagados."""
    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")

    det.revisado = True
    det.veredito = dados.veredito
    det.revisado_por = usuario.id
    det.revisado_em = datetime.now(timezone.utc)

    await audit.registrar(
        db, acao="DET_REVISAO", descricao=f"Detecção revisada: {dados.veredito}",
        severidade="ok" if dados.veredito == "Confirmado" else "warn",
        usuario=usuario, alvo=det.codigo, finalidade=dados.observacao or "Revisão operacional",
        request=request,
        metadados={"similaridade": det.similaridade, "limiar": det.limiar_aplicado},
    )
    return DeteccaoOut.model_validate(det)


@router.delete("/{codigo}", status_code=status.HTTP_204_NO_CONTENT,
               summary="Remover detecção")
async def remover(codigo: str, request: Request,
                  motivo: str = Query("", max_length=300,
                                      description="Por que o registro está sendo descartado"),
                  db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("settings"))):
    """Descarta um registro e o seu recorte, sem esperar a retenção vencer.

    É para o registro que nasceu errado por defeito do detector — não para
    aquele de que o operador discorda. Falso positivo é revisão: fica no
    banco, porque é ele que alimenta a reavaliação do limiar. Apagar cabe
    quando o próprio rótulo deixou de existir e o registro só polui a lista:
    o furgão que saía como "Ônibus" porque o COCO não tem `van`.

    Recusa o que está preso a alerta ou a uma pessoa. O vínculo do alerta é
    `ON DELETE SET NULL`, então apagar por baixo dele não daria erro nenhum —
    deixaria um alerta apontando para o vazio, que é pior do que recusar.

    Admin apenas, como o expurgo: das ações sobre o registro, esta é a única
    que não se desfaz.
    """
    det = (
        await db.execute(select(Deteccao).where(Deteccao.codigo == codigo))
    ).scalars().unique().one_or_none()
    if det is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Detecção não encontrada.")

    if det.pessoa_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Esta detecção identificou uma pessoa e pode integrar inquérito. "
            "Marque como falso positivo em vez de apagar.",
        )

    alertas = (
        await db.execute(
            select(func.count()).select_from(Alerta).where(Alerta.deteccao_id == det.id)
        )
    ).scalar_one()
    if alertas:
        # "Feche o alerta antes" era mentira, e estava aqui desde sempre:
        # encerrar põe o alerta em RESOLVIDO e **mantém** o vínculo, então a
        # contagem acima continua a mesma e a recusa se repete. Medido: encerrar
        # e tentar de novo devolvia o mesmo 409. O operador fazia o que o
        # sistema mandava e batia na mesma parede.
        #
        # O que libera de verdade é excluir o alerta, que é outra operação.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Esta detecção gerou {alertas} alerta(s) e não pode ser apagada — "
            "eles ficariam apontando para o vazio. Exclua o alerta primeiro "
            "(encerrar não basta, o vínculo permanece), ou marque a detecção "
            "como falso positivo, que a preserva para a reavaliação do limiar.",
        )

    # Apagar o registro e deixar o recorte no disco transformaria a retenção
    # numa promessa vazia — o mesmo motivo do expurgo.
    recorte = evidencias.apagar(det.snapshot_key)
    evidencias.apagar(det.placa_snapshot_key)
    await audit.registrar(
        db, acao="DET_EXCLUSAO", descricao=f"Detecção {det.codigo} removida ({det.classe or det.tipo.value})",
        severidade="warn", usuario=usuario, alvo=det.codigo,
        finalidade=motivo or "Remoção manual de registro incorreto",
        request=request,
        metadados={"classe": det.classe, "confianca": det.confianca,
                   "hash_evidencia": det.hash_evidencia, "recorte_apagado": recorte},
    )
    await db.delete(det)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/expurgo", summary="Expurgar capturas vencidas")
async def expurgo(db: AsyncSession = Depends(get_db),
                  usuario: Usuario = Depends(exige("settings"))):
    """Remove capturas sem correspondência cujo prazo de retenção venceu.

    Mantém as vinculadas a alerta ou já revisadas — elas podem integrar
    inquérito.

    A rotina vive em `services/retencao`, e este endpoint é só a porta manual
    dela. Antes a regra estava escrita aqui, e o laço automático teria sido
    uma segunda cópia: duas implementações da retenção acabam divergindo, e
    aí uma delas apaga o que a outra guardaria.
    """
    saida = await retencao.expurgar(db, usuario=usuario, origem="manual")
    saida["vencidas_restantes"] = await retencao.vencidas(db)
    return saida


@router.get("/retencao", summary="Quanto já passou do prazo de retenção")
async def estado_retencao(db: AsyncSession = Depends(get_db),
                          usuario: Usuario = Depends(exige("settings"))):
    """Só conta, não apaga.

    A política de retenção não tinha como ser conferida sem executá-la, e
    "não sei quanto está vencido" é a situação em que o prazo passa sem
    ninguém notar — foi o que aconteceu aqui: zero linhas `EXPURGO` na trilha
    em 2.152 registros.
    """
    return {
        "vencidas": await retencao.vencidas(db),
        "prazo_dias": settings.RETENTION_DAYS_UNMATCHED,
        "automatico_horas": settings.RETENCAO_INTERVALO_HORAS,
        "automatico_ligado": settings.RETENCAO_INTERVALO_HORAS > 0,
    }

