"""
BIA-V2 Fase 0 / Task 0.3 - guarda de regressao da V1.

Familia A (IMPLEMENTADA): hash SHA-256 dos 8 arquivos V1 explicitamente
enumerados pela Task 0.3 do plano como baseline imutavel
(docs/superpowers/plans/2026-08-29-bia-v2.md, secao "Task 0.3"). Nenhum
codigo de app e importado aqui - so leitura de arquivo e comparacao de hash.

Familia B (DIFERIDA ate a Fase 4): ver o bloco proprio mais abaixo. Este
arquivo NAO implementa as 5 provas de independencia semantica agora - so
um tripwire que falha no instante em que a Fase 4 tornar essas provas
possiveis, para que a ausencia de Familia B nunca passe despercebida.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_v2_v1_intacta.py
"""
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONVERSAS_DIR = ROOT / "conversas"

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


def _sha256_normalizado(caminho: pathlib.Path) -> str:
    """SHA-256 dos bytes do arquivo (lido em modo binario) apos normalizar
    fim-de-linha CRLF -> LF. Ver justificativa no comentario da Familia A."""
    dados = caminho.read_bytes()
    return hashlib.sha256(dados.replace(b"\r\n", b"\n")).hexdigest()


# ============================================================================
# FAMILIA A - imutabilidade de arquivo (IMPLEMENTADA)
# ----------------------------------------------------------------------------
# Estes sao os 8 arquivos explicitamente protegidos por hash na Task 0.3.
# Nenhum pode mudar enquanto a V2 for construida em paralelo.
#
# ESCOPO DESTA BASELINE - leia antes de concluir cobertura:
# A Global Constraint e mais ampla que esta baseline de 8 arquivos. Nesta task,
# mudancas V1 fora desta lista continuam proibidas e sao detectadas pelos gates
# de diff/review; ampliar a guarda de hash exige auditoria propria do conjunto
# completo, nao inclusao ad hoc de dependencias isoladas.
#
# POR QUE HASH NORMALIZADO (CRLF -> LF) EM VEZ DO BYTE CRU:
# medido em HEAD 22f9fb24f117197bb59c7248ce88beb6acebcff7: os 8 blobs
# gravados no git sao puramente LF. Este repo tem `core.autocrlf=true` e
# NENHUM `.gitattributes`; os dois jobs do CI (.github/workflows/test.yml)
# rodam em `ubuntu-latest`, onde o checkout entrega o byte do blob tal como
# esta - LF. Neste working tree Windows, porem, 5 dos 8 arquivos
# (outbound.py, webhook.py, conversations.py, leads.py, lead_creation.py)
# ja estao materializados em CRLF no disco; os outros 3 ja estao em LF -
# sem que `git status` acuse nada, porque o git normaliza a comparacao na
# hora de decidir se ha mudanca. Um baseline de byte CRU travaria esses 5
# arquivos em TODA rodada de CI, por um motivo que nao e ninguem ter tocado
# a V1 - e nem seria estavel localmente, ja que o fim-de-linha em disco pode
# ser reescrito por qualquer ferramenta sem isso ser uma mudanca de verdade.
# O preco pago: esta guarda NAO detecta uma troca PURA de fim-de-linha sem
# nenhuma outra alteracao. E um preco aceitavel - o proprio git ja nao trata
# isso como mudanca, nao altera semantica Python nenhuma, e a alternativa e
# uma guarda que grita falso-positivo em toda rodada de CI ate alguem
# desativa-la.
#
# ATUALIZACAO DO BASELINE E MANUAL E DELIBERADA. Uma mudanca aprovada na V1
# exige recalcular o digest (mesma formula acima) e colar o valor aqui a
# mao, por decisao humana. NUNCA adicione um jeito de regenerar isto sozinho
# - nada de flag --update, env var, ou "se nao bate, grava o atual": isso
# transformaria a guarda em um no-op disfarcado.
_PROTEGIDOS = (
    (CONVERSAS_DIR / "app/services/atendimento.py",
     "e7031041d8d302184e1a161b68bef861e34f3e28f1ddd85e37802929aaf95360"),
    (CONVERSAS_DIR / "app/services/outbound.py",
     "02ad2c4e2f4c07e5a4933adb333a02ddc516d197e01106b1d4710fe0caf6dd77"),
    (CONVERSAS_DIR / "app/routers/webhook.py",
     "5b5c23184a28ab293247dff4371675fc0e73ea31603e22db39755b92c7d95f90"),
    (CONVERSAS_DIR / "app/routers/conversations.py",
     "0d7463e2128862cc5260481b9826e29c56b8e8294895cca5ef5b8555636d8e6f"),
    (CONVERSAS_DIR / "app/services/crm.py",
     "2d96421cd1421bb73f2dbd6ced73e188f4b2df9cd8a9f496791f71db14316ac5"),
    (ROOT / "app/routers/leads.py",
     "2c64ba21f835dbf0ff69850de236c282b355afd90e3822cd927b3863a9a1bce4"),
    (ROOT / "app/services/lead_creation.py",
     "cad719206f354a51c3273c6995f0d2832b3d58e1629b6b19d8b4f56abc2a4b58"),
    (ROOT / "app/services/conversas_bridge.py",
     "37ead9905fd497d0f8650a75c57e1f546a70182b579df2c519c0f127b4725ed4"),
)

for caminho, esperado in _PROTEGIDOS:
    if not caminho.is_file():
        check(False, f"ARQUIVO AUSENTE (nao e divergencia de hash): {caminho}")
        continue
    atual = _sha256_normalizado(caminho)
    check(atual == esperado, f"{caminho}: esperado={esperado} atual={atual}")


# ============================================================================
# FAMILIA B - DIFERIDA ate a Fase 4 (ver Task 0.3 do plano)
# ----------------------------------------------------------------------------
# As 5 provas abaixo SO fazem sentido depois que `state`, `state_version` e
# `closed_at` existirem em algum lugar (Fase 4). Escreve-las agora daria
# PASS falso: passariam so porque as colunas nao existem em lugar nenhum,
# nao porque a independencia semantica foi provada. NAO IMPLEMENTADO aqui
# de proposito - so um tripwire que verifica as duas pre-condicoes validas
# na Fase 0-3 e FALHA no instante em que a Fase 4 chegar.
#
# As 5 provas que a Fase 4 exige (NAO implementadas neste arquivo, apenas
# documentadas para quem for ativa-las):
#   B.1 grep por state / state_version / closed_at nos 8 arquivos da
#       Familia A: zero ocorrencias fora de comentario (regex de palavra
#       inteira - status e _STATUS_ABERTOS nao contam).
#   B.2 GET /api/conversations/{id} sobre conversa com state='AI_TRIAGE',
#       state_version=7, closed_at preenchido NAO expoe essas 3 chaves no
#       JSON. Ao implementar, mire a classe que esta de fato no @router.get:
#       o response_model dessa rota e ConversationDetailWithWindow, declarada
#       no proprio router (conversas/app/routers/conversations.py:110), e nao
#       ConversationResponse de schemas/conversation.py - aquela e a base da
#       cadeia. Nenhuma classe da cadeia declara os 3 campos hoje, entao a
#       conclusao vale; so o alvo do teste precisa ser a subclasse certa.
#   B.3 a V1 opera normalmente (listar, claim, assign, release, enviar
#       mensagem, encerrar) sobre uma linha state='AI_TRIAGE', com o mesmo
#       resultado de uma linha LEGACY.
#   B.4 apos claim+assign+release+envio pela V1, state_version permanece
#       INALTERADO (prova do risco R8 - deve falhar se alguem "resolver"
#       R8 sem novo desenho).
#   B.5 uma linha state='LEGACY' com atendente_id != responsavel_id,
#       is_bot_active=True e queued_at=NULL e aceita pelo banco (CHECK
#       inerte para LEGACY).
_MODEL_CONVERSATION = CONVERSAS_DIR / "app/models/conversation.py"
_MIGRATIONS_DIR = ROOT / "migrations"
_COLUNAS_V2 = ("state", "state_version", "closed_at")
# Aceita os dois estilos de declaracao do SQLAlchemy: o classico
# `state = Column(...)`, unico usado no projeto hoje, e o 2.0
# `state: Mapped[str] = mapped_column(...)`. Reconhecer so o primeiro deixaria
# o tripwire MUDO se a Fase 4 mudasse de convencao - reportando "nenhuma
# coluna V2 declarada" com as colunas ja existindo, que e exatamente o falso
# PASS que este bloco existe para impedir. O `\b` depois do grupo garante que
# `status = Column(...)` (campo real da V1) nunca casa.
_PADRAO_COLUNA_V2 = re.compile(
    r"(?m)^\s*\b(" + "|".join(_COLUNAS_V2) + r")\b\s*(?::[^=\n]+)?=\s*"
    r"(?:mapped_column|Column)\("
)

# Qualquer migration numerada acima da m013 - nao so m014/m015. Travar nos dois
# numeros que o plano preve hoje deixaria o tripwire mudo se a Fase 4 saisse com
# outra numeracao, ou se outra migration tomasse esses numeros antes dela.
aviso_fase4 = (
    "FASE 4 CHEGOU: Familia B (B.1-B.5, ver comentario acima) precisa ser "
    "IMPLEMENTADA e ATIVADA agora - nunca silenciada nem apagada deste arquivo."
)

# Ausencia do model e FAIL proprio, e NAO um "nenhuma coluna declarada": sem o
# arquivo o tripwire nao foi avaliado, e dizer OK aqui seria afirmar uma leitura
# que nao aconteceu.
if not _MODEL_CONVERSATION.is_file():
    check(False, f"ARQUIVO AUSENTE (tripwire de colunas nao pode ser avaliado): {_MODEL_CONVERSATION}")
else:
    texto_model = _MODEL_CONVERSATION.read_text(encoding="utf-8")
    colunas_v2_ja_declaradas = sorted(set(_PADRAO_COLUNA_V2.findall(texto_model)))
    if colunas_v2_ja_declaradas:
        msg_tripwire_colunas = f"{aviso_fase4} colunas ja declaradas: {colunas_v2_ja_declaradas}"
    else:
        msg_tripwire_colunas = (
            f"tripwire Familia B: nenhuma coluna V2 declarada ainda em "
            f"{_MODEL_CONVERSATION.name} - B.1-B.5 seguem DIFERIDOS (nao verificados)"
        )
    check(not colunas_v2_ja_declaradas, msg_tripwire_colunas)

# Mesma razao do bloco de colunas acima, com um gotcha proprio:
# `Path("inexistente").glob(...)` devolve lista vazia SEM levantar.
#
# A mensagem de disparo NAO reusa `aviso_fase4`: uma migration acima da m013
# prova que o SCHEMA se moveu, nao que a Fase 4 chegou. As duas coisas coincidem
# no caminho planejado (m014/m015 sao dela), mas uma migration nao relacionada
# tomando esse numero faria a mensagem afirmar uma fase que nao chegou.
if not _MIGRATIONS_DIR.is_dir():
    check(False, f"DIRETORIO AUSENTE (tripwire de migrations nao pode ser avaliado): {_MIGRATIONS_DIR}")
else:
    migracoes_alem_da_m013 = sorted(
        p.name for p in _MIGRATIONS_DIR.glob("m[0-9][0-9][0-9]_*.py")
        if int(p.name[1:4]) > 13
    )
    if migracoes_alem_da_m013:
        msg_tripwire_migrations = (
            f"SCHEMA MUDOU: migration nova alem da m013 ({migracoes_alem_da_m013}). "
            f"Se sao as colunas da Fase 4, a Familia B (B.1-B.5, ver comentario acima) "
            f"precisa ser IMPLEMENTADA e ATIVADA agora - nunca silenciada. Se e outra "
            f"coisa, reavalie a Familia B e atualize este tripwire por decisao humana."
        )
    else:
        msg_tripwire_migrations = "tripwire Familia B: nenhuma migration alem da m013 existe ainda"
    check(not migracoes_alem_da_m013, msg_tripwire_migrations)


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
