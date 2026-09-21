# Segurança inteligente — Centro Integrado de Videomonitoramento

Sistema completo de videomonitoramento com reconhecimento facial, detecção de
objetos e análise de ações. **Front-end, back-end e banco num único projeto
Python** — um comando e está no ar.

```bash
pip install -r requirements-min.txt
python run.py
```

O navegador abre em `http://127.0.0.1:8000`. Sem Docker, sem PostgreSQL, sem
Redis, sem npm.

| E-mail | Senha | Perfil |
|---|---|---|
| `admin@pm.gov.br` | `Admin@2026` | Administrador |
| `supervisor@pm.gov.br` | `Super@2026` | Supervisor |
| `operador@pm.gov.br` | `Oper@2026` | Operador |
| `analista@pm.gov.br` | `Anal@2026` | Analista |
| `auditoria@mp.gov.br` | `Audi@2026` | Auditor |

Na primeira execução o sistema cria o banco, popula 16 câmeras, 55 registros
biométricos e ~650 detecções históricas, e liga um **simulador de câmeras** que
continua gerando eventos enquanto você usa a interface. Os gráficos já abrem
preenchidos e o mosaico já tem movimento.

---

## A regra que organiza o sistema

O identificador biométrico tem a forma **`BIO-<M|F|N>-NNNNNN`** e é emitido
**uma única vez**, no cadastro, a partir do sexo do registro civil:

| Prefixo | Sexo no cadastro | Sequência |
|---------|------------------|-----------|
| `BIO-M` | Masculino        | independente |
| `BIO-F` | Feminino         | independente |
| `BIO-N` | Não informado    | independente |

Três garantias, cada uma com o seu mecanismo:

**1. O motor nunca emite identificador.** `face_engine.buscar_pessoa()` devolve
o vizinho mais próximo já cadastrado — ou `None` quando a similaridade fica
abaixo do limiar da câmera. Ele não classifica sexo a partir da imagem, então
a mesma pessoa não alterna entre prefixos de uma captura para outra.

**2. Emissão serializada.** `biometrics.emitir_id_biometrico()` trava a linha do
contador com `SELECT … FOR UPDATE` (no PostgreSQL; no SQLite a escrita já é
serializada). O índice único em `pessoas.id_biometrico` é a última rede.

**3. Corrigir o sexo preserva o ID.** `PATCH /pessoas/{id}` altera
`Pessoa.sexo`, mas `sexo_emissao` e `id_biometrico` permanecem — reemitir
quebraria o vínculo com as capturas já registradas. Em PostgreSQL uma trigger
recusa a troca mesmo por SQL direto, e a correção fica na trilha de auditoria.

```python
# app/services/biometrics.py
numero = linha.proximo          # sob trava de linha
linha.proximo = numero + 1
return f"{PREFIXO[sexo]}-{numero:06d}"     # BIO-F-000042
```

Na interface, a tela **Base biométrica** mostra o próximo ID de cada prefixo, e
o formulário de cadastro mostra em tempo real qual identificador será emitido
conforme você escolhe o sexo. Ao corrigir o sexo de alguém já cadastrado, a
ficha explica por que o ID não mudou.

---

## Dois modos, o mesmo código

|  | Demonstração (padrão) | Produção |
|---|---|---|
| Banco | SQLite (arquivo) | PostgreSQL + pgvector |
| Busca 1:N | numpy, força bruta | índice HNSW, distância cosseno |
| Eventos | fila em memória | Redis pub/sub |
| Imagem | cenas geradas em `<canvas>` | RTSP → MediaMTX → WebRTC |
| Detecções | simulador server-side | worker de inferência |

A troca é por variável de ambiente — nenhuma linha de código muda:

```bash
DATABASE_URL=postgresql+asyncpg://vigia:vigia@localhost:5432/vigia
REDIS_URL=redis://localhost:6379/0
SIMULADOR=false
```

O simulador usa **a mesma função de ingestão** que o worker real
(`processar_deteccao`), então o caminho exercitado em demonstração — busca 1:N,
limiar da câmera, geração de alerta, auditoria, publicação no barramento — é o
caminho de produção.

---

## O que a interface faz

**Login e recuperação de senha** — bloqueio após 5 tentativas; recuperação em
três etapas (e-mail → código de 6 dígitos com contagem regressiva → nova senha
com medidor de força e 5 regras validadas ao vivo). A etapa 1 responde **igual**
exista ou não a conta: do contrário o endpoint viraria um oráculo de e-mails
válidos. Em desenvolvimento a resposta inclui o código, para facilitar o teste.

**Painel** — KPIs, fluxo de detecções em 24h por tipo, composição, ranking de
câmeras, detecções por setor e esquema de identificadores.

**Monitoramento ao vivo** — mosaico 1×1 / 2×2 / 3×3 / 4×4 com paginação e
filtro por setor, caixas de detecção, zonas desenhadas, OSD com carimbo de
hora, controle PTZ e um fluxo de eventos que chega por WebSocket.

**Câmeras** — cadastro completo (RTSP/ONVIF, resolução, FPS, codec, retenção),
limiares por câmera e zonas de detecção.

**Registros** — facial, objetos e ações, com filtros, paginação, miniatura da
captura, hash da evidência e revisão (confirmar / falso positivo).

**Base biométrica** — cadastro com emissão de ID, upload de foto que guarda só
o template vetorial, histórico de capturas por pessoa.

**Alertas** — triagem, atribuição e encerramento com desfecho obrigatório.

**Usuários** — matriz de permissões por perfil, senha provisória exibida uma
única vez.

**Auditoria** — trilha encadeada por hash, com botão que recalcula a cadeia
inteira e aponta a primeira divergência.

---

## Estrutura

```
run.py                     ponto de entrada único
app/
  config.py                variáveis de ambiente e modo de operação
  models.py                ORM: usuários, câmeras, zonas, pessoas, detecções, auditoria
  tipos.py                 tipos portáteis SQLite ↔ PostgreSQL (GUID, Vetor, JSON)
  schemas.py               contratos da API e política de senha
  security.py              Argon2id, JWT, OTP com HMAC, mascaramento de PII
  audit.py                 trilha append-only encadeada por SHA-256
  deps.py                  usuário autenticado, RBAC, rate limit
  database.py              engine + Redis (ou substituto em memória)
  web.py                   serve a interface e o /api/info
  main.py                  aplicação FastAPI
  routers/                 auth, cameras, pessoas, deteccoes, alertas,
                           usuarios, auditoria, stream (WebSocket)
  services/
    biometrics.py          emissão do identificador (a regra acima)
    face_engine.py         detecção, qualidade, embedding, busca vetorial
    vision.py              YOLO, geometria de zonas, análise de ações
    simulador.py           gera detecções sem câmeras reais
    events.py              pub/sub + fan-out WebSocket
    onvif_ptz.py           comandos PTZ
    notificacoes.py        envio do código de recuperação
frontend/
  templates/index.html     página única
  static/css/vigia.css     tema do console operacional
  static/js/
    api.js                 cliente da API (tokens, renovação, WebSocket)
    ui.js                  formatação, toasts, modais, gráficos SVG
    feeds.js               motor de vídeo sintético em canvas
    views_op.js            painel, mosaico, câmeras
    views_reg.js           registros e base biométrica
    views_adm.js           alertas, usuários, auditoria, configurações
    app.js                 shell, autenticação, navegação
worker/
  pipeline.py              RTSP → YOLO/ArcFace → API
  run.py                   lançador (--camera CAM-001 | --todas)
scripts/seed.py            dados de demonstração
sql/01_postgres.sql        pgvector, HNSW, triggers de imutabilidade
tests/                     38 testes
```

Sem build. O front-end é HTML, CSS e JavaScript servidos como estáticos pelo
próprio FastAPI — sem npm, sem bundler, sem passo de compilação.

---

## Opções do `run.py`

```bash
python run.py --porta 9000        # outra porta
python run.py --reset             # apaga o banco e recomeça
python run.py --sem-simulador     # sem detecções automáticas
python run.py --sem-navegador     # não abre o navegador
python run.py --recarregar        # recarrega ao editar o código
```

---

## Ligando às câmeras reais

**1. Instale os modelos** (opcionais — sem eles o resto funciona):

```bash
pip install -r requirements.txt     # inclui insightface, ultralytics, opencv
```

Com isso, `POST /pessoas/{id}/amostras` passa a extrair o template de uma foto
real e `POST /pessoas/buscar` faz identificação 1:N a partir de imagem.

**2. Banco de produção:**

```bash
docker compose up -d postgres redis
export DATABASE_URL=postgresql+asyncpg://vigia:vigia@localhost:5432/vigia
export REDIS_URL=redis://localhost:6379/0
psql $DATABASE_URL -f sql/01_postgres.sql    # HNSW + triggers
python run.py --reset
```

**3. Vídeo:** as câmeras publicam RTSP num gateway (MediaMTX) que converte para
WebRTC. `GET /stream/{codigo}` devolve a URL assinada; no mosaico, troque o
`<canvas>` por um `<video>` apontado a ela.

**4. Inferência:**

```bash
SIMULADOR=false python run.py &
python -m worker.run --todas --token <ACCESS_TOKEN>
```

O worker lê o RTSP, roda os modelos e envia para `POST /deteccoes/ingest` — a
mesma rota que o simulador usava.

---

## Decisões de segurança

**Senhas** — Argon2id (`time_cost=3`, 64 MiB), 5 regras de complexidade,
histórico das últimas 5, expiração em 90 dias, bloqueio após 5 falhas.

**Tokens** — JWT com `typ` declarado, para que um refresh não sirva como
access. Logout grava o `jti` numa lista de revogação com TTL igual ao que
restava do token.

**OTP** — 6 dígitos de `secrets`, guardado como HMAC-SHA256, 10 minutos, 3
tentativas, invalidado ao usar.

**Auditoria** — cada linha carrega `sha256(hash_anterior ‖ payload_canônico)`.
Em PostgreSQL uma trigger recusa `UPDATE` e `DELETE`;
`GET /auditoria/verificar` recalcula a cadeia e aponta a primeira divergência.

**RBAC** — `PERMISSOES` em `models.py` é a fonte única; as rotas declaram
`Depends(exige("biometrics", "edit"))`. Perfis operacionais veem CPF e RG
mascarados.

**Retenção** — capturas faciais sem correspondência ganham `expurgo_em` na
ingestão e somem em `POST /deteccoes/expurgo`. Vinculadas a alerta ou já
revisadas são preservadas.

**Fotografias não são armazenadas.** O cadastro guarda o template vetorial
irreversível; a imagem enviada é descartada após a extração.

---

## Testes

```bash
pytest -q          # 38 testes, banco próprio e descartável
```

Cobrem formato e unicidade do identificador, independência das sequências por
sexo, **preservação do ID ao corrigir o sexo**, política de senha, hash de OTP,
mascaramento de PII, geometria de zonas, encadeamento da auditoria, e o
caminho completo de ingestão: identificação acima do limiar, ausência de
identidade abaixo dele, geração de alerta e desfecho obrigatório.

---

## Antes de ir a produção

Aqui o código para e a instituição começa:

- **Base legal e autorização.** Reconhecimento facial em segurança pública
  depende das normas da sua jurisdição, de avaliação de impacto à proteção de
  dados e de decisão da autoridade competente. Nada disso é configuração de
  software.
- **Limiar.** O padrão é 0,88. Calibre com dados da sua população e das suas
  câmeras, e **meça a taxa de falso positivo por grupo demográfico** — o custo
  de um falso positivo recai sobre uma pessoa inocente. Publique a metodologia.
- **Confirmação humana.** `POST /deteccoes/{codigo}/revisao` existe para isso.
  Não ligue despacho automático a alerta de severidade 4 sem operador no
  circuito.
- **Segredos.** `SECRET_KEY` e credenciais ONVIF devem vir de um cofre (Vault,
  Secrets Manager), não do `.env` em disco.
- **Rede.** `/deteccoes/ingest` e `/cameras/{codigo}/heartbeat` são endpoints
  de serviço: exponha só na rede interna, ou exija mTLS no gateway.
- **Trilha.** Revogue `UPDATE`/`DELETE` em `auditoria` para o papel da
  aplicação (o `REVOKE` está comentado em `sql/01_postgres.sql`) e mantenha
  réplica em armazenamento WORM.
- **`SIMULADOR=false`.** Em produção, sempre.
