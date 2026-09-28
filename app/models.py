"""Modelos ORM do VIGIA (SQLAlchemy 2.0, async).

Pontos de projeto que importam:

* `Pessoa.id_biometrico` é emitido UMA vez, no cadastro, com prefixo derivado
  do sexo informado no registro civil. Corrigir o sexo depois NÃO reemite o ID.
* `Pessoa.embedding` é um vetor pgvector de 512 dimensões — template
  irreversível. A fotografia original não é persistida.
* `Auditoria` é append-only e encadeada por hash (cada linha carrega o hash da
  anterior), de modo que remoção ou alteração silenciosa quebra a cadeia.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import List, Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, Enum, Float,
    ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings
from app.tipos import GUID, DataHora, JSONPortatil, Vetor


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


# O SQLite só auto-incrementa uma coluna `INTEGER PRIMARY KEY`; um BIGINT
# não é reconhecido como rowid e a inserção falha por NOT NULL. Esta variante
# mantém BIGINT no PostgreSQL e usa INTEGER no SQLite.
PK_SERIAL = BigInteger().with_variant(Integer, "sqlite")


# ======================================================================
# Enumerações
# ======================================================================
class Sexo(str, enum.Enum):
    """Sexo do REGISTRO CIVIL. Nunca inferido a partir da imagem."""
    M = "M"   # masculino  -> BIO-M-######
    F = "F"   # feminino   -> BIO-F-######
    N = "N"   # não informado / indeterminado -> BIO-N-######


class StatusPessoa(str, enum.Enum):
    PROCURADO = "PROCURADO"
    MONITORADO = "MONITORADO"
    DESAPARECIDO = "DESAPARECIDO"
    FUNCIONARIO = "FUNCIONARIO"
    CIDADAO = "CIDADAO"


class StatusCamera(str, enum.Enum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    MANUTENCAO = "MANUTENCAO"


class TipoDeteccao(str, enum.Enum):
    FACIAL = "FACIAL"
    OBJETO = "OBJETO"
    ACAO = "ACAO"


class StatusAlerta(str, enum.Enum):
    ABERTO = "ABERTO"
    EM_ANDAMENTO = "EM_ANDAMENTO"
    RESOLVIDO = "RESOLVIDO"


class Perfil(str, enum.Enum):
    ADMIN = "ADMIN"
    SUPER = "SUPER"
    OPERADOR = "OPERADOR"
    ANALISTA = "ANALISTA"
    AUDITOR = "AUDITOR"


class TipoZona(str, enum.Enum):
    POLIGONO = "POLIGONO"
    LINHA = "LINHA"


# Permissões efetivas por perfil — fonte única da verdade do RBAC.
PERMISSOES: dict[Perfil, set[str]] = {
    Perfil.ADMIN:    {"live", "cams", "facial", "objects", "actions", "biometrics",
                      "alerts", "users", "audit", "settings", "export", "edit"},
    Perfil.SUPER:    {"live", "cams", "facial", "objects", "actions", "biometrics",
                      "alerts", "audit", "export", "edit"},
    Perfil.OPERADOR: {"live", "cams", "facial", "objects", "actions", "biometrics",
                      "alerts", "edit"},
    Perfil.ANALISTA: {"live", "facial", "objects", "actions", "biometrics",
                      "alerts", "export"},
    Perfil.AUDITOR:  {"audit", "alerts", "facial", "objects", "actions"},
}


# ======================================================================
# Usuários e acesso
# ======================================================================
class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    matricula: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    nome: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    senha_hash: Mapped[str] = mapped_column(String(255))
    perfil: Mapped[Perfil] = mapped_column(Enum(Perfil, name="perfil_enum"))
    unidade: Mapped[str] = mapped_column(String(120), default="")
    telefone: Mapped[str] = mapped_column(String(32), default="")

    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    mfa_ativo: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_secret: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    tentativas_falhas: Mapped[int] = mapped_column(Integer, default=0)
    bloqueado_ate: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)
    senha_alterada_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())
    forcar_troca_senha: Mapped[bool] = mapped_column(Boolean, default=False)
    ultimo_acesso: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)

    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())

    senhas_antigas: Mapped[List["SenhaHistorico"]] = relationship(
        back_populates="usuario", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def permissoes(self) -> set[str]:
        return PERMISSOES[self.perfil]

    def pode(self, permissao: str) -> bool:
        return permissao in self.permissoes


class SenhaHistorico(Base):
    """Impede reuso das últimas N senhas."""
    __tablename__ = "senha_historico"

    id: Mapped[int] = mapped_column(PK_SERIAL, primary_key=True, autoincrement=True)
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    senha_hash: Mapped[str] = mapped_column(String(255))
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())

    usuario: Mapped[Usuario] = relationship(back_populates="senhas_antigas")


class TokenRecuperacao(Base):
    """Código OTP de redefinição de senha — guardado como HASH, nunca em claro."""
    __tablename__ = "tokens_recuperacao"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    codigo_hash: Mapped[str] = mapped_column(String(128))
    expira_em: Mapped[datetime] = mapped_column(DataHora(), index=True)
    tentativas: Mapped[int] = mapped_column(Integer, default=0)
    usado: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_solicitante: Mapped[str] = mapped_column(String(64), default="")
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())


# ======================================================================
# Câmeras
# ======================================================================
class Camera(Base):
    __tablename__ = "cameras"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    codigo: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # CAM-001
    nome: Mapped[str] = mapped_column(String(200))
    setor: Mapped[str] = mapped_column(String(120), index=True)
    cena: Mapped[str] = mapped_column(String(32), default="rua")

    status: Mapped[StatusCamera] = mapped_column(
        Enum(StatusCamera, name="status_camera_enum"), default=StatusCamera.ONLINE, index=True
    )
    ip: Mapped[str] = mapped_column(String(64), default="")
    rtsp_url: Mapped[str] = mapped_column(Text)
    onvif_url: Mapped[str] = mapped_column(Text, default="")
    fabricante: Mapped[str] = mapped_column(String(80), default="")
    modelo: Mapped[str] = mapped_column(String(80), default="")

    resolucao: Mapped[str] = mapped_column(String(24), default="1920x1080")
    fps: Mapped[int] = mapped_column(Integer, default=25)
    codec: Mapped[str] = mapped_column(String(16), default="H.265")
    bitrate_kbps: Mapped[int] = mapped_column(Integer, default=4096)

    ptz: Mapped[bool] = mapped_column(Boolean, default=False)
    infravermelho: Mapped[bool] = mapped_column(Boolean, default=True)
    analitico_facial: Mapped[bool] = mapped_column(Boolean, default=True)
    analitico_objeto: Mapped[bool] = mapped_column(Boolean, default=True)
    analitico_acao: Mapped[bool] = mapped_column(Boolean, default=False)

    limiar_facial: Mapped[float] = mapped_column(Float, default=settings.FACE_THRESHOLD)
    limiar_objeto: Mapped[float] = mapped_column(Float, default=settings.OBJECT_THRESHOLD)
    retencao_dias: Mapped[int] = mapped_column(Integer, default=90)

    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    latencia_ms: Mapped[int] = mapped_column(Integer, default=0)
    ultimo_heartbeat: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)
    instalada_em: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())

    zonas: Mapped[List["Zona"]] = relationship(
        back_populates="camera", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("limiar_facial >= 0.5 AND limiar_facial <= 1.0", name="ck_limiar_facial"),
        CheckConstraint("fps > 0 AND fps <= 120", name="ck_fps"),
    )


class Zona(Base):
    """Região de interesse. `pontos` = [[x,y], ...] normalizados em 0..1."""
    __tablename__ = "zonas"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    nome: Mapped[str] = mapped_column(String(120))
    tipo: Mapped[TipoZona] = mapped_column(Enum(TipoZona, name="tipo_zona_enum"), default=TipoZona.POLIGONO)
    cor: Mapped[str] = mapped_column(String(9), default="#35A7DE")
    pontos: Mapped[list] = mapped_column(JSONPortatil, default=list)
    regras: Mapped[dict] = mapped_column(JSONPortatil, default=dict)  # ex.: {"invasao": true, "contagem": true}

    camera: Mapped[Camera] = relationship(back_populates="zonas")


# ======================================================================
# Base biométrica
# ======================================================================
class SequenciaBiometrica(Base):
    """Contador por sexo. Uma linha por prefixo, travada com FOR UPDATE
    na emissão para garantir unicidade sob concorrência."""
    __tablename__ = "sequencia_biometrica"

    sexo: Mapped[Sexo] = mapped_column(Enum(Sexo, name="sexo_enum"), primary_key=True)
    proximo: Mapped[int] = mapped_column(BigInteger, default=1)


class SequenciaCodigo(Base):
    """Contador por série de código: uma linha por prefixo (e ano, quando há).

    Antes o código de uma detecção saía de ``COUNT(*) + 1``. Isso presume que
    registro nunca é apagado — e no dia em que um foi, a contagem recuou
    abaixo do maior código já emitido e o gerador passou a reemitir código
    existente. O ``UNIQUE`` barrava o insert, a contagem não subia, e a
    próxima tentativa repetia o mesmo código: a ingestão parava de vez, com
    o worker detectando normalmente e todo registro sendo descartado.

    Um contador não recua quando uma linha é apagada, e a trava de linha na
    emissão serializa dois envios simultâneos — o mesmo desenho de
    ``sequencia_biometrica``, pelas mesmas duas razões.
    """
    __tablename__ = "sequencia_codigo"

    chave: Mapped[str] = mapped_column(String(16), primary_key=True)
    proximo: Mapped[int] = mapped_column(BigInteger, default=1)


class Pessoa(Base):
    __tablename__ = "pessoas"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)

    # Identificador permanente, emitido uma única vez. Formato: BIO-<M|F|N>-NNNNNN
    id_biometrico: Mapped[str] = mapped_column(String(20), unique=True, index=True)

    # Sexo do registro civil (entrada humana). Define o PREFIXO na emissão.
    sexo: Mapped[Sexo] = mapped_column(Enum(Sexo, name="sexo_enum"), index=True)
    # Prefixo congelado no momento da emissão — preserva o ID se o sexo for corrigido.
    sexo_emissao: Mapped[Sexo] = mapped_column(Enum(Sexo, name="sexo_enum"))

    nome: Mapped[str] = mapped_column(String(200), index=True)
    alcunha: Mapped[str] = mapped_column(String(120), default="")
    nascimento: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    cpf: Mapped[Optional[str]] = mapped_column(String(14), nullable=True, index=True)
    rg: Mapped[Optional[str]] = mapped_column(String(24), nullable=True)

    status: Mapped[StatusPessoa] = mapped_column(
        Enum(StatusPessoa, name="status_pessoa_enum"), default=StatusPessoa.CIDADAO, index=True
    )
    mandado: Mapped[str] = mapped_column(String(64), default="")
    tipificacao: Mapped[str] = mapped_column(String(200), default="")
    orgao_expedidor: Mapped[str] = mapped_column(String(160), default="")
    unidade: Mapped[str] = mapped_column(String(120), default="")

    # Template biométrico irreversível (ArcFace, L2-normalizado).
    embedding: Mapped[Optional[list]] = mapped_column(Vetor(settings.EMBEDDING_DIM), nullable=True)
    qualidade: Mapped[float] = mapped_column(Float, default=0.0)
    amostras: Mapped[int] = mapped_column(Integer, default=0)

    base_legal: Mapped[str] = mapped_column(String(200), default="Segurança pública")
    observacoes: Mapped[str] = mapped_column(Text, default="")

    total_deteccoes: Mapped[int] = mapped_column(BigInteger, default=0)
    ultima_deteccao: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)
    ultima_camera: Mapped[str] = mapped_column(String(200), default="")

    ativo: Mapped[bool] = mapped_column(Boolean, default=True)
    cadastrado_por: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())
    atualizado_em: Mapped[datetime] = mapped_column(
        DataHora(), server_default=func.now(), onupdate=func.now()
    )

    amostras_rel: Mapped[List["AmostraFacial"]] = relationship(
        back_populates="pessoa", cascade="all, delete-orphan", lazy="selectin"
    )

    # A validação do formato do ID vive em três lugares, de propósito:
    # aqui (aplicação), em sql/01_postgres.sql (CHECK + trigger) e nos testes.
    __table_args__ = (
        Index("ix_pessoas_status_sexo", "status", "sexo"),
    )


class AmostraFacial(Base):
    """Cada rosto cadastrado pode ter várias amostras; o template da pessoa é
    a média L2-normalizada delas."""
    __tablename__ = "amostras_faciais"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    pessoa_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pessoas.id", ondelete="CASCADE"), index=True)
    embedding: Mapped[list] = mapped_column(Vetor(settings.EMBEDDING_DIM))
    qualidade: Mapped[float] = mapped_column(Float, default=0.0)
    origem: Mapped[str] = mapped_column(String(32), default="cadastro")  # cadastro | captura
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())

    pessoa: Mapped[Pessoa] = relationship(back_populates="amostras_rel")


# ======================================================================
# Detecções e alertas
# ======================================================================
class Deteccao(Base):
    __tablename__ = "deteccoes"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    codigo: Mapped[str] = mapped_column(String(24), unique=True, index=True)  # FR-2026000001
    tipo: Mapped[TipoDeteccao] = mapped_column(Enum(TipoDeteccao, name="tipo_deteccao_enum"), index=True)
    ocorrido_em: Mapped[datetime] = mapped_column(DataHora(), index=True)

    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)

    # --- facial ---
    pessoa_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("pessoas.id", ondelete="SET NULL"), nullable=True, index=True
    )
    similaridade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    limiar_aplicado: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    qualidade_face: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- objeto / ação ---
    classe: Mapped[str] = mapped_column(String(48), default="", index=True)
    confianca: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    duracao_s: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    envolvidos: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    placa: Mapped[str] = mapped_column(String(16), default="")

    zona_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("zonas.id", ondelete="SET NULL"), nullable=True
    )
    bbox: Mapped[Optional[list]] = mapped_column(JSONPortatil, nullable=True)  # [x, y, w, h] normalizado
    track_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    severidade: Mapped[int] = mapped_column(Integer, default=1, index=True)  # 1..4
    snapshot_key: Mapped[str] = mapped_column(String(300), default="")       # chave no S3
    clip_key: Mapped[str] = mapped_column(String(300), default="")

    revisado: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    veredito: Mapped[str] = mapped_column(String(32), default="")            # Confirmado | Falso positivo
    revisado_por: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    revisado_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)

    hash_evidencia: Mapped[str] = mapped_column(String(64), default="")      # SHA-256 do quadro
    expurgo_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True, index=True)

    camera: Mapped[Camera] = relationship(lazy="joined")
    pessoa: Mapped[Optional[Pessoa]] = relationship(lazy="joined")

    __table_args__ = (
        Index("ix_deteccoes_tipo_data", "tipo", "ocorrido_em"),
        Index("ix_deteccoes_cam_data", "camera_id", "ocorrido_em"),
        CheckConstraint("severidade BETWEEN 1 AND 4", name="ck_severidade"),
    )


class Alerta(Base):
    __tablename__ = "alertas"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    codigo: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    deteccao_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("deteccoes.id", ondelete="SET NULL"), nullable=True
    )
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    pessoa_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("pessoas.id", ondelete="SET NULL"), nullable=True
    )

    tipo: Mapped[TipoDeteccao] = mapped_column(Enum(TipoDeteccao, name="tipo_deteccao_enum"))
    titulo: Mapped[str] = mapped_column(String(200))
    detalhe: Mapped[str] = mapped_column(Text, default="")
    severidade: Mapped[int] = mapped_column(Integer, default=3, index=True)

    status: Mapped[StatusAlerta] = mapped_column(
        Enum(StatusAlerta, name="status_alerta_enum"), default=StatusAlerta.ABERTO, index=True
    )
    atribuido_a: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    resolucao: Mapped[str] = mapped_column(Text, default="")
    disparado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now(), index=True)
    fechado_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)

    camera: Mapped[Camera] = relationship(lazy="joined")
    pessoa: Mapped[Optional[Pessoa]] = relationship(lazy="joined")


# ======================================================================
# Auditoria encadeada
# ======================================================================
class Auditoria(Base):
    """Append-only. `hash_atual = sha256(hash_anterior || payload canônico)`.

    Em produção: revogar UPDATE/DELETE desta tabela para o papel da aplicação
    (veja sql/01_schema.sql) e verificar a cadeia periodicamente.
    """
    __tablename__ = "auditoria"

    id: Mapped[int] = mapped_column(PK_SERIAL, primary_key=True, autoincrement=True)
    ocorrido_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now(), index=True)

    acao: Mapped[str] = mapped_column(String(48), index=True)
    descricao: Mapped[str] = mapped_column(String(300))
    severidade: Mapped[str] = mapped_column(String(16), default="info")  # info|ok|warn|err

    usuario_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True, index=True
    )
    usuario_email: Mapped[str] = mapped_column(String(160), default="")
    usuario_nome: Mapped[str] = mapped_column(String(160), default="")
    perfil: Mapped[str] = mapped_column(String(24), default="")

    alvo: Mapped[str] = mapped_column(String(120), default="")
    finalidade: Mapped[str] = mapped_column(String(200), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    metadados: Mapped[dict] = mapped_column(JSONPortatil, default=dict)

    hash_anterior: Mapped[str] = mapped_column(String(64), default="")
    hash_atual: Mapped[str] = mapped_column(String(64), index=True)

    __table_args__ = (
        UniqueConstraint("hash_atual", name="uq_auditoria_hash"),
    )
