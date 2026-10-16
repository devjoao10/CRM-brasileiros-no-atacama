# -*- coding: utf-8 -*-
"""
CONV-LEAD-FONE-01 - regra unica de identidade de telefone (CRM + Conversas).

O DEFEITO
---------
O Conversas decidia se um numero ja era lead do CRM por igualdade EXATA dos
digitos. So que o MESMO celular brasileiro chega com grafias diferentes por
porta de entrada: o modal do CRM grava `55`+DDD+9 digitos (`5548988711776`), a
Meta entrega o wa_id de boa parte dos celulares SEM o nono digito
(`554888711776`) e o import CSV/API grava o texto cru, muitas vezes sem DDI
(`(11) 98765-4321`). O lead cadastrado a mao nao era reconhecido e o Conversas
criava um DUPLICADO com o wa_id.

O QUE ESTE ARQUIVO PROVA
------------------------
A regra vive em DUAS copias - `app/services/telefone_identidade.py` (CRM) e
`conversas/app/services/telefone_identidade.py` (Conversas): os dois pacotes se
chamam `app` e nao coexistem num processo. Este teste carrega as duas POR
CAMINHO (nunca `import app...`) e prova:

1. PARIDADE: as copias sao identicas byte a byte (fim de linha CRLF ou LF nao
   conta: o repo usa core.autocrlf) e respondem igual a todo o corpus. Se este
   item falhar, uma copia foi editada sem a outra.
2. CORPUS (numero_meta, numero_crm, esperado), nos DOIS sentidos: a mesma linha
   em outra grafia casa; DDD diferente, numero sem DDD, 10 ultimos digitos,
   fixo x celular, o sufixo que cruza DDD (49<->54, 19<->51) e o sufixo que
   junta numero estrangeiro ou registro corrompido a um brasileiro NAO casam.
   As linhas LIMITE CONHECIDO prendem o que so os digitos nao distinguem.
3. PRE-FILTRO: tudo que `mesmo_telefone` aceita esta dentro do que o SQL
   buscaria (`grafias_compativeis` OU `padrao_sufixo`). Python decide a
   identidade e o SQL so pre-filtra: linha que o pre-filtro nao traz, o Python
   nunca chega a ver.
4. VALORES EXATOS de `so_digitos`, `grafias_compativeis`, `padrao_sufixo` e das
   constantes (inclui o digito nao-ASCII, que `so_digitos` mantem).
5. MATRIZ "DDD diferente NUNCA casa entre numeros com leitura brasileira"
   (AUDIT-2026-08-W2F/F10): para todo par de DDDs distintos e alguns
   assinantes (celular 9+8, celular antigo de 8 digitos, fixo), nenhuma grafia
   (com/sem DDI, com/sem o 9) de uma linha casa com nenhuma grafia de OUTRA
   linha de mesma cauda de 8 digitos - nem de outro DDD, nem o fixo contra o
   celular de mesmo final. O corpus sozinho nao bastava: o sufixo de 11
   digitos cruzava `554987654321` (DDD 49) com `54987654321` (DDD 54) e
   `551987654321` (DDD 19) com `51987654321` (DDD 51), e tambem o fixo de um
   com o celular do outro (`554933456789` x `54933456789`) - por isso o sufixo
   so vale entre numeros sem leitura brasileira. A matriz tem controle
   positivo (as grafias da MESMA linha casam entre si), senao "nunca casa"
   poderia valer so porque as grafias geradas sao lixo.

Pura: sem banco, sem rede.

Rodar:  python tests/test_telefone_identidade.py
"""
import importlib.util
import itertools
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COPIA_CRM = ROOT / "app" / "services" / "telefone_identidade.py"
COPIA_CONVERSAS = ROOT / "conversas" / "app" / "services" / "telefone_identidade.py"

# Lista Anatel do plano CONV-LEAD-FONE-01 (67 DDDs), transcrita aqui de forma
# independente do modulo: um DDD que sumisse da constante trocaria em silencio a
# leitura canonica de uma regiao inteira.
DDDS_ANATEL = frozenset("""
    11 12 13 14 15 16 17 18 19 21 22 24 27 28 31 32 33 34 35 37 38 41 42 43 44 45
    46 47 48 49 51 53 54 55 61 62 63 64 65 66 67 68 69 71 73 74 75 77 79 81 82 83
    84 85 86 87 88 89 91 92 93 94 95 96 97 98 99
""".split())

# (numero_meta, numero_crm, esperado). `numero_meta` e o wa_id cru que o webhook
# entrega; `numero_crm` e como o lead esta gravado no CRM. Cada linha vale nos
# dois sentidos (o teste inverte os argumentos).
CORPUS = [
    # -- MESMA linha, outra grafia: casa ------------------------------------
    ("5511987654321", "5511987654321", True),            # igualdade exata
    ("5511987654321", "+55 (11) 98765-4321", True),      # formatacao do site
    ("5511987654321", "(11) 98765-4321", True),          # sem DDI (import CSV)
    ("5511987654321", "11987654321", True),              # sem DDI, so digitos
    ("553187654321", "+55 31 98765-4321", True),         # Meta sem o 9 x modal com o 9
    ("553187654321", "5531987654321", True),
    ("553187654321", "(31) 98765-4321", True),           # sem DDI e com o 9
    ("553187654321", "553187654321", True),
    ("5531987654321", "553187654321", True),             # Meta com o 9 x lead sem
    ("553187654321", "3187654321", True),                # sem DDI e sem o 9
    ("5548988711776", "554888711776", True),             # DDD 48, fora de 11-28
    ("553133456789", "(31) 3345-6789", True),            # fixo, sem DDI
    ("56912345678", "+56 9 1234 5678", True),            # Chile: so igualdade de digitos
    ("56912345678", "(56) 91234-5678", True),
    ("98765-4321", "987654321", True),                   # (a): so formatacao; igualdade de digitos nao pede DDD
    # O sufixo (11+ digitos) so vale quando NENHUM dos dois lados tem leitura brasileira:
    ("56987654321", "5556987654321", True),              # DDD 56 nao existe: nenhum lado le
    ("51987654321", "+51 987 654 321", True),            # Peru
    ("12125551234", "+1 212 555 1234", True),            # EUA
    ("5491112345678", "+54 9 11 1234-5678", True),       # Argentina
    # -- linhas DIFERENTES: NUNCA casam (AUDIT-2026-08-W2F/F10) --------------
    ("5511987654321", "(21) 98765-4321", False),         # DDD diferente, formatado
    ("5511987654321", "5521987654321", False),           # DDD diferente, com DDI
    ("5511987654321", "98765-4321", False),              # sem DDD
    ("5511987654321", "1987654321", False),              # 10 ultimos digitos: ninguem
    ("553133456789", "(31) 93345-6789", False),          # fixo x celular de mesmo final
    ("5531933456789", "(31) 3345-6789", False),          # idem, o outro lado
    ("5555489887117", "5548988711776", False),           # `+55 48 98871-1776` colado no modal
    ("", "5511987654321", False),                        # vazio nunca casa
    (None, None, False),
    # -- o sufixo NAO cruza DDD (AUDIT-2026-08-W2F/F10) ----------------------
    # Nos DDDs 19 e 49, os 11 ultimos digitos de `55`+DDD+8 digitos leem-se como
    # OUTRO DDD (51, 54) + 9 digitos. Quando os dois lados tem leitura, so a
    # leitura canonica decide.
    ("554987654321", "54987654321", False),              # DDD 49 (wa_id sem o 9) x DDD 54
    ("554987654321", "5554987654321", False),            # idem, DDD 54 como o modal grava
    ("551987654321", "51987654321", False),              # DDD 19 x DDD 51
    ("551987654321", "5551987654321", False),            # idem
    # -- o sufixo NAO junta estrangeiro/corrompido a brasileiro ----------------
    # Com UM lado de leitura brasileira so a leitura canonica decide. O outro lado
    # nao le (DDD que nao existe, assinante de forma errada), mas seus 11 ultimos
    # digitos coincidem com os do brasileiro: o sufixo casaria pessoas diferentes.
    ("56987654321", "556987654321", False),              # Chile x DDD 69 sem o 9 (Rondonia)
    ("59167123456", "559167123456", False),              # Bolivia x DDD 91 sem o 9 (celular iniciado em 6)
    ("595981234567", "5595981234567", False),            # Paraguai x DDD 95
    ("5555489887117", "555489887117", False),            # corrompido pelo modal antigo x numero real do DDD 54
    # -- fronteiras da leitura canonica (alem do minimo do plano) ------------
    ("551087654321", "5510987654321", False),            # DDD 10 nao existe: o 9 nao e inferido
    # Reino Unido (+44 7598 765432) NAO vira "DDD 75": so numero com DDI 55 e lido como Brasil.
    ("447598765432", "(75) 99876-5432", False),
    # -- LIMITES CONHECIDOS (cabecalho do modulo): o que so os digitos nao distinguem
    # Numero estrangeiro de 10/11 digitos GRAVADO no CRM pode ler-se como brasileiro sem DDI: o
    # celular do Peru `+51 987 654 321` e o de Porto Alegre `55 51 98765-4321` casam.
    ("51987654321", "5551987654321", True),
    # Celular que perdeu o ultimo digito pode virar a leitura de OUTRA linha valida do mesmo DDD:
    # `5548988711776` sem o `6` final le-se como o celular sem o 9 de `5548998871177`.
    ("554898871177", "5548998871177", True),
    # Prefixo-lixo `00`/`0` deixa de casar com a grafia brasileira: o lado com o
    # prefixo nao le, o outro le, e o sufixo (que antes os juntava) so vale sem leitura.
    ("5511987654321", "005511987654321", False),
    ("(011) 98765-4321", "11987654321", False),
    ("(011) 98765-4321", "5511987654321", False),
]

# Fronteira do assinante de 8 digitos (DDD 31): o 1o digito decide a leitura.
#   6-9 celular antigo (ganha o 9) | 2-5 fixo (nunca ganha) | 0-1 sem leitura.
# Cada digito vira dois pares: o wa_id sem o 9 contra o mesmo numero digitado
# sem DDI, e contra a grafia do modal, com o 9.
CORPUS += [
    par
    for d in "0123456789"
    for par in (
        ("5531" + d + "2345678", "31" + d + "2345678", d in "23456789"),
        ("5531" + d + "2345678", "55319" + d + "2345678", d in "6789"),
    )
]

# (funcao, argumento, esperado) - a especificacao literal do contrato.
VALORES_EXATOS = [
    ("so_digitos", "11 98765-4321 (Maria)", "11987654321"),
    ("so_digitos", None, ""),
    # `str.isdigit()` e Unicode: o digito arabe-indico (U+0663) PERMANECE, `so_digitos`
    # nao vira ASCII-only. O `[^0-9]` do PostgreSQL o remove - por isso o SQL so
    # pre-filtra e quem afirma identidade e o Python.
    ("so_digitos", "1\u06632", "1\u06632"),
    ("grafias_compativeis", "553187654321",
     frozenset({"553187654321", "5531987654321", "3187654321", "31987654321"})),
    # Sem leitura brasileira (DDD 56 nao existe): a unica grafia e o proprio numero.
    ("grafias_compativeis", "56912345678", frozenset({"56912345678"})),
    # 2o digito do assinante em 0-5 nao tem forma de 8 digitos: o pre-filtro
    # nao oferece a grafia do fixo (3133456789) como se fosse a mesma linha.
    ("grafias_compativeis", "5531933456789", frozenset({"5531933456789", "31933456789"})),
    ("grafias_compativeis", "", frozenset()),
    ("grafias_compativeis", None, frozenset()),
    ("padrao_sufixo", "5511987654321", "%11987654321"),
    ("padrao_sufixo", "11987654321", "%11987654321"),    # fronteira: exatamente 11 digitos
    ("padrao_sufixo", "3187654321", None),               # 10 digitos: sem padrao
    ("padrao_sufixo", None, None),
]

# Caudas de 8 digitos da matriz "DDD diferente nunca casa" (o que vem depois do
# DDD e do nono digito). O 1o digito da cauda decide que linhas ela designa:
#   "12345678"  inicia em 0-1: so existe como celular 9+8 (sem o 9 nao e linha)
#   "87654321"  inicia em 6-9: celular antigo; a Meta o entrega SEM o 9 (mesma linha)
#   "33456789", "52345678"  iniciam em 2-5: fixo; COM o 9 seria outra linha (um celular)
CAUDAS_MATRIZ = ("12345678", "87654321", "33456789", "52345678")

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


def check_sem_desvios(descricao, desvios, mostrar=6):
    """Um unico PASS/FAIL para um lote; no FAIL, conta e lista os primeiros desvios."""
    if not desvios:
        check(True, descricao)
        return
    resto = len(desvios) - mostrar
    detalhe = "; ".join(desvios[:mostrar]) + (f"; (+{resto} outros)" if resto > 0 else "")
    check(False, f"{descricao} - {len(desvios)} desvio(s): {detalhe}")


def _carregar(nome, caminho):
    """Carrega uma copia POR CAMINHO: `app` existe no CRM e no Conversas e os dois
    pacotes nao coexistem no mesmo processo, entao nunca `import app...`."""
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def _impressao(modulo):
    """Tudo que o corpus consegue observar de uma copia, comparavel entre copias."""
    numeros = {n for meta, crm, _ in CORPUS for n in (meta, crm)}
    veredictos = [
        (a, b, modulo.mesmo_telefone(a, b))
        for meta, crm, _ in CORPUS
        for a, b in ((meta, crm), (crm, meta))
    ]
    prefiltro = {n: (modulo.grafias_compativeis(n), modulo.padrao_sufixo(n)) for n in numeros}
    return veredictos, prefiltro


def _linhas(ddd, cauda):
    """As linhas que `ddd` + `cauda` designa, cada uma com TODAS as suas grafias
    (com/sem DDI x com/sem o 9).

    Escrito aqui, de proposito, e nao reaproveitado do modulo: o oraculo da
    matriz nao pode herdar um erro do codigo que ele julga."""
    com_nove = [prefixo + ddd + "9" + cauda for prefixo in ("55", "")]
    sem_nove = [prefixo + ddd + cauda for prefixo in ("55", "")]
    if cauda[0] in "6789":  # celular antigo: com e sem o 9 e a MESMA linha
        return [com_nove + sem_nove]
    if cauda[0] in "2345":  # fixo: sem o 9 e o fixo, com o 9 e OUTRA linha (um celular)
        return [sem_nove, com_nove]
    return [com_nove]       # 0-1: so existe como celular 9+8


def _prefiltro_traz(modulo, buscado, gravado):
    """Ao buscar `buscado`, o pre-filtro SQL do passo 2 traria a linha cuja coluna
    normalizada e `gravado`? (`IN grafias` OU `LIKE '%<11 ultimos>'`)."""
    digitos = modulo.so_digitos(gravado)
    if digitos in modulo.grafias_compativeis(buscado):
        return True
    padrao = modulo.padrao_sufixo(buscado)
    # LIKE '%XXXXXXXXXXX' sobre a coluna normalizada == endswith dos 11 digitos
    return padrao is not None and digitos.endswith(padrao[1:])


def _bytes_lf(caminho):
    """Bytes do arquivo com CRLF lido como LF: o repo usa core.autocrlf, entao um
    checkout no Windows pode trazer CRLF numa copia e LF na outra sem que a regra
    tenha divergido."""
    return caminho.read_bytes().replace(b"\r\n", b"\n")


def secao_paridade(copias):
    print("1) paridade - as duas copias sao a MESMA regra")
    iguais = _bytes_lf(COPIA_CRM) == _bytes_lf(COPIA_CONVERSAS)
    check(iguais,
          "as duas copias sao identicas byte a byte (CRLF e LF contam como iguais)"
          if iguais else
          "as duas copias DIVERGEM - uma foi editada sem a outra: copie uma sobre a outra "
          "(cp app/services/telefone_identidade.py "
          "conversas/app/services/telefone_identidade.py)")
    (_, crm), (_, conversas) = copias
    check(_impressao(crm) == _impressao(conversas),
          f"as duas copias dao o mesmo resultado em todo o corpus ({len(CORPUS)} pares "
          "nos dois sentidos, grafias_compativeis e padrao_sufixo de cada numero)")


def secao_corpus(copias):
    print("2) corpus de identidade - nos dois sentidos")
    for rotulo, modulo in copias:
        desvios = []
        for meta, crm, esperado in CORPUS:
            for a, b in ((meta, crm), (crm, meta)):
                obtido = modulo.mesmo_telefone(a, b)
                if obtido is not esperado:  # `is`: o contrato e bool de verdade, nao "truthy"
                    desvios.append(
                        f"mesmo_telefone({a!r}, {b!r}) devolveu {obtido!r}, esperado {esperado}")
        check_sem_desvios(f"[{rotulo}] {len(CORPUS)} pares x 2 sentidos", desvios)


def secao_prefiltro(copias):
    print("3) pre-filtro SQL cobre tudo que mesmo_telefone aceita")
    iguais = [(meta, crm) for meta, crm, esperado in CORPUS if esperado]
    for rotulo, modulo in copias:
        furos = []
        for meta, crm in iguais:
            # O numero buscado pode ser qualquer um dos dois lados: o webhook
            # busca o wa_id; o endpoint do CRM busca o que a BIA/site mandou.
            for buscado, gravado in ((meta, crm), (crm, meta)):
                if not _prefiltro_traz(modulo, buscado, gravado):
                    furos.append(f"buscando {buscado!r} o pre-filtro nao traz {gravado!r}")
        check_sem_desvios(f"[{rotulo}] {len(iguais)} pares casados x 2 sentidos", furos)


def secao_valores_exatos(copias):
    print("4) valores exatos e constantes")
    for rotulo, modulo in copias:
        check(modulo.MIN_DIGITOS_SUFIXO == 11 and modulo.DDDS_BRASIL == DDDS_ANATEL,
              f"[{rotulo}] MIN_DIGITOS_SUFIXO == 11 e DDDS_BRASIL == lista Anatel "
              f"({len(DDDS_ANATEL)} DDDs)")
        desvios = []
        for nome, argumento, esperado in VALORES_EXATOS:
            obtido = getattr(modulo, nome)(argumento)
            if obtido != esperado:
                desvios.append(f"{nome}({argumento!r}) devolveu {obtido!r}, esperado {esperado!r}")
        check_sem_desvios(f"[{rotulo}] {len(VALORES_EXATOS)} valores exatos", desvios)


def secao_ddd_diferente(copias):
    print("5) DDD diferente NUNCA casa entre numeros com leitura brasileira - matriz exaustiva")
    ddds = sorted(DDDS_ANATEL)
    for rotulo, modulo in copias:
        # Controle positivo: as grafias da MESMA linha casam entre si. Sem isto o
        # "nunca casa" abaixo poderia valer so porque as grafias geradas sao lixo.
        desvios = []
        for ddd in ddds:
            for cauda in CAUDAS_MATRIZ:
                for linha in _linhas(ddd, cauda):
                    desvios += [f"{a!r} x {b!r} (DDD {ddd}) nao casou"
                                for a, b in itertools.product(linha, repeat=2)
                                if not modulo.mesmo_telefone(a, b)]
        check_sem_desvios(f"[{rotulo}] controle positivo: {len(ddds)} DDDs x {len(CAUDAS_MATRIZ)} "
                          "caudas, toda grafia da MESMA linha casa", desvios)

        # Duas linhas DIFERENTES com a mesma cauda de 8 digitos - outro DDD, ou o
        # fixo contra o celular de mesmo final (mesmo DDD) - nao casam em NENHUMA
        # combinacao de grafia, em nenhum dos dois sentidos. A mesma cauda e o caso
        # adversario: e ela que o sufixo de 11 digitos alinhava errado.
        cruzamentos, pares = [], 0
        for cauda in CAUDAS_MATRIZ:
            linhas = [(ddd, linha) for ddd in ddds for linha in _linhas(ddd, cauda)]
            for (ddd1, linha1), (ddd2, linha2) in itertools.combinations(linhas, 2):
                pares += 1
                for a, b in itertools.product(linha1, linha2):
                    if modulo.mesmo_telefone(a, b) or modulo.mesmo_telefone(b, a):
                        cruzamentos.append(f"{a!r} (DDD {ddd1}) casou com {b!r} (DDD {ddd2})")
        check_sem_desvios(
            f"[{rotulo}] {pares} pares de linhas diferentes de mesma cauda ({len(ddds)} DDDs x "
            f"{len(CAUDAS_MATRIZ)} caudas, todas as grafias): nenhum casa", cruzamentos)


def resumo():
    print(f"\n{'FALHOU' if falhas else 'OK'} — {len(falhas)} falha(s)")
    return 1 if falhas else 0


def main():
    ausentes = [str(c.relative_to(ROOT)) for c in (COPIA_CRM, COPIA_CONVERSAS) if not c.is_file()]
    if ausentes:
        print("1) paridade - as duas copias sao a MESMA regra")
        check(False, f"as duas copias do modulo existem - faltando: {ausentes}")
        return resumo()
    copias = [
        ("CRM", _carregar("telefone_identidade_crm", COPIA_CRM)),
        ("Conversas", _carregar("telefone_identidade_conversas", COPIA_CONVERSAS)),
    ]
    secao_paridade(copias)
    secao_corpus(copias)
    secao_prefiltro(copias)
    secao_valores_exatos(copias)
    secao_ddd_diferente(copias)
    return resumo()


if __name__ == "__main__":
    sys.exit(main())
