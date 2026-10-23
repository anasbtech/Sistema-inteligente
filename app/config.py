"""Configuração central do SEGURANÇA.

Dois modos de operação, decididos pelas variáveis de ambiente:

* **Demonstração** (padrão) — SQLite, eventos em memória, simulador de câmeras.
  `python run.py` e o sistema sobe inteiro, sem instalar nada além do pip.
* **Produção** — PostgreSQL + pgvector, Redis, workers de inferência lendo RTSP.
  Basta apontar `DATABASE_URL` e `REDIS_URL`; nenhuma linha de código muda.
"""
from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

RAIZ = Path(__file__).resolve().parent.parent


def exige_autenticacao(host: str) -> bool:
    """Este servidor de e-mail cobra credencial?

    Os públicos cobram, sempre. Um relay interno na mesma rede costuma aceitar
    sem credencial — e ali exigir senha seria inventar um impedimento que não
    existe, por isso a pergunta é pelo host e não um "sempre".
    """
    h = (host or "").lower()
    return any(p in h for p in (
        "gmail", "google", "outlook", "office365", "hotmail", "live.com",
        "yahoo", "zoho", "sendgrid", "mailgun", "amazonses", "smtp.resend",
    ))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Aplicação -------------------------------------------------------
    APP_NAME: str = "SEGURANÇA — Centro Integrado de Videomonitoramento"
    APP_VERSION: str = "4.0.0"
    ENV: str = "development"
    DEBUG: bool = False
    # Por IP, que é determinístico: `localhost` resolve ora para 127.0.0.1,
    # ora para ::1, e um cliente preso ao outro ficaria sem resposta. O nome
    # entra só na URL que as pessoas leem — veja `url_local`.
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # --- Banco -----------------------------------------------------------
    # SQLite por padrão. Para produção:
    #   postgresql+asyncpg://seguranca:seguranca@localhost:5432/seguranca
    DATABASE_URL: str = f"sqlite+aiosqlite:///{RAIZ / 'data' / 'seguranca.db'}"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # --- Eventos ---------------------------------------------------------
    # Vazio = barramento em memória (um processo). Com Redis, vários processos
    # e workers compartilham o mesmo fluxo de eventos.
    REDIS_URL: str = ""
    EVENT_CHANNEL: str = "deteccoes.v1"

    # --- Simulador -------------------------------------------------------
    # Gera detecções sem câmeras reais, pela mesma rota que o worker usa.
    # Desligado: o parque é de câmeras reais, e detecções sintéticas sobre
    # elas contaminariam os registros e a trilha de auditoria.
    SIMULADOR: bool = False
    SIMULADOR_INTERVALO: float = 2.5

    # --- Inferência sobre o vídeo real -----------------------------------
    # Roda os modelos nas câmeras on-line e registra o que eles veem. É o
    # oposto do simulador: nada é sorteado, e sem objeto em cena não há
    # registro. Os dois juntos não fazem sentido — um contamina o outro.
    INFERENCIA: bool = False
    INFERENCIA_INTERVALO: float = 3.0
    # Uma passagem, um registro. A trilha do rastreador é a identidade do
    # objeto enquanto ele está em cena, e meia hora é mais do que qualquer
    # passagem dura: na prática, cada objeto entra no registro uma vez só.
    # Um carro estacionado sob janela curta viraria mil linhas dele mesmo.
    INFERENCIA_JANELA_S: float = 1800.0
    # Piso de confiança da inferência, aplicado sobre o limiar da câmera.
    # Abaixo disso o modelo acerta a presença mas erra o rótulo — foi onde
    # apareceu "motocicleta 44%" sobre um recorte sem moto nenhuma.
    INFERENCIA_CONFIANCA_MIN: float = 0.70
    # Caixa menor que isso tem poucos pixels para classificar: o modelo
    # arrisca o rótulo, e é daí que sai a maior parte das trocas de classe.
    INFERENCIA_LADO_MIN_PX: int = 44
    # Piso do rosto para *enquadrar* no vídeo — bem abaixo do que a
    # identificação exige (MIN_FACE_PIXELS). Quem entra no campo da câmera
    # aparece marcado desde longe; só perto o bastante é que vira registro.
    INFERENCIA_ROSTO_MIN_PX: int = 24
    # Rosto se deduplica pelo vetor, não pela posição. O limiar é alto de
    # propósito: não se pergunta quem é a pessoa, e sim se é a mesma captura
    # de segundos atrás — abaixo disso, duas pessoas parecidas viram uma.
    INFERENCIA_ROSTO_IGUAL: float = 0.92
    # Curta porque passar de novo é evento de verdade: quem volta ao saguão
    # mais tarde deve constar outra vez, mas quem fica parado diante da
    # câmera não pode virar uma linha a cada ciclo.
    INFERENCIA_ROSTO_JANELA_S: float = 120.0
    # Corpo que carrega identidade tem janela própria, mais curta que a do
    # objeto anônimo (INFERENCIA_JANELA_S, de 30 min) e mais curta que a do
    # rosto. As três medidas dizem coisas diferentes:
    #
    # * 30 min para objeto anônimo porque pessoa parada não é notícia — era
    #   o que fazia um carro estacionado render uma linha por ciclo;
    # * 2 min para o rosto porque passar de novo é evento de verdade;
    # * 45 s aqui porque é o que preenche o vão entre dois rostos. Medido
    #   neste parque: em 20 h houve 49 rostos identificados e 220 corpos,
    #   mas só 10 quadros com os dois ao mesmo tempo — o corpo quase nunca
    #   tinha linha para receber a identidade. Com janela própria, a presença
    #   de quem foi reconhecido passa a constar entre um rosto e o seguinte,
    #   e a passagem deixa de se partir quando a cara fica ilegível.
    INFERENCIA_CORPO_JANELA_S: float = 45.0

    # --- Parque de câmeras -----------------------------------------------
    # O parque é homogêneo (Intelbras/Dahua, mesma credencial), então a URL
    # RTSP é derivada do IP no cadastro: quem cadastra informa o endereço da
    # câmera, não uma URL com senha dentro. A senha fica aqui, fora do banco
    # e fora do git — no banco ela apareceria em texto claro na listagem de
    # câmeras, ao alcance de qualquer perfil que possa ver o cadastro.
    CAMERA_USUARIO: str = "admin"
    CAMERA_SENHA: str = ""
    CAMERA_RTSP_PORTA: int = 554
    CAMERA_RTSP_CAMINHO: str = "/cam/realmonitor?channel=1&subtype=0"

    # --- Segurança -------------------------------------------------------
    # Assina os tokens de sessão e o segredo do link de redefinição.
    #
    # O padrão sorteado existe para o sistema subir sem configuração nenhuma,
    # mas ele é por *processo*: sem SECRET_KEY no .env, cada reinício gera uma
    # chave nova, e com ela toda sessão aberta cai e todo link de redefinição
    # pendente passa a ser recusado — o HMAC deixa de bater. Medido: dois
    # processos lado a lado produziram chaves diferentes.
    #
    # `chave_efemera` existe para o boot poder avisar disso.
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    ACCESS_TOKEN_MINUTES: int = 60
    REFRESH_TOKEN_DAYS: int = 7
    JWT_ALGORITHM: str = "HS256"
    MAX_LOGIN_ATTEMPTS: int = 5
    LOCKOUT_MINUTES: int = 15
    PASSWORD_EXPIRE_DAYS: int = 90
    PASSWORD_HISTORY: int = 5
    # Validade do link de redefinição. Maior que a do código de seis dígitos
    # que existia antes, porque a pessoa agora precisa abrir o e-mail antes de
    # agir, e caixa de entrada tem atraso — dez minutos deixava o link morrer
    # no caminho.
    RESET_LINK_TTL_SECONDS: int = 1800

    # --- Envio de e-mail -------------------------------------------------
    # Sem SMTP_HOST o código de recuperação só vai para o log, que é o modo
    # de quem está desenvolvendo. Preenchidos, o e-mail sai de verdade.
    # Gmail: smtp.gmail.com:587, usuário é o endereço e a senha precisa ser
    # uma "senha de app" de 16 caracteres — a senha da conta não é aceita.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""          # vazio: usa SMTP_USER
    SMTP_FROM_NOME: str = "SEGURANÇA"
    SMTP_STARTTLS: bool = True   # 587 usa STARTTLS; 465 é TLS direto
    SMTP_TIMEOUT: int = 20

    # --- CORS ------------------------------------------------------------
    # O front-end é servido pela própria API, então CORS só importa se você
    # hospedar a interface em outro domínio.
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # --- Biometria -------------------------------------------------------
    EMBEDDING_DIM: int = 512
    FACE_MODEL: str = "buffalo_l"
    # Similaridade cosseno mínima para atribuir identidade, e a referência do
    # próprio InsightFace para este modelo.
    #
    # Medido nas capturas reais desta instalação, o acerto é bimodal: a mesma
    # pessoa cai entre 0,50 e 0,64, e pessoa diferente não passou de 0,452 em
    # todo o histórico. A 0,55 as passagens da faixa 0,50–0,55 eram perdidas
    # por centésimos — num só dia, 15 de 56. A 0,50 elas entram, e ainda sobra
    # margem sobre o maior falso candidato já visto.
    #
    # Não desça daqui: o CheckConstraint da tabela de câmeras exige
    # `limiar_facial >= 0.5`, e abaixo disso começa a faixa onde pessoa errada
    # já apareceu. Seja qual for o valor, o alerta continua exigindo
    # confirmação humana antes de qualquer ação.
    FACE_THRESHOLD: float = 0.50
    FACE_DET_SIZE: int = 640

    # --- Cobertura do template por câmera --------------------------------
    # A busca 1:N compara contra CADA amostra e fica com a melhor (veja
    # `_topk_memoria`), então uma amostra por ângulo é o que faz a pessoa ser
    # reconhecida fora da câmera onde foi cadastrada — e acrescentar uma
    # nunca piora as que já existem.
    #
    # Medido nesta base: das 10 pessoas cadastradas, 7 só foram identificadas
    # em 1 câmera e nenhuma em 3 ou mais; a identificação acontece em 3 das
    # 26 câmeras. Não é limiar nem qualidade — a qualidade do rosto é boa e
    # uniforme no parque inteiro (0,53 a 0,92). É falta de amostra do ângulo.

    # Acima disto, a captura identificada vira amostra sozinha. Folgado sobre
    # o limiar de propósito: o maior falso candidato já visto nesta base deu
    # 0,452, e aqui nenhum operador confere antes.
    FACE_AUTO_SAMPLE_MIN: float = 0.65
    # Cota por câmera. A busca é força bruta contra todas as amostras, então
    # o custo cresce com elas; e sem cota a câmera movimentada enche o
    # cadastro sozinha. Medido: 508 capturas passariam de 0,65, e 257 delas
    # são da mesma câmera.
    FACE_SAMPLES_PER_CAMERA: int = 3
    # Amostra parecida demais com uma que já existe não acrescenta ângulo
    # nenhum e gasta a cota. Mesmo valor de INFERENCIA_ROSTO_IGUAL, pela
    # mesma razão: acima disto é a mesma captura, não outro ângulo.
    FACE_SAMPLE_DISTINTA: float = 0.92
    # Piso da faixa de conferência: abaixo do limiar, mas perto o bastante
    # para valer os olhos de alguém. Medido, a faixa 0,38–0,50 tem 2.601
    # capturas — mas 2.576 estão nas 3 câmeras que já reconhecem, onde não
    # acrescentam nada. Nas outras oito são 25 capturas, e são essas que
    # abrem câmera nova. Por isso a fila ordena por câmera descoberta antes
    # de ordenar por similaridade: sem isso ela nasce com 2.601 itens inúteis
    # na frente dos 25 que importam.
    FACE_REVIEW_MIN: float = 0.38

    # Rosto que a base não conhece abre um BIO-N, em vez de virar mais uma
    # linha "não identificada" que ninguém consegue ligar à seguinte. O
    # template nasce da própria captura, então o encontro seguinte casa com
    # ele e o mesmo desconhecido vira um registro, não um por passagem.
    #
    # Só abre quando NÃO há candidato nem para conferência (acima de
    # FACE_REVIEW_MIN): se há alguém parecido, a hipótese mais provável é que
    # seja essa pessoa vista de um ângulo que o template ainda não cobre — e
    # abrir registro aí criaria a duplicata que a fila existe para evitar.
    FACE_AUTO_REGISTRO: bool = True
    # Piso de qualidade mais alto que o de reconhecer, de propósito.
    #
    # Medido nesta base: a qualidade não separa identificado de desconhecido
    # — as duas distribuições têm a mesma moda em 0,5–0,6 —, então isto não é
    # um filtro de acerto. É um filtro de *fundação*: o template aberto aqui
    # vira a referência de todos os encontros futuros daquela pessoa, e um que
    # nasce de rosto ruim atrai quem não é ela. Reconhecer mal custa um acerto;
    # fundar mal contamina a base. 97% das identificações desta base estão
    # acima de 0,5, e este piso fica acima disso.
    FACE_AUTO_REGISTRO_QUALIDADE_MIN: float = 0.60

    # Quanto do rosto precisa estar dentro da caixa da pessoa para os dois
    # serem o mesmo alguém. Medido pela interseção sobre a menor área, que é
    # o que já separa duplicata de pessoa distinta no detector de objeto.
    #
    # Não é 1,0 porque as duas caixas vêm de modelos diferentes e não se
    # alinham ao pixel: o detector de pessoa costuma cortar o topo da cabeça,
    # e aí parte do rosto fica de fora. Também não é baixo demais — num
    # corredor cheio, a caixa de quem está atrás encosta na de quem está à
    # frente, e um limiar frouxo poria o nome na pessoa errada.
    #
    # Entre dois corpos que contêm o rosto, vence o menor: a caixa menor é a
    # mais específica, e a maior costuma ser o grupo ao fundo.
    FACE_CORPO_CONTENCAO: float = 0.70
    # Nota mínima para a captura entrar no registro. Baixa porque o que sai
    # destas câmeras é registro de passagem, não identificação: medido na
    # recepção, o rosto de quem cruza o saguão vem com 0,39 — a câmera está
    # no teto, e o rosto chega em ângulo. A 0,55 a tela ficava vazia.
    # O que isso registra é "alguém passou aqui", com o recorte para a
    # revisão humana olhar. Identificar exige rosto grande e de frente;
    # abaixo disso a similaridade não sobe, e o alerta não dispara.
    MIN_FACE_QUALITY: float = 0.35
    # Altura mínima do rosto, em pixels, para o vetor valer alguma coisa.
    # O ArcFace recebe 112x112: o que chega menor é esticado até lá, e o vetor
    # que sai é ruído. Medido nesta instalação, duas capturas da mesma pessoa
    # com rostos de ~50px deram 0,004 de similaridade — o esperado passa de
    # 0,5. E a nota de qualidade não barra isso, porque dá só um quarto do
    # peso à resolução: rosto pequeno e nítido era aprovado.
    # Abaixo deste piso o rosto ainda aparece na caixa do vídeo, mas não vira
    # captura: identificar dali é chute, e registrar só enche a revisão.
    # O valor equilibra duas perdas. A 90 não entrava nada nesta câmera —
    # cinco minutos, nenhuma captura — e com isso ia embora também o registro
    # de passagem, que serve à auditoria mesmo sem identificar ninguém.
    # Está em 48 porque a 70 a recepção seguia sem registrar: medido ali, o
    # rosto de quem atravessa o saguão chega com 63 pixels. A escolha é
    # deliberada e tem preço — nesta faixa o vetor não casa nem com outra
    # captura da mesma pessoa (0,004 de similaridade medidos aqui), então o
    # que entra é passagem registrada, nunca identificação.
    # Para reconhecer de verdade nesta posição, a câmera precisa descer ou
    # fechar o zoom na entrada; nenhum ajuste de piso substitui isso.
    MIN_FACE_PIXELS: int = 48
    # Nitidez mínima, na mesma escala de 0 a 1 da nota de qualidade.
    # Rosto borrado não tem detalhe para extrair, igual a rosto pequeno — e
    # escapava pela mesma brecha: a nota é média ponderada, então uma captura
    # medida aqui com 145 pixels de rosto, frontalidade 0,88 e nitidez 0,01
    # tirava 0,61 e passava, para gerar um vetor sem informação.
    # Borrão assim vem de ambiente escuro: com brilho medido em 45 de 255, a
    # câmera alonga a exposição e quem se move sai arrastado. O piso barra a
    # captura inútil, mas quem resolve é luz na cena, não este número.
    MIN_FACE_SHARPNESS: float = 0.15
    # Rede de segurança para capturas sem correspondência: duas na mesma
    # câmera dentro desta janela contam como uma presença só.
    # Quem consolida de verdade é o seletor de melhor quadro no worker, que
    # acompanha o rosto em cena e emite uma captura por passagem. Esta janela
    # cobre só o caso de ele emitir duas seguidas, e por isso é curta: sem
    # correspondência não há como distinguir duas pessoas desconhecidas, e
    # uma janela longa engoliria a segunda.
    UNMATCHED_FACE_WINDOW_S: int = 30
    # Caminho a partir da raiz: os pesos moram em models/. Como nome solto, o
    # Ultralytics procura no diretório de trabalho, não encontra e baixa de
    # novo — passando a carregar uma cópia na raiz em vez do peso versionado.
    #
    # O small, e não o nano. Medido nos quadros reais da Produção 02 e da
    # Produção 03, com o limiar que as câmeras têm no banco e a varredura
    # abaixo ligada:
    #
    #            Produção 03   Produção 02        por câmera ocupada
    #   nano          9          10, com uma caixa dobrada      ~4s
    #   small        11           8, uma caixa por pessoa       ~5s
    #   medium       10           9                            ~9s
    #
    # Os 8 do small na Produção 02 foram conferidos no vídeo: são as 8 pessoas
    # da sala, uma caixa cada. Os 10 do nano são as mesmas 8 com uma dobrada e
    # uma a mais — contar caixa não basta, é preciso olhar.
    #
    # O medium custa o dobro e não devolve o ganho, porque modelo maior ganha
    # em objeto *difícil*, não em objeto *pequeno*: quem resolve o pequeno é a
    # varredura, que entrega o mesmo corpo com mais pixels.
    OBJECT_MODEL: str = str(RAIZ / "models" / "yolov8s.pt")
    # Medido na Produção 02, uma sala com quatro pessoas sentadas: a 0,65 o
    # modelo marcava só a pessoa da frente, e as três ao fundo ficavam sem
    # caixa nenhuma. A 0,25 saem as quatro. Mais abaixo começa a inventar —
    # a 0,15 a mesma cena ganhou uma caixa sobre uma caixa de papelão.
    # Este piso vale para o que aparece no vídeo. O que entra no registro
    # passa ainda por INFERENCIA_CONFIANCA_MIN, bem mais alto de propósito.
    OBJECT_THRESHOLD: float = 0.25
    # Confiança mínima para afirmar *porte* de veículo — caminhão ou ônibus em
    # vez de carro. Alto de propósito, e por um motivo medido: o modelo não
    # erra caminhão, ele hesita em furgão. O mesmo furgão desta instalação saiu
    # `bus` a 0,49 e `truck` a 0,77, e a ficha de uma van chegou a dizer
    # "Ônibus a 73,1%". Abaixo deste piso a hesitação vira "carro", que é
    # verdadeiro para qualquer um dos três; acima, o rótulo é afirmação.
    #
    # Por isso ele não acompanha OBJECT_THRESHOLD: aquele decide se *existe*
    # um veículo, este decide se dá para dizer *qual*. São perguntas
    # diferentes, e a segunda exige muito mais evidência que a primeira.
    # Acima disto o porte do veículo é afirmação; abaixo vira "carro", que é
    # verdadeiro para qualquer um dos três.
    #
    # Subiu de 0,80 para 0,92 depois de dois erros medidos na Portaria: um
    # caminhão de baú registrado como "Ônibus" a 87,7% e uma van registrada
    # como "Caminhão" a 88,5%. Os dois passaram folgados pela trava antiga —
    # confiança alta e errada é o caso que um limiar baixo não cobre.
    #
    # O preço é conhecido e foi aceito de propósito: caminhão de verdade a
    # 0,88 passa a constar como "carro". Num sistema de segurança, o genérico
    # verdadeiro vale mais que o específico falso — "carro" sobre uma van não
    # engana ninguém, "Ônibus" sobre um caminhão manda procurar a coisa
    # errada. E o volume diz que a troca sai barata: em 19.044 detecções de
    # objeto deste parque havia 1 caminhão e 0 ônibus, e os dois que
    # chegaram ao operador estavam errados.
    OBJECT_PORTE_MIN: float = 0.92
    # Quanto duas caixas de classes de veículo diferentes precisam se cobrir
    # para serem o mesmo veículo lido duas vezes. Mesma medida de contenção
    # que já separa duplicata de pessoa distinta.
    OBJECT_PORTE_CONFLITO: float = 0.70

    # Detectores de classes que o COCO não tem. Vazio = desligado, e é o
    # padrão: o modelo não vem embarcado porque não há, hoje, um público que
    # sirva a este uso e tenha validação — veja a abertura de `extras.py`.
    # Aponte o `.pt` validado pela instituição para ligar.
    EXTRA_CAPACETE_MODEL: str = ""
    EXTRA_ARMA_MODEL: str = ""
    # Limiares próprios, mais altos que o do parque. Estas duas classes não são
    # contagem: cada registro manda alguém conferir, e "arma" manda conferir
    # com urgência. Falso positivo aqui custa mais que detecção perdida —
    # o que se perde volta no próximo quadro, o que se inventa mobiliza gente.
    EXTRA_CAPACETE_MIN: float = 0.60
    EXTRA_ARMA_MIN: float = 0.70
    # Lado a que o YOLO reduz o quadro antes de analisar, e que manda mais que
    # o limiar em quem está longe. O padrão do Ultralytics é 640: nele a sala
    # acima entregava uma pessoa só, porque quem senta ao fundo chega ao
    # modelo com menos de 30 pixels. A 960 saem as quatro, e medindo na mesma
    # cena 1280 e 1600 não acharam mais ninguém — só custaram mais. Num parque
    # analisado em série, o excedente sai do tempo de todas as outras câmeras.
    # O lado a que o YOLO reduz o quadro antes de olhar. Casado com o que as
    # câmeras realmente entregam: o cadastro diz 1920x1080, mas o RTSP vem em
    # 1280x720. Medido num quadro real de sala, yolov8n: a 960 são 1040ms e 9
    # pessoas; a 1280, 1033ms e 11. O mesmo custo, porque a 1280 não há
    # reamostragem — a 960 se paga para encolher e se perde gente no caminho.
    # Mexer aqui só faz sentido relendo a resolução nativa do parque.
    OBJECT_IMGSZ: int = 1280
    DEVICE: str = "cpu"

    # --- Não deixar ninguém de fora -------------------------------------
    # Duas coisas distintas, que o print do operador mostrava juntas: pessoa
    # sem caixa nenhuma, e pessoa com duas caixas. Vêm de pontas opostas do
    # detector e se resolvem separado — baixar o limiar para achar quem falta
    # é justamente o que faz nascer a caixa dobrada.

    # IoU do NMS do próprio YOLO. O padrão do Ultralytics é 0,70, e nele duas
    # caixas sobre a mesma pessoa com 0,48 de IoU passavam as duas. Medido nos
    # quadros desta instalação, duas pessoas *distintas* que se sobrepõem não
    # passaram de 0,29 — o homem em pé à frente da mulher sentada, na Produção
    # 02, é o pior caso e deu 0,29. 0,55 fica no vão entre os dois grupos.
    OBJECT_NMS_IOU: float = 0.55

    # O NMS por IoU não enxerga a caixa *contida* na outra: o tronco marcado
    # dentro do corpo inteiro rende IoU 0,27 e sobrevive — era a caixa dobrada
    # do Service Desk. Daí a interseção sobre a *menor* das duas (ios).
    #
    # Mas ios sozinho também não separa, e isto foi medido rotulando à mão
    # todos os pares sobrepostos de duas capturas ao vivo do parque: duplicata
    # desce a 0,52 e duas pessoas encostadas sobem a 0,58. Os quatro números
    # abaixo trabalham juntos; a tabela e o raciocínio estão em
    # `vision.mesma_pessoa`, que é onde eles são lidos.

    # Praticamente a mesma caixa. Nenhum par de pessoas distintas do parque
    # passou de IoU 0,29.
    OBJECT_FUSAO_IOU: float = 0.55
    # Uma inteiramente dentro da outra.
    OBJECT_FUSAO_IOS: float = 0.80
    # Piso para sequer considerar fusão. Os três pares de pessoas distintas
    # mais sobrepostos ficaram em 0,41–0,44; abaixo deste valor não se mexe.
    OBJECT_FUSAO_IOS_MIN: float = 0.50
    # Mesmo tamanho no mesmo lugar: a mesma pessoa lida por duas passadas.
    # As duas duplicatas medidas deram 0,89 e 0,90 de razão de área; o par de
    # pessoas distintas mais parecido em tamanho deu 0,70.
    OBJECT_FUSAO_RAZAO_IGUAL: float = 0.80
    # Coisa pequena funda dentro de alguém — mão, fone, cabeça — é parte
    # daquela pessoa, não uma segunda. Medido em 0,63 de ios com 0,15 de razão
    # de área; o par de pessoas distintas mais próximo disso tinha ios 0,41.
    OBJECT_FUSAO_IOS_PARTE: float = 0.60
    OBJECT_FUSAO_RAZAO_PARTE: float = 0.25

    # Varredura em ladrilhos. Quem senta ao fundo da sala chega ao modelo com
    # menos de 30 px de altura, e aí não há limiar que resolva: a 0,10 as
    # caixas extras caem empilhadas sobre quem já estava detectado. O que
    # resolve é dar mais pixels ao mesmo corpo — o quadro é recortado em
    # pedaços que se sobrepõem, e cada pedaço vai ao modelo ampliado.
    #
    # Medido nos quadros reais: Produção 03 passou de 7 para 11 pessoas e
    # Produção 02 de 6 para 11, que é quanto há nas duas salas.
    #
    # Custa o que se mede: num quadro real da Produção 02, a passada inteira
    # leva 1007ms e a varredura mais 2936ms — quase 4x a câmera ocupada. Com
    # o parque percorrido em série, esse tempo sai da volta de todas as
    # outras, e é por isso que o modo abaixo importa mais que o resto.
    #
    # "ocupado" varre só o quadro onde a passada inteira já achou alguma
    # coisa — corredor, garagem e datacenter vazios não pagam nada, e são a
    # maioria do parque. "sempre" varre inclusive o quadro em que ela não
    # achou nada, que é o caso da câmera cujo único ocupante é pequeno demais
    # para a passada inteira; custa varrer 26 câmeras, a maior parte vazia.
    # "nunca" volta ao comportamento anterior.
    #
    # "sempre" desde 23/10: em "ocupado" a câmera sem ninguém *grande* ficava
    # sem caixa nenhuma — a passada inteira não achava o corpo distante, e sem
    # achado ela não varria, então o único ocupante da sala nunca era marcado.
    # Era isso que aparecia como "tem câmera que não mostra bounding box". O
    # atalho só é seguro para decidir *se vale procurar mais*, nunca para
    # decidir *se há alguém*: quadro sem achado é exatamente o quadro onde a
    # pessoa pequena se esconde. Custa a volta ~2x mais longa (3,9s por câmera
    # contra 1,0s na vazia), e a caixa passa a acompanhar a cena com atraso —
    # preferência de 07/10: a caixa não faltar a ninguém vale mais que ela ser
    # recente.
    OBJECT_VARREDURA: str = "sempre"
    # 2x2 medido contra 3x2: a grade mais fina achou *menos* gente (10 contra
    # 11) e custou o dobro — o ladrilho menor corta o corpo ao meio e tira do
    # modelo o contexto que ele usa para reconhecê-lo.
    OBJECT_VARREDURA_COLUNAS: int = 2
    OBJECT_VARREDURA_LINHAS: int = 2
    # A sobreposição existe para quem cai na emenda entre dois ladrilhos: sem
    # ela, a pessoa partida ao meio não é achada por nenhum dos dois.
    OBJECT_VARREDURA_SOBREPOSICAO: float = 0.25
    # Lado do ladrilho. Num quadro 1280x720, a grade 2x2 com 25% de emenda dá
    # pedaços de 731x411, e 960 os amplia 1,3x.
    #
    # 1280 foi medido e recusado. A ampliação de 1,75x custa 4416ms de
    # varredura contra 2936ms, e as duas caixas que ela acrescentou nos
    # quadros desta instalação não eram gente: uma era a segunda marca sobre
    # uma pessoa já detectada na Produção 02, e a outra caía sobre a moto da
    # Portaria. Ampliar demais faz o modelo ver pessoa em textura — subir
    # daqui pede reconferir no vídeo, não só contar as caixas.
    OBJECT_VARREDURA_IMGSZ: int = 960
    # Mais baixo que o limiar da passada inteira de propósito: o corpo pequeno
    # sai com confiança baixa mesmo ampliado, e é ele que a varredura foi
    # buscar. O que entra no registro continua passando por
    # INFERENCIA_CONFIANCA_MIN, bem mais alto.
    OBJECT_VARREDURA_LIMIAR: float = 0.20
    # Só pessoa. Varrer todas as classes multiplicaria o custo e devolveria
    # pedaço de carro como carro novo na Portaria — e o que faltava no vídeo
    # era gente.
    OBJECT_VARREDURA_CLASSES: tuple = ("pessoa",)

    # --- Alerta por presença ----------------------------------------------
    # Severidade do alerta que uma câmera com `alerta_presenca` gera.
    #
    # 3 ("Média") cria o alerta na lista do operador. 4 ("Crítica") também
    # dispara a notificação por e-mail — e aí vale medir antes: numa sala de
    # reunião com cinco entradas por hora, isso é um e-mail a cada doze
    # minutos no horário comercial.
    ALERTA_PRESENCA_SEVERIDADE: int = 3

    # Quantas voltas seguidas a contagem precisa cair antes de virar "saída".
    #
    # Assimétrico de propósito: entrada sai na hora, saída espera. A contagem
    # oscila — medido em duas capturas da mesma sala cheia, foi de 12 para 13
    # e de 10 para 12 sem ninguém se mexer — e quase sempre a queda é alguém
    # que o modelo perdeu num quadro, não alguém que foi embora. Dizer que a
    # sala esvaziou com gente ainda lá é o pior dos dois erros.
    #
    # Custa uma volta de atraso no aviso de saída. 1 desliga a espera.
    ALERTA_PRESENCA_CONFIRMA_SAIDA: int = 2

    # --- Análise de ações -------------------------------------------------
    # Os valores originais foram escritos para cena de rua a 25 fps. Aqui a
    # câmera é visitada de tempos em tempos, e o parque é de escritório: o que
    # era "parado tempo demais" vira "pessoa trabalhando sentada".

    # Quanto duas caixas precisam se sobrepor, entre uma visita e a seguinte,
    # para serem tratadas como o mesmo objeto. Não é rastreio de trajetória —
    # é teste de permanência no lugar, que é a pergunta que permanência e
    # aglomeração fazem. Quem atravessou a sala não casa e ganha identidade
    # nova, o que está certo: para o sistema é outra passagem.
    ACAO_RASTREIO_IOU: float = 0.30

    # 15 minutos, não 90 segundos. Com 90s qualquer pessoa sentada à mesa
    # viraria ocorrência a cada volta, e a lista do operador seria o escritório
    # inteiro trabalhando. Permanência só tem sentido onde ninguém deveria
    # ficar: corredor, hall, garagem, doca — e é por câmera que se liga.
    ACAO_PERMANENCIA_S: float = 900.0

    # O cooldown precisa ser maior que a volta pelo parque. Em 30s, com a
    # volta em 80s, a mesma pessoa parada rendia um evento por visita.
    ACAO_COOLDOWN_S: float = 600.0

    # Aglomeração é contada num quadro só, então funciona mesmo no rodízio.
    #
    # Conta quem está *junto*, não quem está em cena: oito pessoas dentro de
    # 18% da largura do quadro. A distinção é o que torna a ação utilizável
    # aqui — a Produção trabalha com onze pessoas espalhadas pela sala, e um
    # total bruto a acusaria o expediente inteiro; as mesmas onze amontoadas
    # numa porta são outra coisa. O raio é a medida de "junto".
    ACAO_AGLOMERACAO_N: int = 8
    ACAO_AGLOMERACAO_RAIO: float = 0.18
    # Padrão do parque. Cada câmera pode ter o seu — ver
    # `Camera.limite_aglomeracao`, que vence este quando preenchido.
    #
    # Quanto o agrupamento precisa durar para virar alerta. Dez segundos
    # derrubam o ajuntamento que se desfaz sozinho: fila que se forma e anda,
    # gente que para no corredor para se cumprimentar, saída de reunião. Não
    # é o mesmo que o cooldown — este diz quando o fato *começa a existir*, o
    # cooldown diz de quanto em quanto tempo ele se repete no painel.
    #
    # Como a câmera é visitada de tempos em tempos, a confirmação costuma vir
    # na visita seguinte: duas observações separadas mostrando o mesmo
    # agrupamento é o que o rodízio consegue afirmar sobre durar.
    ACAO_AGLOMERACAO_S: float = 10.0

    # Corrida e abandono seguem aqui para ficarem no mesmo lugar que o resto,
    # mas não mudam nada hoje: corrida exige análise contínua, e abandono
    # depende de uma classe de objeto que saiu do mapa.
    # Janela em que circular é normal, em hora local do servidor. Fora dela,
    # pessoa detectada vira "entrada em horário não permitido" nas câmeras que
    # ligarem a ação. Atravessa a meia-noite se o fim for menor que o início
    # (ex.: 22:00–06:00 para vigiar a madrugada).
    ACAO_HORARIO_INICIO: str = "07:00"
    ACAO_HORARIO_FIM: str = "19:00"

    # --- Corrida súbita ---------------------------------------------------
    # O rótulo da ação diz "súbita", e é isso que estes números exigem: não
    # basta estar rápido, é preciso ter *acelerado*. Quem atravessa o pátio
    # sempre apressado não vira alerta a cada volta.
    #
    # A unidade é altura de corpo por segundo, e não fração de quadro. Um
    # adulto anda a ~0,8 do próprio corpo por segundo e corre acima de 2.
    # Medir em fração de quadro misturava velocidade com distância da lente:
    # quem passa rente à câmera desloca muito mais quadro andando do que quem
    # corre no fundo da sala, e o piso único disparava na frente e emudecia
    # no fundo. Veja `Trilha.escala`.
    ACAO_CORRIDA_RITMO: float = 1.6
    # Quanto o ritmo tem de subir sobre o que a própria pessoa vinha tendo.
    # É o "súbita": 0,9 corpo/s é a diferença entre andar e correr.
    ACAO_CORRIDA_SALTO: float = 0.9
    # 0 é seguir reto, 1 é inverter o sentido a cada passo. Correr é dirigido;
    # oscilar no mesmo lugar é briga, ou é a caixa trocando de dono entre duas
    # pessoas próximas.
    ACAO_CORRIDA_AGITACAO: float = 0.35
    # Quanto as condições precisam durar juntas. Derruba o salto de posição de
    # um quadro só, que é quase sempre o rastreador reatribuindo a trilha.
    ACAO_CORRIDA_DURACAO_S: float = 0.4
    # Quando a imagem inteira anda — tremor, vento, reposicionamento da PTZ —
    # toda trilha arranca junta e todas pareceriam correr. Mede-se a coerência
    # dos passos: o tamanho da soma dos vetores sobre a soma dos tamanhos, de
    # 0 (cada um para o seu lado) a 1 (o mesmo vetor para todos). Veja
    # `AnalisadorAcoes._camera_moveu`.
    ACAO_CORRIDA_COERENCIA: float = 0.90

    # --- Queda de pessoa --------------------------------------------------
    # Três coisas, nesta ordem, e é a ordem que define o evento: o corpo
    # **despencou**, ficou na **horizontal** e **continua lá**.
    #
    # Sozinha, a horizontal não distingue cair de deitar, e a câmera da área
    # de descanso alertaria o dia inteiro. Sozinho, o tranco não distingue
    # cair de agachar. E sem a permanência, abaixar-se para pegar algo do
    # chão é uma queda — é o movimento normal que mais se parece com uma.
    #
    # Largura sobre altura da caixa. Em pé um corpo dá ~0,4; no chão, mais
    # que 1. O piso fica acima de 1 para não depender de ângulo fino.
    ACAO_QUEDA_RAZAO: float = 1.25
    # Alturas de corpo por segundo, para baixo, na mesma régua da corrida.
    # Um corpo que cede cobre a própria altura em menos de um segundo.
    ACAO_QUEDA_DESPENQUE: float = 1.2
    # Quanto o corpo precisa ficar no chão para virar alerta. É o que derruba
    # quem se abaixou: ninguém amarra o sapato deitado, e quem se agacha
    # levanta antes disto. Com a rajada entregando ~1s por visita, a
    # confirmação costuma vir na visita seguinte — e é uma confirmação forte,
    # porque significa que a pessoa seguia no chão dezenas de segundos depois.
    ACAO_QUEDA_PERMANENCIA_S: float = 3.0

    # --- Agressão física ------------------------------------------------
    # Três sinais exigidos ao mesmo tempo, nas duas pessoas do par. Veja
    # `AnalisadorAcoes._avaliar_briga`: nenhum deles sozinho significa
    # briga, e é a coincidência dos três que vira indício.
    #
    # Caminho *percorrido*, não deslocamento líquido: quem luta vai e volta
    # no mesmo lugar, e medido pelo deslocamento daria zero. Em alturas de
    # corpo por segundo, como a corrida e a queda — em fração de quadro, dois
    # corpos se agarrando rente à lente percorrem muito mais que os mesmos
    # dois no fundo da sala.
    #
    # Fica abaixo do ritmo de quem anda, e é de propósito: numa luta corporal
    # ninguém atravessa a cena. Quem separa daí para baixo é a agitação.
    ACAO_BRIGA_RITMO: float = 0.45
    # 0 é andar reto, 1 é inverter o sentido a cada passo. Acima de 0,45
    # o movimento deixou de ter direção — é o que separa de corrida.
    ACAO_BRIGA_AGITACAO: float = 0.45
    # Interseção sobre a menor das caixas: duas pessoas conversando ficam
    # lado a lado e dão perto de zero; em luta corporal elas se sobrepõem.
    ACAO_BRIGA_PROXIMIDADE: float = 0.22
    # Quanto os três sinais precisam durar juntos. Derruba o esbarrão de
    # corredor, que dura um quadro.
    ACAO_BRIGA_DURACAO_S: float = 2.0

    # --- Rajada para as ações que medem movimento -----------------------
    # Corrida, queda e contrafluxo comparam a posição do corpo entre quadros
    # próximos. No rodízio, duas visitas à mesma câmera distam dezenas de
    # segundos, e `Trilha.velocidade` exige cinco pontos, `despencou`
    # três — seriam minutos para juntar o que a ação precisa, e a pessoa já
    # saiu de cena. Por isso a câmera que vigia uma dessas ações recebe uma
    # rajada: vários quadros seguidos na mesma visita.
    #
    # Oito quadros a 0,22s dão 1,76s de observação — a escala de uma corrida
    # ou de uma queda. Abaixo de cinco quadros `velocidade` nem é calculada.
    ACAO_CONTINUA_QUADROS: int = 8
    # 0,15s entre quadros: oito deles dão 1,05s de observação, que ainda é a
    # escala de uma corrida ou de uma queda. O intervalo é tempo morto — não
    # gasta CPU, mas segura a fila das câmeras seguintes, porque o parque é
    # percorrido em série.
    ACAO_CONTINUA_INTERVALO_S: float = 0.15
    # Lado reduzido só na rajada. Medido nesta máquina: 1.043 ms por quadro a
    # 1280 contra 244 ms a 320, o que decide entre 8,3s e 2,0s por câmera. O
    # que se perde é o alcance em quem está longe — e quem está longe demais
    # para ter caixa não tem trajetória para medir de qualquer forma.
    ACAO_CONTINUA_IMGSZ: int = 320
    # Triagem de movimento antes da rajada. Diferença de luminosidade que
    # conta como pixel mudado, e a fração do quadro que precisa mudar.
    # O piso de 18 passa por cima do ruído de compressão do RTSP, que
    # faz pixels oscilarem alguns níveis mesmo com a cena parada.
    MOVIMENTO_DELTA: int = 18
    MOVIMENTO_FRACAO_MIN: float = 0.012
    # Teto duro de rajadas por volta. É o limite do orçamento: com a
    # rajada solta, a volta do parque foi de 14–36s para 46s. Análise
    # contínua de movimento em 24 câmeras não cabe em 8 núcleos, e o
    # que se faz aqui é amostragem — a câmera com movimento é atendida
    # a cada poucas voltas, em vez de nunca.
    # Duas por volta, e o número é prudência, não correção de um problema
    # medido.
    #
    # A suspeita era de que a rajada estourasse o ciclo: com teto de 4 a volta
    # chegou a 57,7s. Mas o mesmo parque com a rajada **desligada** chegou a
    # 55,4s na mesma janela — o ciclo deste parque cresce sozinho enquanto as
    # 24 câmeras sobem o RTSP, e a rajada responde por uma fração pequena
    # disso. A causa do crescimento é outra e não foi investigada aqui.
    #
    # O teto fica porque torna o custo **previsível**: no pior caso, duas
    # câmeras por volta a ~2,1s cada. Subi-lo troca ciclo do parque inteiro
    # por amostragem mais densa numa câmera, e é o ciclo que mantém a
    # sobreposição do vídeo viva e a detecção das outras em dia.
    ACAO_CONTINUA_MAX_POR_VOLTA: int = 2

    # --- Fogo e fumaça por assinatura de imagem -------------------------
    # Veja `services/chama.py`. Os pisos são altos de propósito: num presídio
    # um alarme de incêndio falso esvazia uma galeria, e errar para menos é
    # mais barato que errar para mais.
    FOGO_R_MIN: int = 170            # vermelho mínimo no canal cru
    FOGO_S_MIN: int = 110            # saturação — derruba bege e marrom
    FOGO_V_MIN: int = 150            # brilho — chama é clara
    FOGO_AREA_MIN: int = 420         # px; abaixo disso é reflexo ou ruído
    # A fração da região cuja máscara muda de um quadro para o seguinte. É o
    # que separa chama de objeto laranja parado: um cone de sinalização dá
    # perto de zero, uma chama dá dezenas de por cento.
    FOGO_CINTILACAO_MIN: float = 0.08

    FUMACA_S_MAX: int = 46           # saturação baixa: fumaça não tem cor
    FUMACA_V_MIN: int = 70           # nem sombra…
    FUMACA_V_MAX: int = 205          # …nem estouro de branco
    FUMACA_AREA_MIN: int = 1800      # piso absoluto, para quadro pequeno
    # Piso que realmente manda: fração do quadro. Os dois falsos positivos da
    # Produção — mão passando sobre papel — mediam 0,75% do quadro e passaram
    # folgado no piso em pixels. Fumaça que justifica alarme de incêndio toma
    # a cena, não um cantinho dela.
    FUMACA_AREA_FRACAO_MIN: float = 0.04
    # Quanto da região pode cair sobre uma pessoa já detectada. Mão, braço e
    # roupa clara em movimento borram e perdem textura igual a fumaça; o
    # detector de objetos já sabe onde está a gente.
    FUMACA_SOBREPOSICAO_MAX: float = 0.25
    # Folga entre quadros consecutivos: a textura pode subir um pouco por
    # ruído de compressão, mas não pode voltar ao que era. Mão borra num
    # quadro e a cena volta no seguinte; fumaça não volta.
    FUMACA_FOLGA_PASSO: float = 1.15
    # Textura local mínima para a região valer análise. É medida numa janela
    # de 31 px, não no quadro inteiro: parede lisa dá perto de zero e fica
    # fora, porque onde não havia detalhe não há perda que signifique algo.
    FUMACA_TEXTURA_LOCAL_MIN: float = 40.0
    # Quanto do detalhe a região perdeu entre o primeiro quadro e o último.
    # É o sinal que distingue fumaça de parede cinza, que não perde nada.
    FUMACA_PERDA_MIN: float = 0.35
    # Textura mínima para a cena valer triagem de fumaça. Parede uniforme
    # dá 0,0; uma cena com mobília e marcação de piso, 1.741. Longe do
    # piso dos dois lados, de propósito — é filtro grosso.
    FUMACA_TEXTURA_MIN: float = 120.0

    # Quadros para decidir cintilação. Três é o mínimo: com dois, uma porta
    # abrindo é indistinguível de uma chama.
    CHAMA_QUADROS: int = 4
    CHAMA_INTERVALO_S: float = 0.18
    # Faixa de borda descartada em fogo e fumaça. As câmeras carimbam data
    # e hora numa borda, os dígitos mudam a cada segundo, e isso imita o
    # sinal de fumaça — textura que some. Medido: a caixa do falso
    # positivo dos Analistas ia de x 0,95 a 1,00 e y 0,05 a 0,18.
    CHAMA_MARGEM_BORDA: float = 0.08
    ACAO_ABANDONO_S: float = 45.0

    # --- Placa veicular ---------------------------------------------------
    # Lida no recorte do veículo, não no quadro inteiro: a placa é o que há
    # de menor em cena, e procurá-la em 1080p desperdiça o que o detector de
    # objeto já delimitou. Custa ~29ms por veículo, e veículo só aparece em
    # duas das 26 câmeras deste parque.
    PLACA_LEITURA: bool = True
    PLACA_MODELO_DETECTOR: str = "yolo-v9-t-256-license-plate-end2end"
    PLACA_MODELO_OCR: str = "cct-s-v1-global-model"
    # Piso da confiança do caractere mais fraco da leitura. Medido nas
    # capturas desta instalação: leitura correta não desceu de 0,69 e leitura
    # errada não passou de 0,25. 0,60 fica no vão entre os dois grupos.
    PLACA_CONFIANCA_MIN: float = 0.60
    # Abaixo disto não há pixel por caractere. O modelo devolve texto mesmo
    # assim — `OO006` saiu de uma placa de 13x4 px.
    PLACA_MIN_LARGURA_PX: int = 40
    PLACA_MIN_ALTURA_PX: int = 15

    # --- Retenção / privacidade -----------------------------------------
    RETENTION_DAYS_UNMATCHED: int = 90
    MASK_PII_FOR_ROLES: str = "OPERADOR,ANALISTA"

    # --- Evidências -------------------------------------------------------
    # Recortes das capturas, quando não há S3 configurado. Em produção use
    # volume dedicado: o expurgo apaga daqui junto com o registro, e é isso
    # que mantém a retenção honesta.
    SNAPSHOT_DIR: str = "evidencias"

    # --- Evidências (S3 compatível) --------------------------------------
    S3_ENDPOINT: str = ""
    S3_BUCKET: str = "seguranca-evidencias"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""

    # --- Gateway de mídia -------------------------------------------------
    MEDIA_GATEWAY: str = "http://localhost:8889"

    @field_validator("SECRET_KEY")
    @classmethod
    def _chave_forte(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("SECRET_KEY precisa de pelo menos 32 caracteres")
        return v

    @property
    def chave_efemera(self) -> bool:
        """A SECRET_KEY foi sorteada agora, em vez de vir da configuração?

        Quando sim, ela morre com o processo: no próximo reinício as sessões
        caem e os links de redefinição pendentes deixam de valer. Serve para
        rodar sem configurar nada, não para operar.
        """
        import os
        return not (os.getenv("SECRET_KEY") or self._do_env_file("SECRET_KEY"))

    @staticmethod
    def _do_env_file(chave: str) -> str:
        try:
            caminho = RAIZ / ".env"
            if not caminho.exists():
                return ""
            for linha in caminho.read_text(encoding="utf-8").splitlines():
                nome, _, valor = linha.partition("=")
                if nome.strip() == chave:
                    return valor.strip()
        except OSError:
            pass
        return ""

    @property
    def email_configurado(self) -> bool:
        """Há como enviar e-mail de verdade?

        A senha conta. Antes bastavam host e usuário, e com a senha em branco o
        sistema se declarava configurado, tentava enviar e falhava em toda
        tentativa — anunciando **falha de entrega** onde havia **configuração
        ausente**. São estados diferentes e pedem respostas diferentes: falha de
        entrega é defeito, e assusta com razão; configuração ausente é só uma
        instalação que ainda não chegou lá, e quem a vê precisa do caminho
        alternativo, não de um alarme.

        Só cobra a senha de quem a exige: um relay interno sem autenticação
        continua valendo como configurado.
        """
        if not (self.SMTP_HOST and self.SMTP_USER):
            return False
        if exige_autenticacao(self.SMTP_HOST) and not self.SMTP_PASSWORD:
            return False
        return True

    @property
    def falta_para_email(self) -> str:
        """O que impede o envio, em uma linha — vazio quando nada impede.

        Fica aqui, e não em quem envia, porque a resposta é a mesma para a tela
        de recuperação, para o aviso de arranque e para o script de diagnóstico.
        """
        if not self.SMTP_HOST:
            return "falta SMTP_HOST no .env"
        if not self.SMTP_USER:
            return "falta SMTP_USER no .env"
        if exige_autenticacao(self.SMTP_HOST) and not self.SMTP_PASSWORD:
            return (
                f"falta SMTP_PASSWORD — {self.SMTP_HOST} exige autenticação. "
                "No Gmail é uma 'senha de app' de 16 caracteres, gerada em "
                "myaccount.google.com/apppasswords com a verificação em duas "
                "etapas ativa"
            )
        return ""

    @property
    def smtp_remetente(self) -> str:
        return self.SMTP_FROM or self.SMTP_USER

    @property
    def url_local(self) -> str:
        """Endereço para ler, copiar e abrir no navegador.

        O laço local aparece como `localhost` — é o que a pessoa reconhece,
        e o que mantém a barra de endereço igual em toda a instalação. Um
        host de verdade, quando configurado, aparece como está.
        """
        laco = {"127.0.0.1", "0.0.0.0", "::1", "::", "localhost", ""}
        nome = "localhost" if self.HOST in laco else self.HOST
        return f"http://{nome}:{self.PORT}"

    @property
    def cors_list(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def mask_roles(self) -> List[str]:
        return [r.strip() for r in self.MASK_PII_FOR_ROLES.split(",") if r.strip()]

    @property
    def usa_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def usa_redis(self) -> bool:
        return bool(self.REDIS_URL.strip())

    @property
    def modo(self) -> str:
        return "demonstração (SQLite)" if self.usa_sqlite else "produção (PostgreSQL)"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

