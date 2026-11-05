"""Contratos de entrada e saída da API (Pydantic v2)."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.config import settings
from app.models import Perfil, Sexo, StatusAlerta, StatusCamera, StatusPessoa, TipoDeteccao, TipoZona
from app.security import CREDENCIAL_OCULTA, mascarar_credencial
from app.services.vision import ACOES_PADRAO, FICHA_ACAO, acoes_validas

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


class VerificarLinkIn(BaseModel):
    """O segredo que veio na URL do e-mail.

    Não pede e-mail junto: o segredo já identifica o token, e por ele o
    usuário. Exigir o endereço de novo só daria mais um campo para errar, sem
    acrescentar segurança — quem tem o link tem o link.
    """
    token: str = Field(..., min_length=20, max_length=200)


class RedefinirSenhaIn(BaseModel):
    token: str = Field(..., min_length=20, max_length=200)
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
    excluido_em: Optional[datetime] = None
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


class PrimeiroAcesso(BaseModel):
    """Como o convite de primeiro acesso terminou."""
    envio: Literal["enviado", "nao_configurado", "falhou"]
    erro: str = ""
    expira_em_segundos: int
    # Só vem preenchido quando o e-mail não saiu: é o caminho alternativo
    # para a conta não nascer inacessível. Com o envio funcionando, o
    # segredo não precisa passar pela tela de ninguém.
    link: str = ""


class UsuarioCriado(UsuarioOut):
    """A resposta do cadastro, que leva o desfecho do convite junto.

    Existe porque `response_model=UsuarioOut` filtra o que não está
    declarado — e foi exatamente isso que descartou a senha provisória antes
    dela chegar à tela do administrador. A conta de PA-000010 nasceu assim:
    senha gerada, hasheada e perdida, com troca obrigatória no primeiro
    acesso e ninguém sabendo qual era. Nunca foi acessada.
    """
    primeiro_acesso: Optional[PrimeiroAcesso] = None


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


def validar_hora(v: Optional[str]) -> Optional[str]:
    """Aceita "HH:MM" de 24 horas, e só isso.

    Vazio vira `None`, que é "sem janela própria, usa a do sistema" — assim
    limpar o campo na tela devolve a câmera ao padrão, em vez de gravar uma
    string vazia que depois seria lida como meia-noite.
    """
    if v is None:
        return None
    v = v.strip()
    if not v:
        return None
    partes = v.split(":")
    if len(partes) != 2 or not all(p.isdigit() for p in partes):
        raise ValueError("Use o formato HH:MM, por exemplo 19:00.")
    h, m = int(partes[0]), int(partes[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Hora fora do relógio de 24 horas.")
    return f"{h:02d}:{m:02d}"


def validar_acoes(v: Optional[List[str]]) -> Optional[List[str]]:
    """Recusa o que o sistema não sabe emitir.

    Guardar "Fogo" ligado numa câmera que não tem detector de fogo é prometer
    vigilância que não acontece — e é pior que recusar, porque quem configurou
    sai achando que está coberto. A recusa diz o motivo; o catálogo em
    `vision.CATALOGO_ACOES` é quem o conhece.

    Lista vazia passa: desligar tudo é uma escolha legítima.
    """
    if v is None:
        return None
    desconhecidas = [c for c in v if c not in FICHA_ACAO]
    if desconhecidas:
        raise ValueError("ação inexistente: " + ", ".join(sorted(desconhecidas)))
    impossiveis = [c for c in v if not FICHA_ACAO[c].disponivel]
    if impossiveis:
        detalhe = "; ".join(f"{FICHA_ACAO[c].rotulo} — {FICHA_ACAO[c].indisponivel}"
                            for c in sorted(impossiveis))
        raise ValueError("ação sem detector no sistema: " + detalhe)
    # Normaliza: sem repetição e na ordem do catálogo, que é a ordem em que a
    # tela as mostra. Sem isto a mesma seleção gravava em ordens diferentes e
    # a auditoria acusava alteração onde nada mudou.
    return acoes_validas(v)


class CameraBase(BaseModel):
    nome: str = Field(..., min_length=3, max_length=200)
    setor: str
    cena: str = "rua"
    status: StatusCamera = StatusCamera.ONLINE
    ip: str = ""
    # Em branco, o servidor deriva do IP com o padrão do parque. Preencher
    # continua valendo para uma câmera que fuja do padrão.
    rtsp_url: str = ""
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
    acoes_ativas: List[str] = Field(default_factory=lambda: list(ACOES_PADRAO))
    # Janela de horário permitido desta câmera; em branco, vale a do sistema.
    horario_inicio: Optional[str] = None
    horario_fim: Optional[str] = None
    alerta_presenca: bool = False
    detecta_veiculo: bool = False
    detecta_bagagem: bool = False
    detecta_animal: bool = False
    detecta_celular: bool = False
    detecta_capacete: bool = False
    detecta_arma: bool = False
    # Quantas pessoas juntas são aglomeração nesta câmera; em branco,
    # vale o padrão do parque. Piso 2 porque uma pessoa sozinha não
    # aglomera, e duas é a menor reunião possível.
    limite_aglomeracao: Optional[int] = Field(None, ge=2, le=200)
    limiar_facial: float = Field(0.88, ge=0.5, le=1.0)
    # Casado com settings.OBJECT_THRESHOLD, e não com um número próprio: a
    # câmera nascia em 0,65, valor em que — medido na Produção 02 — o modelo
    # marcava só a pessoa da frente e deixava as três do fundo sem caixa.
    limiar_objeto: float = Field(settings.OBJECT_THRESHOLD, ge=0.1, le=1.0)
    retencao_dias: int = 90
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    @field_validator("acoes_ativas")
    @classmethod
    def _acoes(cls, v: List[str]) -> List[str]:
        return validar_acoes(v) or []

    @field_validator("horario_inicio", "horario_fim")
    @classmethod
    def _hora(cls, v: Optional[str]) -> Optional[str]:
        return validar_hora(v)


class CameraCreate(CameraBase):
    codigo: Optional[str] = None
    zonas: List[ZonaIn] = []


class CameraUpdate(BaseModel):
    """Edição parcial. As faixas repetem as de `CameraBase` de propósito.

    Sem elas, o valor fora da faixa passava pela validação e só parava no
    CHECK do banco — que devolve erro 500 e uma mensagem de SQL. O operador
    via a tela recusar sem dizer o quê, e o log ganhava um traceback de
    integridade onde cabia um campo marcado em vermelho.
    """
    model_config = ConfigDict(extra="forbid")
    nome: Optional[str] = Field(None, min_length=3, max_length=200)
    setor: Optional[str] = None
    cena: Optional[str] = None
    status: Optional[StatusCamera] = None
    ip: Optional[str] = None
    rtsp_url: Optional[str] = None
    onvif_url: Optional[str] = None
    fabricante: Optional[str] = None
    modelo: Optional[str] = None
    resolucao: Optional[str] = None
    # ck_fps no banco é `> 0 AND <= 120`.
    fps: Optional[int] = Field(None, ge=1, le=120)
    codec: Optional[str] = None
    bitrate_kbps: Optional[int] = Field(None, ge=128, le=65536)
    ptz: Optional[bool] = None
    infravermelho: Optional[bool] = None
    analitico_facial: Optional[bool] = None
    analitico_objeto: Optional[bool] = None
    analitico_acao: Optional[bool] = None
    acoes_ativas: Optional[List[str]] = None
    horario_inicio: Optional[str] = None
    horario_fim: Optional[str] = None
    alerta_presenca: Optional[bool] = None
    detecta_veiculo: Optional[bool] = None
    detecta_bagagem: Optional[bool] = None
    detecta_animal: Optional[bool] = None
    detecta_celular: Optional[bool] = None
    detecta_capacete: Optional[bool] = None
    detecta_arma: Optional[bool] = None
    limite_aglomeracao: Optional[int] = Field(None, ge=2, le=200)
    limiar_facial: Optional[float] = Field(None, ge=0.5, le=1.0)
    limiar_objeto: Optional[float] = Field(None, ge=0.1, le=1.0)
    retencao_dias: Optional[int] = Field(None, ge=1, le=3650)
    latitude: Optional[float] = Field(None, ge=-90, le=90)
    longitude: Optional[float] = Field(None, ge=-180, le=180)
    zonas: Optional[List[ZonaIn]] = None

    @field_validator("acoes_ativas")
    @classmethod
    def _acoes(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return validar_acoes(v)

    @field_validator("horario_inicio", "horario_fim")
    @classmethod
    def _hora(cls, v: Optional[str]) -> Optional[str]:
        return validar_hora(v)


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
    acoes_ativas: List[str] = []
    horario_inicio: Optional[str] = None
    horario_fim: Optional[str] = None
    alerta_presenca: bool = False
    limite_aglomeracao: Optional[int] = None
    detecta_veiculo: bool
    detecta_bagagem: bool
    detecta_animal: bool
    detecta_celular: bool = False
    detecta_capacete: bool
    detecta_arma: bool
    limiar_facial: float
    limiar_objeto: float
    retencao_dias: int
    latitude: Optional[float]
    longitude: Optional[float]
    latencia_ms: int
    ultimo_heartbeat: Optional[datetime]
    zonas: List[ZonaOut] = []

    @field_validator("rtsp_url", mode="after")
    @classmethod
    def _sem_credencial(cls, v: str) -> str:
        return mascarar_credencial(v)


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
    status: StatusPessoa = StatusPessoa.VISITANTE
    alcunha: str = ""
    nascimento: Optional[date] = None
    cpf: Optional[str] = None
    rg: Optional[str] = None
    restricao: str = ""
    tipificacao: str = ""
    orgao_expedidor: str = ""
    unidade: str = ""
    base_legal: str = "Controle de acesso predial"
    observacoes: str = ""

    @field_validator("restricao")
    @classmethod
    def _restricao_obrigatoria(cls, v: str, info) -> str:
        if info.data.get("status") == StatusPessoa.RESTRITO and not v.strip():
            raise ValueError("Situação 'RESTRITO' exige o motivo da restrição")
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
    restricao: Optional[str] = None
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
    restricao: str
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
    # Recorte da captura em base64. A API é quem decide onde gravar: aceitar
    # um caminho vindo pela rede seria aceitar travessia de diretório.
    snapshot_jpeg: Optional[str] = None
    # Recorte da placa lida, em JPEG base64. Só vem quando houve leitura.
    placa_jpeg: Optional[str] = None
    hash_evidencia: str = ""


class CaixaAoVivo(BaseModel):
    classe: str = ""
    confianca: Optional[float] = None
    bbox: List[float]
    track_id: Optional[int] = None


class QuadroAoVivoIn(BaseModel):
    """Caixas do quadro que o worker acabou de analisar.

    Nada aqui é persistido: serve para a sobreposição do mosaico acompanhar o
    objeto enquanto ele está em cena. O registro durável é o `DeteccaoIn`, e
    nasce uma vez só por passagem.
    """
    camera_codigo: str
    caixas: List[CaixaAoVivo] = []


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
    # Há recorte da placa guardado? A tela usa para decidir se mostra.
    placa_snapshot_key: str = ""
    revisado: bool
    veredito: str
    hash_evidencia: str
    camera: Optional[CameraOut] = None
    pessoa: Optional[PessoaOut] = None
    # Os alertas que este registro gerou, por código. Vai na ficha porque é o
    # que impede de apagá-lo: sem isto, a tela só descobre o impedimento
    # depois de o operador escrever o motivo e confirmar.
    #
    # São os códigos, e não só a contagem, porque a tela precisa *agir* sobre
    # eles — encerrar não desfaz o vínculo, e o caminho que libera a exclusão é
    # apagar o alerta.
    alertas: List[str] = []


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
    # O nome de quem assumiu. Sem ele a tela só tinha o identificador, que
    # não diz nada a quem está operando — e saber quem já está cuidando é
    # justamente o que evita duas guarnições no mesmo chamado.
    atribuido_nome: str = ""
    resolucao: str
    disparado_em: datetime
    fechado_em: Optional[datetime]
    deteccao_codigo: str = ""
    # O alerta tem imagem própria? A tela usa isto para decidir de onde
    # buscar a miniatura: da detecção que o gerou, ou do próprio alerta.
    tem_imagem: bool = False
    camera: Optional[CameraOut] = None
    pessoa: Optional[PessoaOut] = None

class AlertaFechaIn(BaseModel):
    resolucao: str = Field(..., min_length=8, description="Providência adotada")


class AlertaLoteIn(BaseModel):
    """Vários alertas encerrados de uma vez, com o mesmo desfecho.

    Existe porque a triagem de um em um não dá conta de uma fila de centenas —
    e quase sempre o desfecho é o mesmo (falso positivo na mesma câmera). O
    teto de 200 é o de uma página cheia: encerrar mais que isso de uma vez
    pede revisão, não um clique.
    """
    codigos: List[str] = Field(..., min_length=1, max_length=200)
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
    """O veredito sobre a trilha, separado por causa.

    Um campo só não servia. A verificação dizia "a trilha pode ter sido
    alterada fora da aplicação" para três coisas muito diferentes, e quem lê
    não tinha como saber qual: linha adulterada, elo bifurcado por duas
    gravações simultâneas, e vínculo de usuário zerado pelo banco quando a
    conta foi apagada. Nesta instalação as três conviviam — 10, 83 e 10
    linhas — e o resultado prático é que o alarme deixa de ser lido.

    `integra` continua estrito: verdadeiro só quando nada diverge. O que
    muda é que agora dá para responder *o quê* divergiu.
    """

    integra: bool
    linhas_verificadas: int
    primeira_divergencia: Optional[int] = None
    mensagem: str
    # Linha cujo conteúdo não corresponde ao próprio hash. É o sinal de
    # adulteração, e o único que acusa texto reescrito.
    conteudo_divergente: int = 0
    # Linha que aponta para um elo que não é o da anterior. Duas gravações
    # simultâneas produzem isso sem ninguém tocar em nada.
    elos_bifurcados: int = 0
    # Subconjunto de `conteudo_divergente` com assinatura conhecida: o
    # `usuario_id` foi zerado pelo `ON DELETE SET NULL` depois que o hash já
    # o cobria. O e-mail e o perfil continuam gravados, então a autoria não
    # se perdeu — só o vínculo.
    vinculo_de_usuario_perdido: int = 0


TokenOut.model_rebuild()


class VideoTesteIn(BaseModel):
    """Um disparo do visualizador de teste, para virar alerta no sistema."""
    video: str = Field(..., max_length=200)
    acao: str = Field(..., max_length=40)
    segundo: float = Field(..., ge=0)
    tempo_video: str = Field("", max_length=16)
    quadro: int = Field(0, ge=0)
    confianca: float = Field(..., ge=0, le=1)
    pessoa_id: Optional[int] = None
    velocidade_corpos_s: Optional[float] = None
    gabarito: str = Field("", max_length=40)
    bbox_normalizado: Optional[List[float]] = None
    print_jpeg: str = Field(..., description="Print do disparo, JPEG em base64")
