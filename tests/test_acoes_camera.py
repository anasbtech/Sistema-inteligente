"""Testes da seleção de ações por câmera.

    pytest -q tests/test_acoes_camera.py

A regra que estes testes protegem: **a tela não promete vigilância que o
sistema não exerce**. Das dez ações que a interface lista, nem todas têm
detector por trás, e ligar uma delas numa câmera precisa falhar alto — não
gravar quieto um interruptor que nunca vai acender.

O resto é a mecânica de escolher: cada câmera vigia o que faz sentido no
lugar onde está, e o analisador respeita essa escolha.
"""
from __future__ import annotations

import pytest

from app.services.vision import (
    ACOES_PADRAO, CATALOGO_ACOES, FICHA_ACAO, AnalisadorAcoes, ObjetoDetectado,
    RastreioPorPosicao, acoes_validas, detalhe_acao, fora_do_horario,
    janela_horario,
)
from app.schemas import CameraUpdate, validar_acoes


# ----------------------------------------------------------------------
# O catálogo diz a verdade
# ----------------------------------------------------------------------
def test_catalogo_cobre_exatamente_as_acoes_da_interface():
    """A lista do filtro de registros e a do catálogo são a mesma lista.

    Se divergirem, o operador filtra por uma ação que nenhuma câmera pode
    ligar, ou liga uma que o registro não sabe exibir.

    O lado da interface é **lido do arquivo**, não repetido aqui: uma lista
    escrita duas vezes no teste não detecta divergência nenhuma — ela só
    confirma que as duas cópias do teste concordam entre si.
    """
    import re
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent
          / "frontend" / "static" / "js" / "ui.js").read_text(encoding="utf-8-sig")
    corpo = re.search(r"const ROTULO_ACAO_UI = \{(.*?)\};", js, re.S)
    assert corpo, "ROTULO_ACAO_UI não encontrado em ui.js"
    da_interface = set(re.findall(r"(\w+)\s*:", corpo.group(1)))

    assert {f.codigo for f in CATALOGO_ACOES} == da_interface


def test_acao_sem_detector_declara_o_motivo():
    """Indisponível sem motivo escrito é pior que indisponível: a tela mostra
    o item apagado e ninguém sabe se é bug ou projeto."""
    for ficha in CATALOGO_ACOES:
        if not ficha.disponivel:
            assert ficha.indisponivel, f"{ficha.codigo} sem motivo declarado"


def test_padrao_so_oferece_o_que_dispara():
    """Câmera nova não nasce prometendo o que não cumpre."""
    assert ACOES_PADRAO
    assert all(FICHA_ACAO[c].disponivel for c in ACOES_PADRAO)


# ----------------------------------------------------------------------
# A API recusa o impossível
# ----------------------------------------------------------------------
def test_ligar_acao_sem_detector_e_recusado():
    """Dano ao patrimônio não tem detector. Aceitar o pedido deixaria quem
    configurou achando que a câmera o vigia.

    Fogo, fumaça e agressão física estavam aqui até 15/10, quando ganharam
    detector — os dois primeiros por assinatura de imagem (`services/chama.py`)
    e o terceiro por heurística de trajetória."""
    with pytest.raises(ValueError, match="sem detector"):
        validar_acoes(["vandalismo"])


def test_acao_inexistente_e_recusada():
    with pytest.raises(ValueError, match="inexistente"):
        validar_acoes(["teletransporte"])


def test_lista_vazia_e_escolha_legitima():
    """Desmarcar tudo é uma decisão de quem configura, não um erro."""
    assert validar_acoes([]) == []


def test_selecao_e_normalizada_na_ordem_do_catalogo():
    """Mesma seleção, mesma gravação.

    Sem normalizar, ['queda','corrida'] e ['corrida','queda'] gravavam
    diferente e a auditoria acusava alteração onde nada tinha mudado.
    """
    a = CameraUpdate(acoes_ativas=["queda", "corrida", "queda"]).acoes_ativas
    b = CameraUpdate(acoes_ativas=["corrida", "queda"]).acoes_ativas
    assert a == b == ["corrida", "queda"]


# ----------------------------------------------------------------------
# O analisador obedece à câmera
# ----------------------------------------------------------------------
def _cena_aglomerada(n: int = 12):
    """n pessoas juntas no centro do quadro, cada uma com sua trilha."""
    return [ObjetoDetectado("pessoa", 0.9, (500 + i * 4, 400, 540 + i * 4, 520), track_id=i)
            for i in range(n)]


def _disparar_aglomeracao(analisador: AnalisadorAcoes):
    """Aglomeração exige persistir por 10s antes de virar evento."""
    objetos = _cena_aglomerada()
    analisador.atualizar(objetos, 1280, 720, agora=1000.0)
    return analisador.atualizar(objetos, 1280, 720, agora=1011.0)


def test_acao_desmarcada_nao_emite():
    """O caso que a funcionalidade existe para permitir: aglomeração num
    corredor de passagem é ruído, e a câmera pode dizer que não quer."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"queda"}
    assert _disparar_aglomeracao(analisador) == []


def test_acao_marcada_emite():
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"aglomeracao"}
    eventos = _disparar_aglomeracao(analisador)
    assert [e.classe for e in eventos] == ["aglomeracao"]


def test_sem_selecao_vale_tudo():
    """`None` é o analisador usado fora de uma câmera cadastrada — o worker
    avulso e os próprios testes. Ali não há seleção a respeitar."""
    analisador = AnalisadorAcoes()
    assert analisador.acoes_ativas is None
    assert [e.classe for e in _disparar_aglomeracao(analisador)] == ["aglomeracao"]


def test_desmarcada_nao_consome_o_cooldown():
    """Religar a ação precisa voltar a valer na hora.

    Se a checagem viesse depois do cooldown, desmarcar e remarcar deixaria a
    câmera muda até vencer a espera de um alerta que nunca foi emitido.
    """
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = set()
    assert _disparar_aglomeracao(analisador) == []

    analisador.acoes_ativas = {"aglomeracao"}
    assert [e.classe for e in _disparar_aglomeracao(analisador)] == ["aglomeracao"]


# ----------------------------------------------------------------------
# Aglomeração: quantos, quão juntos, por quanto tempo
# ----------------------------------------------------------------------
def test_ajuntamento_de_um_instante_nao_alerta():
    """O que se desfaz sozinho não é aglomeração.

    Fila que se forma e anda, gente que para para se cumprimentar, saída de
    reunião. Sem a espera, cada um deles é um alerta.
    """
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"aglomeracao"}
    objetos = _cena_aglomerada()
    assert analisador.atualizar(objetos, 1280, 720, agora=1000.0) == []
    assert analisador.atualizar(objetos, 1280, 720, agora=1005.0) == [], (
        "cinco segundos ainda são menos que a espera configurada")


def test_cada_camera_tem_o_seu_limite():
    """Oito num corredor é ocorrência; na Produção, onde treze trabalham, não.

    Um número único para o parque obrigaria uma das duas a mentir — por isso
    o limite é da câmera, e o do sistema só vale para quem não quis decidir.
    """
    cinco = _cena_aglomerada(5)

    padrao = AnalisadorAcoes()
    padrao.acoes_ativas = {"aglomeracao"}
    padrao.atualizar(cinco, 1280, 720, agora=1000.0)
    assert padrao.atualizar(cinco, 1280, 720, agora=1011.0) == [], (
        "cinco pessoas estão abaixo do limite do sistema")

    apertada = AnalisadorAcoes()
    apertada.acoes_ativas = {"aglomeracao"}
    apertada.n_aglomeracao = 4
    apertada.atualizar(cinco, 1280, 720, agora=1000.0)
    eventos = apertada.atualizar(cinco, 1280, 720, agora=1011.0)
    assert [e.classe for e in eventos] == ["aglomeracao"]
    assert eventos[0].envolvidos == 5


def test_gente_espalhada_pela_sala_nao_e_aglomeracao():
    """Conta quem está *junto*, não quem está em cena.

    Nove pessoas numa grade bem separada passam do limite pelo total e não
    formam agrupamento nenhum. É a distinção que torna a ação utilizável numa
    sala que trabalha cheia: pelo total, a Produção seria alerta todo dia.
    """
    espalhadas = []
    for i, x in enumerate((192, 576, 960)):
        for j, y in enumerate((144, 360, 576)):
            espalhadas.append(ObjetoDetectado(
                "pessoa", 0.9, (x, y, x + 40, y + 110), track_id=i * 3 + j))

    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"aglomeracao"}
    assert len(espalhadas) >= analisador.n_aglomeracao, "o total passa do limite"
    analisador.atualizar(espalhadas, 1280, 720, agora=1000.0)
    assert analisador.atualizar(espalhadas, 1280, 720, agora=1011.0) == []


def test_duas_leituras_do_mesmo_corpo_nao_contam_duas_pessoas():
    """A contagem herda a fusão de caixas, e é dela que depende.

    Oito pessoas das quais metade veio marcada duas vezes são quatro pessoas.
    Sem desfazer a duplicata antes de contar, a aglomeração nasceria de gente
    que não está lá.
    """
    from app.services.vision import fundir_sobrepostas

    gente, duplicatas = [], []
    for i in range(4):
        x = 500 + i * 46
        corpo = ObjetoDetectado("pessoa", 0.90, (x, 400, x + 40, 520), i)
        gente.append(corpo)
        # Tronco marcado dentro do corpo inteiro — a duplicata real do
        # Service Desk, que o IoU não pega e a contenção pega.
        duplicatas.append(ObjetoDetectado("pessoa", 0.21,
                                          (x + 8, 400, x + 36, 505), 90 + i))

    assert len(gente + duplicatas) >= 8, "sem fundir, passaria do limite"
    assert len(fundir_sobrepostas(gente + duplicatas)) == 4


def test_a_duracao_relatada_e_o_tempo_real_do_ajuntamento():
    """O alerta diz há quanto tempo aquilo está acontecendo.

    O represamento e o início do fato são perguntas diferentes, e dividiam a
    mesma variável: somar o cooldown ao início fazia a duração do segundo
    alerta nascer de um instante que nunca existiu.
    """
    from app.config import settings

    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"aglomeracao"}
    objetos = _cena_aglomerada()
    t = 1000.0
    analisador.atualizar(objetos, 1280, 720, agora=t)
    primeiro = analisador.atualizar(objetos, 1280, 720, agora=t + 11.0)
    assert primeiro[0].duracao_s == 11

    # Mesmo ajuntamento, depois do represamento: segue sendo o mesmo fato,
    # e a duração conta desde que ele começou.
    adiante = t + settings.ACAO_COOLDOWN_S + 20.0
    assert analisador.atualizar(objetos, 1280, 720, agora=t + 30.0) == [], (
        "dentro do cooldown o mesmo ajuntamento não volta ao painel")
    segundo = analisador.atualizar(objetos, 1280, 720, agora=adiante)
    assert [e.classe for e in segundo] == ["aglomeracao"]
    assert segundo[0].duracao_s == int(adiante - t)


def test_ajuntamento_que_se_desfaz_recomeca_a_contagem():
    """Esvaziar e voltar a juntar é outro fato, e começa do zero."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"aglomeracao"}
    objetos = _cena_aglomerada()
    analisador.atualizar(objetos, 1280, 720, agora=1000.0)
    analisador.atualizar(_cena_aglomerada(2), 1280, 720, agora=1005.0)
    assert analisador.atualizar(objetos, 1280, 720, agora=1011.0) == [], (
        "a espera recomeça quando o agrupamento se desfaz")


def test_acoes_validas_preserva_a_ordem_do_catalogo():
    assert acoes_validas(["aglomeracao", "corrida"]) == ["corrida", "aglomeracao"]
    assert acoes_validas(["nada"]) == []


# ----------------------------------------------------------------------
# Identidade entre visitas — o que destrava a análise
# ----------------------------------------------------------------------
def _pessoa_em(bbox, conf=0.9):
    return ObjetoDetectado("pessoa", conf, bbox)


def test_objeto_parado_mantem_a_identidade_entre_visitas():
    """É o teste que a análise de ação faz, disfarçado de rastreio.

    Entre duas visitas separadas por dezenas de segundos não há trajetória
    para seguir. O que sobrevive à sobreposição é quem continuou no mesmo
    lugar — exatamente o que permanência e aglomeração perguntam.
    """
    r = RastreioPorPosicao()
    antes = [_pessoa_em((100, 100, 200, 400))]
    r.atribuir(antes, agora=1000.0)

    depois = [_pessoa_em((104, 98, 203, 405))]      # a mesma pessoa, sentada
    r.atribuir(depois, agora=1080.0)
    assert depois[0].track_id == antes[0].track_id


def test_quem_atravessou_a_sala_ganha_identidade_nova():
    """E está certo: para o sistema é outra passagem, não a mesma parada."""
    r = RastreioPorPosicao()
    antes = [_pessoa_em((100, 100, 200, 400))]
    r.atribuir(antes, agora=1000.0)

    depois = [_pessoa_em((800, 100, 900, 400))]
    r.atribuir(depois, agora=1080.0)
    assert depois[0].track_id != antes[0].track_id


def test_identidade_nao_passa_de_uma_classe_para_outra():
    r = RastreioPorPosicao()
    carro = [ObjetoDetectado("carro", 0.9, (100, 100, 300, 300))]
    r.atribuir(carro, agora=1000.0)

    gente = [_pessoa_em((100, 100, 300, 300))]
    r.atribuir(gente, agora=1080.0)
    assert gente[0].track_id != carro[0].track_id


def test_cada_pessoa_recebe_uma_identidade():
    """Sem pareamento guloso, duas pessoas próximas disputavam o mesmo id e
    uma delas sumia da contagem."""
    r = RastreioPorPosicao()
    antes = [_pessoa_em((100, 100, 200, 400)), _pessoa_em((210, 100, 310, 400))]
    r.atribuir(antes, agora=1000.0)
    depois = [_pessoa_em((102, 101, 201, 402)), _pessoa_em((212, 99, 311, 401))]
    r.atribuir(depois, agora=1080.0)
    assert len({o.track_id for o in depois}) == 2
    assert {o.track_id for o in depois} == {o.track_id for o in antes}


def test_identidade_expira_depois_do_ttl():
    """Sem expirar, a sala de ontem casaria com a de hoje."""
    r = RastreioPorPosicao(ttl_s=120.0)
    antes = [_pessoa_em((100, 100, 200, 400))]
    r.atribuir(antes, agora=1000.0)
    depois = [_pessoa_em((100, 100, 200, 400))]
    r.atribuir(depois, agora=1000.0 + 500)
    assert depois[0].track_id != antes[0].track_id


def test_analise_de_acao_emite_com_o_rastreio_ligado():
    """O defeito que isto fecha: o banco tinha zero ocorrência de ação desde
    sempre, porque o analisador descarta objeto sem `track_id` e a inferência
    rodava sem rastreio — nem a contagem de aglomeração escapava."""
    r = RastreioPorPosicao(ttl_s=600.0)
    an = AnalisadorAcoes(seg_permanencia=300.0, cooldown_s=600.0)
    an.ttl_trilha = 600.0
    an.acoes_ativas = {"permanencia"}

    emitidos = []
    t = 1000.0
    for _ in range(12):                       # 12 visitas de 80s = 16 minutos
        objetos = [_pessoa_em((100, 100, 200, 400))]
        r.atribuir(objetos, agora=t)
        emitidos += an.atualizar(objetos, 1280, 720, agora=t)
        t += 80.0

    assert [e.classe for e in emitidos][:1] == ["permanencia"]


def _vigiar_permanencia(caixa_do_quadro, quadros=80, limite=300.0):
    """Roda como a câmera entrega: rajada de quadros, depois salto de visita.

    `caixa_do_quadro(i)` devolve a caixa da pessoa no quadro i.
    """
    r = RastreioPorPosicao(ttl_s=1200.0)
    an = AnalisadorAcoes(seg_permanencia=limite)
    an.ttl_trilha = 1200.0
    an.acoes_ativas = {"permanencia"}
    eventos, t = [], 1000.0
    for i in range(quadros):
        objetos = [_pessoa_em(c) for c in caixa_do_quadro(i)]
        r.atribuir(objetos, agora=t)
        eventos += an.atualizar(objetos, 1280, 720, agora=t)
        t += 0.15
        if i % 8 == 7:
            t += 80.0          # fim da rajada: a câmera só volta depois
    return eventos


def test_andar_de_um_lado_para_o_outro_e_permanencia():
    """Permanência é não sair dali, não é ficar imóvel.

    Quem anda de um lado para o outro do corredor por vinte minutos está lá
    há vinte minutos — e é essa a pessoa que a ação existe para apontar.
    Medida por velocidade, cada passo zerava o relógio e ela nunca disparava.
    """
    def vaivem(i):
        x = 400 + (90 if (i // 4) % 2 == 0 else -90)
        return [(x, 300, x + 90, 420)]

    assert [e.classe for e in _vigiar_permanencia(vaivem)] == ["permanencia"], (
        "um alerta: é uma pessoa só, por mais identidades que ela ganhe"
    )


def test_quem_atravessa_a_cena_nao_permanece():
    """O contraponto: sair da área é o que a ação não procura."""
    assert _vigiar_permanencia(
        lambda i: [(400 + i * 14, 300, 490 + i * 14, 420)]) == []


def test_duas_pessoas_paradas_em_cantos_distintos_alertam_as_duas():
    """O represamento é do lugar, e não pode engolir quem está noutro canto."""
    eventos = _vigiar_permanencia(
        lambda i: [(150, 300, 240, 420), (1000, 300, 1090, 420)])
    assert [e.classe for e in eventos] == ["permanencia", "permanencia"]


def test_o_alerta_diz_quanto_tempo():
    """A duração é o fato: sem ela o alerta diz só que havia alguém parado.

    Quem recebe precisa saber de cara se é um quarto de hora ou três horas,
    porque é disso que depende ir até lá.
    """
    eventos = _vigiar_permanencia(lambda i: [(400, 300, 490, 420)])
    assert eventos[0].duracao_s > 300, "conta desde que a permanência começou"


def test_sem_rastreio_a_analise_continua_muda():
    """A prova pelo avesso, para o defeito não voltar sem ninguém notar."""
    an = AnalisadorAcoes(seg_permanencia=300.0)
    an.ttl_trilha = 600.0
    an.acoes_ativas = {"permanencia"}
    t = 1000.0
    emitidos = []
    for _ in range(12):
        emitidos += an.atualizar([_pessoa_em((100, 100, 200, 400))], 1280, 720, agora=t)
        t += 80.0
    assert emitidos == []


def test_condicao_de_disparo_sai_da_configuracao():
    """Texto fixo divergiria do parâmetro e a tela passaria a mentir."""
    from app.config import settings
    texto = detalhe_acao("permanencia")
    assert str(int(round(settings.ACAO_PERMANENCIA_S / 60))) in texto
    assert detalhe_acao("aglomeracao").count(str(settings.ACAO_AGLOMERACAO_N)) == 1
    # Briga ganhou detector em 15/10, e a linha aqui dizia "sem detector,
    # nada a prometer" desde antes disso. A tela mostrava a única ação
    # disponível sem explicar o que a dispara — e esta é justamente a que o
    # operador precisa saber que não afirma nada sozinha.
    assert "confirmação" in detalhe_acao("briga")


def test_a_acao_de_briga_nao_afirma_agressao():
    """O rótulo é o que o operador lê no alerta e na lista de ocorrências.

    Afirmar "Agressão física" é afirmar intenção a partir de vídeo, que é o
    que este sistema não faz — e um alerta assim, entre duas pessoas
    identificadas, tem consequência real para elas. O que existe são três
    sinais de trajetória que coincidiram.
    """
    from app.services.vision import ROTULO_ACAO

    assert ROTULO_ACAO["briga"].lower().startswith("possível")


# ----------------------------------------------------------------------
# As cinco ações acrescentadas em 2026-10-08
# ----------------------------------------------------------------------
ZONA_RESTRITA = {"nome": "Cofre", "tipo": "POLIGONO",
                 "pontos": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]],
                 "regras": {"restrita": True}}
ZONA_SEM_VEICULO = {"nome": "Calçada", "tipo": "POLIGONO",
                    "pontos": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5], [0.0, 0.5]],
                    "regras": {"veiculo_proibido": True}}


def _analisador(acoes):
    a = AnalisadorAcoes()
    a.acoes_ativas = set(acoes)
    a.ttl_trilha = 600.0
    return a


def test_pessoa_dentro_da_zona_restrita_dispara():
    """Decide-se num quadro só — ao contrário de invasão, que precisa saber
    de que lado a pessoa veio. Por isso esta funciona no rodízio."""
    an = _analisador(["area_restrita"])
    dentro = ObjetoDetectado("pessoa", 0.9, (100, 100, 200, 300), track_id=1)
    ev = an.atualizar([dentro], 1280, 720, zonas=[ZONA_RESTRITA], agora=1000.0)
    assert [e.classe for e in ev] == ["area_restrita"]
    assert ev[0].zona_nome == "Cofre", "o alerta precisa dizer qual área"


def test_pessoa_fora_da_zona_restrita_nao_dispara():
    an = _analisador(["area_restrita"])
    fora = ObjetoDetectado("pessoa", 0.9, (1000, 600, 1100, 700), track_id=1)
    assert an.atualizar([fora], 1280, 720, zonas=[ZONA_RESTRITA], agora=1000.0) == []


# ----------------------------------------------------------------------
# Autorização prévia: quem a zona libera não vira ocorrência
# ----------------------------------------------------------------------
ZONA_COM_AUTORIZADOS = {**ZONA_RESTRITA,
                        "regras": {"restrita": True,
                                   "autorizados": ["BIO-M-000012"]}}
PERIMETRO = {"nome": "Portão dos fundos", "tipo": "LINHA",
             "pontos": [[0.5, 0.0], [0.5, 1.0]],
             "regras": {"invasao": True, "autorizados": ["BIO-M-000012"]}}


def test_quem_a_zona_autoriza_nao_vira_ocorrencia():
    """O que a lista existe para evitar: a equipe que trabalha ali dentro.

    Sem ela, a área restrita do datacenter acusa o próprio time de TI a cada
    volta, e o alerta que importa se perde no meio.
    """
    an = _analisador(["area_restrita"])
    an.identidades = {1: "BIO-M-000012"}
    dentro = ObjetoDetectado("pessoa", 0.9, (100, 100, 200, 300), track_id=1)
    assert an.atualizar([dentro], 1280, 720,
                        zonas=[ZONA_COM_AUTORIZADOS], agora=1000.0) == []


def test_quem_nao_esta_na_lista_dispara():
    an = _analisador(["area_restrita"])
    an.identidades = {1: "BIO-F-000099"}      # reconhecida, mas não liberada
    dentro = ObjetoDetectado("pessoa", 0.9, (100, 100, 200, 300), track_id=1)
    ev = an.atualizar([dentro], 1280, 720,
                      zonas=[ZONA_COM_AUTORIZADOS], agora=1000.0)
    assert [e.classe for e in ev] == ["area_restrita"]


def test_desconhecido_nao_e_autorizado():
    """O lado certo do erro para um perímetro.

    Quem o facial ainda não reconheceu não está na lista — e não pode ser
    tratado como se estivesse: seria um perímetro que se abre justamente
    para quem o sistema não sabe quem é.
    """
    an = _analisador(["area_restrita"])
    an.identidades = {}                        # ninguém identificado ainda
    dentro = ObjetoDetectado("pessoa", 0.9, (100, 100, 200, 300), track_id=1)
    ev = an.atualizar([dentro], 1280, 720,
                      zonas=[ZONA_COM_AUTORIZADOS], agora=1000.0)
    assert [e.classe for e in ev] == ["area_restrita"]


def test_zona_sem_lista_segue_acusando_qualquer_um():
    """A lista é opcional, e sem ela nada muda para quem já usava a ação."""
    an = _analisador(["area_restrita"])
    an.identidades = {1: "BIO-M-000012"}
    dentro = ObjetoDetectado("pessoa", 0.9, (100, 100, 200, 300), track_id=1)
    ev = an.atualizar([dentro], 1280, 720, zonas=[ZONA_RESTRITA], agora=1000.0)
    assert [e.classe for e in ev] == ["area_restrita"]


def test_autorizacao_vale_tambem_para_o_perimetro():
    """Cruzar a linha é a mesma pergunta: quem cruzou tem permissão de estar lá?"""
    an = _analisador(["invasao"])
    an.identidades = {1: "BIO-M-000012"}
    # Dois quadros: a linha precisa de duas posições para saber de que lado veio.
    an.atualizar([ObjetoDetectado("pessoa", 0.9, (200, 300, 300, 500), 1)],
                 1280, 720, zonas=[PERIMETRO], agora=1000.0)
    ev = an.atualizar([ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 500), 1)],
                      1280, 720, zonas=[PERIMETRO], agora=1000.5)
    assert ev == [], "quem a zona autoriza atravessa sem acusar"


def test_perimetro_acusa_quem_nao_tem_autorizacao():
    an = _analisador(["invasao"])
    an.identidades = {1: "BIO-F-000099"}
    an.atualizar([ObjetoDetectado("pessoa", 0.9, (200, 300, 300, 500), 1)],
                 1280, 720, zonas=[PERIMETRO], agora=1000.0)
    ev = an.atualizar([ObjetoDetectado("pessoa", 0.9, (800, 300, 900, 500), 1)],
                      1280, 720, zonas=[PERIMETRO], agora=1000.5)
    assert [e.classe for e in ev] == ["invasao"]
    assert ev[0].zona_nome == "Portão dos fundos", "o alerta diz qual perímetro"


# ----------------------------------------------------------------------
# Contrafluxo: andar contra o sentido do corredor, e insistir
#
# Corredor com o fluxo permitido para baixo — y cresce para baixo, como no
# canvas e como o detector compara. Corpo de 120 px num quadro de 720.
# ----------------------------------------------------------------------
CORREDOR = {"nome": "Corredor", "tipo": "POLIGONO",
            "pontos": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
            "regras": {"contrafluxo": True, "direcao": [0, 1]}}


def _contrafluxo(y_do_quadro, zona=CORREDOR, quadros=24):
    from app.config import settings

    an = _analisador(["contrafluxo"])
    eventos, t = [], 5000.0
    for i in range(quadros):
        y = y_do_quadro(i)
        eventos += an.atualizar(
            [ObjetoDetectado("pessoa", 0.9, (600, y, 690, y + 120), 7)],
            1280, 720, zonas=[zona],
            agora=t + i * settings.ACAO_CONTINUA_INTERVALO_S)
    return an, eventos


def test_andar_contra_o_fluxo_e_insistir_dispara():
    _, eventos = _contrafluxo(lambda i: 500 - i * 18)
    assert [e.classe for e in eventos] == ["contrafluxo"]
    assert eventos[0].zona_nome == "Corredor", "o alerta diz qual corredor"


def test_andar_a_favor_do_fluxo_nao_dispara():
    assert _contrafluxo(lambda i: 100 + i * 18)[1] == []


def test_recuar_para_dar_passagem_nao_e_contrafluxo():
    """O "persistentemente" da regra.

    Virar-se para falar com alguém, recuar para deixar passar ou contornar
    um obstáculo são movimentos contra o fluxo — e duram menos de um
    segundo. Sem a exigência de insistir, cada um deles é um alerta.
    """
    assert _contrafluxo(
        lambda i: (300 - i * 18) if i < 3 else (246 + (i - 3) * 18))[1] == []


def test_pessoa_parada_tremendo_nao_anda_contra_o_fluxo():
    """O defeito do vetor unitário, que é o pior desta ação.

    `Trilha.direcao` normaliza: um pixel de tremor devolve um vetor do mesmo
    tamanho que uma passada inteira. Medido nesta cena — pessoa imóvel
    oscilando 1 px — o cosseno dá -1,0, exatamente contra o fluxo, com
    avanço de 0,008 altura de corpo. A regra antiga, que olhava só o
    cosseno, acusava quem estava parado.
    """
    import numpy as np

    an, eventos = _contrafluxo(lambda i: 300 - (1 if i % 2 else 0))
    trilha = an.trilhas[7]

    assert float(np.dot(trilha.direcao(), [0, 1])) < -0.6, (
        "a direção aponta contra o fluxo: era o que bastava antes")
    assert trilha.avanco() < 0.05, "e o corpo não saiu do lugar"
    assert eventos == []


def test_zona_que_nao_declara_o_sentido_fica_muda():
    """Marcar contrafluxo sem dizer a direção não deixa contra o quê comparar.

    A zona grava e não vigia nada — o editor avisa antes de salvar, e aqui
    fica registrado que o detector realmente não inventa um sentido.
    """
    sem_sentido = {**CORREDOR, "regras": {"contrafluxo": True}}
    assert _contrafluxo(lambda i: 500 - i * 18, zona=sem_sentido)[1] == []


def test_a_duracao_e_o_tempo_andando_contra_e_nao_a_idade_da_trilha():
    """Quem está em cena há dez minutos e virou agora não anda contra há dez
    minutos. O alerta relatava a idade da trilha."""
    _, eventos = _contrafluxo(lambda i: 500 - i * 18, quadros=40)
    assert eventos[0].duracao_s <= 6, "é o tempo insistindo, não o da trilha"


def test_zona_sem_a_regra_nao_torna_a_area_restrita():
    """Zona desenhada para outra coisa não pode virar área restrita sozinha."""
    an = _analisador(["area_restrita"])
    zona = dict(ZONA_RESTRITA, regras={})
    dentro = ObjetoDetectado("pessoa", 0.9, (100, 100, 200, 300), track_id=1)
    assert an.atualizar([dentro], 1280, 720, zonas=[zona], agora=1000.0) == []


def test_veiculo_na_zona_proibida_dispara_e_pessoa_nao():
    an = _analisador(["veiculo_proibido", "area_restrita"])
    carro = ObjetoDetectado("carro", 0.88, (100, 100, 300, 300), track_id=1)
    ev = an.atualizar([carro], 1280, 720, zonas=[ZONA_SEM_VEICULO], agora=1000.0)
    assert [e.classe for e in ev] == ["veiculo_proibido"]

    # A mesma zona não torna a área restrita para gente.
    an2 = _analisador(["veiculo_proibido", "area_restrita"])
    gente = ObjetoDetectado("pessoa", 0.9, (100, 100, 200, 300), track_id=2)
    assert an2.atualizar([gente], 1280, 720, zonas=[ZONA_SEM_VEICULO], agora=1000.0) == []


def test_arma_em_cena_dispara_sem_zona_nem_horario():
    """O objeto estar ali já é o fato — não depende de nada mais."""
    an = _analisador(["arma_detectada"])
    arma = ObjetoDetectado("arma", 0.71, (400, 300, 460, 340), track_id=1)
    ev = an.atualizar([arma], 1280, 720, agora=1000.0)
    assert [e.classe for e in ev] == ["arma_detectada"]
    assert ev[0].severidade == 4


def test_janela_de_horario_inclui_a_madrugada_quando_cruza_a_meia_noite():
    """22:00–06:00 é a configuração de quem quer vigiar a madrugada: o fim
    menor que o início faz a janela atravessar a virada do dia."""
    from datetime import datetime

    from app.config import settings
    inicio, fim = settings.ACAO_HORARIO_INICIO, settings.ACAO_HORARIO_FIM
    try:
        settings.ACAO_HORARIO_INICIO, settings.ACAO_HORARIO_FIM = "22:00", "06:00"
        assert fora_do_horario(datetime(2026, 10, 8, 23, 0)) is False
        assert fora_do_horario(datetime(2026, 10, 8, 3, 0)) is False
        assert fora_do_horario(datetime(2026, 10, 8, 13, 0)) is True
    finally:
        settings.ACAO_HORARIO_INICIO, settings.ACAO_HORARIO_FIM = inicio, fim


def test_horario_comercial_e_a_janela_normal():
    from datetime import datetime
    assert fora_do_horario(datetime(2026, 10, 8, 13, 0)) is False
    assert fora_do_horario(datetime(2026, 10, 8, 4, 0)) is True


def test_pessoa_fora_de_hora_dispara_com_a_caixa_de_quem_foi_visto():
    """O alerta carrega a caixa: sem ela, quem revisa recebe "há alguém fora
    de hora" e não tem onde olhar."""
    from datetime import datetime

    from app.config import settings
    inicio, fim = settings.ACAO_HORARIO_INICIO, settings.ACAO_HORARIO_FIM
    try:
        # janela de um minuto no passado: agora está sempre fora dela
        settings.ACAO_HORARIO_INICIO, settings.ACAO_HORARIO_FIM = "00:00", "00:01"
        an = _analisador(["fora_de_horario"])
        gente = ObjetoDetectado("pessoa", 0.84, (300, 200, 400, 500), track_id=1)
        ev = an.atualizar([gente], 1280, 720, agora=1000.0)
        assert [e.classe for e in ev] == ["fora_de_horario"]
        assert ev[0].bbox == (300, 200, 400, 500)
    finally:
        settings.ACAO_HORARIO_INICIO, settings.ACAO_HORARIO_FIM = inicio, fim


# ----------------------------------------------------------------------
# Janela de horário por câmera
# ----------------------------------------------------------------------
def test_janela_da_camera_vence_a_do_sistema():
    """O sistema permite 07:00–19:00; esta câmera, só 08:00–09:00. Às 13h a
    pessoa está dentro do expediente e fora do que vale aqui."""
    from datetime import datetime

    from app.config import settings
    treze = datetime(2026, 10, 13, 13, 0)
    assert fora_do_horario(treze) is False
    assert fora_do_horario(treze, inicio="08:00", fim="09:00") is True
    # Só um dos lados definido: o outro continua vindo do sistema.
    assert fora_do_horario(treze, inicio="14:00") is True
    assert janela_horario("08:00", None) == ("08:00", "19:00")
    assert janela_horario(None, None) == (settings.ACAO_HORARIO_INICIO,
                                          settings.ACAO_HORARIO_FIM)


def test_janela_de_24h_na_camera_nao_acusa_ninguem():
    """Início igual ao fim é tudo permitido. Configuração legítima — deixa a
    ação armada com a vigilância suspensa — e é por isso que ela precisa
    aparecer no diagnóstico, não ser recusada."""
    from datetime import datetime
    for hora in (3, 13, 23):
        assert fora_do_horario(datetime(2026, 10, 13, hora, 0),
                               inicio="09:00", fim="09:00") is False


def test_analisador_usa_a_janela_da_camera():
    an = _analisador(["fora_de_horario"])
    an.horario_inicio, an.horario_fim = "00:00", "00:01"
    gente = ObjetoDetectado("pessoa", 0.84, (300, 200, 400, 500), track_id=1)
    assert [e.classe for e in an.atualizar([gente], 1280, 720, agora=1000.0)]         == ["fora_de_horario"]

    # A mesma câmera com a janela aberta não acusa nada, sem tocar no sistema.
    outra = _analisador(["fora_de_horario"])
    outra.horario_inicio, outra.horario_fim = "00:00", "00:00"
    assert outra.atualizar([gente], 1280, 720, agora=1000.0) == []


def test_caixa_fraca_nao_sustenta_alerta_de_horario():
    """Medido num quadro real do 1º Andar Lobby: o detector entregou uma
    pessoa a 0,32 de confiança ao lado de outra a 0,85. A fraca virava alerta
    de severidade 3 sozinha, enquanto a inferência a considerava fraca demais
    para entrar no registro como simples detecção."""
    an = _analisador(["fora_de_horario"])
    an.horario_inicio, an.horario_fim = "00:00", "00:01"
    fraca = ObjetoDetectado("pessoa", 0.32, (217, 10, 243, 108), track_id=1)
    firme = ObjetoDetectado("pessoa", 0.85, (242, 16, 308, 110), track_id=2)
    ev = an.atualizar([fraca, firme], 1280, 720, agora=1000.0)
    assert [e.track_id for e in ev] == [2]

    # Caixa confiante mas minúscula também não serve: é de onde vem a troca
    # de classe, e o alerta carrega a caixa para alguém conferir.
    an2 = _analisador(["fora_de_horario"])
    an2.horario_inicio, an2.horario_fim = "00:00", "00:01"
    mosca = ObjetoDetectado("pessoa", 0.91, (100, 100, 120, 160), track_id=3)
    assert an2.atualizar([mosca], 1280, 720, agora=1000.0) == []


def test_hora_invalida_e_recusada_na_api():
    from app.schemas import CameraUpdate, validar_hora
    assert validar_hora("  7:5 ") == "07:05"
    assert validar_hora("") is None          # limpar o campo volta ao padrão
    assert validar_hora(None) is None
    for ruim in ("25:00", "07:61", "sete", "07", "7:00:00"):
        with pytest.raises(ValueError):
            validar_hora(ruim)
    assert CameraUpdate(horario_inicio="22:00", horario_fim="06:00").horario_inicio == "22:00"


def test_fogo_e_aceito_e_fumaca_nao():
    """Fogo tem detector confiável; fumaça foi desligada em 16/10.

    O detector de fumaça continua escrito em `services/chama.py`, mas os
    sinais que ela deixa — cinza de baixa saturação e perda de textura — são
    os mesmos de uma mão em movimento. Medido ao calibrar: a mão rendeu região
    de 5,7% do quadro contra 1,7% da fumaça de verdade. Não há limiar de
    tamanho que deixe passar uma e barre a outra.

    Fogo fica porque a base é outra: cor específica somada à cintilação da
    borda, testada contra cone parado, camisa laranja e lâmpada.
    """
    assert validar_acoes(["fogo"]) == ["fogo"]

    for sem_detector in ("fumaca", "vandalismo"):
        with pytest.raises(ValueError, match="sem detector"):
            validar_acoes([sem_detector])


def test_acoes_novas_que_funcionam_sao_aceitas():
    aceitas = ["area_restrita", "fora_de_horario", "veiculo_proibido", "arma_detectada"]
    assert validar_acoes(aceitas) == acoes_validas(aceitas)
    assert len(validar_acoes(aceitas)) == 4


# ----------------------------------------------------------------------
# Zonas: a região desenhada só vale se disser o que vigia
# ----------------------------------------------------------------------
def _zona_quadrado(regras, tipo="POLIGONO"):
    """O polígono que o editor grava: pontos normalizados em 0..1."""
    return {"nome": "Vaga proibida", "tipo": tipo, "regras": regras,
            "pontos": [[0.2, 0.3], [0.7, 0.3], [0.7, 0.8], [0.2, 0.8]]}


def _pessoa_no_centro():
    # Centro em (0.45, 0.55) de 1280x720 — dentro do quadrado acima.
    return [ObjetoDetectado("pessoa", 0.9, (512, 360, 640, 432), 1)]


def test_zona_sem_regra_nao_dispara_nada():
    """O caso que o editor de zonas podia criar sem querer.

    Desenhar a região não basta: o analisador pergunta à zona *o que* ela
    guarda. Uma zona sem regra é consultada por ninguém, e seria uma
    vigilância que o operador acha que ligou e não existe.
    """
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"area_restrita"}
    eventos = analisador.atualizar(_pessoa_no_centro(), 1280, 720,
                                   zonas=[_zona_quadrado({})], agora=2000.0)
    assert [e.classe for e in eventos] == []


def test_zona_marcada_como_restrita_dispara():
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"area_restrita"}
    eventos = analisador.atualizar(_pessoa_no_centro(), 1280, 720,
                                   zonas=[_zona_quadrado({"restrita": True})],
                                   agora=2000.0)
    assert [e.classe for e in eventos] == ["area_restrita"]


def test_pessoa_fora_do_poligono_nao_dispara():
    """Sem isto a zona seria decorativa: qualquer pessoa da cena alertaria."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"area_restrita"}
    # Canto superior esquerdo, fora do quadrado.
    fora = [ObjetoDetectado("pessoa", 0.9, (10, 10, 60, 80), 2)]
    eventos = analisador.atualizar(fora, 1280, 720,
                                   zonas=[_zona_quadrado({"restrita": True})],
                                   agora=2000.0)
    assert [e.classe for e in eventos] == []


def test_o_nome_da_zona_chega_ao_alerta():
    """O alerta diz "onde", e o nome é a única coisa que responde isso.

    O caso real: `_zonas` montava o dicionário sem a chave `nome`, então o
    analisador lia string vazia e o alerta mandava o operador procurar uma
    região sem dizer qual.
    """
    from app.services.inferencia import Inferencia

    class Z:
        nome = "Vaga proibida"
        tipo = "POLIGONO"
        pontos = [[0.2, 0.3], [0.7, 0.3], [0.7, 0.8]]
        regras = {"restrita": True}

    class Cam:
        zonas = [Z()]

    montadas = Inferencia._zonas(Cam())
    assert montadas[0]["nome"] == "Vaga proibida", (
        "sem o nome, o alerta de zona não diz onde foi")


# ----------------------------------------------------------------------
# Ações que medem movimento: a rajada de quadros
# ----------------------------------------------------------------------
def test_uma_posicao_por_visita_nunca_vira_corrida():
    """O estado anterior: no rodízio a câmera entrega um quadro por visita.

    `Trilha.velocidade` exige cinco posições. Com uma por visita, e visitas a
    dezenas de segundos, a janela nunca fecha dentro do tempo em que a pessoa
    está em cena — "corrida" ficava ligável e permanentemente muda.
    """
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"corrida"}
    eventos = []
    # Quatro visitas, 30s entre elas, pessoa atravessando a cena.
    for i in range(4):
        x = 100 + i * 300
        eventos += analisador.atualizar(
            [ObjetoDetectado("pessoa", 0.9, (x, 300, x + 90, 500), 7)],
            1280, 720, agora=5000.0 + i * 30)
    assert [e.classe for e in eventos] == [], "sem pontos não há velocidade"


# ----------------------------------------------------------------------
# Corrida súbita: acelerar, e não apenas estar rápido
#
# As caixas abaixo são de uma pessoa no fundo da sala — corpo de 120 px num
# quadro de 720, que é 0,167 de altura. A 0,15 s por quadro, andar são 22 px
# (1,2 corpo/s, passo largo) e correr são 80 px (4,4 corpos/s).
# ----------------------------------------------------------------------
TOPO, BASE = 300, 420                      # corpo de 120 px, ao fundo
ANDANDO, CORRENDO = 22, 80                 # px por quadro


def _trajeto(n, x0, passo_px, tid=7, topo=TOPO, base=BASE):
    return [[(tid, (x0 + i * passo_px, topo, x0 + i * passo_px + 90, base))]
            for i in range(n)]


def _classes(quadros):
    from app.config import settings

    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"corrida"}
    return [e.classe for e in _alimentar(
        analisador, quadros, passo=settings.ACAO_CONTINUA_INTERVALO_S)]


def test_disparar_a_correr_vira_corrida():
    """O evento que a ação nomeia: andava, e de repente saiu correndo.

    É também o que a rajada de quadros tornou mensurável — sem posições
    próximas no tempo não há ritmo nenhum para comparar.
    """
    anda = _trajeto(7, 100, ANDANDO)
    corre = _trajeto(10, 100 + 7 * ANDANDO, CORRENDO)
    assert "corrida" in _classes(anda + corre)


def test_andar_depressa_a_cena_inteira_nao_e_corrida_subita():
    """Quem cruza o pátio sempre apressado não pode alertar a cada volta.

    O rótulo diz *súbita*, e é a aceleração que o sustenta. Sem comparar a
    pessoa com ela mesma, um limiar de velocidade transforma o passo largo de
    quem tem pressa num alerta por volta, todo dia.
    """
    assert _classes(_trajeto(17, 100, ANDANDO)) == []


def test_quem_ja_entrou_correndo_dispara():
    """Entrar em cena correndo também é corrida súbita.

    Era mudo, como contrapartida assumida. O teste com o Avenue mostrou o
    preço: nenhuma das corridas do dataset disparava, porque todo corredor
    entra no quadro já correndo — e quem foge na vida real também.
    """
    assert "corrida" in _classes(_trajeto(17, 100, CORRENDO))


def test_quem_ja_foi_visto_andando_ainda_precisa_acelerar():
    """A exceção vale para quem nunca foi visto devagar.

    Quem anda e segue andando no mesmo ritmo não vira corrida, por mais que
    o passo seja largo — a aceleração continua sendo exigida dela.
    """
    from app.services.vision import Trilha

    tr = Trilha(7, "pessoa")
    tr.viu_devagar = True
    an = AnalisadorAcoes()
    an.acoes_ativas = {"corrida"}
    an.trilhas[7] = tr
    from app.config import settings
    passo = settings.ACAO_CONTINUA_INTERVALO_S
    # 2,0 corpos/s constantes, sem salto sobre o ritmo anterior.
    eventos = []
    for i in range(17):
        x = 100 + i * int(2.0 * 120 * passo)
        eventos += an.atualizar([ObjetoDetectado("pessoa", 0.9, (x, TOPO, x + 90, BASE), 7)],
                                1280, 720, agora=9000.0 + i * passo)
    assert "corrida" not in [e.classe for e in eventos]


def test_andar_rente_a_lente_nao_vale_como_corrida():
    """A razão de o ritmo ser medido em alturas de corpo, e não em quadro.

    Esta pessoa tem corpo de 400 px porque está perto da câmera, e anda: 0,80
    corpo/s. Em fração de quadro, porém, ela desloca 0,44 por segundo — o
    dobro do piso de 0,22 que a regra antiga usava, que a fazia alertar
    andando enquanto emudecia para quem corria ao fundo.
    """
    from app.config import settings

    perto = _trajeto(17, 60, 85, topo=160, base=560)   # corpo de 400 px
    assert 85 / 1280 / settings.ACAO_CONTINUA_INTERVALO_S > 0.22, (
        "em fração de quadro este passo é rápido — é só perspectiva")
    assert _classes(perto) == []


def test_tremor_da_camera_nao_faz_todo_mundo_correr():
    """Imagem inteira deslizando: toda trilha arranca junta, e nenhuma é gente.

    O caso difícil é o reposicionamento da PTZ, porque desliza para um lado
    só — a checagem de direção não o pega, já que o movimento é dirigido. O
    que o denuncia é ninguém ter ficado parado.
    """
    quadros = []
    for i in range(17):
        d = 0 if i < 6 else (i - 5) * 80
        quadros.append([(1, (200 + d, TOPO, 290 + d, BASE)),
                        (2, (500 + d, 310, 590 + d, 430)),
                        (3, (800 + d, 290, 890 + d, 410))])
    assert _classes(quadros) == []


def test_uma_pessoa_correndo_entre_paradas_nao_e_tremor():
    """O contraponto do teste acima: o veto não pode engolir a corrida real.

    Duas pessoas imóveis e uma disparando — é justamente a cena para a qual a
    ação existe, e o que a separa do tremor é que o resto ficou onde estava.
    """
    quadros = []
    for i in range(17):
        d = 0 if i < 6 else (i - 5) * 80
        quadros.append([(1, (200 + d, TOPO, 290 + d, BASE)),
                        (2, (500, 310, 590, 430)),
                        (3, (800, 290, 890, 410))])
    assert "corrida" in _classes(quadros)


def test_a_mesma_corrida_alerta_uma_vez_so():
    """A pessoa segue correndo por vários quadros — o alerta é um.

    O represamento é por trilha, não por câmera: é o que deixa uma segunda
    pessoa disparar no mesmo instante sem ser engolida pela primeira.
    """
    anda = _trajeto(7, 100, ANDANDO)
    corre = _trajeto(24, 100 + 7 * ANDANDO, CORRENDO)
    assert _classes(anda + corre) == ["corrida"]


def test_so_a_camera_que_vigia_movimento_paga_a_rajada():
    """A rajada custa segundos por câmera — não pode rodar no parque inteiro."""
    from app.services.inferencia import Inferencia
    from app.services.vision import ACOES_CONTINUAS

    # Briga entrou aqui em 15/10: a heurística lê velocidade e agitação da
    # trajetória, e sem a rajada as duas valem zero. Roubo entrou em 03/11,
    # pelo mesmo motivo: a fuga é medida na régua da corrida.
    assert ACOES_CONTINUAS == {"corrida", "queda", "contrafluxo", "briga", "roubo"}

    class Cam:
        acoes_ativas = ["aglomeracao", "permanencia"]

    assert Inferencia._quer_rajada(Cam()) is False

    class ComCorrida(Cam):
        acoes_ativas = ["aglomeracao", "corrida"]

    assert Inferencia._quer_rajada(ComCorrida()) is True

    class SemNada(Cam):
        acoes_ativas = None

    assert Inferencia._quer_rajada(SemNada()) is False


# ----------------------------------------------------------------------
# Queda: despencar, ficar na horizontal e continuar lá
#
# Mesma pessoa nas duas posturas, com os pés na mesma linha do chão: em pé
# ocupa 90x240 px, no chão 240x90. O centro do corpo desce de 0,50 para 0,60
# da altura do quadro, que é o tranco que a regra procura.
# ----------------------------------------------------------------------
def _em_pe(n, x=400):
    return [[(7, (x, 240, x + 90, 480))] for _ in range(n)]


def _no_chao(n, x=400):
    return [[(7, (x, 390, x + 240, 480))] for _ in range(n)]


def _quedas(quadros):
    from app.config import settings

    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"queda"}
    return [e for e in _alimentar(analisador, quadros,
                                  passo=settings.ACAO_CONTINUA_INTERVALO_S)]


def test_cair_e_ficar_no_chao_vira_alerta():
    eventos = _quedas(_em_pe(6) + _no_chao(40))
    assert [e.classe for e in eventos] == ["queda"]


def test_abaixar_para_pegar_algo_do_chao_nao_e_queda():
    """O movimento normal que mais se parece com uma queda.

    A caixa achata igual, e o corpo desce com o mesmo tranco — a regra
    antiga, que exigia só essas duas coisas, alertava aqui. O que separa é
    levantar em seguida: ninguém se abaixa e fica.
    """
    assert _quedas(_em_pe(6) + _no_chao(7) + _em_pe(10)) == []


def test_quem_ja_estava_deitado_nao_caiu():
    """A câmera da área de descanso, que alertaria o dia inteiro.

    Postura de queda sem o tranco que leva a ela: a pessoa já estava no chão
    quando a câmera chegou, e isso não é uma queda — é alguém deitado.
    """
    assert _quedas(_no_chao(40)) == []


def test_a_queda_se_confirma_na_visita_seguinte():
    """Onde a confirmação acontece de verdade, e por que ela é forte.

    A rajada entrega ~1s por visita, menos que a permanência exigida. Quem
    confirma é a visita seguinte, dezenas de segundos depois — e aí o que se
    sabe não é só que o corpo foi ao chão, é que ele continuava lá meio
    minuto depois. A duração relatada é esse tempo, não um número fixo.
    """
    from app.config import settings

    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"queda"}
    t = 9000.0
    eventos = _alimentar(analisador, _em_pe(6) + _no_chao(2), t0=t,
                         passo=settings.ACAO_CONTINUA_INTERVALO_S)
    assert eventos == [], "um segundo no chão ainda não confirma"

    eventos = analisador.atualizar(
        [ObjetoDetectado("pessoa", 0.9, (400, 390, 640, 480), 7)],
        1280, 720, agora=t + 30.0)
    assert [e.classe for e in eventos] == ["queda"]
    assert eventos[0].duracao_s >= 29, "o alerta diz há quanto tempo está caído"


def test_a_mesma_queda_alerta_uma_vez_so():
    """A pessoa segue no chão por minutos — o alerta é um."""
    assert len(_quedas(_em_pe(6) + _no_chao(120))) == 1


# ----------------------------------------------------------------------
# Agressão física: três sinais que só juntos viram indício
# ----------------------------------------------------------------------
def _alimentar(analisador, caixas_por_quadro, t0=9000.0, passo=0.22):
    """Roda a sequência como a rajada entrega — quadros próximos no tempo."""
    eventos = []
    for i, caixas in enumerate(caixas_por_quadro):
        objs = [ObjetoDetectado("pessoa", 0.9, c, tid) for tid, c in caixas]
        eventos += analisador.atualizar(objs, 1280, 720, agora=t0 + i * passo)
    return eventos


def test_duas_pessoas_em_luta_corporal_viram_indicio():
    """Colados, rápidos e oscilando — os três sinais ao mesmo tempo."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"briga"}
    quadros = []
    for i in range(22):
        # Vaivém no mesmo lugar: o par percorre muito caminho e termina onde
        # começou, e as caixas seguem sobrepostas o tempo todo. A amplitude é
        # de ~44 px por passo, que numa cena de 1280 px equivale a um corpo
        # oscilando algumas dezenas de centímetros — a escala de um agarrão.
        d = 22 if i % 2 == 0 else -22
        quadros.append([(1, (600 + d, 300, 700 + d, 500)),
                        (2, (620 - d, 305, 720 - d, 505))])
    classes = [e.classe for e in _alimentar(analisador, quadros)]
    assert "briga" in classes


def test_duas_pessoas_correndo_lado_a_lado_nao_e_briga():
    """A armadilha: correr também é rápido e também é a dois.

    O que separa é a direção — correr é dirigido, lutar oscila. Sem este
    teste, toda dupla atravessando o corredor viraria agressão física.
    """
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"briga"}
    quadros = []
    for i in range(22):
        x = 200 + i * 34          # os dois seguem na mesma direção
        quadros.append([(1, (x, 300, x + 100, 500)),
                        (2, (x + 55, 305, x + 155, 505))])
    assert "briga" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_pessoa_sozinha_agitada_nao_e_briga():
    """Briga é relação entre duas pessoas: uma só nunca basta."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"briga"}
    quadros = []
    for i in range(22):
        d = 30 if i % 2 == 0 else -30
        quadros.append([(1, (600 + d, 300, 700 + d, 500))])
    assert "briga" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_esbarrao_curto_nao_e_briga():
    """Um encontrão dura um quadro; uma luta dura. Sem a exigência de
    persistência, todo cruzamento de corredor alarmaria."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"briga"}
    quadros = []
    for i in range(4):            # menos que ACAO_BRIGA_DURACAO_S
        d = 26 if i % 2 == 0 else -26
        quadros.append([(1, (600 + d, 300, 700 + d, 500)),
                        (2, (660 - d, 305, 760 - d, 505))])
    assert "briga" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_a_mesma_luta_no_fundo_da_sala_tambem_dispara():
    """A razão de o ritmo ser medido em alturas de corpo, e não em quadro.

    Mesmo vaivém, mesma proporção, corpos com um terço do tamanho porque
    estão longe da lente. Em fração de quadro esta luta percorre 0,05 por
    segundo contra o piso antigo de 0,12 — ficava muda, e o operador só era
    avisado de brigas que acontecessem perto da câmera.
    """
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"briga"}
    quadros = []
    for i in range(22):
        d = 7 if i % 2 == 0 else -7
        quadros.append([(1, (600 + d, 300, 633 + d, 366)),
                        (2, (607 - d, 302, 640 - d, 368))])

    eventos = _alimentar(analisador, quadros)
    assert "briga" in [e.classe for e in eventos]
    trilha = analisador.trilhas[1]
    assert trilha.caminho() < 0.12, "em fração de quadro não passaria do piso antigo"
    assert trilha.caminho_corporal() >= analisador.ritmo_briga


def test_tremor_da_camera_nao_vira_briga():
    """O caso em que os dois sinais de movimento passam e nada aconteceu.

    Com a imagem saltando, toda trilha fica rápida e errática de uma vez —
    medido, ritmo 0,77 e agitação 1,00, acima dos dois pisos. Quaisquer duas
    pessoas encostadas em cena virariam indício de agressão. É o único
    portão que segura este caso.
    """
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"briga"}
    quadros = []
    for i in range(22):
        d = 30 if i % 2 == 0 else -30
        quadros.append([(1, (600 + d, 300, 700 + d, 500)),
                        (2, (620 + d, 305, 720 + d, 505)),
                        (3, (200 + d, 300, 300 + d, 500))])
    assert "briga" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_o_alerta_de_briga_pede_confirmacao_humana():
    """O texto que o operador lê não pode afirmar o que o sistema não sabe.

    O título vem do catálogo e o corpo do alerta é montado no registro; os
    dois são checados juntos porque é o par que chega à tela. Abraço efusivo
    e brincadeira produzem os mesmos três sinais, e um alerta que afirma
    agressão entre duas pessoas identificadas tem consequência real.
    """
    from app.services.vision import ROTULO_ACAO, detalhe_acao

    assert "possível" in ROTULO_ACAO["briga"].lower()
    assert "confirmação" in detalhe_acao("briga")


# ----------------------------------------------------------------------
# Possível roubo — acrescentado em 2026-11-03
# ----------------------------------------------------------------------
# Corpos de 120 px de altura num quadro de 720: um passo de 90 px a cada
# 0,22s é ~2,2 alturas de corpo por segundo, acima do piso da corrida.
def _corpo(x, y=300):
    return (x, y, x + 50, y + 120)


def _abordagem(quadros_de_contato=6):
    """Duas pessoas coladas, paradas — a abordagem."""
    return [[(1, _corpo(200)), (2, _corpo(215))]
            for _ in range(quadros_de_contato)]


def _roubo_classico():
    """Contato, e então a pessoa 1 dispara para a direita; a 2 fica."""
    quadros = _abordagem()
    for i in range(1, 10):
        quadros.append([(1, _corpo(200 + i * 90)), (2, _corpo(215))])
    return quadros


def test_contato_seguido_de_fuga_e_indicio_de_roubo():
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"roubo"}
    eventos = _alimentar(analisador, _roubo_classico())
    roubos = [e for e in eventos if e.classe == "roubo"]
    assert len(roubos) == 1, [e.classe for e in eventos]
    assert roubos[0].track_id == 1, "o alerta aponta quem fugiu"
    assert roubos[0].envolvidos == 2


def test_o_rotulo_de_roubo_nao_afirma_o_crime():
    """O vídeo mostra contato e fuga, não o objeto mudando de mão."""
    from app.services.vision import ROTULO_ACAO

    assert ROTULO_ACAO["roubo"].lower().startswith("possível")


def test_roubo_usa_a_rajada_de_quadros():
    """Sem trajetória densa não há fuga para medir."""
    from app.services.vision import ACOES_CONTINUAS

    assert "roubo" in ACOES_CONTINUAS


def test_correr_sem_contato_antes_nao_e_roubo():
    """Quem só corre é corrida — e só se a câmera vigia corrida."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"roubo"}
    quadros = [[(1, _corpo(200)), (2, _corpo(900))] for _ in range(6)]
    for i in range(1, 10):
        quadros.append([(1, _corpo(200 + i * 70)), (2, _corpo(1100))])
    assert "roubo" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_dois_que_se_encontram_e_saem_correndo_juntos_nao_e_roubo():
    """Os dois disparam: ninguém ficou para trás."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"roubo"}
    quadros = _abordagem()
    for i in range(1, 10):
        quadros.append([(1, _corpo(200 + i * 90)), (2, _corpo(215 + i * 90, 380))])
    assert "roubo" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_despedir_se_e_sair_andando_nao_e_roubo():
    """Conversa colada e cada um segue o seu caminho, a passo."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"roubo"}
    quadros = _abordagem()
    for i in range(1, 16):
        quadros.append([(1, _corpo(200 + i * 25)), (2, _corpo(215))])
    assert "roubo" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_pressa_muito_depois_do_contato_nao_e_roubo():
    """Quem conversou, se afastou e só correu bem depois não fugiu daquele
    contato — ele já tinha vencido."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"roubo"}
    quadros = _abordagem()
    # Sai andando para o lado (~0,8 corpo/s, 21 px a cada 0,22 s) e espera
    # parado mais que ACAO_ROUBO_JANELA_S. O passo era um salto de 165 px num
    # quadro só — 6 corpos/s, que só não contava como fuga porque a régua
    # encolhia o movimento horizontal (veja `Trilha.aspecto`).
    for i in range(1, 9):
        quadros.append([(1, _corpo(215 + i * 21)), (2, _corpo(215))])
    for _ in range(30):
        quadros.append([(1, _corpo(380)), (2, _corpo(215))])
    for i in range(1, 9):
        quadros.append([(1, _corpo(380 + i * 90)), (2, _corpo(215))])
    assert "roubo" not in [e.classe for e in _alimentar(analisador, quadros)]


def test_fuga_do_roubo_nao_vira_tambem_corrida():
    """Uma cena, uma ocorrência: quem fugiu correu, mas o fato é o roubo."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"roubo", "corrida"}
    classes = [e.classe for e in _alimentar(analisador, _roubo_classico())]
    assert classes.count("roubo") == 1
    assert "corrida" not in classes, classes


def test_correr_sem_contato_segue_sendo_corrida_com_roubo_ligado():
    """Ligar roubo não pode calar a corrida de quem não encostou em ninguém."""
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"roubo", "corrida"}
    quadros = [[(1, _corpo(200, 300))] for _ in range(6)]
    for i in range(1, 10):
        quadros.append([(1, _corpo(200 + i * 90, 300))])
    classes = [e.classe for e in _alimentar(analisador, quadros)]
    assert "corrida" in classes and "roubo" not in classes


def test_roubo_desligado_nao_emite():
    analisador = AnalisadorAcoes()
    analisador.acoes_ativas = {"briga"}
    assert "roubo" not in [e.classe for e in _alimentar(analisador, _roubo_classico())]


# ----------------------------------------------------------------------
# Corrida coletiva — acrescentada em 2026-11-03 (UMN)
# ----------------------------------------------------------------------
def _multidao(n_aceleram, n_parados, ritmo_antes, ritmo_depois, quadros_antes=30,
              quadros_depois=12, passo=0.23):
    """Pessoas de 40 px num quadro de 320x240; parte acelera, parte fica parada."""
    an = AnalisadorAcoes()
    an.acoes_ativas = {"corrida"}
    eventos, t = [], 5000.0
    xs = {i: 20.0 + i * 18 for i in range(n_aceleram)}
    for k in range(quadros_antes + quadros_depois):
        ritmo = ritmo_antes if k < quadros_antes else ritmo_depois
        objs = []
        for i in range(n_aceleram):
            # Cada um anda para um lado — não é a câmera se mexendo.
            sentido = 1 if i % 2 == 0 else -1
            xs[i] += sentido * ritmo * 40 * passo
            y = 60 + i * 12
            objs.append(ObjetoDetectado("pessoa", 0.9, (int(xs[i]), y, int(xs[i]) + 14, y + 40),
                                        i + 1))
        for j in range(n_parados):
            x = 200 + j * 10
            objs.append(ObjetoDetectado("pessoa", 0.9, (x, 150, x + 14, 190), 100 + j))
        eventos += an.atualizar(objs, 320, 240, agora=t)
        t += passo
    return {e.track_id for e in eventos if e.classe == "corrida"}


def test_multidao_que_acelera_junta_vira_corrida_mesmo_abaixo_do_piso():
    """O corredor do UMN: na imagem a multidão corre a ~0,9 corpo/s, abaixo
    do piso individual de 1,6, mas três vezes o ritmo de antes."""
    alertados = _multidao(6, 0, ritmo_antes=0.3, ritmo_depois=0.95)
    assert len(alertados) >= 5, alertados


def test_tres_andando_entre_muitos_parados_nao_e_multidao_em_panico():
    """O Avenue: gente parada puxa a base da cena para baixo, e três
    pedestres andando não podem virar pânico."""
    alertados = _multidao(3, 8, ritmo_antes=0.3, ritmo_depois=0.95)
    assert alertados == set(), alertados
