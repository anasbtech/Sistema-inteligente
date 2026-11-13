"""Aizon Guardian: sinais de socorro e de possível violência nas câmeras.

O módulo identifica **sinais de risco** e os entrega a uma pessoa para
avaliar. Nunca conclui que houve crime nem aponta culpado: o que ele vê é um
gesto, uma trajetória, uma coincidência de sinais — e é assim que fala deles.

    gesto.py       Signal for Help: geometria da mão e sequência no tempo
    maos.py        MediaPipe Hands sobre o recorte de cada pessoa
    correlacao.py  sinais → ocorrência com prioridade
    evidencia.py   vídeo dos segundos antes e depois, criptografado
    servico.py     laço por câmera (só nas que têm o Guardian ligado)
"""
