"""Modelos ORM do SEGURANÇA (SQLAlchemy 2.0, async).

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
    """Situação de quem está na base biométrica deste prédio.

    Só `RESTRITO` vira ocorrência ao ser reconhecido: quem trabalha ou visita
    é registrado como passagem, sem acionar ninguém.
    """
    FUNCIONARIO = "FUNCIONARIO"
    VISITANTE = "VISITANTE"
    TERCEIRIZADO = "TERCEIRIZADO"
    RESTRITO = "RESTRITO"


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
    # Excluir não apaga a linha: `auditoria.usuario_id` é ON DELETE SET NULL, e
    # zerá-lo reescreveria linhas já encadeadas por hash, quebrando a cadeia.
    excluido_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)
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
    """Segredo do link de redefinição — guardado como HASH, nunca em claro.

    `tentativas` sobrou do código de seis dígitos, que precisava limitar o
    chute. O segredo do link tem entropia demais para ser adivinhado, então
    hoje o campo fica em zero; continua aqui porque remover coluna de banco em
    produção custa mais do que o espaço que ela ocupa.
    """
    __tablename__ = "tokens_recuperacao"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    usuario_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), index=True)
    codigo_hash: Mapped[str] = mapped_column(String(128))
    expira_em: Mapped[datetime] = mapped_column(DataHora(), index=True)
    tentativas: Mapped[int] = mapped_column(Integer, default=0)
    usado: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_solicitante: Mapped[str] = mapped_column(String(64), default="")
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())


class Configuracao(Base):
    """Parâmetro ajustado pela interface, sobrepondo o padrão do `.env`.

    Só o que a aplicação relê a cada uso entra aqui; o que é lido na subida
    do processo continua vindo do `.env`, porque gravar um valor que não
    passa a valer é pior que não oferecer o campo.
    """
    __tablename__ = "configuracao"

    chave: Mapped[str] = mapped_column(String(64), primary_key=True)
    valor: Mapped[str] = mapped_column(String(200))
    alterado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())
    alterado_por: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )


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
    # Quais ações esta câmera vigia, entre as do catálogo em `vision`.
    #
    # A chave acima liga a análise; esta lista diz o que ela procura. Separar
    # as duas é o que permite a mesma instalação vigiar queda no arquivo e
    # aglomeração no saguão sem obrigar as duas a tudo — e sem cobrar da CPU
    # um cálculo que naquele lugar não significa nada.
    #
    # Lista vazia com a análise ligada é análise que não procura nada, e é
    # assim de propósito: o padrão de quem liga está em `vision.ACOES_PADRAO`,
    # mas desmarcar tudo é uma escolha legítima de quem configura.
    acoes_ativas: Mapped[list] = mapped_column(JSONPortatil, default=list)
    # Toda pessoa nesta câmera vira alerta, identificada ou não.
    #
    # É o oposto do reconhecimento facial, que alerta sobre *quem* entrou:
    # aqui o que importa é o lugar, e autorização não entra na conta. Sala de
    # reunião fora de hora, datacenter, almoxarifado — presença ali já é o
    # fato a registrar, e dizer "mas essa pessoa tem acesso" é julgamento de
    # quem revisa, não do detector.
    #
    # Desligado por padrão: ligado numa sala de trabalho, onde há gente
    # sentada o dia inteiro, isso vira uma enxurrada que some com os alertas
    # que importam.
    alerta_presenca: Mapped[bool] = mapped_column(Boolean, default=False)

    # Janela em que circular aqui é normal, em hora de parede ("HH:MM"). Fora
    # dela, a ação `fora_de_horario` acusa quem aparecer.
    #
    # Por câmera, e não só no sistema, porque "horário não permitido" é uma
    # propriedade do lugar: a portaria tem gente a noite inteira, o
    # almoxarifado não deveria ter ninguém depois das 18h, e o datacenter não
    # deveria ter ninguém nunca. Uma janela única para o parque obrigaria as
    # três a mentir.
    #
    # Nulo é "usa a do sistema" (`ACAO_HORARIO_INICIO`/`FIM`) — assim mudar o
    # padrão vale para quem não quis decidir, sem passar por cima de quem quis.
    # Início igual ao fim é janela de 24 h: tudo permitido, nada acusa.
    horario_inicio: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    horario_fim: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)

    # Esta câmera alcança via, pátio ou garagem? Desligado, carro, moto e
    # bicicleta são ignorados nela — num hall de andar eles só podem vir da
    # janela, e o registro diria que havia um carro dentro da sala.
    # Padrão desligado: o parque é interno, e a exceção é quem vê veículo.
    detecta_veiculo: Mapped[bool] = mapped_column(Boolean, default=False)
    # Inteligência veicular (services/veiculos): laço próprio, com rastreio
    # ByteTrack, cor, marca/modelo e placa por passagem. Ligada, os veículos
    # desta câmera passam a ser dela — o laço geral deixa de registrá-los, e
    # um carro não vira dois registros. Exige `detecta_veiculo`.
    inteligencia_veicular: Mapped[bool] = mapped_column(Boolean, default=False)
    # Aizon Guardian: sinais de socorro (gesto) e correlação de sinais de
    # possível violência. Desligado em todas: liga quem conhece o lugar.
    guardian: Mapped[bool] = mapped_column(Boolean, default=False)

    # Bagagem — mochila, bolsa, mala. Desligado, porque numa câmera de teto o
    # que tem essa silhueta é saco de lixo e caixa de papelão: em seis semanas
    # vieram 15 registros e nenhum era bagagem. Ligue onde bagagem é o assunto
    # — revista de entrada, guarda-volumes, esteira.
    detecta_bagagem: Mapped[bool] = mapped_column(Boolean, default=False)

    # Animal. Desligado porque o COCO não tem rótulo para pessoa agachada ou de
    # costas, e o modelo resolve a dúvida escolhendo o animal mais parecido em
    # silhueta: das 10 detecções que chegaram ao registro, as 10 eram pessoas.
    # Ligue em pátio, perímetro ou via — onde animal solto é ocorrência.
    detecta_animal: Mapped[bool] = mapped_column(Boolean, default=False)
    # Celular. Desligado porque numa sala de trabalho todo mundo tem um na mão;
    # ligue na revista de entrada, na cela ou onde ele é proibido.
    detecta_celular: Mapped[bool] = mapped_column(Boolean, default=False)
    # EPIs exigidos nesta câmera (services/epi.py): ["capacete", "colete"...].
    # Vazia = conformidade de EPI desligada.
    epi_obrigatorios: Mapped[list] = mapped_column(JSONPortatil, default=list)

    # Capacete e arma de fogo, que vêm de detectores próprios e não do YOLO do
    # parque. Desligados por padrão: cada um custa uma passada a mais sobre o
    # quadro, e o orçamento de CPU deste parque já é apertado.
    detecta_capacete: Mapped[bool] = mapped_column(Boolean, default=False)
    detecta_arma: Mapped[bool] = mapped_column(Boolean, default=False)

    # Quantas pessoas juntas são aglomeração *aqui*. Por câmera pelo mesmo
    # motivo do horário: é propriedade do lugar. Oito pessoas num corredor de
    # passagem é ocorrência; na Produção, onde treze trabalham, é terça-feira.
    # Um número único para o parque obrigaria uma das duas a mentir.
    #
    # Nulo é "usa o do sistema" (`ACAO_AGLOMERACAO_N`).
    limite_aglomeracao: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

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
        Enum(StatusPessoa, name="status_pessoa_enum"), default=StatusPessoa.VISITANTE, index=True
    )
    restricao: Mapped[str] = mapped_column(String(64), default="")
    tipificacao: Mapped[str] = mapped_column(String(200), default="")
    orgao_expedidor: Mapped[str] = mapped_column(String(160), default="")
    unidade: Mapped[str] = mapped_column(String(120), default="")

    # Template biométrico irreversível (ArcFace, L2-normalizado).
    embedding: Mapped[Optional[list]] = mapped_column(Vetor(settings.EMBEDDING_DIM), nullable=True)
    qualidade: Mapped[float] = mapped_column(Float, default=0.0)
    amostras: Mapped[int] = mapped_column(Integer, default=0)

    base_legal: Mapped[str] = mapped_column(String(200), default="Segurança pública")
    observacoes: Mapped[str] = mapped_column(Text, default="")
    # Captura escolhida por uma pessoa como foto da ficha (código da detecção).
    # Vazio = automático: o rosto mais confiável ainda vinculado a ela.
    foto_deteccao: Mapped[str] = mapped_column(String(24), default="")

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
    # De qual câmera veio a captura que virou esta amostra; nulo na foto de
    # cadastro. A busca 1:N compara contra cada amostra e fica com a melhor,
    # então uma amostra por ângulo é o que faz a pessoa ser reconhecida fora
    # da câmera onde foi cadastrada. Guardar a origem é o que permite saber
    # quais ângulos o template já cobre — e aplicar cota por câmera, sem a
    # qual a câmera movimentada enche o cadastro sozinha e as outras seguem
    # descobertas.
    camera_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("cameras.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # A captura que virou esta amostra. É o que deixa o "Falso positivo"
    # desfazer o erro por inteiro: sem este vínculo a captura saía da ficha,
    # mas o rosto dela seguia no template e voltava a puxar a mesma pessoa
    # errada todo dia (06/11/2026: um homem na ficha da Thais, três vezes).
    deteccao_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("deteccoes.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    # Quem o motor apontou em primeiro quando a similaridade não alcançou o
    # limiar. Não é identificação, e por isso não é `pessoa_id`: é o palpite
    # que a fila de conferência mostra ao operador. Confirmado por ele, a
    # captura vira amostra e aí sim `pessoa_id` é preenchido.
    pessoa_sugerida_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("pessoas.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    # Recorte da placa lida, separado do recorte do veículo: na captura do
    # carro inteiro a placa ocupa poucos pixels, e conferir a leitura exigia
    # ampliar uma imagem que não aguenta ampliação.
    placa_snapshot_key: Mapped[str] = mapped_column(String(300), default="")
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
    # `foreign_keys` explícito: esta tabela passou a apontar para `pessoas`
    # por dois caminhos — quem foi identificado e quem foi apenas sugerido —,
    # e sem dizer qual deles é o relacionamento o SQLAlchemy recusa o
    # mapeamento inteiro, derrubando o sistema no arranque.
    pessoa: Mapped[Optional[Pessoa]] = relationship(
        lazy="joined", foreign_keys=[pessoa_id]
    )
    # `noload`: a sugestão só interessa à fila de conferência, que a busca
    # explicitamente. Carregá-la junto de toda detecção seria um join a mais
    # em todas as listagens, para um campo quase sempre nulo.
    pessoa_sugerida: Mapped[Optional[Pessoa]] = relationship(
        lazy="noload", foreign_keys=[pessoa_sugerida_id]
    )

    __table_args__ = (
        Index("ix_deteccoes_tipo_data", "tipo", "ocorrido_em"),
        Index("ix_deteccoes_cam_data", "camera_id", "ocorrido_em"),
        CheckConstraint("severidade BETWEEN 1 AND 4", name="ck_severidade"),
    )


class PassagemVeiculo(Base):
    """Um veículo, do momento em que entrou em cena até sair (ou parar por
    VEICULO_PASSAGEM_MAX_S). Cada atributo vem com a sua confiança; vazio é
    "não identificado", nunca um palpite."""
    __tablename__ = "passagens_veiculo"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    # O registro geral da mesma passagem (lista de objetos, alertas, retenção).
    deteccao_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("deteccoes.id", ondelete="SET NULL"), nullable=True, index=True)
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    trilha: Mapped[int] = mapped_column(Integer)                 # id do ByteTrack na câmera
    inicio: Mapped[datetime] = mapped_column(DataHora(), index=True)
    fim: Mapped[datetime] = mapped_column(DataHora())
    quadros: Mapped[int] = mapped_column(Integer, default=0)

    # Tipo (identificacao.TIPOS): carro | picape | van | caminhao_pequeno |
    # caminhao_grande | onibus | motocicleta | veiculo (sem convicção).
    categoria: Mapped[str] = mapped_column(String(20), index=True)
    categoria_conf: Mapped[float] = mapped_column(Float, default=0.0)
    cor: Mapped[str] = mapped_column(String(20), default="", index=True)
    cor_conf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    marca: Mapped[str] = mapped_column(String(40), default="", index=True)
    modelo: Mapped[str] = mapped_column(String(60), default="")
    marca_modelo_conf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ano: Mapped[str] = mapped_column(String(12), default="")
    placa: Mapped[str] = mapped_column(String(16), default="", index=True)
    placa_conf: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Por que um atributo ficou vazio ("modelo não treinado", "imagem sem
    # cor"...): é o que separa "não sei" de "não olhei".
    observacoes: Mapped[Optional[dict]] = mapped_column(JSONPortatil, nullable=True)

    bbox: Mapped[Optional[list]] = mapped_column(JSONPortatil, nullable=True)  # [x, y, w, h] no quadro
    recorte_key: Mapped[str] = mapped_column(String(300), default="")
    quadro_key: Mapped[str] = mapped_column(String(300), default="")
    placa_key: Mapped[str] = mapped_column(String(300), default="")
    # Vetor da imagem (CLIP), guardado para treinar e medir sem depender de a
    # imagem ainda existir: a retenção apaga o recorte, o vetor não identifica
    # ninguém sozinho.
    vetor: Mapped[Optional[list]] = mapped_column(JSONPortatil, nullable=True)
    # Vetor do CLIP grande (modelo_fino), só de carro: é sobre ele que a marca
    # e o modelo são treinados e afirmados. Nulo = ainda não analisado.
    vetor_modelo: Mapped[Optional[list]] = mapped_column(JSONPortatil, nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())

    camera: Mapped[Camera] = relationship(lazy="joined")


class RotuloVeiculo(Base):
    """O que uma pessoa disse que o veículo é. Base de treino e de medição da
    cor e da marca/modelo — nunca altera a passagem sozinho."""
    __tablename__ = "rotulos_veiculo"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    passagem_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("passagens_veiculo.id", ondelete="CASCADE"), unique=True, index=True)
    tipo: Mapped[str] = mapped_column(String(20), default="")         # "" = não sei
    cor: Mapped[str] = mapped_column(String(20), default="")          # "" = não sei
    marca: Mapped[str] = mapped_column(String(40), default="")
    modelo: Mapped[str] = mapped_column(String(60), default="")
    usuario_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True)
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())


class OcorrenciaGuardian(Base):
    """Uma ocorrência do Aizon Guardian: sinais de risco que coincidiram na
    mesma câmera, com a prioridade que a combinação deles merece.

    Não afirma fato: `sinais` são o que a IA viu (gesto, trajetória), cada um
    apontando para a detecção que o registrou. Quem decide é a avaliação
    humana, que fica em `historico` com autor, hora e justificativa."""
    __tablename__ = "guardian_ocorrencias"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    codigo: Mapped[str] = mapped_column(String(20), unique=True, index=True)   # GRD-00001
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    inicio: Mapped[datetime] = mapped_column(DataHora(), index=True)
    ultimo_sinal: Mapped[datetime] = mapped_column(DataHora())
    prioridade: Mapped[str] = mapped_column(String(10), index=True)     # CRITICA | ALTA | MEDIA | BAIXA
    pontuacao: Mapped[float] = mapped_column(Float, default=0.0)
    confianca: Mapped[float] = mapped_column(Float, default=0.0)
    sinais: Mapped[list] = mapped_column(JSONPortatil, default=list)
    status: Mapped[str] = mapped_column(String(20), default="NOVA", index=True)
    imagem_key: Mapped[str] = mapped_column(String(300), default="")
    clipe_key: Mapped[str] = mapped_column(String(300), default="")
    alerta_codigo: Mapped[str] = mapped_column(String(20), default="")
    avaliado_por: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True)
    avaliado_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)
    historico: Mapped[list] = mapped_column(JSONPortatil, default=list)
    criado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())

    camera: Mapped[Camera] = relationship(lazy="joined")


class OcupacaoHora(Base):
    """Ocupação de uma câmera numa hora: ONDE as pessoas ficaram e por quanto
    tempo, para o mapa de calor histórico.

    A detecção registra uma linha por passagem (o ponto em que a pessoa
    apareceu); isto aqui é o complemento contínuo: a cada volta da análise,
    cada pessoa em cena soma ao pé dela os segundos desde a volta anterior.
    Quem fica parado aquece o lugar onde está; quem atravessa deixa o
    caminho. Nada identifica ninguém: são segundos por célula e contagens.

    `grade_*` são esparsas: {índice da célula (linha * colunas + coluna):
    segundos}, numa grade de `ocupacao.COLUNAS` × `ocupacao.LINHAS`."""
    __tablename__ = "ocupacao_hora"
    __table_args__ = (UniqueConstraint("camera_id", "inicio", name="uq_ocupacao_camera_hora"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_uuid)
    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    inicio: Mapped[datetime] = mapped_column(DataHora(), index=True)          # hora cheia, UTC
    observado_s: Mapped[float] = mapped_column(Float, default=0.0)            # tempo com a câmera sob análise
    presenca_s: Mapped[float] = mapped_column(Float, default=0.0)             # pessoa × segundos
    veiculos_s: Mapped[float] = mapped_column(Float, default=0.0)
    pico: Mapped[int] = mapped_column(Integer, default=0)                     # pessoas ao mesmo tempo
    pico_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)
    maior_grupo: Mapped[int] = mapped_column(Integer, default=0)              # pessoas juntas
    maior_grupo_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)
    grupo_s: Mapped[float] = mapped_column(Float, default=0.0)                # tempo com aglomeração
    trilhas: Mapped[list] = mapped_column(JSONPortatil, default=list)         # track_ids vistos na hora
    grade_pessoas: Mapped[dict] = mapped_column(JSONPortatil, default=dict)
    grade_veiculos: Mapped[dict] = mapped_column(JSONPortatil, default=dict)
    grade_grupos: Mapped[dict] = mapped_column(JSONPortatil, default=dict)
    atualizado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now())


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

    # Imagem do próprio alerta, quando ele não nasce de uma detecção.
    #
    # Alerta de sala reporta um *estado* — "a sala ficou vazia" — e não há
    # detecção para anexar: ninguém é fotografado indo embora. Antes disso a
    # coluna CAPTURA ficava em branco e o operador tinha de ir procurar a
    # imagem em outro lugar, que é o oposto do que um alerta serve.
    #
    # Também cobre a entrada: o registro de detecção é deduplicado por 30
    # minutos, então a mesma pessoa voltando à sala gerava alerta sem captura.
    snapshot_key: Mapped[str] = mapped_column(String(200), default="")

    status: Mapped[StatusAlerta] = mapped_column(
        Enum(StatusAlerta, name="status_alerta_enum"), default=StatusAlerta.ABERTO, index=True
    )
    atribuido_a: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
    )
    # Quem assumiu, para a tela poder dizer o nome em vez do identificador.
    # `selectin` porque a lista de alertas é paginada: uma consulta a mais
    # para a página inteira, e não uma por linha.
    responsavel: Mapped[Optional["Usuario"]] = relationship(
        "Usuario", lazy="selectin", foreign_keys=[atribuido_a]
    )

    @property
    def atribuido_nome(self) -> str:
        """Nome de quem assumiu — vazio enquanto ninguém assumiu."""
        return self.responsavel.nome if self.responsavel else ""
    resolucao: Mapped[str] = mapped_column(Text, default="")
    disparado_em: Mapped[datetime] = mapped_column(DataHora(), server_default=func.now(), index=True)
    fechado_em: Mapped[Optional[datetime]] = mapped_column(DataHora(), nullable=True)

    camera: Mapped[Camera] = relationship(lazy="joined")
    pessoa: Mapped[Optional[Pessoa]] = relationship(lazy="joined")
    deteccao: Mapped[Optional["Deteccao"]] = relationship(lazy="joined")

    @property
    def deteccao_codigo(self) -> str:
        """Código da captura que originou o alerta, para a tela buscar o recorte.

        Sem ele a ficha do alerta descreve o que foi visto mas não mostra, e
        quem tria decide no texto. O vínculo é `ON DELETE SET NULL`: captura
        expurgada deixa o alerta sem imagem, não sem sentido.
        """
        return self.deteccao.codigo if self.deteccao else ""

    @property
    def tem_imagem(self) -> bool:
        """O alerta guarda imagem própria, independente de detecção."""
        return bool(self.snapshot_key)


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
