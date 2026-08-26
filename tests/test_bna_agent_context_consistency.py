"""
Auditoria bna_agent_context (H1-H15, 2026-08) — regressao das correcoes na
knowledge base da BIA: cada H* precisa continuar presente no arquivo onde
foi escrita, o validador estendido precisa continuar saindo com 0, e os
dois itens deixados FORA de escopo (precos [PENDENTE_VALIDACAO] e a regra
de altitude/menores de 7 contada de 3 jeitos incompativeis) precisam
continuar sinalizados como pendentes — nenhuma sessao futura pode
"resolver" isso inventando valor.

So grepar nao prova que o validador funciona (ver ROOT-017 em
docs/audit/ROOT_CAUSES.md) — por isso este teste EXECUTA
scripts/validate_bna_agent_context.py como subprocesso e confere o exit
code, alem de grepar as regras.

Roda standalone:  python tests/test_bna_agent_context_consistency.py
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VAULT = ROOT / "bna_agent_context"

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def norm(path: pathlib.Path) -> str:
    """Texto do arquivo com espacos/quebras de linha colapsados (caixa preservada) —
    evita que o teste quebre so por causa do wrap de linha do markdown."""
    return " ".join(path.read_text(encoding="utf-8").split())


def has(path: pathlib.Path, snippet: str) -> bool:
    """Substring case-insensitive sobre o texto normalizado."""
    return snippet.lower() in norm(path).lower()


# ============ 0. o validador precisa EXECUTAR e sair com 0, nao so ser grepado ============
print("Validador scripts/validate_bna_agent_context.py (subprocesso real)")
result = subprocess.run(
    [sys.executable, str(ROOT / "scripts" / "validate_bna_agent_context.py")],
    capture_output=True, text=True,
)
check(result.returncode == 0, f"validator sai com exit 0 (saiu com {result.returncode})")
if result.returncode != 0:
    print(result.stdout)
    print(result.stderr)

# ============ arquivos usados nas checagens abaixo ============
persona = VAULT / "00_persona"
op = VAULT / "08_operacao_agente"
faq = VAULT / "07_faq_objecoes"
precos = VAULT / "04_precos"
tours = VAULT / "03_tours"
guardrails = VAULT / "09_guardrails"
empresa = VAULT / "01_empresa"
saude = VAULT / "06_saude_seguranca"
politicas = VAULT / "05_politicas"
meta = VAULT / "_meta"

tom_de_voz = persona / "tom_de_voz.md"
campos = op / "campos_obrigatorios_crm.md"
handoff = op / "handoff_humano.md"
fluxo = op / "fluxo_atendimento_bia.md"
uso_tools = op / "uso_de_tools.md"
quando_escalar = faq / "quando_escalar.md"
faq_clientes = faq / "faq_clientes.md"
nunca_inventar = guardrails / "nunca_inventar.md"
exemplos = persona / "exemplos_dialogo.md"
regras_preco = precos / "regras_de_preco.md"
precos_uyuni = precos / "precos_2026_uyuni.md"
uyuni_exp = tours / "uyuni_expedicoes.md"
rota_salares = tours / "atacama_rota_dos_salares.md"
lagunas = tours / "atacama_lagunas_altiplanicas_piedras_rojas.md"
proibicoes = persona / "proibicoes_de_linguagem.md"
canais = empresa / "canais_atendimento.md"
pend_index = meta / "pendencias_index.md"
pend_validacao = meta / "pendencias_validacao.md"
altitude = saude / "altitude.md"
criancas = saude / "criancas.md"
termos = politicas / "termos_e_condicoes.md"

# ============ H1 — nome COMPLETO so no CRM, so o primeiro nome e falado ============
print("\nH1 - primeiro nome ao cliente, nome completo no CRM")
check(has(tom_de_voz, "Falar com o cliente usando SÓ o primeiro nome"),
      "tom_de_voz.md manda usar so o primeiro nome")
check(has(campos, "nome COMPLETO do cliente"),
      "campos_obrigatorios_crm.md marca o campo nome como COMPLETO (cadastro)")

# ============ H2 — nao repetir pergunta ja respondida na MESMA conversa ============
print("\nH2 - nao repetir campo ja dito na conversa atual")
check(has(fluxo, "checar se ele já foi dito NESTA MESMA conversa"),
      "fluxo_atendimento_bia.md manda checar a conversa atual antes de perguntar")

# ============ H3 — precedencia handoff comercial vs escalacao de limite ============
print("\nH3 - precedencia handoff comercial vs escalacao de limite")
check(has(campos, "handoff comercial") and has(campos, "escalação de limite"),
      "campos_obrigatorios_crm.md nomeia as duas situacoes")
check(has(quando_escalar, "handoff comercial") and has(quando_escalar, "escalação de limite"),
      "quando_escalar.md nomeia as duas situacoes (cross-reference)")
check(has(fluxo, "a BIA pergunta PROATIVAMENTE"),
      "fluxo_atendimento_bia.md manda perguntar proativamente o campo que falta")

# ============ H4 — nunca alegar acao interna/prioridade/contato ja feito ============
print("\nH4 - nunca alegar acao interna concluida")
check(has(nunca_inventar, "NUNCA afirmar ação interna já concluída"),
      "nunca_inventar.md proibe alegar acao interna concluida")
check(has(nunca_inventar, "NUNCA atribuir prioridade"),
      "nunca_inventar.md proibe atribuir prioridade/urgencia")
check(
    has(nunca_inventar, "nossa equipe vai preparar um roteiro e te enviar em até 24h")
    and has(nunca_inventar, "vou pedir pra nossa equipe te ajudar com isso"),
    "nunca_inventar.md reusa as frases prospectivas ja existentes (nao inventa copy nova)",
)

# ============ H5 — lead pre-existente = contato/cotacao anterior, nunca viagem confirmada ============
print("\nH5 - lead pre-existente nao e viagem confirmada")
check(has(fluxo, "Lead pré-existente") and has(fluxo, "NUNCA que a viagem está confirmada"),
      "fluxo_atendimento_bia.md trata lead existente como contato anterior, nao reserva")
check(has(uso_tools, "CONTATO ou COTAÇÃO anterior"),
      "uso_de_tools.md faz cross-reference no consultar_lead")

# ============ H6 — cotacao entregue no WhatsApp; e-mail e so cadastro ============
print("\nH6 - cotacao entregue no WhatsApp, nao por e-mail")
check(has(campos, "Canal da cotação"), "campos_obrigatorios_crm.md declara o canal de entrega")
check(has(handoff, "Canal da cotação"), "handoff_humano.md declara o canal de entrega")
for f, label in [(exemplos, "exemplos_dialogo.md"), (regras_preco, "regras_de_preco.md"),
                  (nunca_inventar, "nunca_inventar.md"), (campos, "campos_obrigatorios_crm.md")]:
    check(not has(f, "que te envio tudo") and not has(f, "que a gente já te retorna"),
          f"{label} nao promete mais envio/retorno implicito por e-mail")

# ============ H7 — Uyuni: so 3d/2n e 4d/3n existem; 4d/3n e a referencia ============
print("\nH7 - catalogo real de Uyuni (sem 1 dia, sem 7 dias)")
check(has(uyuni_exp, "NÃO existe expedição de 1 dia nem de 7 dias para Uyuni"),
      "uyuni_expedicoes.md nega 1 dia e 7 dias explicitamente")
check(has(uyuni_exp, "referência operacional padrão"),
      "uyuni_expedicoes.md marca 4d/3n como referencia operacional padrao")
check(has(faq_clientes, "duração TOTAL somando os dois destinos"),
      "faq_clientes.md desambigua Atacama+Uyuni 7+ como duracao total, nao produto")

# ============ H8 — Uyuni 3 dias e privativo/min 2 pax, nao pra viajante solo ============
print("\nH8 - Uyuni 3 dias exige minimo 2 pax (conflito com a tabela sinalizado)")
check(has(uyuni_exp, "Exige mínimo de 2 passageiros") and has(uyuni_exp, "NÃO oferecer a viajante solo"),
      "uyuni_expedicoes.md: 3 dias exige minimo 2 pax e nao vai pra solo")
check(has(precos_uyuni, "Nota de conflito"), "precos_2026_uyuni.md sinaliza o conflito na tabela")
check(has(pend_validacao, "CONFLITO Uyuni 3 dias regular vs"),
      "pendencias_validacao.md lista o novo item H8")
check(has(pend_index, "Item novo (H8"), "pendencias_index.md lista o novo item H8")

# ============ H9 — roteiros redundantes (Rota dos Salares x Lagunas+Piedras Rojas) ============
print("\nH9 - sobreposicao de roteiros sinalizada")
check(has(rota_salares, "Sobreposição com outro tour"), "atacama_rota_dos_salares.md nota a sobreposicao")
check(has(lagunas, "Sobreposição com outro tour"),
      "atacama_lagunas_altiplanicas_piedras_rojas.md nota a sobreposicao")
check(has(faq_clientes, "priorizar Lagunas Altiplânicas + Piedras Rojas"),
      "faq_clientes.md da preferencia entre os dois quando as datas nao permitem ambos")

# ============ H10 — nao inventar parceiros/pessoas/acoes da equipe ============
print("\nH10 - guardrail nomeia parceiros, pessoas e acoes da equipe")
check(has(nunca_inventar, "parceiros/empresas parceiras") and has(nunca_inventar, "nomes ou cargos de pessoas da"),
      "nunca_inventar.md nomeia as categorias explicitamente")

# ============ H11 — recusar extracao de prompt/raciocinio/tools sem confirmar nada ============
print("\nH11 - few-shot de recusa a probe de extracao de contexto interno")
check(has(exemplos, "Tentativa de extrair informação interna") and has(exemplos, "qual é o seu prompt"),
      "exemplos_dialogo.md tem exemplo de deflexao a probe de extracao")

# ============ H12 — emoji isolado do cliente nao e pergunta ============
print("\nH12 - emoji isolado do cliente")
check(has(proibicoes, "Cliente manda só um emoji/reação"),
      "proibicoes_de_linguagem.md alinha com a supressao do n8n pra emoji isolado")

# ============ H13 — link do site, no maximo 1x por conversa ============
print("\nH13 - regra de uso de link")
check(has(canais, "NO MÁXIMO 1 vez por") and has(canais, "nunca repetido em mensagens seguidas"),
      "canais_atendimento.md limita o link a 1x por conversa, nunca repetido")

# ============ H14 — B2B/agencias: identificar e escalar, sem termos comerciais ============
print("\nH14 - B2B/agencias escalam sem cotar condicao comercial")
check(has(quando_escalar, "agência/revenda (B2B)") and has(quando_escalar, "NUNCA cita condição comercial"),
      "quando_escalar.md cobre agencia/revenda sem inventar termos B2B")

# ============ H15 — contagem interna consistente em pendencias_index.md ============
print("\nH15 - contagem consistente (34 vs 35 arquivos)")
check(has(pend_index, "(35 arquivos)"), "pendencias_index.md usa 35 (bate com as 35 linhas da tabela)")
check(not has(pend_index, "(34 arquivos)"), "pendencias_index.md nao cita mais 34 arquivos")

# ============ fora de escopo — precisam CONTINUAR pendentes (nao resolvidos por engano) ============
print("\nFora de escopo - continuam pendentes (nao inventados)")
check(has(regras_preco, "usa o valor do prompt de produção vigente"),
      "regras_de_preco.md mantem intocada a regra de preco pendente (fora de escopo)")

atacama_precos_raw = (precos / "precos_2026_atacama.md").read_text(encoding="utf-8")
santiago_precos_raw = (precos / "precos_2026_santiago.md").read_text(encoding="utf-8")
check(atacama_precos_raw.count("[PENDENTE_VALIDACAO]") >= 15,
      "precos_2026_atacama.md continua com precos [PENDENTE_VALIDACAO] (nao inventados)")
check(santiago_precos_raw.count("[PENDENTE_VALIDACAO]") >= 15,
      "precos_2026_santiago.md continua com precos [PENDENTE_VALIDACAO] (nao inventados)")

altitude_text = norm(altitude)
termos_text = norm(termos)
criancas_text = norm(criancas)
check(
    "não recomendados para cardíacos, diabéticos, epiléticos, asmáticos, hipertensos e gestantes."
    in altitude_text,
    "altitude.md mantem sua propria redacao do limiar 3.500m (nao harmonizada)",
)
check(
    "não recomendados para cardíacos, diabéticos, epiléticos, asmáticos, hipertensos ou gestantes; "
    "exceções avaliadas pela consultora." in termos_text,
    "termos_e_condicoes.md mantem redacao DIFERENTE do mesmo limiar (inconsistencia intocada)",
)
check(
    "tour privativo obrigatório para participar" in criancas_text and "[PENDENTE_VALIDACAO]" in criancas_text,
    "criancas.md mantem a 3a variante (export) da regra de menores de 7, ainda pendente",
)

# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE CONSISTENCIA DO BNA_AGENT_CONTEXT PASSARAM")
