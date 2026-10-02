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
from app.services import face_engine, video, vision
from app.services.events import publicar

log = logging.getLogger("seguranca.inferencia")


class Inferencia:
    """Laço que percorre as câmeras on-line aplicando os modelos."""

    def __init__(self, intervalo: float = 3.0, janela_track: float = 45.0) -> None:
        self.intervalo = intervalo
        self.janela_track = janela_track
        self._task: Optional[asyncio.Task] = None
        self._visto: Dict[Tuple[str, str], float] = {}
        self._rostos: Dict[str, list] = {}
        self.gerados = 0
        # Quanto uma volta completa leva de fato. Cresce com o parque, e é o
        # que o mosaico precisa saber para não apagar a caixa cedo demais.
        self.ciclo_real = intervalo
        self.ultimo_total = 0
        # Um analisador por câmera: as trilhas são a memória daquela cena, e
        # misturá-las entre câmeras faria a mesma pessoa "saltar" de uma para
        # outra e disparar corrida.
        self._acoes: Dict[str, vision.AnalisadorAcoes] = {}

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
                    await self._processar(db, cam)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Inferência falhou em %s: %s", cam.codigo, exc)

    def _rosto_repetido(self, camera: str, embedding) -> bool:
        """Mesmo rosto que já entrou há pouco naquela câmera.

        Compara o vetor, não a posição: é o que distingue duas pessoas no
        mesmo ponto de uma pessoa parada. O limiar é bem mais alto que o da
        identificação — aqui não se pergunta *quem* é, e sim se é a mesma
        captura de instantes atrás, então o parecido de menos tem de passar.

        A janela é curta porque passar de novo é um evento de verdade: quem
        volta ao saguão depois de um tempo deve constar outra vez.
        """
        agora = time.monotonic()
        recentes = [
            (v, t) for v, t in self._rostos.get(camera, [])
            if agora - t < settings.INFERENCIA_ROSTO_JANELA_S
        ]
        for vetor, _ in recentes:
            if face_engine.similaridade_cosseno(vetor, embedding) >= settings.INFERENCIA_ROSTO_IGUAL:
                self._rostos[camera] = recentes
                return True
        recentes.append((embedding, agora))
        # Guarda só as últimas: o saguão cheio não pode fazer a lista crescer
        # sem teto, e comparar contra as mais recentes já resolve.
        self._rostos[camera] = recentes[-24:]
        return False

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

        try:
            eventos = analisador.atualizar(objetos, largura, altura,
                                           zonas=self._zonas(cam))
        except Exception as exc:  # noqa: BLE001
            log.warning("Análise de ações falhou em %s: %s", cam.codigo, exc)
            return

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
                bbox=caixa,
                track_id=ev.track_id,
                snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
            ), origem="inferencia")
            self.gerados += 1
            log.info("%s: ação detectada — %s (%d envolvidos)",
                     cam.codigo, ev.rotulo, ev.envolvidos)

    @staticmethod
    def _zonas(cam: Camera) -> list:
        """Zonas da câmera no formato que o analisador espera."""
        return [
            {"tipo": getattr(z.tipo, "value", z.tipo),
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
            return
        altura, largura = quadro.shape[:2]
        agora = datetime.now(timezone.utc)

        caixas_do_quadro = []
        # `None` enquanto não se sabe: a câmera pode não ter análise de objeto,
        # e aí não há o que consultar antes de procurar rosto.
        tem_pessoa = None

        if cam.analitico_objeto and vision.yolo_disponivel():
            objetos = await asyncio.to_thread(
                vision.detectar_objetos, quadro, cam.limiar_objeto, True
            )
            tem_pessoa = any(o.classe == "pessoa" for o in objetos)

            if cam.analitico_acao:
                await self._registrar_acoes(db, cam, objetos, quadro,
                                            largura, altura, agora)

            for obj in objetos:
                # A sobreposição mostra o que a câmera está vendo agora, no
                # limiar que o operador configurou nela. Sai antes das podas:
                # a caixa acompanha o objeto enquanto ele está em cena, e o
                # registro nasce uma vez por passagem — o carro parado segue
                # enquadrado no vídeo sem voltar à lista a cada ciclo.
                caixas_do_quadro.append({
                    "classe": obj.classe,
                    "confianca": round(float(obj.confianca), 4),
                    "bbox": obj.bbox_normalizado(largura, altura),
                    "track_id": obj.track_id,
                })

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

                jpeg = await asyncio.to_thread(video.recortar_bgr, quadro, obj.bbox)
                await processar_deteccao(db, DeteccaoIn(
                    tipo=TipoDeteccao.OBJETO,
                    camera_codigo=cam.codigo,
                    ocorrido_em=agora,
                    classe=obj.classe,
                    confianca=round(float(obj.confianca), 4),
                    bbox=obj.bbox_normalizado(largura, altura),
                    track_id=obj.track_id,
                    snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
                ), origem="inferencia")
                self.gerados += 1

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
            for face in faces:
                x1, y1, x2, y2 = face.bbox
                caixas_do_quadro.append({
                    "classe": "rosto",
                    "confianca": round(float(face.qualidade), 4),
                    "bbox": face.bbox_normalizado(largura, altura),
                    "track_id": None,
                })

                # Só entra no registro o que serve para identificar: capturas
                # ruins gravadas viram identificação que não se sustenta.
                if (face.qualidade < settings.MIN_FACE_QUALITY
                        or (y2 - y1) < settings.MIN_FACE_PIXELS):
                    continue
                # Rosto se deduplica pelo próprio rosto, não pelo lugar onde
                # apareceu: duas pessoas que passam pelo mesmo ponto são duas
                # passagens, e a segunda não pode sumir porque a primeira
                # esteve ali. O embedding separa uma da outra; a posição não.
                if self._rosto_repetido(cam.codigo, face.embedding):
                    continue

                jpeg = await asyncio.to_thread(video.recortar_bgr, quadro, face.bbox)
                await processar_deteccao(db, DeteccaoIn(
                    tipo=TipoDeteccao.FACIAL,
                    camera_codigo=cam.codigo,
                    ocorrido_em=agora,
                    embedding=face.embedding.tolist(),
                    qualidade_face=round(float(face.qualidade), 4),
                    bbox=face.bbox_normalizado(largura, altura),
                    snapshot_jpeg=base64.b64encode(jpeg).decode("ascii") if jpeg else None,
                ), origem="inferencia")
                self.gerados += 1

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
