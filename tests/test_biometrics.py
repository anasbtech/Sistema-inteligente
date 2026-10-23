"""Testes da regra central: emissão e permanência do identificador.

    pytest -q

Usa SQLite em memória para os testes que não dependem de pgvector; os que
dependem são marcados e pulados sem PostgreSQL.
"""
from __future__ import annotations

import pytest

from app.services.biometrics import (
    PREFIXO, IdentificadorInvalido, sem_nome, sexo_do_id, validar_id,
)
from app.models import Sexo
from app.security import (
    conferir_otp, gerar_codigo_otp, hash_otp, hash_senha, mascarar_cpf, verificar_senha,
)
from app.schemas import validar_senha
from app.services.vision import cruzou_linha, ponto_em_poligono


# ----------------------------------------------------------------------
# Formato do identificador
# ----------------------------------------------------------------------
@pytest.mark.parametrize("id_bio,sexo,numero", [
    ("BIO-M-000001", Sexo.M, 1),
    ("BIO-F-000042", Sexo.F, 42),
    ("BIO-N-999999", Sexo.N, 999999),
])
def test_valida_id_correto(id_bio, sexo, numero):
    assert validar_id(id_bio) == (sexo, numero)


@pytest.mark.parametrize("ruim", [
    "BIO-X-000001",     # prefixo inexistente
    "BIO-M-1",          # sem zeros à esquerda
    "BIOM000001",       # sem separadores
    "bio-m-000001",     # minúsculo
    "BIO-M-0000001",    # sete dígitos
    "",
])
def test_rejeita_id_malformado(ruim):
    with pytest.raises(IdentificadorInvalido):
        validar_id(ruim)


def test_prefixo_por_sexo_e_unico():
    prefixos = list(PREFIXO.values())
    assert prefixos == ["BIO-M", "BIO-F", "BIO-N"]
    assert len(set(prefixos)) == 3, "Cada sexo precisa de um prefixo distinto"


def test_sexo_vem_do_prefixo_nao_da_imagem():
    """O sexo é legível a partir do ID porque veio do cadastro — nunca de
    inferência do modelo sobre a fotografia."""
    assert sexo_do_id("BIO-F-000100") is Sexo.F
    assert sexo_do_id("BIO-M-000100") is Sexo.M
    # Mesmo número, prefixos diferentes: são pessoas diferentes.
    assert validar_id("BIO-M-000100")[1] == validar_id("BIO-F-000100")[1]
    assert sexo_do_id("BIO-M-000100") is not sexo_do_id("BIO-F-000100")


def test_so_o_bio_n_e_registro_sem_nome():
    """Quem pergunta "esta pessoa tem nome?" pergunta ao identificador.

    O BIO-N guarda o literal "Não identificado" no campo `nome`, e quem
    comparasse com essa frase teria um teste que passa hoje e quebra calado
    no dia em que ela mudar. Foi esse literal que apareceu escrito sobre o
    vídeo, na mesma tarja de quem tem nome de verdade.
    """
    assert sem_nome("BIO-N-000042")
    assert not sem_nome("BIO-M-000042"), "cadastrado tem nome"
    assert not sem_nome("BIO-F-000042"), "cadastrado tem nome"
    assert not sem_nome(None), "sem casamento nenhum não é BIO-N"
    assert not sem_nome(""), "sem casamento nenhum não é BIO-N"


# ----------------------------------------------------------------------
# Senhas
# ----------------------------------------------------------------------
def test_hash_de_senha_nao_e_reversivel():
    h = hash_senha("Senha@Forte2026")
    assert h != "Senha@Forte2026"
    assert verificar_senha("Senha@Forte2026", h)
    assert not verificar_senha("senha@forte2026", h)


@pytest.mark.parametrize("fraca", [
    "curta1!A", "semmaiuscula1!", "SEMMINUSCULA1!", "SemNumero!!", "SemSimbolo123",
])
def test_politica_de_senha_rejeita_fraca(fraca):
    with pytest.raises(ValueError):
        validar_senha(fraca)


def test_politica_de_senha_aceita_forte():
    assert validar_senha("Seguranca@Central2026")


# ----------------------------------------------------------------------
# OTP
# ----------------------------------------------------------------------
def test_otp_tem_seis_digitos():
    for _ in range(200):
        c = gerar_codigo_otp()
        assert len(c) == 6 and c.isdigit()


def test_otp_guardado_como_hash():
    codigo = "045821"
    h = hash_otp(codigo)
    assert h != codigo and len(h) == 64
    assert conferir_otp(codigo, h)
    assert not conferir_otp("045822", h)


# ----------------------------------------------------------------------
# Mascaramento de PII
# ----------------------------------------------------------------------
def test_mascara_cpf():
    assert mascarar_cpf("123.456.789-01") == "***.456.***-**"
    assert mascarar_cpf(None) == ""
    assert mascarar_cpf("123") == "***"


# ----------------------------------------------------------------------
# Geometria de zonas
# ----------------------------------------------------------------------
def test_ponto_dentro_e_fora_do_poligono():
    quadrado = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]
    assert ponto_em_poligono((0.5, 0.5), quadrado)
    assert not ponto_em_poligono((0.1, 0.5), quadrado)
    assert not ponto_em_poligono((0.5, 0.95), quadrado)


def test_cruzamento_de_linha_detecta_sentido():
    linha = [[0.0, 0.5], [1.0, 0.5]]
    assert cruzou_linha((0.5, 0.4), (0.5, 0.6), linha) is not None
    assert cruzou_linha((0.5, 0.6), (0.5, 0.4), linha) is not None
    # Sentidos opostos devem receber rótulos opostos
    a = cruzou_linha((0.5, 0.4), (0.5, 0.6), linha)
    b = cruzou_linha((0.5, 0.6), (0.5, 0.4), linha)
    assert a != b
    # Sem cruzar
    assert cruzou_linha((0.5, 0.3), (0.5, 0.4), linha) is None


# ----------------------------------------------------------------------
# Auditoria
# ----------------------------------------------------------------------
def test_cadeia_de_hash_muda_com_o_conteudo():
    from app.audit import GENESIS, calcular_hash

    a = calcular_hash(GENESIS, {"acao": "LOGIN", "alvo": "x"})
    b = calcular_hash(GENESIS, {"acao": "LOGIN", "alvo": "y"})
    c = calcular_hash(a, {"acao": "LOGIN", "alvo": "x"})
    assert a != b, "Conteúdos diferentes devem gerar hashes diferentes"
    assert a != c, "O hash depende do elo anterior"
    assert len(a) == 64


def test_cadeia_e_deterministica():
    from app.audit import GENESIS, calcular_hash

    dados = {"b": 2, "a": 1}
    assert calcular_hash(GENESIS, dados) == calcular_hash(GENESIS, {"a": 1, "b": 2})
