# Inteligência veicular

Rastreia cada veículo nas câmeras que veem via, pátio ou garagem e registra
**uma passagem por veículo**: tipo, cor, marca/modelo e placa, cada um com
a sua confiança. Quando um atributo não é afirmado, a passagem guarda o motivo
("modelo ainda não treinado", "confiança abaixo do piso", "nenhuma leitura de
placa com qualidade"). O módulo nunca preenche um campo com palpite.

## Como funciona

```
RTSP (leitor já existente, mesma conexão)
  └─ laço por câmera, 2 quadros/s ─ YOLO11s (GPU Intel via OpenVINO)
        └─ ByteTrack da câmera ─ caixas na tela (evento veiculos_ao_vivo)
              └─ passagem (melhor imagem, votação de categoria, veículo parado)
                    ├─ prévia do tipo (uma trilha por vez) ─ tarja da caixa ao vivo
                    └─ fila de identificação (uma por vez, em CPU)
                          ├─ contorno do veículo (YOLO11s-seg)
                          ├─ vetor CLIP ViT-B-16 do veículo isolado
                          ├─ sonda de tipo ─┐
                          ├─ sonda de cor  ─┤ só afirma classes validadas
                          ├─ sonda de marca/modelo ─┘ acima do piso de cada classe
                          └─ placa (services/placa.py) por consenso entre recortes
                                └─ gravação: deteccoes (registro geral) + passagens_veiculo
```

| Arquivo | Responsabilidade |
|---|---|
| `app/services/veiculos/deteccao.py` | Detector YOLO11 só de veículos; escolhe CUDA, GPU Intel ou CPU |
| `app/services/veiculos/rastreio.py` | ByteTrack, uma instância por câmera |
| `app/services/veiculos/passagem.py` | Agrega quadros em passagens; melhor imagem; veículo parado; regra de porte |
| `app/services/veiculos/identificacao.py` | Contorno, vetor CLIP, sondas de cor e marca/modelo, placa |
| `app/services/veiculos/catalogo_br.py` | Modelos da frota brasileira que a rotulagem oferece |
| `app/services/veiculos/persistencia.py` | Grava o registro geral e a passagem, com recorte, quadro inteiro e placa |
| `app/services/veiculos/servico.py` | Laços por câmera e fila de identificação; liga e desliga com o interruptor |
| `app/routers/veiculos.py` | API: estado, histórico, detalhe, imagens, rotulagem |
| `frontend/static/js/views_veiculos.js` | Tela "Inteligência veicular" |
| `scripts/veiculos_treinar.py` | Treina e valida as sondas; define o que pode ser afirmado |
| `tests/test_veiculos.py` | Testes do módulo |

Alterações em arquivos existentes:

- `models.py` (campo `Camera.inteligencia_veicular` e tabelas `passagens_veiculo` e `rotulos_veiculo`);
- `run.py` (migração da coluna);
- `video.py` (ritmo cheio de leitura enquanto há rastreio, sem conexão nova);
- `inferencia.py` (o laço geral deixa os veículos dessas câmeras para o módulo);
- `main.py`, telas `app.js`, `feeds.js`, `api.js`, `views_op.js`, `index.html` e o CSS.

## Ligar numa câmera

1. Em **Câmeras › Configurar**, ligue **Alcança via, pátio ou garagem** e
   **Inteligência veicular**.
2. Em até 10 s o laço da câmera sobe. Na primeira vez, o modelo é convertido
   para OpenVINO e compilado para a GPU integrada, o que leva alguns minutos.
   Depois, o cache em `models/yolo11s_480_openvino_model/cache_gpu` evita repetir.
3. A tela **Inteligência veicular** mostra o vídeo com as caixas
   (`#id CARRO · BRANCO · ABC1D23`), os veículos em cena e o histórico.

Com o interruptor ligado, os carros, motos, caminhões e ônibus dessa câmera são
registrados **só** pelo módulo (uma linha por passagem, com `track_id`). As
ações, como "veículo em área proibida", continuam vendo os veículos como antes.

## Arquivos de modelo (`models/`, fora do git)

| Arquivo | Origem |
|---|---|
| `yolo11s.pt` | github.com/ultralytics/assets (release v8.3.0) |
| `yolo11s-seg.pt` | idem |
| `veiculos/cor/base_inicial.npz` | 94 vetores rotulados à mão em 06/11/2026 (Portaria e Garagem) |
| `veiculos/cor/sonda.npz` + `relatorio.json` | gerados por `scripts/veiculos_treinar.py` |
| `veiculos/marca_modelo/…` | gerados quando houver rótulos suficientes |

O CLIP (`ViT-B-16`, pesos `laion2b_s34b_b88k`) é baixado pelo `open_clip` na
primeira passagem, para o cache do Hugging Face.

## Tipo do veículo

| Tipo | Exemplos |
|---|---|
| `carro` | hatch, sedã, SUV |
| `picape` | Strada, Saveiro, Montana, Toro, Hilux, Ranger |
| `kombi` | Volkswagen Kombi (tipo próprio a pedido da operação: é a van mais comum na Portaria) |
| `van` | Fiorino, Expert, Ducato, Sprinter, Master |
| `caminhao_pequeno` | VUC, 3/4, baú leve (Accelo, VW Delivery) |
| `caminhao_grande` | toco, truck, carreta, bitrem, baú grande |
| `onibus` | ônibus e micro-ônibus |
| `motocicleta` | motos e scooters |
| `veiculo` | resposta quando nenhuma fonte tem convicção — nunca é rótulo |

O detector YOLO só conhece carro, moto, caminhão e ônibus. Não existe "van"
nele, e em 09/11/2026 ele chamava a Kombi da Portaria de "ônibus 49%" na tarja
ao vivo. Por isso:

- a **sonda de tipo** decide, com o mesmo regime de pisos da cor;
- a tarja ao vivo usa a **prévia do tipo** da trilha, feita uma vez assim que
  o veículo tem 3 quadros. Antes dela, vale a votação dos quadros com a
  ressalva de porte: caminhão e ônibus só acima de 0,92; abaixo disso, "veículo";
- sem a sonda, caminhão sai "veículo", porque o detector não sabe o porte;
- a Kombi parada que o rastreio perde e reencontra herda o tipo já visto;
- no registro geral (lista de objetos, regras de zona) os tipos voltam ao
  vocabulário do detector: picape e van como `carro`, os dois portes como `caminhao`.

Base inicial (`models/veiculos/tipo/base_inicial.npz`): recortes do histórico
da Portaria e da Garagem conferidos um a um em 09/11/2026, mais as passagens
gravadas pelo módulo. Validação: precisão 97,4% respondendo 97% dos veículos.
Caminhão grande tinha 4 exemplos e ficou travado. Ônibus não apareceu em
nenhuma câmera. Os dois liberam sozinhos quando houver 5 rótulos validados.

## Marca e modelo (só carros)

Medido em 10/11/2026 nos 31 carros rotulados da Portaria: o CLIP grande
(ViT-L/14-quickgelu, pesos DFN2B), comparando o recorte com os 87 modelos de
carro do catálogo, acerta o modelo exato em 15 (48%) e erra até com 90% de
confiança (Fox → Etios). O CLIP pequeno não serve: carros de modelos
diferentes chegam a 0,90 de semelhança nele. Por isso:

- `scripts/veiculos_modelo_rodar.py` roda em processo próprio (o modelo usa
  ~1,7 GB e ~12 s de CPU por carro): analisa cada passagem de carro, guarda o
  vetor (`passagens_veiculo.vetor_modelo`) e as **3 sugestões** do catálogo;
- a ficha mostra as sugestões como botões ("não conferidas"): rotular é um
  clique;
- marca/modelo só é **afirmado** pela sonda `models/veiculos/modelo_carro/`,
  treinada com os rótulos sobre esse vetor, com o piso de 95% de cada modelo.
  O analisador treina de novo sozinho quando há rótulo novo e reaplica a
  todas as passagens já vistas;
- moto não recebe modelo.

    py -3.13 -m scripts.veiculos_modelo_rodar      # deixar rodando junto do servidor

## Treinar e medir cor e marca/modelo

Cor e marca/modelo dependem de exemplos **desta instalação** (câmera, luz e
frota daqui). O CLIP sem treino específico foi medido e reprovado:

- **cor:** acertou 56% dos veículos;
- **marca/modelo:** chamou de outra coisa 8 de 11 Kombis brancas da Portaria,
  uma delas de "Jeep Renegade", com 69% de confiança.

O ciclo de treino:

1. Na tela, abra uma passagem e preencha **O que este veículo é?** (tipo,
   cor, marca/modelo). Use o
   filtro "só sem rótulo". Deixe em branco o que não souber.
2. Rode `py -3.13 -m scripts.veiculos_treinar`. Use `--simular` para só medir
   e `--so tipo` ou `--so cor` para treinar um atributo só. Leva segundos em
   CPU livre, ou alguns minutos com a triagem facial rodando junto.
3. O script julga cada exemplo com uma sonda treinada **sem** ele. Para cada
   classe, procura o menor piso de confiança com precisão de pelo menos
   `VEICULO_PRECISAO_ALVO` (95%). Classe que não chega a essa precisão, ou que
   tem menos de `VEICULO_EXEMPLOS_MIN` (5) exemplos, fica **travada**: nunca é
   afirmada.
4. A sonda nova vale já na próxima passagem, sem reiniciar.

Situação em 06/11/2026, cor: precisão 98,2% respondendo 61,5% dos veículos,
com branco, prata/cinza, preto e vermelho liberados. Azul tem 3 exemplos e está
travado. Prata e cinza ficaram juntos porque, nessas câmeras, nem a rotulagem
humana os separa com segurança.

Os pisos são escolhidos na mesma validação que os mede, então a precisão real
tende a ser um pouco menor. A medida se firma com mais rótulos.

Marca/modelo tem zero rótulos e está travado. Cada modelo precisa de pelo menos
5 exemplos rotulados (de preferência 15 ou mais, em dias e horários
diferentes).

## Configuração (`.env` ou Configurações)

| Variável | Padrão | Para quê |
|---|---|---|
| `VEICULO_ATIVO` | `true` | Liga o serviço; cada câmera ainda precisa do interruptor |
| `VEICULO_MOTOR` | `auto` | `auto`, `cuda`, `openvino-gpu`, `openvino-cpu` |
| `VEICULO_IMGSZ` | `480` | Entrada do detector (713 → 390 ms por quadro contra 640, na GPU Intel) |
| `VEICULO_FPS` | `2.0` | Quadros por segundo por câmera |
| `VEICULO_CONF_MIN` / `VEICULO_REGISTRO_CONF_MIN` | `0.35` / `0.50` | Caixa na tela / passagem no registro |
| `VEICULO_SUMIU_S` | `4` | Ausência que encerra a passagem |
| `VEICULO_PASSAGEM_MAX_S` | `120` | Veículo parado: registra e segue rastreado sem novo registro |
| `VEICULO_PARADO_MEMORIA_S` | `1800` | Trilha nova no lugar de um parado conhecido é o mesmo veículo |
| `VEICULO_PLACA_VOTOS` | `2` | Leituras iguais para afirmar a placa (ou uma acima de 0,90) |
| `VEICULO_PRECISAO_ALVO` / `VEICULO_EXEMPLOS_MIN` | `0.95` / `5` | Regra de liberação das sondas |
| `VEICULO_CLIP_THREADS` | `2` | Threads do CLIP (não tomar a CPU do reconhecimento facial) |

## Medições (Portaria, CAM-019, 06/11/2026, 4 min gravados)

- **Detector:** YOLO11s achou 15% mais veículos que o YOLOv8s atual. Na GPU
  Intel, com entrada de 480 px, leva 200–390 ms por quadro com a máquina
  carregada.
- **Rastreio e passagem:** 9 passagens para cerca de 9 veículos. Antes da
  memória de parados e do NMS sem classe, eram 21: a Kombi estacionada na
  borda virava 12.
- **Identificação:** de 7 a 45 s por passagem com a CPU disputada (triagem
  facial rodando). Fica numa fila separada e não atrasa o rastreio.
- **Placa:** nenhuma lida nesses 4 minutos, o que é coerente com o histórico
  (35 de 2.240 veículos). Placa de portaria vista de cima e de longe tem poucos
  pixels. É limite da câmera, não do módulo.

## Limites conhecidos

- A GPU integrada é dividida com o detector geral. Com mais de 2 câmeras
  veiculares, os quadros por segundo de cada uma caem.
- Ano e geração não são estimados: não há base rotulada para isso. O campo
  existe e fica vazio.
- A reidentificação entre câmeras ainda não está ligada. O vetor CLIP de cada
  passagem já fica gravado (`passagens_veiculo.vetor`) para isso.
- O treino pesado com CompCars, Stanford Cars ou VeRi-776 não foi feito: exige
  GPU dedicada, e essas bases não cobrem a frota brasileira. As sondas locais
  servem de ponte. Um classificador ajustado nessas bases precisaria passar pela
  mesma validação local antes de ser ligado.
