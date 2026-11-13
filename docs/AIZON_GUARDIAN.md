# Aizon Guardian

Módulo do AizonVMS para **identificação precoce de possíveis situações de
risco**, com foco em sinais de socorro e de possível violência contra mulheres.

> A IA **identifica possíveis sinais de risco**. Ela nunca confirma crime nem
> aponta culpado. Toda ocorrência exige **avaliação humana justificada**, e
> cada consulta, vídeo assistido e avaliação fica na trilha de auditoria.

## O que ele observa

| Sinal | De onde vem | Peso |
|---|---|---|
| **Gesto Signal for Help** (palma com o polegar dobrado → dedos fechando sobre o polegar) | laço próprio do Guardian: MediaPipe Hands + sequência no tempo | crítico sozinho |
| Possível agressão física | análise de ações existente (`briga`, por trajetória) | 4 |
| Contato seguido de fuga | análise de ações existente (`roubo`) | 4 |
| Queda de pessoa | análise de ações existente (`queda`) | 3 |
| Corrida súbita | análise de ações existente (`corrida`) | 1 |

Os sinais de trajetória já vinham da Análise de ações, calibrados nestas
câmeras desde outubro (velocidade em alturas de corpo, persistência, veto de
câmera que se moveu). O Guardian **não duplica** esses detectores: ele os
recebe e correlaciona.

## Correlação e prioridade (`services/guardian/correlacao.py`)

Sinais na mesma câmera dentro de `GUARDIAN_JANELA_S` (180 s) formam uma
**ocorrência** (`GRD-00001`), e a prioridade é recalculada a cada sinal:

- **Crítica**: gesto de socorro; agressão + queda; agressão + contato com
  fuga; agressão repetida.
- **Alta**: agressão; contato com fuga; queda + corrida.
- **Média**: queda.
- **Baixa**: corrida sozinha.

O gesto e os sinais com severidade ≥ 3 também viram **alerta** no fluxo de
alertas que já existe (fila, notificação, toast). A ocorrência guarda o
código do alerta.

## O gesto (`services/guardian/gesto.py`, `maos.py`)

1. O laço da câmera pega as **pessoas que a análise geral já achou** (não roda
   outro detector de pessoa).
2. Nas pessoas com altura ≥ `GUARDIAN_PESSOA_MIN_PX` (150 px), roda o
   MediaPipe Hands na metade de cima, ampliada.
3. Cada mão é classificada pelos 21 pontos, em larguras de palma (independe
   da distância), em **palma com o polegar recolhido**, **punho com o polegar
   preso** ou outro.
4. O sinal fecha quando a mesma mão passa do primeiro ao segundo tempo em até
   3 s, com cada tempo visto em ≥ 2 quadros. A mesma mão gera um aviso por
   20 s.

**Limite medido (10/11/2026):** nas câmeras de sala, as mãos achadas tinham
**18 e 32 px**. Para os dedos se separarem é preciso ≥ **48 px**. O gesto só é
legível em **câmera próxima** (recepção, balcão, entrada). A tela mostra, por
câmera, o tamanho mediano das mãos vistas e se o gesto é legível ali.

Custo: 40–190 ms por pessoa avaliada, em CPU, a 4 quadros/s, só nas câmeras
com o Guardian ligado e só em pessoas grandes o bastante.

## Vídeo de evidência (`services/guardian/evidencia.py`)

Cada câmera do Guardian guarda os últimos ~18 s em memória (JPEG reduzido).
Quando uma ocorrência nasce, o serviço grava **8 s antes e 6 s depois** num
arquivo **cifrado** (Fernet: AES-128-CBC + HMAC-SHA256), com a chave em
`.secrets/guardian.key`. A chave não vai para o git, e quem copiar a pasta
`evidencias/` não consegue ver o vídeo. A tela toca o vídeo decifrado na hora,
como MJPEG.

## Arquivos

| Arquivo | Função |
|---|---|
| `app/services/guardian/gesto.py` | geometria da mão e sequência do gesto |
| `app/services/guardian/maos.py` | MediaPipe Hands no recorte da pessoa |
| `app/services/guardian/correlacao.py` | sinais → ocorrência e prioridade |
| `app/services/guardian/evidencia.py` | memória de quadros, clipe cifrado |
| `app/services/guardian/servico.py` | laço por câmera e gravação dos clipes |
| `app/routers/guardian.py` | API: estado, ocorrências, vídeo, avaliação, estatísticas |
| `frontend/static/js/views_guardian.js` | tela Aizon Guardian |
| `tests/test_guardian.py` | 15 testes (gesto, prioridade, cifra, fluxo pela API) |

Alterações em arquivos existentes:

- `models.py`: `Camera.guardian` e a tabela `guardian_ocorrencias`;
- `run.py`: migração da coluna;
- `routers/deteccoes.py`: texto do alerta do gesto, e repasse ao Guardian;
- `vision.py`: rótulo e severidade de `sinal_socorro`;
- `main.py`, `config.py`, a tela de câmeras (interruptor), o menu e o CSS.

## Instalação

```
py -3.13 -m pip install --no-deps mediapipe==1.2.2 absl-py
py -3.13 -m pip install cryptography==50.0.2
```

O `--no-deps` é obrigatório: a instalação padrão do MediaPipe traz
`opencv-contrib-python`, que entra em conflito com o OpenCV do sistema.

Modelo das mãos (fora do git, em `models/guardian/`):

```
curl -L -o models/guardian/hand_landmarker.task https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task
```

## Ligar numa câmera

**Câmeras › Configurar › Aizon Guardian.** Para o gesto, prefira câmeras onde
a pessoa fica perto da lente. Nas demais, o Guardian usa os sinais de
trajetória, que exigem a **Análise de ações** ligada com as ações `briga`,
`queda`, `roubo` e `corrida`.

## Configuração

| Variável | Padrão | Para quê |
|---|---|---|
| `GUARDIAN_ATIVO` | `true` | liga o laço do gesto (exige `INFERENCIA`) |
| `GUARDIAN_FPS` | `4` | quadros por segundo por câmera |
| `GUARDIAN_PESSOA_MIN_PX` | `150` | altura mínima da pessoa para procurar as mãos |
| `GUARDIAN_PESSOAS_POR_QUADRO` | `4` | quantas pessoas, das maiores |
| `GUARDIAN_JANELA_S` | `180` | sinais nessa janela formam uma ocorrência |
| `GUARDIAN_CLIPE_ANTES_S` / `DEPOIS_S` | `8` / `6` | duração do vídeo de evidência |

## Privacidade e LGPD

- Nenhuma identificação de pessoa é feita pelo Guardian; a ocorrência guarda
  sinais, não nomes.
- Vídeo cifrado em repouso; imagens servidas só com token e sem cache.
- Acesso pelo perfil que trata alertas; avaliar exige também `edit`.
- Auditoria: `GUARDIAN_CONSULTA`, `GUARDIAN_VIDEO`, `GUARDIAN_AVALIACAO`.
- O expurgo por prazo segue a política geral de retenção do sistema.

## Próximas etapas

1. **Perseguição e contenção**: novas regras no analisador de trajetórias
   (uma pessoa seguindo outra a distância constante; duas trilhas coladas e
   uma imóvel).
2. **Modelo temporal (LSTM/GRU)** sobre a sequência de pose: exige vídeos
   rotulados desta instalação. As avaliações humanas do Guardian (risco
   confirmado / falso alarme) são justamente esse material.
3. **Câmeras dedicadas ao gesto**: posicionadas na altura do rosto em
   balcões e recepções.
