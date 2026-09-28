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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Aplicação -------------------------------------------------------
    APP_NAME: str = "SEGURANÇA — Centro Integrado de Videomonitoramento"
    APP_VERSION: str = "4.0.0"
    ENV: str = "development"
    DEBUG: bool = False
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
    SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    ACCESS_TOKEN_MINUTES: int = 60
    REFRESH_TOKEN_DAYS: int = 7
    JWT_ALGORITHM: str = "HS256"
    MAX_LOGIN_ATTEMPTS: int = 5
    LOCKOUT_MINUTES: int = 15
    PASSWORD_EXPIRE_DAYS: int = 90
    PASSWORD_HISTORY: int = 5
    OTP_TTL_SECONDS: int = 600
    OTP_MAX_ATTEMPTS: int = 3

    # --- CORS ------------------------------------------------------------
    # O front-end é servido pela própria API, então CORS só importa se você
    # hospedar a interface em outro domínio.
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # --- Biometria -------------------------------------------------------
    EMBEDDING_DIM: int = 512
    FACE_MODEL: str = "buffalo_l"
    # Similaridade cosseno mínima para atribuir identidade. A referência do
    # próprio InsightFace para este modelo é 0,5; fica acima disso porque aqui
    # um falso positivo leva alguém a ser abordado pelo nome de outro.
    # Estava em 0,88, que na prática desliga o reconhecimento: exigiria a
    # mesma pessoa no mesmo ângulo e na mesma luz da foto de cadastro. Em
    # centenas de capturas reais desta instalação, nada passou de 0,15.
    # Seja qual for o valor, o alerta continua exigindo confirmação humana
    # antes de qualquer abordagem.
    FACE_THRESHOLD: float = 0.55
    FACE_DET_SIZE: int = 640
    MIN_FACE_QUALITY: float = 0.55
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
    # de passagem, que serve à auditoria mesmo sem identificar ninguém. A 70
    # metade volta a entrar, e o lixo de 26 a 50 pixels continua de fora.
    # Identificação, porém, só sai quando o rosto vem grande: abaixe a câmera
    # ou feche o zoom se o reconhecimento é o objetivo desta posição.
    MIN_FACE_PIXELS: int = 70
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
    # Medido em CPU num quadro 1080p real: o nano leva 3,2s e o large 5,1s,
    # achando os mesmos objetos. Com GPU, troque por yolov8l.pt — aí o modelo
    # maior sai quase de graça e ganha nos objetos pequenos e distantes.
    # Caminho a partir da raiz: os pesos moram em models/. Como nome solto, o
    # Ultralytics procura no diretório de trabalho, não encontra e baixa de
    # novo — passando a carregar uma cópia na raiz em vez do peso versionado.
    OBJECT_MODEL: str = str(RAIZ / "models" / "yolov8n.pt")
    OBJECT_THRESHOLD: float = 0.65
    DEVICE: str = "cpu"

    # --- Retenção / privacidade -----------------------------------------
    RETENTION_DAYS_UNMATCHED: int = 90
    MASK_PII_FOR_ROLES: str = "OPERADOR,ANALISTA"
    WATERMARK_EVIDENCE: bool = True

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

