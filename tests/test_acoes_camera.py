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
    assert detalhe_acao("briga") == ""      # sem detector, nada a prometer


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
# quadro de 720, que é 0,167 de altura. Andar são 22 px por quadro (0,69
# corpo/s) e correr são 80 px (2,50 corpo/s).
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


def test_quem_ja_entrou_correndo_nao_dispara_sem_a_transicao():
    """A contrapartida assumida da regra, escrita para não surpreender.

    Ritmo de corrida do primeiro ao último quadro, sem transição observada:
    fica mudo. Quem quiser pegar também esse caso baixa `ACAO_CORRIDA_SALTO`
    — e passa a pagar em alerta de quem apenas anda depressa.
    """
    assert _classes(_trajeto(17, 100, CORRENDO)) == []


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
    # trajetória, e sem a rajada as duas valem zero.
    assert ACOES_CONTINUAS == {"corrida", "queda", "contrafluxo", "briga"}

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
