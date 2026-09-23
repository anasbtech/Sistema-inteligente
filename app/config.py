"""Configuração central do VIGIA.

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
    APP_NAME: str = "VIGIA — Centro Integrado de Videomonitoramento"
    APP_VERSION: str = "4.0.0"
    ENV: str = "development"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # --- Banco -----------------------------------------------------------
    # SQLite por padrão. Para produção:
    #   postgresql+asyncpg://vigia:vigia@localhost:5432/vigia
    DATABASE_URL: str = f"sqlite+aiosqlite:///{RAIZ / 'vigia.db'}"
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
    MIN_FACE_PIXELS: int = 90
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
    OBJECT_MODEL: str = "yolov8n.pt"
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
    S3_BUCKET: str = "vigia-evidencias"
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
