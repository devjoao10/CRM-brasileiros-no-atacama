# -*- coding: utf-8 -*-
"""
CONV-LEAD-FONE-01 - identidade de telefone: "estes dois numeros sao a MESMA
linha?", numa regra unica usada pelo CRM e pelo Conversas.

O PROBLEMA
----------
O Conversas decidia se um numero ja era lead do CRM por igualdade EXATA dos
digitos. So que o MESMO celular brasileiro chega com grafias diferentes
conforme a porta de entrada:

  * o modal do CRM (cadastro a mao) grava `55` + DDD + 9 digitos
    (`5548988711776`);
  * a Meta entrega o wa_id/from de boa parte dos celulares SEM o nono digito
    quando o DDD fica fora de 11-19, 21, 22, 24, 27 e 28 (`554888711776`);
  * import CSV e API gravam o texto cru, muitas vezes sem DDI
    (`(11) 98765-4321`).

Igualdade exata nao enxerga nada disso: o lead cadastrado a mao passava por
desconhecido, `auto_create_lead_in_crm` (Conversas) criava um lead DUPLICADO
com o wa_id e prendia a conversa nele para sempre. O endpoint irmao do CRM
(`GET /api/leads/by-whatsapp`) tolerava o DDI de um lado so, mas nao o nono
digito - a BIA tambem criava duplicata.

A REGRA (`mesmo_telefone`)
--------------------------
Dois telefones sao a MESMA linha sse, sobre os digitos dos dois lados (ambos
nao vazios), vale uma destas, avaliadas nesta ordem:
  (a) os digitos sao iguais; OU
  (c) os DOIS lados tem leitura brasileira CANONICA (`_formas_canonicas`) e as
      leituras se cruzam: `55` + DDD valido + assinante, onde o celular de
      8 digitos iniciado em 6-9 ganha o nono digito e o fixo (8 digitos
      iniciados em 2-5) nunca ganha; OU
  (b) NENHUM dos dois lados tem leitura canonica (prefixo `00`, digito
      sobrando, DDD que nao existe, numero estrangeiro que nao se le como
      brasileiro), o menor tem >= 11 digitos e o maior termina com ele - o
      sufixo que o CRM ja aceitava, agora restrito a esse par.
Se algum lado tem leitura canonica o sufixo nao entra: com os dois lendo decide
o cruzamento (c); com so um lendo nao ha o que cruzar e os numeros nao casam.

POR QUE O SUFIXO SO VALE ENTRE NUMEROS SEM LEITURA BRASILEIRA
-------------------------------------------------------------
Alinhamento nao e prova. Quando um lado le como brasileiro, o sufixo pode juntar
numeros que NAO sao a mesma linha (e a leitura canonica ja cobre os que sao):
  * Entre dois brasileiros, pode cruzar DDD. Para os DDDs 19 e 49, os 11 ultimos
    digitos de `55` + DDD + 8 digitos (celular antigo sem o nono digito, como a
    Meta entrega, ou fixo) leem-se como os DDDs 51 e 54 + 9 digitos: o 2o
    digito do DDI e a 1a casa do DDD formam o falso DDD, e a 2a casa do DDD, o
    9, faz o papel do nono digito.
        554987654321 (DDD 49)  termina em  54987654321 (DDD 54)
        551987654321 (DDD 19)  termina em  51987654321 (DDD 51)
    O inverso tambem alinha (`5554987654321`, DDD 54 com DDI, termina em
    `554987654321`, DDD 49), e o fixo de um DDD alinha com o celular do outro.
  * Entre um brasileiro e um numero que nao le como brasileiro, os 11 ultimos
    digitos podem coincidir por acaso, e entao o sufixo junta pessoas diferentes:
        556987654321  (DDD 69, sem o 9)  termina em  56987654321  (Chile)
        559167123456  (DDD 91, sem o 9)  termina em  59167123456  (Bolivia)
        5595981234567 (DDD 95)           termina em  595981234567 (Paraguai)
    O mesmo vale para o registro que o modal antigo gravava ao colar
    `+55 48 98871-1776` (`5555489887117`, sem leitura): ele termina em
    `555489887117`, o celular sem o 9 de OUTRA pessoa, do DDD 54.
A leitura canonica ja resolve DDI e nono digito, entao quando algum lado le
decidem so ela e a igualdade; o sufixo fica para o par em que nenhum dos dois le.

POR QUE O F10 (AUDIT-2026-08-W2F) CONTINUA VALENDO
--------------------------------------------------
Casar o numero errado e vazamento entre clientes (nome/e-mail do cliente B no
template do cliente A) - o F10 existe por isso. A regra e mais larga que a
igualdade exata, mas mantem estas garantias (o que digitos sozinhos nao
resolvem esta em LIMITES CONHECIDOS, abaixo):
  * se algum lado tem leitura brasileira, DDD diferente NUNCA casa: a leitura
    canonica exige DDD valido, so infere o nono digito DENTRO do mesmo DDD e,
    com um lado brasileiro, e a UNICA que decide alem da igualdade;
  * numero sem DDD nao casa com numero que tem DDD: 9 digitos ou menos nao tem
    leitura canonica e nao alcanca o sufixo de 11;
  * os 10 ultimos digitos nao identificam ninguem - por isso o sufixo exige 11;
  * fixo nunca ganha o 9, entao nunca casa com celular de mesmo final;
  * ambiguidade (2+ leads DISTINTOS) continua RECUSADA por quem chama, nunca
    resolvida por ORDER BY/LIMIT 1 - e tambem quando o pre-filtro de "mesma
    linha em outra grafia" enche o LIMIT do SQL, porque a decisao passaria a
    depender da amostra que o banco devolveu;
  * a igualdade exata segue com precedencia: so se ela nao achar NENHUM lead e
    que quem chama aplica "mesma linha em outra grafia".

LIMITES CONHECIDOS
------------------
O que so os digitos nao distinguem; a regra os aceita (ou recusa) de proposito:
  1. Um numero estrangeiro de 10 ou 11 digitos GRAVADO no CRM pode ler-se como
     brasileiro sem DDI: o celular do Peru `51987654321` e o de Porto Alegre
     `5551987654321` (DDD 51) casam. O Conversas contorna isso no wa_id, que a
     Meta sempre entrega em E.164: so aplica a regra a `55` + 12 ou 13 digitos.
  2. Celular que perdeu o ultimo digito pode virar a leitura de OUTRA linha
     valida do mesmo DDD: `554898871177` (`5548988711776` sem o `6`) le-se como
     o celular sem o 9 de `5548998871177` e casa com ele.
  3. Prefixo-lixo `0` ou `00` deixa de casar com a grafia brasileira: o lado com
     o prefixo nao le, o outro le, e o sufixo so vale quando nenhum dos dois
     le. `(011) 98765-4321` nao casa com `11987654321` (o sufixo casava) nem
     com `5511987654321`.

PYTHON DECIDE, O SQL SO PRE-FILTRA
----------------------------------
`grafias_compativeis` (`IN`) e `padrao_sufixo` (`LIKE`) descrevem, sobre os
digitos NORMALIZADOS da coluna, um conjunto que cobre tudo que `mesmo_telefone`
aceita; cada candidato devolvido ainda passa por `mesmo_telefone`. `[^0-9]` do
PostgreSQL e ASCII e `str.isdigit()` nao e: nos casos exoticos os dois
divergem, entao quem afirma identidade e sempre o Python. Quem monta o SQL
passa os valores por bind param e aplica o LIKE so sobre a expressao
normalizada, nunca sobre a coluna crua (que guarda `+`, espacos, `()` e `-`).

DUAS COPIAS, BYTE A BYTE IDENTICAS
----------------------------------
    app/services/telefone_identidade.py            (CRM, Python 3.11)
    conversas/app/services/telefone_identidade.py  (Conversas, Python 3.12)
Os dois pacotes se chamam `app` e nunca coexistem no mesmo processo, entao um
nao pode importar o outro: a regra e COPIADA, nao compartilhada.
`tests/test_telefone_identidade.py` FALHA se as copias divergirem - edite uma e
copie sobre a outra. Modulo puro: sem I/O, sem banco, sem importar `app.*`.
"""
from typing import Optional

# DDDs do Brasil (lista Anatel, 67). So eles tem leitura canonica: e o que
# impede o nono digito de ser inferido para um DDD que nao existe.
DDDS_BRASIL = frozenset({
    "11", "12", "13", "14", "15", "16", "17", "18", "19",
    "21", "22", "24", "27", "28",
    "31", "32", "33", "34", "35", "37", "38",
    "41", "42", "43", "44", "45", "46", "47", "48", "49",
    "51", "53", "54", "55",
    "61", "62", "63", "64", "65", "66", "67", "68", "69",
    "71", "73", "74", "75", "77", "79",
    "81", "82", "83", "84", "85", "86", "87", "88", "89",
    "91", "92", "93", "94", "95", "96", "97", "98", "99",
})

# Menor sufixo aceito como evidencia quando NENHUM dos numeros tem leitura
# brasileira. Os 10 ultimos digitos NAO identificam ninguem (5511987654321 e
# 5521987654321 terminam ambos em 1987654321 - DDDs diferentes, clientes
# diferentes). 11 e um piso, nao uma prova: quando algum dos numeros tem leitura
# o sufixo nem entra, porque alinha errado (ver `mesmo_telefone`).
MIN_DIGITOS_SUFIXO = 11


def so_digitos(valor: Optional[str]) -> str:
    """
    Normalizacao unica de telefone: so digitos (+, espacos, (), - e . somem).

    `str.isdigit()` e Unicode: um digito nao-ASCII (arabe-indico, por exemplo)
    PERMANECE, o resultado nao e ASCII-only. O `[^0-9]` do PostgreSQL o remove;
    por isso o SQL so pre-filtra e quem afirma identidade e o Python.
    """
    return "".join(ch for ch in (valor or "") if ch.isdigit())


def _forma_canonica_br(ddd: str, assinante: str) -> Optional[str]:
    """
    Forma canonica `55` + DDD + assinante de uma linha brasileira, ou None.

    None quando o DDD nao existe ou o assinante nao tem cara de linha: sem
    leitura canonica o numero so casa por igualdade ou, contra outro numero que
    tambem nao le, por sufixo - nunca por "provavelmente e o mesmo".
    """
    if ddd not in DDDS_BRASIL:
        return None
    if len(assinante) == 9 and assinante[0] == "9":
        return "55" + ddd + assinante
    if len(assinante) == 8 and assinante[0] in "6789":
        # Celular sem o nono digito: a Meta entrega assim fora dos DDDs 11-19,
        # 21, 22, 24, 27 e 28. Recoloca o 9 para as duas grafias se encontrarem.
        return "55" + ddd + "9" + assinante
    if len(assinante) == 8 and assinante[0] in "2345":
        # Fixo: NUNCA ganha o 9, senao casaria com um celular de mesmo final.
        return "55" + ddd + assinante
    return None


def _formas_canonicas(digitos: str) -> frozenset[str]:
    """
    Leituras canonicas brasileiras dos digitos: com DDI (12 ou 13 digitos
    comecando em 55) ou sem DDI (10 ou 11 digitos).

    Os comprimentos nao se sobrepoem, entao ha no maximo UMA leitura. O
    frozenset serve a tres usos: o cruzamento (`&`) da regra (c); o portao
    "tem leitura?" (vazio = nao le como brasileiro), que decide se o sufixo (b)
    pode valer; e a iteracao de `grafias_compativeis`, que enumera as grafias
    da linha.
    """
    formas = set()
    if len(digitos) in (12, 13) and digitos.startswith("55"):
        forma = _forma_canonica_br(digitos[2:4], digitos[4:])
        if forma:
            formas.add(forma)
    if len(digitos) in (10, 11):
        forma = _forma_canonica_br(digitos[:2], digitos[2:])
        if forma:
            formas.add(forma)
    return frozenset(formas)


def mesmo_telefone(a: Optional[str], b: Optional[str]) -> bool:
    """
    True se `a` e `b` sao a MESMA linha, em qualquer grafia (regras (a), (c) e
    (b) do cabecalho do modulo, nesta ordem). Simetrica; numero vazio ou sem
    digitos nunca casa.

    Se algum lado tem leitura canonica o sufixo nao entra: com os dois lendo
    decide o cruzamento das leituras; com so um lendo nao ha o que cruzar e nao
    casa. O sufixo alinharia errado - cruzaria DDD (49 com 54, 19 com 51) ou
    juntaria estrangeiro e registro corrompido a um brasileiro - e so vale
    quando NENHUM lado tem leitura canonica.
    """
    digitos_a, digitos_b = so_digitos(a), so_digitos(b)
    if not digitos_a or not digitos_b:
        return False
    if digitos_a == digitos_b:
        return True
    formas_a, formas_b = _formas_canonicas(digitos_a), _formas_canonicas(digitos_b)
    if formas_a or formas_b:
        # Algum lado le como brasileiro: so o cruzamento decide (vazio se o outro nao le).
        return bool(formas_a & formas_b)
    menor, maior = sorted((digitos_a, digitos_b), key=len)
    return len(menor) >= MIN_DIGITOS_SUFIXO and maior.endswith(menor)


def grafias_compativeis(numero: Optional[str]) -> frozenset[str]:
    """
    Toda grafia (so digitos) que um lead da MESMA linha pode ter no banco.

    E o `IN` do pre-filtro SQL (sobre os digitos normalizados da coluna). Junto
    com `padrao_sufixo` precisa cobrir tudo que `mesmo_telefone` aceita - quem
    decide identidade continua sendo o Python. Vazio se nao ha digitos.
    """
    digitos = so_digitos(numero)
    if not digitos:
        return frozenset()
    grafias = {digitos}
    for forma in _formas_canonicas(digitos):
        ddd, assinante = forma[2:4], forma[4:]
        variantes = [assinante]
        if len(assinante) == 9 and assinante[1] in "6789":
            # Celular antigo: tinha 8 digitos iniciados em 6-9, e e assim que a
            # Meta ainda o entrega fora dos DDDs 11-19, 21, 22, 24, 27 e 28.
            variantes.append(assinante[1:])
        for variante in variantes:
            grafias.add("55" + ddd + variante)  # com DDI
            grafias.add(ddd + variante)         # sem DDI
    return frozenset(grafias)


def padrao_sufixo(numero: Optional[str]) -> Optional[str]:
    """
    Padrao LIKE do passo de sufixo: '%' + 11 ultimos digitos, ou None.

    None com menos de `MIN_DIGITOS_SUFIXO` digitos: sufixo curto nao identifica
    ninguem (F10). Passe o padrao por bind param e aplique o LIKE so sobre a
    expressao NORMALIZADA da coluna.
    """
    digitos = so_digitos(numero)
    if len(digitos) < MIN_DIGITOS_SUFIXO:
        return None
    return "%" + digitos[-MIN_DIGITOS_SUFIXO:]
