"""Contratos de entrada e saída da API (Pydantic v2)."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import Perfil, Sexo, StatusAlerta, StatusCamera, StatusPessoa, TipoDeteccao, TipoZona

SENHA_REGRAS = [
    (r".{10,}", "mínimo de 10 caracteres"),
    (r"[A-Z]", "ao menos 1 letra maiúscula"),
    (r"[a-z]", "ao menos 1 letra minúscula"),
    (r"[0-9]", "ao menos 1 número"),
    (r"[^A-Za-z0-9]", "ao menos 1 caractere especial"),
]


def validar_senha(v: str) -> str:
    faltando = [msg for rx, msg in SENHA_REGRAS if not re.search(rx, v)]
    if faltando:
        raise ValueError("A senha precisa de: " + ", ".join(faltando))
    return v


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ======================================================================
# Autenticação
# ======================================================================
class LoginIn(BaseModel):
    identificador: str = Field(..., description="E-mail funcional ou matrícula")
    senha: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    usuario: "UsuarioOut"


class RefreshIn(BaseModel):
    refresh_token: str


class RecuperarIn(BaseModel):
    email: EmailStr


class VerificarCodigoIn(BaseModel):
    email: EmailStr
    codigo: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class RedefinirSenhaIn(BaseModel):
    email: EmailStr
    codigo: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
    nova_senha: str

    @field_validator("nova_senha")
    @classmethod
    def _forte(cls, v: str) -> str:
        return validar_senha(v)


class TrocarSenhaIn(BaseModel):
    senha_atual: str
    nova_senha: str

    @field_validator("nova_senha")
    @classmethod
    def _forte(cls, v: str) -> str:
        return validar_senha(v)


# ======================================================================
# Usuários
# ======================================================================
class UsuarioBase(BaseModel):
    nome: str = Field(..., min_length=4, max_length=160)
    email: EmailStr
    matricula: str = Field(..., min_length=3, max_length=32)
    perfil: Perfil
    unidade: str = ""
    telefone: str = ""
    ativo: bool = True
    mfa_ativo: bool = False


class UsuarioCreate(UsuarioBase):
    senha: Optional[str] = None  # ausente => senha provisória gerada

    @field_validator("senha")
    @classmethod
    def _forte(cls, v: Optional[str]) -> Optional[str]:
        return validar_senha(v) if v else v


class UsuarioUpdate(BaseModel):
    nome: Optional[str] = None
    email: Optional[EmailStr] = None
    matricula: Optional[str] = None
    perfil: Optional[Perfil] = None
    unidade: Optional[str] = None
    telefone: Optional[str] = None
    ativo: Optional[bool] = None
    mfa_ativo: Optional[bool] = None
    forcar_troca_senha: Optional[bool] = None


class UsuarioOut(ORMModel):
    id: uuid.UUID
    nome: str
    email: str
    matricula: str
    perfil: Perfil
    unidade: str
    ativo: bool
    mfa_ativo: bool
    ultimo_acesso: Optional[datetime]
    criado_em: datetime
    permissoes: List[str] = []

    @field_validator("permissoes", mode="before")
    @classmethod
    def _perms(cls, v: Any) -> List[str]:
        return sorted(v) if v else []


# ======================================================================
# Câmeras e zonas
# ======================================================================
class ZonaIn(BaseModel):
    nome: str
    tipo: TipoZona = TipoZona.POLIGONO
    cor: str = "#35A7DE"
    pontos: List[List[float]] = []
    regras: dict = {}

    @field_validator("pontos")
    @classmethod
    def _valida_pontos(cls, v: List[List[float]]) -> List[List[float]]:
        for p in v:
            if len(p) != 2 or not all(0.0 <= c <= 1.0 for c in p):
                raise ValueError("pontos devem ser pares [x, y] normalizados entre 0 e 1")
        return v


class ZonaOut(ZonaIn, ORMModel):
    id: uuid.UUID


class CameraBase(BaseModel):
    nome: str = Field(..., min_length=3, max_length=200)
    setor: str
    cena: str = "rua"
    status: StatusCamera = StatusCamera.ONLINE
    ip: str = ""
    rtsp_url: str
    onvif_url: str = ""
    fabricante: str = ""
    modelo: str = ""
    resolucao: str = "1920x1080"
    fps: int = Field(25, ge=1, le=120)
    codec: str = "H.265"
    bitrate_kbps: int = 4096
    ptz: bool = False
    infravermelho: bool = True
    analitico_facial: bool = True
    analitico_objeto: bool = True
    analitico_acao: bool = False
    limiar_facial: float = Field(0.88, ge=0.5, le=1.0)
    limiar_objeto: float = Field(0.65, ge=0.1, le=1.0)
    retencao_dias: int = 90
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class CameraCreate(CameraBase):
    codigo: Optional[str] = None
    zonas: List[ZonaIn] = []


class CameraUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nome: Optional[str] = None
    setor: Optional[str] = None
    cena: Optional[str] = None
    status: Optional[StatusCamera] = None
    ip: Optional[str] = None
    rtsp_url: Optional[str] = None
    onvif_url: Optional[str] = None
    fabricante: Optional[str] = None
    modelo: Optional[str] = None
    resolucao: Optional[str] = None
    fps: Optional[int] = None
    codec: Optional[str] = None
    bitrate_kbps: Optional[int] = None
    ptz: Optional[bool] = None
    infravermelho: Optional[bool] = None
    analitico_facial: Optional[bool] = None
    analitico_objeto: Optional[bool] = None
    analitico_acao: Optional[bool] = None
    limiar_facial: Optional[float] = Field(None, ge=0.5, le=1.0)
    limiar_objeto: Optional[float] = Field(None, ge=0.1, le=1.0)
    retencao_dias: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    zonas: Optional[List[ZonaIn]] = None


class CameraOut(ORMModel):
    id: uuid.UUID
    codigo: str
    nome: str
    setor: str
    cena: str
    status: StatusCamera
    ip: str
    rtsp_url: str
    fabricante: str
    modelo: str
    resolucao: str
    fps: int
    codec: str
    bitrate_kbps: int
    ptz: bool
    infravermelho: bool
    analitico_facial: bool
    analitico_objeto: bool
    analitico_acao: bool
    limiar_facial: float
    limiar_objeto: float
    retencao_dias: int
    latitude: Optional[float]
    longitude: Optional[float]
    latencia_ms: int
    ultimo_heartbeat: Optional[datetime]
    zonas: List[ZonaOut] = []


class PTZComando(BaseModel):
    comando: Literal["up", "down", "left", "right", "zoom_in", "zoom_out", "home", "preset"]
    velocidade: float = Field(0.5, ge=0.1, le=1.0)
    preset: Optional[int] = None


# ======================================================================
# Base biométrica
# ======================================================================
class PessoaCreate(BaseModel):
    """Cadastro. O `sexo` aqui é o do registro civil e define o PREFIXO do ID
    a ser emitido — o motor de reconhecimento jamais o infere da imagem."""
    nome: str = Field(..., min_length=5, max_length=200)
    sexo: Sexo
    status: StatusPessoa = StatusPessoa.CIDADAO
    alcunha: str = ""
    nascimento: Optional[date] = None
    cpf: Optional[str] = None
    rg: Optional[str] = None
    mandado: str = ""
    tipificacao: str = ""
    orgao_expedidor: str = ""
    unidade: str = ""
    base_legal: str = "Segurança pública"
    observacoes: str = ""

    @field_validator("mandado")
    @classmethod
    def _mandado_obrigatorio(cls, v: str, info) -> str:
        if info.data.get("status") == StatusPessoa.PROCURADO and not v.strip():
            raise ValueError("Situação 'PROCURADO' exige o número do mandado judicial")
        return v


class PessoaUpdate(BaseModel):
    """`sexo` pode ser corrigido; `id_biometrico` permanece o mesmo."""
    nome: Optional[str] = None
    sexo: Optional[Sexo] = None
    status: Optional[StatusPessoa] = None
    alcunha: Optional[str] = None
    nascimento: Optional[date] = None
    cpf: Optional[str] = None
    rg: Optional[str] = None
    mandado: Optional[str] = None
    tipificacao: Optional[str] = None
    orgao_expedidor: Optional[str] = None
    unidade: Optional[str] = None
    base_legal: Optional[str] = None
    observacoes: Optional[str] = None
    ativo: Optional[bool] = None


class PessoaOut(ORMModel):
    id: uuid.UUID
    id_biometrico: str
    sexo: Sexo
    sexo_emissao: Sexo
    nome: str
    alcunha: str
    nascimento: Optional[date]
    cpf: Optional[str]
    rg: Optional[str]
    status: StatusPessoa
    mandado: str
    tipificacao: str
    orgao_expedidor: str
    unidade: str
    qualidade: float
    amostras: int
    base_legal: str
    total_deteccoes: int
    ultima_deteccao: Optional[datetime]
    ultima_camera: str
    ativo: bool
    criado_em: datetime


class MatchOut(BaseModel):
    """Resultado de uma busca 1:N. `pessoa` é None quando nada supera o limiar."""
    pessoa: Optional[PessoaOut]
    similaridade: float
    limiar: float
    match: bool
    qualidade_face: float


# ======================================================================
# Detecções e alertas
# ======================================================================
class DeteccaoIn(BaseModel):
    """Payload que o worker de inferência envia à API."""
    tipo: TipoDeteccao
    camera_codigo: str
    ocorrido_em: datetime
    classe: str = ""
    confianca: Optional[float] = None
    similaridade: Optional[float] = None
    qualidade_face: Optional[float] = None
    embedding: Optional[List[float]] = None
    duracao_s: Optional[int] = None
    envolvidos: Optional[int] = None
    placa: str = ""
    zona_nome: str = ""
    bbox: Optional[List[float]] = None
    track_id: Optional[int] = None
    snapshot_key: str = ""
    hash_evidencia: str = ""


class DeteccaoOut(ORMModel):
    id: uuid.UUID
    codigo: str
    tipo: TipoDeteccao
    ocorrido_em: datetime
    camera_id: uuid.UUID
    pessoa_id: Optional[uuid.UUID]
    similaridade: Optional[float]
    limiar_aplicado: Optional[float]
    qualidade_face: Optional[float]
    classe: str
    confianca: Optional[float]
    duracao_s: Optional[int]
    envolvidos: Optional[int]
    placa: str
    bbox: Optional[List[float]]
    track_id: Optional[int]
    severidade: int
    snapshot_key: str
    revisado: bool
    veredito: str
    hash_evidencia: str
    camera: Optional[CameraOut] = None
    pessoa: Optional[PessoaOut] = None


class RevisaoIn(BaseModel):
    veredito: Literal["Confirmado", "Falso positivo"]
    observacao: str = ""


class AlertaOut(ORMModel):
    id: uuid.UUID
    codigo: str
    tipo: TipoDeteccao
    titulo: str
    detalhe: str
    severidade: int
    status: StatusAlerta
    atribuido_a: Optional[uuid.UUID]
    resolucao: str
    disparado_em: datetime
    fechado_em: Optional[datetime]
    camera: Optional[CameraOut] = None
    pessoa: Optional[PessoaOut] = None


class AlertaFechaIn(BaseModel):
    resolucao: str = Field(..., min_length=8, description="Providência adotada")


# ======================================================================
# Auditoria e paginação
# ======================================================================
class AuditoriaOut(ORMModel):
    id: int
    ocorrido_em: datetime
    acao: str
    descricao: str
    severidade: str
    usuario_email: str
    usuario_nome: str
    perfil: str
    alvo: str
    finalidade: str
    ip: str
    hash_anterior: str
    hash_atual: str


class Pagina(BaseModel):
    total: int
    pagina: int
    por_pagina: int
    itens: List[Any]


class VerificacaoCadeia(BaseModel):
    integra: bool
    linhas_verificadas: int
    primeira_divergencia: Optional[int] = None
    mensagem: str


TokenOut.model_rebuild()
