"""
BIA-V2 Fase 9 - output guard deterministico de conversas/app/v2/output_guard.py.

Trava `filtrar_saida(texto: str, *, acao: str) -> ResultadoGuard` contra os
casos literais do plano (docs/superpowers/plans/2026-08-29-bia-v2.md, linha
1971: "R$ 5.000 bloqueado - 5000 reais bloqueado - cinco mil reais bloqueado -
USD 300 bloqueado - texto sem valor passa - acao nao autorizada bloqueada")
mais a cobertura adicional pedida na tarefa desta fase: simbolo colado sem
espaco, moeda por extenso plural/singular, e os casos de fronteira do
scanner por extenso documentados em output_guard.py.

Roda standalone:  python tests/test_v2_output_guard.py
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []
executados = []


def check(cond, msg):
    executados.append(msg)
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from app.v2.contratos import ResultadoGuard  # noqa: E402
from app.v2.output_guard import ACOES_PERMITIDAS, filtrar_saida  # noqa: E402

# Acao valida generica para os casos que nao testam a propria acao.
_ACAO_OK = "informar_fatos_confirmados"
assert _ACAO_OK in ACOES_PERMITIDAS  # premissa do arquivo inteiro - falha alto se cair.


def bloqueado(rotulo, texto, acao=_ACAO_OK):
    """Roda `filtrar_saida` e verifica o padrao de bloqueio: `permitido is
    False`, `motivo` e o token esperado (nunca None), e `texto_final == ""`
    (NUNCA o texto original - ver "QUANDO BLOQUEIA" no docstring de
    output_guard.py). Devolve o resultado para asserts extras do chamador."""
    r = filtrar_saida(texto, acao=acao)
    check(isinstance(r, ResultadoGuard), f"{rotulo}: devolve ResultadoGuard")
    check(r.permitido is False, f"{rotulo}: permitido is False (obtido={r.permitido!r})")
    check(r.motivo is not None, f"{rotulo}: motivo nao e None quando bloqueado")
    check(r.texto_final == "", f"{rotulo}: texto_final e string vazia (obtido={r.texto_final!r})")
    return r


def permitido(rotulo, texto, acao=_ACAO_OK):
    """Roda `filtrar_saida` e verifica o padrao de permissao: `permitido is
    True`, `motivo is None`, `texto_final == texto` (inalterado - o guard
    aprova ou recusa, nunca reescreve)."""
    r = filtrar_saida(texto, acao=acao)
    check(isinstance(r, ResultadoGuard), f"{rotulo}: devolve ResultadoGuard")
    check(r.permitido is True, f"{rotulo}: permitido is True (obtido={r.permitido!r})")
    check(r.motivo is None, f"{rotulo}: motivo is None quando permitido (obtido={r.motivo!r})")
    check(r.texto_final == texto, f"{rotulo}: texto_final == texto original, inalterado")
    return r


# ---------------------------------------------------------------------------
# A. Os 4 casos BLOQUEADOS literais do plano (linha 1971) - strings EXATAS.
# ---------------------------------------------------------------------------
r_a1 = bloqueado("A1. simbolo literal do plano", "O total ficou R$ 5.000")
check(r_a1.motivo == "valor_monetario_detectado", "A1b. motivo e o token de valor monetario")

bloqueado("A2. digito+extenso-moeda literal do plano", "O total ficou 5000 reais")
bloqueado("A3. por extenso literal do plano", "O total ficou cinco mil reais")

r_a4 = bloqueado("A4. codigo de moeda literal do plano", "O total ficou USD 300")
check(r_a4.motivo == "valor_monetario_detectado", "A4b. motivo e o token de valor monetario")

# ---------------------------------------------------------------------------
# B. O caso PASSA literal do plano - string EXATA, com prova de bicondicional.
# ---------------------------------------------------------------------------
r_b1 = permitido("B1. texto sem valor monetario (literal do plano)", "Sua viagem tem 5 pessoas para 3 destinos")
check(r_b1 == ResultadoGuard(permitido=True, motivo=None, texto_final="Sua viagem tem 5 pessoas para 3 destinos"),
      "B1b. ResultadoGuard inteiro bate exatamente (permitido/motivo/texto_final)")

# ---------------------------------------------------------------------------
# C. Acao nao autorizada bloqueia INDEPENDENTE do texto (literal do plano) -
# inclusive quando o texto em si e perfeitamente seguro.
# ---------------------------------------------------------------------------
check("acao_desconhecida_qualquer" not in ACOES_PERMITIDAS, "C0. premissa: a acao do teste realmente nao esta na allowlist")
r_c1 = bloqueado("C1. acao nao autorizada, texto seguro", "Oi! Tudo certo por aqui.", acao="acao_desconhecida_qualquer")
check(r_c1.motivo == "acao_nao_autorizada", "C1b. motivo e o token de acao nao autorizada, nao o de valor monetario")
bloqueado("C2. acao nao autorizada, texto TAMBEM com valor (acao vence a ordem)", "Custa R$ 5.000", acao="")
bloqueado("C3. acao e string vazia e bloqueada", "oi", acao="")

# ---------------------------------------------------------------------------
# D. Cobertura extra pedida na tarefa - simbolo colado sem espaco.
# ---------------------------------------------------------------------------
bloqueado("D1. simbolo colado sem espaco", "O total ficou R$5000")
bloqueado("D2. simbolo com milhar e centavos", "O total ficou R$ 5.000,00")
bloqueado("D3. simbolo minusculo (tolerancia extra)", "o total ficou r$ 300")

# ---------------------------------------------------------------------------
# E. Cobertura extra pedida na tarefa - moeda por extenso plural/singular,
# com e sem acento (cliente de WhatsApp digita sem acento com frequencia).
# ---------------------------------------------------------------------------
bloqueado("E1. dolar singular, com acento", "Isso custa 1 dólar")
bloqueado("E2. dolares plural, com acento", "Isso custa 500 dólares")
bloqueado("E3. dolar singular, sem acento", "Isso custa 1 dolar")
bloqueado("E4. dolares plural, sem acento", "Isso custa 500 dolares")
bloqueado("E5. euro singular", "Isso custa 1 euro")
bloqueado("E6. euros plural", "Isso custa 10 euros")

# ---------------------------------------------------------------------------
# F. Codigo de moeda - as duas ordens (numero-codigo e codigo-numero) e
# case-insensitive, alem do caso literal do plano (grupo A4).
# ---------------------------------------------------------------------------
bloqueado("F1. numero antes do codigo", "Ficou 300 USD")
bloqueado("F2. codigo BRL", "Ficou BRL 500")
bloqueado("F3. codigo em minusculo", "ficou usd 300")

# ---------------------------------------------------------------------------
# G. Falsos positivos que a tarefa exige NAO bloquear - numero sozinho,
# mesmo com valor alto, nunca e gatilho sem moeda adjacente.
# ---------------------------------------------------------------------------
permitido("G1. numero sozinho sem moeda (clientes)", "temos 5000 clientes")
permitido("G2. numero sozinho sem moeda (pessoas)", "a viagem tem 5 pessoas para 3 destinos")
permitido("G3. numero sozinho sem moeda (destinos)", "voces visitam 3 destinos incriveis")
permitido("G4. palavra de moeda SEM numero adjacente nao dispara nada", "Aceitamos reais e dolares no pagamento")
permitido("G5. numero seguido de palavra comum, nao de moeda", "Reservei vinte quartos para o grupo")

# ---------------------------------------------------------------------------
# H. Casos de FRONTEIRA do parser por extenso - documentados em
# output_guard.py, cada um com o teste que prova a decisao tomada.
# ---------------------------------------------------------------------------
# H1 - "de" partitivo entre escala (milhao) e moeda: sem esta regra extra,
# a UNICA forma natural de falar "milhao" em portugues ("um milhao DE
# reais") vazaria sempre. Risco assumido e fechado deliberadamente.
bloqueado("H1. milhao com conectivo partitivo 'de' (com acento)", "Isso sai por um milhão de reais")
bloqueado("H2. milhao com conectivo partitivo 'de' (sem acento)", "Isso sai por um milhao de reais")

# H3 - ambiguidade de "real" (moeda vs. adjetivo "genuino"): a gramatica do
# portugues nunca cola o adjetivo direto no numeral (sempre ha um
# substantivo no meio, como "motivos"), entao o scanner nao deveria
# confundir os dois - prova de que o caso considerado arriscado e seguro.
permitido("H3. 'real' como adjetivo, nao como moeda (substantivo separa numero e a palavra)",
          "Trinta motivos reais me fazem confiar nessa agencia")

# H4 - compound tens+units com conector aditivo "e", alem do minimo do plano
# (so exigia "cinco mil reais", sem conector) - prova que a extensao e
# consistente com o vocabulario ja aprovado, nao um vocabulario novo.
bloqueado("H4. compound com conector aditivo 'e' (vinte e cinco mil reais)", "Fechou em vinte e cinco mil reais")

# H5 - centena isolada (acrescentada alem do minimo "unidades, dezenas, mil,
# milhao" do plano - ver docstring do modulo, "CENTENAS foram ACRESCENTADAS").
bloqueado("H5. centena isolada (quinhentos reais)", "O pacote sai por quinhentos reais")

# H6 - escala isolada, sem numero explicito na frente ("mil reais" = 1000).
bloqueado("H6. escala isolada sem numeral (mil reais)", "Custa mil reais")

# ---------------------------------------------------------------------------
# I. Estrutura de ACOES_PERMITIDAS - allowlist fechada, nao mutavel por
# engano (mesmo cuidado de TRANSICOES_PERMITIDAS em estados.py).
# ---------------------------------------------------------------------------
check(isinstance(ACOES_PERMITIDAS, frozenset), "I1. ACOES_PERMITIDAS e frozenset (imutavel)")
check(len(ACOES_PERMITIDAS) >= 1, "I2. ACOES_PERMITIDAS tem pelo menos 1 membro")
check(all(isinstance(a, str) and a for a in ACOES_PERMITIDAS), "I3. todo membro de ACOES_PERMITIDAS e string nao vazia")

# ---------------------------------------------------------------------------
# J. Pureza (AST) - output_guard.py so importa `re`, `unicodedata` e
# `app.v2.contratos` (para ResultadoGuard). Mesmo estilo do bloco H de
# tests/test_v2_estados.py ("Pureza (AST) - allowlist propria da Fase 4").
# ---------------------------------------------------------------------------
_FONTE_GUARD = (CONVERSAS_DIR / "app" / "v2" / "output_guard.py").read_text(encoding="utf-8")
_ARVORE_GUARD = ast.parse(_FONTE_GUARD)
_RAIZES_PERMITIDAS_GUARD = {"re", "unicodedata", "app"}
_PROIBIDOS_GUARD = (
    "sqlalchemy", "fastapi", "starlette", "httpx", "requests", "os", "pathlib",
    "subprocess", "sqlite3", "psycopg2", "google", "app.database", "app.models",
    "app.services", "app.config", "app.routers", "app.schemas", "app.main",
    "app.v2.eventos", "app.v2.comunicador",
)
_importados_guard = set()
for _no in ast.walk(_ARVORE_GUARD):  # walk pega import dentro de funcao, if ou try
    if isinstance(_no, ast.Import):
        for _alias in _no.names:
            _importados_guard.add(_alias.name)
    elif isinstance(_no, ast.ImportFrom):
        _importados_guard.add("." * _no.level + (_no.module or ""))
check(
    all(n.split(".")[0] in _RAIZES_PERMITIDAS_GUARD for n in _importados_guard),
    f"J1. output_guard.py so importa de re/unicodedata/app (achado={sorted(_importados_guard)})",
)
check(
    not any(n == p or n.startswith(p + ".") for n in _importados_guard for p in _PROIBIDOS_GUARD),
    f"J2. nenhum import proibido (sem LLM, sem IO, sem infra) - achado={sorted(_importados_guard)}",
)
check(
    all(n == "app.v2.contratos" for n in _importados_guard if n.startswith("app.")),
    f"J3. o unico import sob 'app.' e 'app.v2.contratos' (achado={sorted(n for n in _importados_guard if n.startswith('app.'))})",
)


# ---------------------------------------------------------------------------
# K. Achado de revisao de seguranca: caracteres Unicode invisiveis (categoria
# Cf - zero-width space, soft hyphen, word joiner) nao sao espaco (`\s` nao
# casa) nem marca de acento (NFKD nao remove), entao inseri-los junto ao
# simbolo/digito/palavra de moeda burlava os tres detectores. Strings
# confirmadas empiricamente pela revisao antes da correcao. Fixed em
# `_remover_invisiveis`, aplicado no topo de `_contem_valor_monetario`.
# ---------------------------------------------------------------------------
bloqueado("K1. zero-width space colado no simbolo (R\\u200b$5000)", "Isso custa R​$5000 no total")
bloqueado("K2. zero-width space entre simbolo e digito (R$\\u200b5000)", "Isso custa R$​5000 no total")
bloqueado("K3. zero-width space entre digito e extenso (5000\\u200breais)", "Isso custa 5000​reais no total")
bloqueado("K4. soft hyphen no mesmo ponto (5000\\xadreais)", "Isso custa 5000\xadreais no total")
bloqueado("K5. zero-width space no codigo de moeda (USD\\u200b300)", "Ficou USD​300 no total")

# ---------------------------------------------------------------------------
# L. Segunda revisao de seguranca (python-reviewer), RODANDO DEPOIS da
# correcao do bloco K: digito colado DIRETO numa letra - sem espaco, sem
# pontuacao, sem invisivel Cf no meio - atravessava o Detector 2 (codigo de
# moeda) e o Detector 3 (extenso/digito+moeda). Strings confirmadas
# empiricamente pela revisao ANTES da correcao (todas com permitido=True).
# Fixed em duas frentes, ambas em output_guard.py:
#   1. `_inserir_espaco_fronteira_digito_letra` - normalizacao nova, aplicada
#      a `texto_espacado` antes dos detectores 2 e 3 em
#      `_contem_valor_monetario` - insere espaco em toda fronteira
#      digito<->letra (os dois sentidos), sem precisar de lista de casos.
#   2. `_BORDA_PONTUACAO` ampliada com os marcadores de markdown que o
#      WhatsApp reconhece ("*negrito*", "_italico_", "~riscado~") e
#      colchetes - cobre o numero EMBRULHADO em formatacao (nao colado).
# ---------------------------------------------------------------------------
# L1-L2 - achado original: digito colado direto em palavra/escala por
# extenso (Detector 3), sem nenhum separador.
bloqueado("L1. digito colado direto em palavra de moeda (5000reais)", "Isso custa 5000reais")
bloqueado("L2. digito colado direto em escala por extenso (5mil reais)", "sao 5mil reais")

# L3-L4 - achado original: numero embrulhado em marcador de markdown que a
# propria IA gera pro WhatsApp - prova que ampliar `_BORDA_PONTUACAO` com
# "*" e colchetes fecha os dois casos reportados.
bloqueado("L3. numero embrulhado em negrito markdown (*5000*)", "Isso custa *5000* reais")
bloqueado("L4. numero embrulhado em colchetes (sao [5000] reais)", "sao [5000] reais")

# L5 - achado original: Detector 2 (codigo de moeda) - `\b` nao marca
# fronteira nenhuma entre digito e letra colados (os dois sao \w para o
# regex) - "300USD" atravessava intacto, mesmo "300 USD" (com espaco) ja
# sendo pego corretamente pelo F1.
bloqueado("L5. codigo de moeda colado direto apos o digito (300USD)", "Isso custa 300USD")

# L6-L7 - mesma classe do L3/L4: os outros dois marcadores de markdown que o
# WhatsApp reconhece (italico "_", riscado "~") - a IA usa os tres de forma
# intercambiavel, cobertura simetrica confirmada por teste, nao presumida.
bloqueado("L6. numero embrulhado em italico markdown (_5000_)", "Isso custa _5000_ reais")
bloqueado("L7. numero embrulhado em riscado markdown (~5000~)", "Isso custa ~5000~ reais")

# L8 - simetria da fronteira nova: letra->digito (codigo ANTES do numero),
# alem do digito->letra do L5 - prova que a normalizacao e bidirecional de
# verdade, nao so no sentido que os 5 casos reportados exercitam.
bloqueado("L8. codigo de moeda colado ANTES do digito (USD300)", "Ficou USD300 no total")

# L9 - falso positivo que a correcao NAO pode introduzir: o lookaround novo
# so insere espaco onde NAO ha nada entre digito e letra, entao numero(s) ja
# espacado(s) e sem moeda adjacente tem que continuar passando livre - a
# mesma garantia do bloco G, agora sob a normalizacao nova.
permitido("L9. fronteira digito-letra nova nao cria falso bloqueio em numero ja espacado", "Temos 3 quartos e 5 camas disponiveis")

print()
print(f"{len(executados)} verificacoes executadas.")
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
