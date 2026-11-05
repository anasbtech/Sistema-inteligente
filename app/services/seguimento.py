"""Seguimento das caixas entre duas análises do detector.

O detector (YOLO) leva de 1,3 a 2,4 s por câmera com o parque inteiro na
CPU, e o vídeo da tela roda a vários quadros por segundo. Sem nada entre uma
análise e a outra, a caixa ficava parada enquanto a pessoa andava, e só
"pulava" para a posição nova na análise seguinte — o "travamento" e a caixa
que não acompanha a pessoa até ela sair.

Aqui a caixa é **seguida**, não redescoberta: pontos de textura dentro dela
são rastreados de um quadro para o seguinte por fluxo óptico (Lucas-Kanade),
e a caixa anda com a mediana do deslocamento desses pontos. Custa poucos
milissegundos por câmera numa imagem reduzida, contra segundos do detector.

O detector continua sendo a autoridade: cada análise nova substitui o
conjunto de caixas — é ela que acha quem entrou e apaga quem saiu. Como a
análise demora, as caixas que ela devolve descrevem um quadro que já passou;
por isso elas são *reproduzidas* pelos quadros guardados desde então
(`historico`), chegando à posição de agora em vez de voltar no tempo.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Deque, List, Optional, Tuple

import numpy as np

# Largura da imagem em que o rastreio roda. O fluxo óptico não precisa de
# resolução: em 640 px um corpo de pé ainda tem dezenas de pontos de textura,
# e o custo cai ao quadrado.
LARGURA = 640
# Quantos quadros reduzidos guardar por câmera, para reproduzir as caixas de
# uma análise que demorou. A ~6 por segundo, isso cobre ~7 s — a passada
# curta leva de 1,2 a 4,5 s entre ler o quadro e devolver as caixas.
HISTORICO = 40
# Abaixo disto a caixa perdeu o que seguir — saiu de cena, ficou atrás de
# algo, ou o rastreio desistiu — e some até a próxima análise.
MIN_PONTOS = 3
# Quadros seguidos sem conseguir seguir até a caixa ser largada. Um quadro
# corrompido do HEVC (borrão de meia imagem) derruba o fluxo óptico de todas
# as caixas ao mesmo tempo; largar na primeira falha apagava a cena inteira
# até a próxima análise. A ~6 quadros por segundo, isto é ~1,5 s.
MAX_FALHAS = 9
# Quadros que uma caixa vive depois de perder a pessoa, mesmo voltando a
# "seguir" algo. Quem entra no elevador some atrás da porta: o rastreio falha,
# acha pontos novos na textura da porta — que fica parada — e a caixa ficava
# presa ali até a próxima análise da câmera (CAM-001, 05/11/2026). Perdida
# uma vez, só o detector a devolve; a ~6 quadros por segundo, isto é ~2 s.
MAX_PERDIDA = 12
# Por quanto tempo uma pessoa confirmada (acima do piso de entrada) abona
# uma detecção fraca no mesmo lugar. Quem está sentado à mesa fica no lugar
# por minutos; a volta completa, que é quem confirma com a varredura, passa
# a cada ~4 minutos com o parque inteiro.
CONFIRMADO_S = 300.0


def _cinza_reduzido(quadro: np.ndarray) -> Tuple[np.ndarray, float]:
    """Quadro em cinza na largura de rastreio, e o fator de escala usado."""
    import cv2

    alt, larg = quadro.shape[:2]
    fator = LARGURA / float(larg) if larg > LARGURA else 1.0
    if fator != 1.0:
        quadro = cv2.resize(quadro, (LARGURA, max(1, int(alt * fator))),
                            interpolation=cv2.INTER_AREA)
    if quadro.ndim == 3:
        quadro = cv2.cvtColor(quadro, cv2.COLOR_BGR2GRAY)
    return quadro, fator


class _Seguida:
    """Uma caixa sendo seguida: a caixa (normalizada) e os seus pontos."""

    __slots__ = ("caixa", "pontos", "falhas", "perdida")

    def __init__(self, caixa: dict, pontos: Optional[np.ndarray]) -> None:
        self.caixa = dict(caixa)
        self.pontos = pontos
        self.falhas = 0
        # Quadros desde que o rastreio perdeu a pessoa; None enquanto não.
        self.perdida: Optional[int] = None


class Seguidor:
    """As caixas de uma câmera, seguidas quadro a quadro."""

    def __init__(self) -> None:
        self.historico: Deque[Tuple[float, np.ndarray]] = deque(maxlen=HISTORICO)
        self._seguidas: List[_Seguida] = []
        self._ultimo: Optional[np.ndarray] = None
        # O laço de acompanhamento e a semeadura rodam em threads diferentes
        # (ambos via `asyncio.to_thread`), sobre o mesmo estado.
        self._trava = threading.Lock()
        # O último quadro nativo recebido, para não reprocessar o mesmo: o
        # leitor só troca a matriz quando decodifica uma imagem nova.
        self.quadro_visto: Optional[np.ndarray] = None
        self._proximo_id = 1
        # Onde houve pessoa confirmada recentemente: (quando, caixa).
        self._confirmados: List[Tuple[float, dict]] = []

    # ------------------------------------------------------------------
    @staticmethod
    def _pontos_em(cinza: np.ndarray, caixa: dict) -> Optional[np.ndarray]:
        """Pontos de textura dentro da caixa, em pixels da imagem reduzida."""
        import cv2

        alt, larg = cinza.shape[:2]
        x, y, w, h = caixa["bbox"]
        # Um pouco para dentro: a borda da caixa costuma pegar o fundo, e
        # ponto do fundo não anda com a pessoa.
        x1, y1 = int((x + w * 0.15) * larg), int((y + h * 0.08) * alt)
        x2, y2 = int((x + w * 0.85) * larg), int((y + h * 0.92) * alt)
        if x2 - x1 < 4 or y2 - y1 < 4:
            return None
        mascara = np.zeros_like(cinza)
        mascara[y1:y2, x1:x2] = 255
        pts = cv2.goodFeaturesToTrack(cinza, maxCorners=24, qualityLevel=0.01,
                                      minDistance=3, mask=mascara, blockSize=5)
        return pts if pts is not None and len(pts) >= MIN_PONTOS else None

    def _avancar_para(self, cinza: np.ndarray) -> None:
        """Move cada caixa do quadro guardado para `cinza`."""
        import cv2

        if self._ultimo is None or self._ultimo.shape != cinza.shape:
            self._ultimo = cinza
            return
        alt, larg = cinza.shape[:2]
        vivas: List[_Seguida] = []
        for s in self._seguidas:
            if s.perdida is not None:
                s.perdida += 1
                if s.perdida > MAX_PERDIDA:
                    continue
            if s.pontos is None:
                # Sem textura para seguir (rosto pequeno, pessoa ao fundo, ou
                # o rastreio falhou no quadro anterior): fica onde está e
                # tenta achar pontos de novo neste quadro.
                s.pontos = self._pontos_em(cinza, s.caixa)
                vivas.append(s)
                continue
            novos, st, _ = cv2.calcOpticalFlowPyrLK(
                self._ultimo, cinza, s.pontos, None, winSize=(15, 15), maxLevel=3,
                criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03))
            bons = None
            if novos is not None:
                # Ida e volta: o ponto que não volta para onde estava é ruído.
                volta, st2, _ = cv2.calcOpticalFlowPyrLK(
                    cinza, self._ultimo, novos, None, winSize=(15, 15), maxLevel=3)
                erro = np.linalg.norm((volta - s.pontos).reshape(-1, 2), axis=1)
                bons = (st.ravel() == 1) & (st2.ravel() == 1) & (erro < 1.5)
            if bons is None or bons.sum() < MIN_PONTOS:
                # Perdeu o que seguir neste quadro: a caixa fica parada e
                # tenta de novo no próximo, até MAX_FALHAS seguidas.
                s.falhas += 1
                if s.perdida is None:
                    s.perdida = 0
                if s.falhas < MAX_FALHAS:
                    s.pontos = None
                    vivas.append(s)
                continue
            s.falhas = 0
            desloc = (novos - s.pontos).reshape(-1, 2)[bons]
            dx, dy = np.median(desloc, axis=0)
            x, y, w, h = s.caixa["bbox"]
            nx, ny = x + dx / larg, y + dy / alt
            # Saiu do quadro: a pessoa foi embora, a caixa vai junto.
            cx, cy = nx + w / 2, ny + h / 2
            if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
                continue
            s.caixa["bbox"] = [round(float(nx), 6), round(float(ny), 6), w, h]
            s.pontos = novos[bons].reshape(-1, 1, 2)
            if len(s.pontos) < 8:
                # Poucos pontos sobrando: renova dentro da caixa já movida.
                renovados = self._pontos_em(cinza, s.caixa)
                if renovados is not None:
                    s.pontos = renovados
            vivas.append(s)
        self._seguidas = vivas
        self._ultimo = cinza

    # ------------------------------------------------------------------
    def quadro(self, quadro_bgr: np.ndarray, quando: float) -> List[dict]:
        """Um quadro novo da câmera: avança as caixas e devolve as atuais."""
        cinza, _ = _cinza_reduzido(quadro_bgr)
        with self._trava:
            self.quadro_visto = quadro_bgr
            self.historico.append((quando, cinza))
            self._avancar_para(cinza)
            return self.caixas()

    def semear(self, quadro_bgr: np.ndarray, quando: float, caixas: list,
               fracas: Optional[list] = None) -> List[dict]:
        """Caixas novas do detector, sobre o quadro em que ele as achou.

        Substitui o conjunto: quem o detector não achou some, quem ele achou
        entra. Depois reproduz os quadros guardados *depois* daquele, para as
        caixas chegarem à posição de agora — a análise levou segundos, e
        publicá-las como vieram faria a marcação voltar no tempo.

        `fracas` são caixas abaixo do piso de entrada da tela: só ficam se
        caírem sobre alguém que já estava sendo seguido (histerese).
        """
        cinza, _ = _cinza_reduzido(quadro_bgr)
        with self._trava:
            antigas = self._seguidas
            fortes = [_Seguida(c, self._pontos_em(cinza, c))
                      for c in caixas if c.get("bbox")]
            candidatas = [_Seguida(c, self._pontos_em(cinza, c))
                          for c in (fracas or []) if c.get("bbox")]
            self._seguidas = fortes + candidatas
            self._ultimo = cinza
            for t, depois in list(self.historico):
                if t > quando:
                    self._avancar_para(depois)
            self._herdar_identidade(antigas)
            # Fraca só fica se herdou de alguém já seguido ou cai onde houve
            # pessoa confirmada há pouco — e se não é a mesma pessoa de uma
            # caixa forte.
            ids_antigos = {a.caixa.get("track_id") for a in antigas}
            fortes_vivas = [s for s in self._seguidas if s not in candidatas]
            self._registrar_confirmados([s.caixa for s in fortes_vivas], quando)
            self._seguidas = fortes_vivas + [
                s for s in self._seguidas if s in candidatas
                and (s.caixa.get("track_id") in ids_antigos
                     or self._ja_confirmado(s.caixa))
                and not any(f.caixa.get("classe") == s.caixa.get("classe")
                            and _iou(f.caixa["bbox"], s.caixa["bbox"]) > 0.3
                            for f in fortes_vivas)]
            return self.caixas()

    def _herdar_identidade(self, antigas: List[_Seguida]) -> None:
        """Quem já estava sendo seguido mantém o número.

        A tela desliza a caixa de uma posição à seguinte pelo `track_id`;
        número novo a cada análise faria a caixa sumir e reaparecer. As duas
        listas já estão na posição de agora (a nova foi reproduzida até aqui),
        então a sobreposição entre elas é confiável — mais que a do rastreio
        por posição entre duas análises, em que quem anda já saiu da caixa.
        """
        livres = list(antigas)
        for s in sorted(self._seguidas, key=lambda s: -float(s.caixa.get("confianca") or 0)):
            melhor, melhor_iou = None, 0.3
            for a in livres:
                if a.caixa.get("classe") != s.caixa.get("classe"):
                    continue
                v = _iou(a.caixa["bbox"], s.caixa["bbox"])
                if v > melhor_iou:
                    melhor, melhor_iou = a, v
            if melhor is not None:
                livres.remove(melhor)
                s.caixa["track_id"] = melhor.caixa["track_id"]
            else:
                s.caixa["track_id"] = self._proximo_id
                self._proximo_id += 1

    def confirmar(self, caixas: list, quando: float) -> None:
        """Pessoas confirmadas numa análise de câmera fora da tela."""
        with self._trava:
            self._registrar_confirmados(caixas, quando)

    def _registrar_confirmados(self, caixas: list, quando: float) -> None:
        novos = [(quando, dict(c)) for c in caixas
                 if c.get("bbox") and c.get("classe") == "pessoa"]
        if novos:
            # Os de agora substituem os antigos que caem no mesmo lugar.
            self._confirmados = [
                (t, c) for t, c in self._confirmados
                if not any(_iou(c["bbox"], n["bbox"]) > 0.3 for _, n in novos)] + novos
        self._confirmados = [(t, c) for t, c in self._confirmados
                             if t >= quando - CONFIRMADO_S]

    def _ja_confirmado(self, caixa: dict) -> bool:
        return any(c.get("classe") == caixa.get("classe")
                   and _iou(c["bbox"], caixa["bbox"]) > 0.3
                   for _, c in self._confirmados)

    def caixas(self) -> List[dict]:
        return [dict(s.caixa) for s in self._seguidas]

    def cobre(self, quando: float) -> bool:
        """O histórico ainda tem os quadros desde `quando`?

        Sem eles não há como reproduzir a análise até agora: ela poria a
        caixa onde a pessoa estava há dez, vinte segundos.
        """
        with self._trava:
            return bool(self.historico) and self.historico[0][0] <= quando

    def tem_caixas(self) -> bool:
        return bool(self._seguidas)

    def esvaziar(self) -> None:
        """Sem ninguém em cena: para de seguir, mas guarda o histórico."""
        with self._trava:
            self._seguidas = []


def _iou(a: list, b: list) -> float:
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    uniao = a[2] * a[3] + b[2] * b[3] - inter
    return inter / uniao if uniao > 0 else 0.0
