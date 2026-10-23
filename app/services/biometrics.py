"""Emissão e gestão do identificador biométrico permanente.

REGRA CENTRAL DO SISTEMA
------------------------
O identificador tem a forma ``BIO-<M|F|N>-NNNNNN`` e é emitido **uma única
vez**, no cadastro da pessoa, a partir do sexo informado no registro civil.

Três consequências, todas implementadas aqui:

1. **O motor nunca emite ID.** O reconhecimento facial devolve o vizinho mais
   próximo já existente na base (ou nada). Ele não classifica sexo a partir da
   imagem, então não há como a mesma pessoa aparecer ora como BIO-M ora como
   BIO-F entre capturas.
2. **Sequências independentes por prefixo.** BIO-M-000001 e BIO-F-000001
   coexistem; cada contador anda sozinho, sob trava de linha.
3. **Corrigir o sexo preserva o ID.** `Pessoa.sexo` muda; `Pessoa.sexo_emissao`
   e `Pessoa.id_biometrico` permanecem, para não órfãos no histórico de
   capturas. A correção fica registrada na trilha de auditoria.

A ÚNICA EXCEÇÃO: a saída de BIO-N
---------------------------------
Quem o reconhecimento encontra sem cadastro nenhum entra como BIO-N, aberto
por `abrir_registro_nao_identificado`. Esse prefixo não afirma nada sobre a
pessoa — diz que ainda não se sabe quem ela é. Quando alguém a cadastra e
informa o sexo, o identificador é reemitido como BIO-M ou BIO-F.

Não contradiz a regra acima, porque os dois casos afirmam coisas diferentes.
Num cadastro humano o prefixo registra o que constava no ato da emissão, e
corrigir o sexo depois não desfaz o que foi declarado então. Em BIO-N não há
declaração alguma a preservar, e manter o prefixo depois de saber seria deixar
no identificador uma afirmação que o próprio cadastro já contradiz.

A promoção é de mão única, acontece uma vez por registro, e carrega o
identificador antigo para a trilha de auditoria. Fora dela o ID segue
intocável, inclusive por SQL direto — veja o gatilho em sql/01_postgres.sql.
"""
from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Pessoa, SequenciaBiometrica, Sexo, StatusPessoa
from app.tipos import travar_linha

FORMATO_ID = re.compile(r"^BIO-([MFN])-(\d{6})$")

PREFIXO = {Sexo.M: "BIO-M", Sexo.F: "BIO-F", Sexo.N: "BIO-N"}

ROTULO_SEXO = {Sexo.M: "Masculino", Sexo.F: "Feminino", Sexo.N: "Não informado"}


def sem_nome(id_biometrico: Optional[str]) -> bool:
    """O registro existe e ainda não tem nome — é um BIO-N.

    Pergunta-se ao identificador, não ao campo `nome`: o BIO-N guarda o
    literal "Não identificado" ali, e comparar com essa frase daria um teste
    que passa hoje e quebra calado no dia em que ela mudar. O identificador é
    o que o sistema garante.
    """
    return bool(id_biometrico) and id_biometrico.startswith(f"{PREFIXO[Sexo.N]}-")


class IdentificadorInvalido(ValueError):
    pass


async def emitir_id_biometrico(db: AsyncSession, sexo: Sexo) -> str:
    """Reserva o próximo número da sequência do sexo informado.

    Usa ``SELECT ... FOR UPDATE`` na linha do contador: dois cadastros
    simultâneos do mesmo sexo serializam em vez de colidir. A unicidade final
    ainda é garantida pelo índice único em ``pessoas.id_biometrico``.
    """
    linha = (
        await db.execute(
            travar_linha(
                select(SequenciaBiometrica).where(SequenciaBiometrica.sexo == sexo),
                db,
            )
        )
    ).scalar_one_or_none()

    if linha is None:
        linha = SequenciaBiometrica(sexo=sexo, proximo=1)
        db.add(linha)
        await db.flush()

    numero = linha.proximo
    linha.proximo = numero + 1
    await db.flush()

    return f"{PREFIXO[sexo]}-{numero:06d}"


async def proximo_id_previsto(db: AsyncSession, sexo: Sexo) -> str:
    """Somente leitura — o que a interface mostra antes de confirmar o cadastro."""
    linha = (
        await db.execute(
            select(SequenciaBiometrica).where(SequenciaBiometrica.sexo == sexo)
        )
    ).scalar_one_or_none()
    proximo = linha.proximo if linha else 1
    return f"{PREFIXO[sexo]}-{proximo:06d}"


async def contagem_por_sexo(db: AsyncSession) -> dict[str, dict]:
    """Resumo usado no painel: quantos registros e qual o próximo ID de cada prefixo."""
    from sqlalchemy import func

    contagens = dict(
        (await db.execute(select(Pessoa.sexo, func.count()).group_by(Pessoa.sexo))).all()
    )
    saida: dict[str, dict] = {}
    for sexo in Sexo:
        saida[sexo.value] = {
            "prefixo": PREFIXO[sexo],
            "rotulo": ROTULO_SEXO[sexo],
            "registros": int(contagens.get(sexo, 0)),
            "proximo": await proximo_id_previsto(db, sexo),
        }
    return saida


def validar_id(id_biometrico: str) -> tuple[Sexo, int]:
    m = FORMATO_ID.match(id_biometrico or "")
    if not m:
        raise IdentificadorInvalido(
            f"'{id_biometrico}' não segue o formato BIO-<M|F|N>-NNNNNN"
        )
    return Sexo(m.group(1)), int(m.group(2))


def sexo_do_id(id_biometrico: str) -> Sexo:
    return validar_id(id_biometrico)[0]


async def abrir_registro_nao_identificado(
        db: AsyncSession, usuario_id=None, observacao: str = "") -> Pessoa:
    """Abre um BIO-N: existe uma pessoa, ainda não se sabe quem.

    Sai daqui sem template. Quem chama anexa as amostras, e é de propósito:
    medido nesta base, duas capturas da mesma pessoa só se reconhecem em 12%
    dos pares (mediana 0,366 contra um limiar de 0,50). Um registro aberto com
    uma amostra não reconheceria a própria pessoa no encontro seguinte e
    abriria outro, e outro — a base encheria de registros da mesma pessoa.

    O que funciona é o que já funciona para os cadastrados: várias amostras,
    com a busca comparando contra cada uma e ficando com a melhor. Por isso o
    BIO-N nasce de um punhado de capturas que alguém reconheceu como a mesma
    pessoa, e não de uma.

    `cadastrado_por` guarda quem abriu. Diferente de um cadastro comum, aqui
    não há nome a conferir: o que a trilha pode registrar é quem olhou os
    recortes e afirmou que eram a mesma pessoa.
    """
    pessoa = Pessoa(
        id_biometrico=await emitir_id_biometrico(db, Sexo.N),
        sexo=Sexo.N,
        sexo_emissao=Sexo.N,
        nome="Não identificado",
        status=StatusPessoa.VISITANTE,
        base_legal="Reconhecimento sem cadastro — pendente de identificação",
        observacoes=observacao or (
            "Registro aberto a partir de capturas sem correspondência na base. "
            "Ao cadastrar a pessoa e informar o sexo, o identificador passa a "
            "BIO-M ou BIO-F."
        ),
        cadastrado_por=usuario_id,
    )
    db.add(pessoa)
    await db.flush()
    return pessoa


async def corrigir_sexo(db: AsyncSession, pessoa: Pessoa, novo_sexo: Sexo) -> dict:
    """Corrige o sexo do cadastro, reemitindo o identificador **só na promoção**.

    O identificador é permanente, com uma exceção: quem entrou na base sem
    identificação (BIO-N, aberto pelo reconhecimento sem cadastro humano) sai
    dela ao ser cadastrado, e aí o prefixo passa a BIO-M ou BIO-F.

    A diferença entre os dois casos é o que o prefixo afirma. Num cadastro
    humano, BIO-F registra o que constava no ato da emissão: corrigir o sexo
    depois não desfaz o que foi declarado então, e reemitir só trocaria um
    registro verdadeiro por outro. Em BIO-N não há nada declarado — o prefixo
    diz "ainda não se sabe", e deixá-lo depois de saber seria manter no ID uma
    afirmação que o cadastro já contradiz.

    Por isso a promoção é de mão única e acontece uma vez só. Fora dela o
    identificador continua intocável, inclusive por SQL direto: veja o gatilho
    em sql/01_postgres.sql.

    Devolve um resumo para a trilha de auditoria. Na promoção ele carrega o
    identificador antigo, que é o que liga esta linha às anteriores — sem
    isso, a trilha de um BIO-N promovido ficaria órfã.
    """
    anterior = pessoa.sexo
    if anterior == novo_sexo:
        return {"alterado": False, "id_biometrico": pessoa.id_biometrico}

    pessoa.sexo = novo_sexo

    if pessoa.sexo_emissao == Sexo.N and novo_sexo != Sexo.N:
        id_anterior = pessoa.id_biometrico
        pessoa.id_biometrico = await emitir_id_biometrico(db, novo_sexo)
        pessoa.sexo_emissao = novo_sexo
        return {
            "alterado": True,
            "promovido": True,
            "id_biometrico": pessoa.id_biometrico,
            "id_anterior": id_anterior,
            "sexo_anterior": anterior.value,
            "sexo_atual": novo_sexo.value,
            "prefixo_emissao": PREFIXO[novo_sexo],
            "observacao": (
                f"Identificador reemitido na saída de BIO-N: {id_anterior} → "
                f"{pessoa.id_biometrico}. As capturas seguem vinculadas, porque o "
                "vínculo é pela chave interna e não pelo identificador legível."
            ),
        }

    return {
        "alterado": True,
        "promovido": False,
        "id_biometrico": pessoa.id_biometrico,
        "sexo_anterior": anterior.value,
        "sexo_atual": novo_sexo.value,
        "prefixo_emissao": PREFIXO[pessoa.sexo_emissao],
        "observacao": (
            "Identificador preservado por decisão de projeto: reemitir quebraria o "
            "vínculo com as capturas já registradas."
        ),
    }


async def sincronizar_sequencias(db: AsyncSession) -> dict[str, int]:
    """Realinha os contadores ao maior número já usado por prefixo.

    Útil depois de uma carga em massa ou restauração de backup.
    """
    from sqlalchemy import func

    resultado: dict[str, int] = {}
    for sexo in Sexo:
        maior = (
            await db.execute(
                select(
                    func.max(func.cast(func.substr(Pessoa.id_biometrico, 7), __import__("sqlalchemy").Integer))
                ).where(Pessoa.id_biometrico.like(f"{PREFIXO[sexo]}-%"))
            )
        ).scalar()
        proximo = (maior or 0) + 1

        linha = (
            await db.execute(
                travar_linha(
                    select(SequenciaBiometrica).where(SequenciaBiometrica.sexo == sexo),
                    db,
                )
            )
        ).scalar_one_or_none()
        if linha is None:
            db.add(SequenciaBiometrica(sexo=sexo, proximo=proximo))
        else:
            linha.proximo = max(linha.proximo, proximo)
        resultado[PREFIXO[sexo]] = proximo

    await db.flush()
    return resultado
