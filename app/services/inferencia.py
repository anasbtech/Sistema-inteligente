"""Worker de inferência sobre o vídeo das câmeras.

Lê o quadro corrente de cada câmera on-line, roda os modelos que aquela
câmera tem habilitados e registra pela mesma rota que o worker externo usa —
`processar_deteccao`. O que entra no banco é o que o modelo viu: classe,
confiança e caixa saem da inferência, e o recorte é da região que o modelo
apontou.

É a diferença para o simulador, que sorteia classe e caixa. Um rótulo
sorteado sobre um recorte real produz registro que não se sustenta: a ficha
diz "Capacete 90,9%" sobre uma imagem de asfalto, e quem revisa não tem como
saber se o sistema erra assim sempre ou só ali.

Dedução de carga: cada objeto é registrado uma vez por janela, identificado
pela trilha do rastreador. Sem isso um carro estacionado vira uma detecção a
cada ciclo, e em uma hora o registro tem mil linhas do mesmo carro parado.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import select

from app.config import settings
from app.database import db_context
from app.models import Camera, StatusCamera, TipoDeteccao
from app.schemas import DeteccaoIn
from app.services import (
    biometrics, chama, extras, face_engine, placa, video, vision,
)
from app.services.events import publicar

log = logging.getLogger("seguranca.inferencia")


def _classes_barradas(cam) -> set:
    """O que esta câmera NÃO pode registrar.

    A lista do que o modelo sabe ver é uma só, do parque inteiro; o que cada
    câmera registra é decisão da câmera. Antes isso valia só para veículo, e as
    demais classes problemáticas eram resolvidas apagando-as do mapa — o que as
    tirava também de onde faziam sentido. Uma câmera de pátio não via animal
    porque um hall de andar confundia pessoa agachada com cachorro.

    O que torna a classe errada quase nunca é o modelo: é o enquadramento. Por
    isso quem decide é quem conhece a câmera.
    """
    fora = set()
    if not cam.detecta_veiculo:
        fora |= vision.CLASSES_VIA
    if not getattr(cam, "detecta_bagagem", False):
        fora |= vision.CLASSES_BAGAGEM
    if not getattr(cam, "detecta_animal", False):
        fora |= vision.CLASSES_ANIMAL
    return fora


class Inferencia:
    """Laço que percorre as câmeras on-line aplicando os modelos."""

    def __init__(self, intervalo: float = 3.0, janela_track: float = 45.0) -> None:
        self.intervalo = intervalo
        self.janela_track = janela_track
        self._task: Optional[asyncio.Task] = None
        self._visto: Dict[Tuple[str, str], float] = {}
        self._rostos: Dict[str, list] = {}
        # Quem é cada corpo em cena, por câmera: track_id -> (id_biometrico,
        # instante). É o que permite a zona restrita liberar quem tem
        # autorização, e vem do facial — que roda depois da análise de ação,
        # então a resposta é sempre a da volta anterior.
        self._identidades: Dict[str, Dict[int, Tuple[str, float]]] = {}
        self.gerados = 0
        # Quanto uma volta completa leva de fato. Cresce com o parque, e é o
        # que o mosaico precisa saber para não apagar a caixa cedo demais.
        self.ciclo_real = intervalo
        self.ultimo_total = 0
        # Um analisador por câmera: as trilhas são a memória daquela cena, e
        # misturá-las entre câmeras faria a mesma pessoa "saltar" de uma para
        # outra e disparar corrida.
        self._acoes: Dict[str, vision.AnalisadorAcoes] = {}
        # E um rastreio por câmera, pelo mesmo motivo: a identidade de um
        # objeto vale dentro da cena dele. É o que dá `track_id` aos objetos
        # onde a análise de ação está ligada — sem ele o analisador descarta
        # tudo e nenhuma ação chega a ser avaliada.
        self._rastreios: Dict[str, vision.RastreioPorPosicao] = {}
        # Ocupação confirmada de cada sala vigiada, e a contagem que está
        # esperando confirmação. É o que transforma "há gente na sala" em
        # "alguém entrou". A queda também é acompanhada, mas em silêncio: sem
        # baixar a referência, a entrada seguinte não seria vista como
        # aumento.
        self._ocupacao: Dict[str, int] = {}
        self._ocupacao_pendente: Dict[str, Tuple[int, int]] = {}  # (contagem, vezes)
        # Último alerta de fogo/fumaça por câmera: chama não se apaga entre
        # duas visitas, e sem represa viraria uma ocorrência por volta.
        self._ultima_chama: Dict[Tuple[str, str], float] = {}
        # Orçamento da rajada: quantas já saíram nesta volta e quando cada
        # câmera foi atendida pela última vez.
        self._rajadas_na_volta = 0
        self._ultima_rajada: Dict[str, float] = {}

    async def iniciar(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        log.info(
            "Inferência ativa (ciclo ~%.1fs, objeto=%s, face=%s)",
            self.intervalo, vision.yolo_disponivel(), face_engine.modelos_disponiveis(),
        )

    async def parar(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        # Deixa a API subir antes de disputar CPU com ela.
        await asyncio.sleep(4)
        while True:
            inicio = time.monotonic()
            try:
                await self._rodada()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha na rodada de inferência: %s", exc)
            # Dorme o que falta para o intervalo, não o intervalo inteiro: as
            # câmeras são percorridas em série, e somar a espera ao tempo da
            # rodada fazia o ciclo crescer junto com o parque. Com nove
            # câmeras o intervalo pedido de 3s virava 8,5s reais, e a caixa
            # expirava no vídeo antes do quadro seguinte chegar.
            gasto = time.monotonic() - inicio
            anterior = self.ciclo_real
            medido = max(gasto, self.intervalo)
            # Sobe na hora, desce devagar. A validade publicada vale para a
            # volta seguinte, e uma volta pode ser bem mais lenta que a
            # anterior quando a máquina enche: medido de fato, o ciclo pulou
            # de 28s para 169s, e as caixas emitidas com a validade dos 28s
            # morreram deixando quase dois minutos de vídeo sem marcação.
            # Encolher aos poucos custa uma caixa parada por alguns segundos
            # depois que o parque alivia; o contrário custa o vídeo limpo.
            self.ciclo_real = max(medido, anterior * 0.7 + medido * 0.3)
            # Em info, e não debug, porque é o número que explica a sobreposição
            # ficar parada: a caixa de uma câmera só se renova quando o worker
            # volta a ela. Logar só a mudança relevante evita uma linha por
            # volta sem esconder o parque passando do aceitável.
            if abs(self.ciclo_real - anterior) > max(1.0, anterior * 0.25):
                log.info("Volta completa em %.1fs (%d câmeras)",
                         self.ciclo_real, self.ultimo_total)
            await asyncio.sleep(max(0.0, self.intervalo - gasto))

    async def _rodada(self) -> None:
        """Uma sessão de banco por câmera, nunca uma para a volta inteira.

        O SQLite aceita um escritor de cada vez. Com a volta toda dentro de um
        `db_context`, a transação de escrita ficava aberta do começo ao fim —
        e a volta passou de 5s para mais de 80s quando a varredura em
        ladrilhos entrou. Qualquer requisição que também escrevesse ficava na
        fila até o `busy_timeout`, que é de 30 segundos.

        Isso chegou à tela como "a ficha biométrica demora e fica carregando":
        abrir uma ficha grava a consulta na trilha de auditoria, então ela é
        uma escritora. Medido, 1 em cada 25 chamadas parava 28–33s, enquanto
        um arquivo estático — que não toca no banco — nunca passou de 0,12s.

        As câmeras são lidas numa sessão curta e usadas destacadas depois:
        `expire_on_commit=False` mantém os atributos já carregados.
        """
        self._rajadas_na_volta = 0
        async with db_context() as db:
            cameras = (
                await db.execute(
                    select(Camera).where(Camera.status == StatusCamera.ONLINE)
                )
            ).scalars().unique().all()
            self.ultimo_total = len(cameras)

        for cam in cameras:
            if not cam.rtsp_url:
                continue
            try:
                async with db_context() as db:
                    await self._processar(db, cam)
            except Exception as exc:  # noqa: BLE001
                log.warning("Inferência falhou em %s: %s", cam.codigo, exc)

    def _rosto_recente(self, camera: str, embedding) -> Tuple[bool, list]:
        """Mesmo rosto que já entrou há pouco naquela câmera.

        Compara o vetor, não a posição: é o que distingue duas pessoas no
        mesmo ponto de uma pessoa parada. O limiar é bem mais alto que o da
        identificação — aqui não se pergunta *quem* é, e sim se é a mesma
        captura de instantes atrás, então o parecido de menos tem de passar.

        A janela é curta porque passar de novo é um evento de verdade: quem
        volta ao saguão depois de um tempo deve constar outra vez.

        Devolve `(repetido, entrada)`. A entrada é `[vetor, instante, nome,
        pessoa_id, id_biometrico]`, e a identidade fica guardada ali porque
        quem repete não volta a passar pela busca 1:N: sem lembrá-la, ela
        apareceria só na volta em que o registro nasce e sumiria nas
        seguintes, com a pessoa ainda em cena. O `pessoa_id` acompanha o nome
        porque é ele que liga o corpo ao rosto no banco, e a volta que repete
        tem corpo igual; o `id_biometrico` acompanha os dois porque é por ele
        que a zona restrita diz quem tem autorização para estar ali.
        """
        agora = time.monotonic()
        recentes = [
            e for e in self._rostos.get(camera, [])
            if agora - e[1] < settings.INFERENCIA_ROSTO_JANELA_S
        ]
        for entrada in recentes:
            if face_engine.similaridade_cosseno(entrada[0], embedding) >= settings.INFERENCIA_ROSTO_IGUAL:
                self._rostos[camera] = recentes
                return True, entrada
        nova = [embedding, agora, None, None, None]
        recentes.append(nova)
        # Guarda só as últimas: o saguão cheio não pode fazer a lista crescer
        # sem teto, e comparar contra as mais recentes já resolve.
        self._rostos[camera] = recentes[-24:]
        return False, nova

    @staticmethod
    def _corpo_do_rosto(bbox_rosto, corpos: list) -> Optional[dict]:
        """Qual corpo em cena contém este rosto.

        Mede pela interseção sobre a menor área — o mesmo critério que separa
        duplicata de pessoa distinta no detector de objeto. Havendo mais de um
        corpo que contenha o rosto, vence o **menor**: num corredor a caixa de
        quem está à frente encosta na de quem está atrás, e a menor é sempre a
        mais específica.
        """
        area = lambda b: max(1, (b[2] - b[0]) * (b[3] - b[1]))
        dentro = [c for c in corpos
                  if vision.ios(tuple(bbox_rosto), tuple(c["bbox"]))
                  >= settings.FACE_CORPO_CONTENCAO]
        return min(dentro, key=lambda c: area(c["bbox"])) if dentro else None

    @staticmethod
    def _rotular(caixa_rosto: dict, nome: str, nomes_no_quadro: list) -> None:
        """Um nome por pessoa na tela, sempre na caixa do rosto.

        Rosto e corpo da mesma pessoa são duas caixas, e escrever o nome nas
        duas o repetia empilhado no vídeo. Fica com o **rosto** porque é ele
        que identifica: é ali que a leitura aconteceu, e é ali que quem
        confere vai olhar para decidir se o sistema acertou. A caixa do corpo
        segue existindo e segue recebendo a identidade no banco — o que ela
        não leva é a tarja.

        A troca tem preço, assumido: quem foi identificado antes e está de
        costas agora aparece só como "pessoa", porque não há rosto em cena
        para rotular.

        Sem nome não há tarja: a caixa volta a ser a de qualquer um em cena,
        que é o que ela de fato sabe dizer do BIO-N.
        """
        if not nome:
            return
        caixa_rosto["pessoa_nome"] = nome
        if nome not in nomes_no_quadro:
            nomes_no_quadro.append(nome)

    def _marcar_corpo(self, cena: dict, bbox_rosto, corpos: list,
                      id_biometrico=None) -> None:
        """Anota de quem é o corpo que contém este rosto — só em memória.

        Não registra nada e não rotula a caixa do corpo no vídeo. O sistema
        reconhece **pelo rosto**; o corpo não identifica ninguém, e a
        identidade dele não vira linha no banco nem consta na ficha da
        pessoa. Isto aqui existe por um motivo só: a zona restrita precisa
        saber quem está dentro dela, e o que ela recebe do analisador é um
        `track_id` de corpo. Sem a ponte, autorizar alguém a entrar numa área
        seria impossível — todo mundo seria desconhecido.

        A anotação vale uma volta e morre com a trilha. A análise de ação
        roda antes do facial na mesma volta, então a resposta é sempre a da
        volta anterior.

        O corpo sai da lista depois de casado: dois rostos no mesmo corpo
        seriam duas pessoas na mesma caixa.
        """
        alvo = self._corpo_do_rosto(bbox_rosto, corpos)
        if alvo is None:
            return
        marca = alvo["caixa"].get("track_id")
        if id_biometrico and marca is not None:
            self._identidades.setdefault(cena["cam"].codigo, {})[marca] = (
                id_biometrico, time.monotonic())
        corpos.remove(alvo)

    def _confiavel(self, obj) -> bool:
        """Descarta o que o modelo classifica sem base suficiente.

        Confiança baixa e caixa pequena são de onde vem a troca de classe: o
        modelo acerta que há *algo* ali e erra o que é. Um registro com rótulo
        errado é pior que registro nenhum — manda o operador atrás da coisa
        errada e contamina a estatística por classe.
        """
        if obj.confianca < settings.INFERENCIA_CONFIANCA_MIN:
            return False
        x1, y1, x2, y2 = obj.bbox
        return min(x2 - x1, y2 - y1) >= settings.INFERENCIA_LADO_MIN_PX

    def _repetido(self, chave: Tuple[str, str]) -> bool:
        """Mesma trilha dentro da janela — já foi registrada."""
        agora = time.monotonic()
        anterior = self._visto.get(chave)
        if anterior is not None and (agora - anterior) < self.janela_track:
            return True
        self._visto[chave] = agora
        # Poda o que envelheceu, senão o dicionário cresce sem teto.
        if len(self._visto) > 4000:
            corte = agora - self.janela_track
            self._visto = {k: v for k, v in self._visto.items() if v > corte}
        return False

    async def _registrar_acoes(self, db, cam: Camera, objetos, quadro,
                               largura: int, altura: int, agora) -> None:
        """Eventos comportamentais a partir das trilhas desta câmera.

        O que sai daqui depende de quantas vezes por minuto a câmera é vista.
        Aglomeração é contada num quadro só e sai correta; permanência e
        abandono precisam da trilha sobreviver entre uma volta e a seguinte, e
        é por isso que o tempo de vida dela acompanha o ciclo medido. Já
        corrida, queda e contrafluxo medem movimento entre quadros próximos —
        com o parque em série, dois quadros da mesma câmera estão a dezenas de
        segundos de distância, e nessa escala o movimento não se mede. Essas
        três só valem com a câmera analisada de forma contínua.
        """
        from app.routers.deteccoes import processar_deteccao

        analisador = self._acoes.get(cam.codigo)
        if analisador is None:
            analisador = vision.AnalisadorAcoes()
            self._acoes[cam.codigo] = analisador
        # A trilha precisa durar mais que o intervalo entre duas visitas, senão
        # é descartada antes de a câmera ser vista de novo e nada acumula.
        analisador.ttl_trilha = max(8.0, self.ciclo_real * 2.5)
        # Relido a cada volta, e não guardado na criação: a seleção muda pela
        # tela de configuração, e o analisador vive enquanto o processo vive.
        # Sem reler, desmarcar uma ação só valeria no próximo reinício.
        analisador.acoes_ativas = set(cam.acoes_ativas or ())
        # Pelo mesmo motivo: a janela de horário é editável na ficha da câmera.
        analisador.horario_inicio = cam.horario_inicio
        analisador.horario_fim = cam.horario_fim
        # Quantas pessoas juntas são aglomeração aqui. Nulo cai no padrão do
        # parque — o mesmo acordo do horário.
        analisador.n_aglomeracao = (cam.limite_aglomeracao
                                    or settings.ACAO_AGLOMERACAO_N)
        # Quem é cada corpo, para a zona restrita liberar quem tem
        # autorização. Vence junto com a trilha que o rastreio mantém: uma
        # identidade mais velha que isso estaria falando de outra pessoa que
        # herdou o mesmo número.
        agora_mono = time.monotonic()
        conhecidos = self._identidades.get(cam.codigo, {})
        for marca in [m for m, (_, quando) in conhecidos.items()
                      if agora_mono - quando > analisador.ttl_trilha]:
            conhecidos.pop(marca, None)
        analisador.identidades = {m: bio for m, (bio, _) in conhecidos.items()}

        try:
            eventos = analisador.atualizar(objetos, largura, altura,
                                           zonas=self._zonas(cam))
        except Exception as exc:  # noqa: BLE001
            log.warning("Análise de ações falhou em %s: %s", cam.codigo, exc)
            return

        # Fogo e fumaça não saem das trilhas: são assinatura da imagem, e têm
        # detector próprio. Entram aqui para seguirem pelo mesmo caminho de
        # registro e alerta que as demais ações.
        #
        # As caixas de pessoa vão junto: mão em movimento borra e perde textura
        # exatamente como fumaça, e foi assim que a Produção alarmou incêndio
        # duas vezes. O detector de objetos já sabe onde está a gente.
        pessoas = [o.bbox for o in objetos if o.classe == "pessoa"]
        eventos += await self._eventos_de_chama(cam, quadro, pessoas)

        for ev in eventos:
            caixa = None
            if ev.bbox:
                x1, y1, x2, y2 = ev.bbox
                caixa = [x1 / largura, y1 / altura,
                         (x2 - x1) / largura, (y2 - y1) / altura]

            # Sem a imagem, a linha do registro diz que algo aconteceu e não
            # deixa conferir o quê — e evento comportamental é indício, que
            # existe justamente para alguém olhar. Aglomeração não tem caixa,
            # porque é da cena inteira: ali o recorte é o quadro todo.
            recorte = ev.bbox or (0, 0, largura, altura)
            jpeg = await asyncio.to_thread(
                video.recortar_bgr, quadro, recorte, 0.25 if ev.bbox else 0.0
            )

            await processar_deteccao(db, DeteccaoIn(
                tipo=TipoDeteccao.ACAO,
                camera_codigo=cam.codigo,
                ocorrido_em=agora,
                classe=ev.classe,
                confianca=round(float(ev.confianca), 4),
                duracao_s=ev.duracao_s,
                envolvidos=ev.envolvidos,
                # Qual área disparou. Sem isto o registro de área restrita
                # diz que alguém entrou em lugar proibido, mas não em qual.
                zona_nome=ev.zona_nome,
                bbox=caixa,
                track_id=ev.track_id,
                snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
            ), origem="inferencia")
            self.gerados += 1
            log.info("%s: ação detectada — %s (%d envolvidos)",
                     cam.codigo, ev.rotulo, ev.envolvidos)

    async def _vigiar_sala(self, db, cam: Camera, objetos, det_entrada,
                           quadro=None, nomes=()) -> None:
        """Alerta quando a ocupação da sala muda — não enquanto ela dura.

        A contagem oscila: medido em duas capturas da mesma sala cheia, ela
        variou de 12 para 13 e de 10 para 12 sem ninguém entrar. Reagir a cada
        oscilação produziria "saiu" seguido de "entrou" a cada volta.

        Daí a confirmação ser **assimétrica**, e não um número só:

        * **Entrada sai na hora.** Contagem que sobe raramente é ruído — o
          piso de confiança do registro já derrubou o que era duvidoso — e um
          aviso de entrada atrasado não serve para nada.
        * **Saída espera confirmação.** Contagem que cai costuma ser alguém
          que o modelo perdeu num quadro: de costas, atrás de uma cadeira,
          encoberto por quem passou na frente. Dizer "a sala esvaziou" quando
          ainda há gente lá é o erro caro dos dois.
        """
        from app.models import Deteccao
        from app.routers.deteccoes import alertar_presenca

        atual = sum(1 for o in objetos
                    if o.classe == "pessoa" and self._confiavel(o))
        antes = self._ocupacao.get(cam.codigo)

        if antes is None:                      # primeira volta nesta câmera
            self._ocupacao[cam.codigo] = atual
            return
        if atual == antes:
            self._ocupacao_pendente.pop(cam.codigo, None)
            return

        # A imagem do alerta é o quadro inteiro desta volta: o que ele afirma
        # é sobre a sala, e é a sala que quem revisa precisa ver. Não depende
        # de ter havido detecção — ela é deduplicada por 30 minutos, e era
        # por isso que a segunda entrada da mesma pessoa saía sem captura.
        jpeg = None
        if quadro is not None:
            altura, largura = quadro.shape[:2]
            jpeg = await asyncio.to_thread(
                video.recortar_bgr, quadro, (0, 0, largura, altura), 0.0
            )

        if atual > antes:
            det = None
            if det_entrada:
                det = (await db.execute(
                    select(Deteccao).where(Deteccao.codigo == det_entrada)
                )).scalar_one_or_none()
            await alertar_presenca(db, cam, atual - antes, atual, det, jpeg,
                                   nomes=list(nomes))
            log.info("%s: entrada — %d pessoa(s), agora %d na sala",
                     cam.codigo, atual - antes, atual)
            self._ocupacao[cam.codigo] = atual
            self._ocupacao_pendente.pop(cam.codigo, None)
            return

        # Caiu: a ocupação é atualizada, mas **não vira alerta**.
        #
        # Quem opera pediu só as entradas: "a sala ficou vazia" não é
        # ocorrência, é o fim de uma. Ainda assim a queda precisa ser
        # registrada aqui — sem baixar a referência, a sala esvaziando e
        # enchendo de novo iria de 1 para 0 e de volta para 1, e a entrada
        # seguinte não seria vista como aumento. O silêncio é do alerta, não
        # da contagem.
        #
        # A confirmação continua valendo pelo mesmo motivo de antes: uma
        # queda de uma volta só costuma ser alguém que o modelo perdeu, e
        # baixar a referência cedo demais faria a próxima leitura correta
        # parecer uma entrada que nunca houve.
        contagem, vezes = self._ocupacao_pendente.get(cam.codigo, (atual, 0))
        vezes = vezes + 1 if contagem == atual else 1
        if vezes < settings.ALERTA_PRESENCA_CONFIRMA_SAIDA:
            self._ocupacao_pendente[cam.codigo] = (atual, vezes)
            return

        log.info("%s: saída — %d pessoa(s), restam %d (sem alerta, por projeto)",
                 cam.codigo, antes - atual, atual)
        self._ocupacao[cam.codigo] = atual
        self._ocupacao_pendente.pop(cam.codigo, None)

    async def _eventos_de_chama(self, cam: Camera, quadro, pessoas=()) -> list:
        """Fogo e fumaça, por assinatura de imagem.

        Duas etapas, e a ordem importa para o custo. A primeira é uma varredura
        de cor sobre o quadro que já está em mãos — operação de matriz, barata
        o bastante para rodar em toda visita. Só quando ela acusa algo é que se
        pedem os quadros extras, e é a comparação entre eles que decide: chama
        oscila, cone de sinalização não.

        Sem essa separação, toda câmera que vigia fogo pagaria quatro leituras
        por visita para, quase sempre, concluir que não há nada laranja em
        cena.
        """
        ativas = set(cam.acoes_ativas or ())
        querem = ativas & {"fogo", "fumaca"}
        if not querem or quadro is None:
            return []
        if not await asyncio.to_thread(chama.ha_candidato, quadro):
            return []

        quadros = [quadro]
        for _ in range(max(0, settings.CHAMA_QUADROS - 1)):
            await asyncio.sleep(settings.CHAMA_INTERVALO_S)
            extra = await asyncio.to_thread(
                video.quadro_bgr, cam.codigo, cam.rtsp_url, True)
            if extra is None or extra.shape != quadro.shape:
                return []
            quadros.append(extra)

        try:
            achados = await asyncio.to_thread(chama.analisar, quadros, pessoas)
        except Exception as exc:  # noqa: BLE001
            log.warning("Detecção de fogo/fumaça falhou em %s: %s", cam.codigo, exc)
            return []

        agora = time.time()
        eventos = []
        for c in achados:
            if c.classe not in querem:
                continue
            # Fogo não se apaga entre uma visita e a seguinte: sem represa, a
            # mesma chama viraria uma ocorrência por volta, e o operador
            # receberia dezenas de alertas do mesmo incêndio.
            chave = (cam.codigo, c.classe)
            if agora - self._ultima_chama.get(chave, 0.0) < settings.ACAO_COOLDOWN_S:
                continue
            self._ultima_chama[chave] = agora
            eventos.append(vision.EventoAcao(
                classe=c.classe,
                rotulo=vision.ROTULO_ACAO.get(c.classe, c.classe),
                confianca=float(c.confianca),
                severidade=vision.SEVERIDADE_ACAO.get(c.classe, 4),
                duracao_s=0, envolvidos=0, bbox=c.bbox,
            ))
        if eventos:
            log.info("%s: %s", cam.codigo,
                     ", ".join(f"{e.classe} ({e.confianca:.0%})" for e in eventos))
        return eventos

    @staticmethod
    def _quer_rajada(cam: Camera) -> bool:
        """Esta câmera vigia alguma ação que mede movimento?"""
        ativas = set(cam.acoes_ativas or ())
        return bool(ativas & vision.ACOES_CONTINUAS)

    def _tem_cota_rajada(self, cam: Camera) -> bool:
        """Teto de rajadas por volta, com rodízio justo entre as câmeras.

        Este é o limite duro do orçamento. Medido com a rajada solta, a volta
        do parque passou de 14–36s para 46s: análise contínua de movimento em
        24 câmeras não cabe em 8 núcleos, e nenhum ajuste de resolução muda
        isso — muda só o quanto não cabe.

        Então a rajada vira **amostragem**: por volta, no máximo
        `ACAO_CONTINUA_MAX_POR_VOLTA` câmeras a recebem, e a vez é de quem
        está esperando há mais tempo. Uma câmera com movimento contínuo é
        atendida a cada poucas voltas em vez de nunca, e o parque inteiro não
        degrada por causa dela.

        O custo honesto disto: corrida e queda duram segundos, e entre duas
        rajadas da mesma câmera pode passar um evento. É amostragem, não
        vigilância contínua — e a ficha da ação diz isso.
        """
        if self._rajadas_na_volta >= settings.ACAO_CONTINUA_MAX_POR_VOLTA:
            return False
        # Quem acabou de ser atendida cede a vez. Sem isto, as primeiras
        # câmeras da ordem do parque consumiriam a cota em toda volta e as
        # últimas nunca seriam vistas — a ordem é fixa.
        agora = time.monotonic()
        espera = max(self.ciclo_real * 2.0, 10.0)
        if agora - self._ultima_rajada.get(cam.codigo, 0.0) < espera:
            return False
        self._rajadas_na_volta += 1
        self._ultima_rajada[cam.codigo] = agora
        return True

    async def _ha_movimento(self, cam: Camera, quadro) -> bool:
        """A cena mudou o bastante para valer uma rajada?

        Medido no parque: em expediente, 16 a 20 das 26 câmeras têm pessoa em
        cena, e a rajada custa ~2s cada — quarenta segundos somados a uma volta
        que leva de catorze a trinta e seis. Mas quase toda essa gente está
        *sentada*: corrida, queda e briga exigem movimento, e a sala de
        trabalho não tem nenhum.

        Este teste é uma leitura de quadro e uma subtração de matriz — ordem
        de dezenas de milissegundos contra os dois segundos que ele evita.
        """
        import numpy as np

        anterior = await asyncio.to_thread(
            video.quadro_bgr, cam.codigo, cam.rtsp_url, True)
        if anterior is None or anterior.shape != quadro.shape:
            # Sem comparação possível, segue para a rajada: perder um evento
            # custa mais que gastar dois segundos.
            return True

        def medir():
            a = anterior[::4, ::4].astype(np.int16)
            b = quadro[::4, ::4].astype(np.int16)
            dif = np.abs(a - b).max(axis=2)
            return float(np.count_nonzero(dif > settings.MOVIMENTO_DELTA)) / dif.size

        try:
            fracao = await asyncio.to_thread(medir)
        except Exception:                                    # noqa: BLE001
            return True
        return fracao >= settings.MOVIMENTO_FRACAO_MIN

    async def _rajada_continua(self, cam: Camera, rastreio, fora: set) -> None:
        """Vários quadros seguidos, só para alimentar as trilhas de movimento.

        Nada daqui vira registro nem caixa na tela: o que estes quadros
        produzem é *trajetória*. O analisador precisa de cinco posições para
        calcular velocidade e oito para queda, e uma visita do rodízio entrega
        uma. Sem a rajada, `corrida`, `queda` e `contrafluxo` ficavam ligáveis
        e permanentemente mudas — era o que a ficha chamava de "exige análise
        contínua".

        Roda num lado reduzido e sem varredura em ladrilhos: aqui interessa
        onde o corpo está, não quantos corpos distantes existem. Medido nesta
        máquina, a diferença é de 8,3s para 2,0s por câmera.
        """
        analisador = self._acoes.get(cam.codigo)
        if analisador is None:
            return
        zonas = self._zonas(cam)
        for _ in range(max(0, settings.ACAO_CONTINUA_QUADROS - 1)):
            await asyncio.sleep(settings.ACAO_CONTINUA_INTERVALO_S)
            extra = await asyncio.to_thread(
                video.quadro_bgr, cam.codigo, cam.rtsp_url, True)
            if extra is None:
                return
            objs = await asyncio.to_thread(
                vision.detectar_objetos, extra, cam.limiar_objeto, False, fora,
                "nunca", settings.ACAO_CONTINUA_IMGSZ)
            objs = await asyncio.to_thread(rastreio.atribuir, objs)
            # Os eventos desta passada são descartados de propósito: quem
            # decide alerta é a análise da visita, com o quadro cheio. Aqui só
            # se acumula a trajetória que ela vai consultar.
            await asyncio.to_thread(
                analisador.atualizar, objs, extra.shape[1], extra.shape[0], zonas)

    @staticmethod
    def _fora_das_zonas_ignoradas(objetos: list, cam: Camera, quadro) -> list:
        """Tira o que cai numa região marcada como "ignorar".

        O detector confunde objeto fixo com pessoa, e não há limiar que
        resolva: o hidrante da Garagem sai com 0,9 de confiança, acima de
        qualquer piso que ainda deixe passar gente de verdade. O que o
        distingue não é a aparência — é saber que *naquele pedaço de parede*
        não passa ninguém, e isso quem sabe é quem conhece a câmera.

        Deliberadamente **não** se suprime "pessoa que não se mexe". Seria o
        filtro óbvio e destruiria a permanência prolongada, cuja razão de
        existir é alertar sobre quem fica parado — e numa portaria apagaria
        o vigia no posto.

        O critério é o pé da caixa, não o centro: é por onde a pessoa toca o
        chão, e é o que decide de que lado de uma borda ela está. Pelo centro
        um corpo alto rente à zona ficaria metade fora.
        """
        zonas = [z for z in (cam.zonas or [])
                 if (z.regras or {}).get("ignorar") and (z.pontos or [])]
        if not zonas or not objetos:
            return objetos

        altura, largura = quadro.shape[0], quadro.shape[1]
        sobraram = []
        for obj in objetos:
            x1, y1, x2, y2 = obj.bbox
            pe = ((x1 + x2) / 2 / largura, y2 / altura)
            if any(vision.ponto_em_poligono(pe, z.pontos) for z in zonas):
                continue
            sobraram.append(obj)
        return sobraram

    @staticmethod
    def _zonas(cam: Camera) -> list:
        """Zonas da câmera no formato que o analisador espera."""
        # O nome vai junto: o analisador o escreve no alerta ("Pessoa em área
        # restrita — Vaga proibida"), e sem ele a frase chegava ao operador
        # com a zona em branco, mandando procurar uma região sem dizer qual.
        return [
            {"nome": z.nome or "",
             "tipo": getattr(z.tipo, "value", z.tipo),
             "pontos": z.pontos or [],
             "regras": z.regras or {}}
            for z in (cam.zonas or [])
        ]

    async def _processar(self, db, cam: Camera) -> None:
        from app.routers.deteccoes import processar_deteccao

        # Resolução nativa: no fluxo reduzido do mosaico o rosto chega pequeno
        # demais para qualquer piso de identificação.
        quadro = await asyncio.to_thread(
            video.quadro_bgr, cam.codigo, cam.rtsp_url, True
        )
        if quadro is None:
            # Sem quadro não há o que afirmar sobre a cena — e sair calado
            # deixa a sobreposição anterior de pé até a validade vencer, que
            # é `ciclo_real * 2`: de 28 a 72 segundos de caixa sobre gente
            # que já saiu. O rastro não vem do rastreador, vem daqui.
            #
            # Publicar vazio apaga a marcação e assume a ignorância. Numa
            # câmera que oscila isso faz a caixa piscar por uma volta; a
            # alternativa é afirmar presença sem imagem que a sustente, que
            # num sistema de vigilância é o erro pior.
            await publicar("quadro_ao_vivo", {
                "camera_codigo": cam.codigo,
                "caixas": [],
                "validade_s": round(self.ciclo_real * 2.0, 1),
            })
            return
        altura, largura = quadro.shape[:2]
        agora = datetime.now(timezone.utc)

        caixas_do_quadro = []
        # Os corpos desta cena, para o rosto identificado achar o seu. Guarda
        # a caixa por referência (é nela que o nome entra) e o código do
        # registro, que é o que recebe a identidade no banco.
        corpos_no_quadro: list[dict] = []
        # Quem o facial reconheceu nesta cena. Alimenta o alerta de sala, que
        # antes só sabia contar cabeças.
        nomes_no_quadro: list[str] = []
        # Declarada aqui porque a vigilância da sala passou a ocorrer depois
        # dos rostos, fora do bloco que a preenche: sem câmera com análise de
        # objeto o nome não existiria na hora de consultar.
        objetos: list = []
        # `None` enquanto não se sabe: a câmera pode não ter análise de objeto,
        # e aí não há o que consultar antes de procurar rosto.
        tem_pessoa = None
        codigo_pessoa = None     # recorte que o alerta de entrada vai levar

        if cam.analitico_objeto and vision.yolo_disponivel():
            # SEM rastreio. O ByteTrack do ultralytics guarda estado entre
            # chamadas, e aqui o mesmo modelo atende as 24 câmeras em rodízio:
            # cada quadro chega como um corte de cena, nada associa com a
            # trilha anterior, e o rastreador descarta o que não associou.
            #
            # Medido nos mesmos quadros, três voltas idênticas: com rastreio a
            # Produção 02 relatava ZERO pessoas onde há 11, e a Produção, 3 de
            # 5. As câmeras que não perdiam nada eram as que calhavam de vir
            # logo depois de uma cena parecida.
            #
            # O custo é o `track_id`: a deduplicação do registro cai para a
            # chave por posição, que já existe logo abaixo, e a análise de
            # ação deixa de emitir (ela ignora objeto sem trilha). Perder a
            # trilha vale menos que perder onze pessoas da sobreposição.
            fora = _classes_barradas(cam)
            objetos = await asyncio.to_thread(
                vision.detectar_objetos, quadro, cam.limiar_objeto, False, fora
            )

            # Capacete e arma de fogo não existem no COCO: quando a câmera os
            # pede e há modelo configurado, vêm de uma passada à parte. O custo
            # é por câmera, não do parque — o orçamento de CPU aqui é apertado
            # e uma passada extra em 24 câmeras não caberia.
            pedidos = [c for c in ("capacete", "arma")
                       if getattr(cam, f"detecta_{c}", False) and extras.configurado(c)]
            if pedidos:
                achados = await asyncio.to_thread(extras.detectar, quadro, pedidos)
                objetos.extend(
                    vision.ObjetoDetectado(classe, conf, caixa, None)
                    for classe, conf, caixa in achados
                )

            objetos = self._fora_das_zonas_ignoradas(objetos, cam, quadro)

            tem_pessoa = any(o.classe == "pessoa" for o in objetos)

            if cam.analitico_acao:
                # A identidade é dada aqui, e só onde a análise de ação está
                # ligada: ela muda a chave de deduplicação do registro, e não
                # há por que mexer nisso numa câmera que não analisa ação.
                rastreio = self._rastreios.get(cam.codigo)
                if rastreio is None:
                    rastreio = vision.RastreioPorPosicao()
                    self._rastreios[cam.codigo] = rastreio
                # O objeto precisa sobreviver de uma visita até a seguinte,
                # como a trilha — senão a identidade se perde no caminho e
                # nada acumula.
                rastreio.ttl_s = max(120.0, self.ciclo_real * 3)
                objetos = await asyncio.to_thread(rastreio.atribuir, objetos)

                # Corrida, queda e contrafluxo comparam posições entre quadros
                # próximos. O quadro único da visita não dá movimento nenhum,
                # então esta câmera recebe uma rajada antes de analisar — e só
                # ela, porque a rajada custa segundos.
                # Só com gente em cena. Medido no parque: 25 das 26 câmeras
                # vigiam alguma ação de movimento, e a rajada custa ~2s cada —
                # cinquenta segundos somados a uma volta que leva de catorze a
                # trinta e seis. Mas corredor, arquivo e datacenter estão
                # vazios quase sempre, e quadro sem pessoa não tem trajetória
                # nenhuma para medir: a rajada ali seria gasto pelo gasto.
                if (tem_pessoa
                        and self._quer_rajada(cam)
                        and self._tem_cota_rajada(cam)
                        and await self._ha_movimento(cam, quadro)):
                    await self._rajada_continua(cam, rastreio, fora)

                await self._registrar_acoes(db, cam, objetos, quadro,
                                            largura, altura, agora)

            for obj in objetos:
                # A sobreposição mostra o que a câmera está vendo agora, no
                # limiar que o operador configurou nela. Sai antes das podas:
                # a caixa acompanha o objeto enquanto ele está em cena, e o
                # registro nasce uma vez por passagem — o carro parado segue
                # enquadrado no vídeo sem voltar à lista a cada ciclo.
                # Guardada por referência: a placa só se conhece depois da
                # leitura, lá embaixo, e é nesta mesma caixa que ela entra.
                caixa_obj = {
                    "classe": obj.classe,
                    "confianca": round(float(obj.confianca), 4),
                    "bbox": obj.bbox_normalizado(largura, altura),
                    "track_id": obj.track_id,
                }
                caixas_do_quadro.append(caixa_obj)
                corpo = None
                if obj.classe == "pessoa":
                    corpo = {"bbox": obj.bbox, "caixa": caixa_obj, "codigo": None}
                    corpos_no_quadro.append(corpo)

                # O piso extra vale só para o registro: o que fica gravado com
                # rótulo errado manda o operador atrás da coisa errada, e a
                # estatística por classe passa a mentir. No vídeo, o mesmo
                # objeto duvidoso está à vista de quem julga.
                if not self._confiavel(obj):
                    continue

                # Sem trilha, a posição serve de identidade aproximada. A
                # célula é grossa de propósito: fina demais, o mesmo objeto
                # andando alguns pixels vira uma marca nova e volta ao
                # registro — justamente o que a trilha existe para evitar.
                marca = str(obj.track_id) if obj.track_id is not None else (
                    f"{obj.classe}:{obj.bbox[0] // 160}:{obj.bbox[1] // 160}"
                )
                if self._repetido((cam.codigo, f"obj:{marca}")):
                    continue

                # A placa é lida no recorte que o detector de objeto já
                # delimitou: procurá-la no quadro inteiro seria desperdiçar
                # essa delimitação, e ela é o que há de menor em cena.
                texto_placa = ""
                if obj.classe in vision.VEICULOS:
                    x1, y1, x2, y2 = obj.bbox
                    recorte_bgr = quadro[max(0, y1):y2, max(0, x1):x2]
                    lida = await asyncio.to_thread(placa.ler, recorte_bgr)
                    if lida:
                        texto_placa, conf_placa = lida
                        caixa_obj["placa"] = texto_placa
                        log.info("%s: placa %s lida em %s (%.0f%%)",
                                 cam.codigo, texto_placa, obj.classe, conf_placa * 100)

                jpeg = await asyncio.to_thread(video.recortar_bgr, quadro, obj.bbox)
                registro = await processar_deteccao(db, DeteccaoIn(
                    tipo=TipoDeteccao.OBJETO,
                    camera_codigo=cam.codigo,
                    ocorrido_em=agora,
                    classe=obj.classe,
                    confianca=round(float(obj.confianca), 4),
                    bbox=obj.bbox_normalizado(largura, altura),
                    track_id=obj.track_id,
                    placa=texto_placa,
                    snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
                ), origem="inferencia")
                self.gerados += 1
                if corpo is not None:
                    corpo["codigo"] = registro.get("codigo")
                # Guarda a primeira pessoa da volta para o alerta de entrada
                # levar um recorte: alerta de sala sem imagem obriga quem
                # revisa a ir procurar a captura na lista.
                if obj.classe == "pessoa" and codigo_pessoa is None:
                    codigo_pessoa = registro.get("codigo")

        # Fecha a transação antes da parte lenta. A extração de rosto leva
        # perto de um segundo, e segurar a trava de escrita do SQLite durante
        # ela põe na fila quem for gravar nesse meio tempo — inclusive a
        # trilha de auditoria de quem abre uma ficha biométrica.
        await db.commit()

        # Rosto só existe onde há pessoa, e procurar por um num quadro vazio
        # custava 0,97s contra 0,17s do detector de objeto — com o parque em
        # série, era 85% da volta gasto em corredores e garagens sem ninguém.
        # Pular o quadro vazio é o que mantém a sobreposição acompanhando a
        # cena. Quando a câmera não tem análise de objeto, `tem_pessoa` fica
        # em None e o rosto é procurado como antes: sem detector de pessoa,
        # não há informação que justifique pular.
        if (cam.analitico_facial and face_engine.modelos_disponiveis()
                and tem_pessoa is not False):
            # Uma passada só, com os pisos afrouxados: os filtros padrão
            # existem para a identificação 1:N, não para o enquadramento. Um
            # rosto de perfil, pequeno ou borrado não serve para dizer quem é
            # — mas é um rosto, e o operador precisa vê-lo marcado no vídeo.
            # A triagem de qualidade acontece logo abaixo, só para o registro.
            faces = await asyncio.to_thread(
                face_engine.extrair_faces, quadro,
                0.0, settings.INFERENCIA_ROSTO_MIN_PX, 0.0,
            )
            # O que o alinhamento precisa saber da cena para, se for o caso,
            # fazer nascer a linha do corpo que recebe a identidade.
            cena = {"cam": cam, "quadro": quadro, "agora": agora}
            for face in faces:
                x1, y1, x2, y2 = face.bbox
                # Guardada por referência: a identidade só se conhece depois da
                # busca 1:N, lá embaixo, e é nesta mesma caixa que o nome entra.
                caixa_rosto = {
                    "classe": "rosto",
                    "confianca": round(float(face.qualidade), 4),
                    "bbox": face.bbox_normalizado(largura, altura),
                    "track_id": None,
                }
                caixas_do_quadro.append(caixa_rosto)

                # Só entra no registro o que serve para identificar: capturas
                # ruins gravadas viram identificação que não se sustenta.
                if (face.qualidade < settings.MIN_FACE_QUALITY
                        or (y2 - y1) < settings.MIN_FACE_PIXELS):
                    continue
                # Rosto se deduplica pelo próprio rosto, não pelo lugar onde
                # apareceu: duas pessoas que passam pelo mesmo ponto são duas
                # passagens, e a segunda não pode sumir porque a primeira
                # esteve ali. O embedding separa uma da outra; a posição não.
                repetido, entrada = self._rosto_recente(cam.codigo, face.embedding)
                if repetido:
                    # Pelo `pessoa_id`, não pelo nome: o BIO-N não tem nome e
                    # mesmo assim é alguém, com corpo para ligar e trajetória
                    # para alimentar.
                    if entrada[3] or entrada[2]:
                        self._marcar_corpo(cena, face.bbox, corpos_no_quadro,
                                           entrada[4])
                        self._rotular(caixa_rosto, entrada[2], nomes_no_quadro)
                    continue

                jpeg = await asyncio.to_thread(video.recortar_bgr, quadro, face.bbox)
                resultado = await processar_deteccao(db, DeteccaoIn(
                    tipo=TipoDeteccao.FACIAL,
                    camera_codigo=cam.codigo,
                    ocorrido_em=agora,
                    embedding=face.embedding.tolist(),
                    qualidade_face=round(float(face.qualidade), 4),
                    bbox=face.bbox_normalizado(largura, altura),
                    snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
                ), origem="inferencia")
                self.gerados += 1

                # O BIO-N casa como qualquer cadastrado — tem amostras e
                # template —, só não tem nome para pôr na tela.
                nome = resultado.get("nome") or ""
                if biometrics.sem_nome(resultado.get("id_biometrico")):
                    nome = ""
                pessoa_id = resultado.get("pessoa_id")
                if nome or pessoa_id:
                    entrada[2] = nome
                    entrada[3] = pessoa_id
                    entrada[4] = resultado.get("id_biometrico")
                    caixa_rosto["similaridade"] = resultado.get("similaridade")
                    self._marcar_corpo(cena, face.bbox, corpos_no_quadro,
                                       entrada[4])
                    self._rotular(caixa_rosto, nome, nomes_no_quadro)
                    log.info("%s: %s reconhecido (%.0f%%)", cam.codigo,
                             nome or resultado.get("id_biometrico"),
                             (resultado.get("similaridade") or 0) * 100)

        # Sala vigiada: compara a ocupação com a da volta anterior — o fato é
        # a *mudança*, e mudança só existe entre duas voltas.
        #
        # Fica depois do facial, e não junto da contagem: antes o alerta
        # nascia sabendo quantas cabeças havia e nada sobre quem eram, porque
        # os rostos só são lidos adiante. Quem opera uma sala de reunião
        # precisa do nome, não do número — e o nome já está aqui.
        if cam.alerta_presenca and cam.analitico_objeto:
            await self._vigiar_sala(db, cam, objetos, codigo_pessoa, quadro,
                                    nomes_no_quadro)

        # Publicado sempre, inclusive vazio: a lista é a cena atual, e mandar
        # vazio é o que apaga a caixa de um objeto que acabou de sair. Sem
        # isso a última caixa ficaria pairando sobre o vídeo até expirar.
        await publicar("quadro_ao_vivo", {
            "camera_codigo": cam.codigo,
            "caixas": caixas_do_quadro,
            # Até quando esta lista ainda vale. Vai daqui porque só o worker
            # sabe quanto demora uma volta pelo parque, e isso muda a cada
            # câmera cadastrada: com validade fixa, a caixa apagava entre um
            # quadro e o seguinte assim que o parque crescia.
            "validade_s": round(self.ciclo_real * 2.0, 1),
        })


inferencia = Inferencia(settings.INFERENCIA_INTERVALO, settings.INFERENCIA_JANELA_S)
