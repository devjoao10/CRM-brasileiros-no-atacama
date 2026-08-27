# BACKUP_RESTORE_VALIDATION.md

A Fase 1 corrigiu `scripts/backup_postgres.sh` e afirmou: *"o script foi
corrigido, mas nenhum restore foi verificado"*. Esta fase transformou isso em
evidência executada — e o resultado desmente a primeira metade da afirmação.

## O que a execução revelou: o script "corrigido" abortava TODO backup real

**Este é o achado mais importante desta fase, e ele é sobre o meu próprio
trabalho da fase anterior.**

### Defeito A — `pipefail` + SIGPIPE invertiam as verificações

Primeira execução do script com um dump válido contendo `COPY public.users`:

```
rc=1   [backup][ERRO] tabela 'users' ausente no dump — abortando
```

Mecanismo isolado, e **reproduzido por mim de forma independente**:

```
gzip -dc ok.gz | grep -qE "COPY (public\.)?users\b"
  status do pipeline com pipefail LIGADO : 141     <- SIGPIPE
  status do pipeline com pipefail DESLIGADO:   0
```

`grep -q` sai no primeiro casamento e mata o `gzip` a montante com SIGPIPE; com
`set -o pipefail`, o status do pipeline vira 141, e o `if ! ...` **inverte o
sentido da guarda**. Vale para todo dump maior que o buffer do pipe (~64 KB) —
ou seja, **todo dump real**.

Consequências, as duas graves:
1. O backup **nunca completava**. O script da Fase 1, tal como entregue, abortaria
   toda execução em produção.
2. O mesmo mecanismo, aplicado à guarda de CR, faria um dump **corrompido passar
   batido** — exatamente a proteção que a Fase 1 dizia ter instalado.

### Defeito B — a guarda anti-CR era decoração fora do Linux

Sobre um dump corrompido com 7.746 CRs em 1 MB:

```
guarda ORIGINAL (grep): status=1     <- NÃO detecta; o corrompido seria PROMOVIDO
guarda NOVA (tr -dc):   crs=7746     <- aborta
```

O `grep` da família Cygwin/MSYS descarta o CR final de cada linha antes de casar
— justamente o CR que o pseudo-TTY produz.

### Por que a Fase 1 não pegou isso

`tests/test_filter_normalization_and_backup.py` verificava o **texto** do script
(`"gzip -t" in sh`, `"trap cleanup EXIT" in sh`), nunca o **executava**. É
exatamente a classe de defeito que esta auditoria mais encontrou — teste que
confirma que uma string existe no arquivo e chama isso de verificação — e ela
estava no meu próprio trabalho. Um teste de grep não pode encontrar um bug de
propagação de status de pipeline.

**Correções aplicadas:** `set +o pipefail` em volta das verificações 2 e 3,
reativado logo depois (o `pipefail` existe para o pipeline do dump, onde a falha
do `pg_dump` precisa derrubar o script); e a guarda de CR passou a usar
`tr -dc '\r' | wc -c`, que conta bytes sem noção de linha e vale nos dois mundos.

## BACKUP GENERATED — sim

`scripts/backup_postgres.sh` executado de ponta a ponta com um `docker` **falso**
no início do PATH, emitindo um dump plain-format realista: 1.196.760 bytes,
`CREATE TABLE` + blocos `COPY public.<t> ... FROM stdin;` para
users/leads/conversations/messages, com acentos UTF-8, hashes bcrypt, telefones e
texto livre.

```
[backup] OK tamanho=74748B checksum=afa9bd17a90881678669e9e4b26f0a6b12ae0558b3d5d7668abc01eeaf39ab1b
```

Verificado: exit 0; exatamente um `.sql.gz`; nenhum `.tmp` remanescente;
`.sha256` com caminho **relativo**; digest confere; e — a prova operacional que
faltava — `sha256sum -c` executado de dentro do `BACKUP_DIR` responde `OK`.

## RESTORE RESULT — validado por dois métodos

1. **Byte a byte.** `gzip.decompress()` do `.sql.gz` devolve exatamente os
   1.196.760 bytes que o `pg_dump` falso emitiu. Igualdade total, zero CR
   introduzido.
2. **Restore de verdade em SQLite.** O teste parseia os blocos `COPY`, cria as
   tabelas com as colunas do cabeçalho, insere linha a linha (split em `\t`,
   terminador `\.`) e consulta o resultado com SQL. Não é comparação de texto:
   os dados são materializados num banco e consultados.

## DATA/SCHEMA VALIDATION

| tabela | esperado | restaurado |
|---|---:|---:|
| users | 25 | 25 |
| leads | 500 | 500 |
| conversations | 300 | 300 |
| messages | 8000 | 8000 |

Nenhuma linha com contagem de colunas divergente. `WHERE col LIKE '%\r%'` em
todas as 18 colunas: **0**. Conteúdo conferido por valor, não só por contagem — e
a **última coluna** de `messages`, que é a que o defeito original sujava, saiu
idêntica ao original.

## Cenários exercitados

| # | esperado | obtido |
|---|---|---|
| 1 | exit 0, `.sql.gz`, `.sha256` relativo, sem `.tmp` | **falhou na 1ª execução** (defeito A); após correção, tudo confere |
| 2 | bytes idênticos, contagens batem | idênticos (1196760 = 1196760), 4/4 tabelas |
| 3 | aborta pela guarda anti-CR | `[backup][ERRO] CR encontrado no dump (7746 em 1MB…)`, rc=1 |
| 4 | aborta por tabela ausente | `[backup][ERRO] tabela 'users' ausente no dump`, rc=1 |
| 5 | aborta em `gzip -t` | `unexpected end of file` + `[backup][ERRO] gzip invalido`, rc=1 |
| 6 | poda só após backup verificado | antigo (30d) podado, recente (2d) e o novo preservados; com dump ruim, rc=1 e **nada** foi podado |
| 7 | trap limpa o parcial | rc=3 propagado, sem `.sql.gz`, sem `.tmp`, sem `.sha256` |

## Teste criado

`tests/test_backup_restore_e2e.py` — script autônomo, **45 checks**, exit 0. Tudo
em `scratch/backup_e2e/`, apagado no início de cada execução. Sem `bash` ou sem
ferramenta POSIX ele **reprova com mensagem explícita**, nunca faz skip.

O teste tem dentes comprovados: contra o script anterior deu **9 falhas**, e foi
assim que o defeito A apareceu.

Um detalhe que quase virou falso verde: o cenário 3 passou por acidente na
primeira rodada porque a verificação procurava a string `"CR"` na saída — e o
caminho do repositório contém "**CR**M". Hoje exige a frase `CR encontrado`.

## Limitações

- **Permissões não são verificáveis nesta máquina.** O `H:` é montado `noacl`: o
  `chmod` retorna 0, o `stat` do mesmo processo mostra 700/600, e outro processo
  lê 755/644 — nada foi gravado. O teste sonda isso gravando num processo e lendo
  em outro; quando a sonda falha, ele diz em voz alta e verifica que o script
  continua mandando fechar (`umask 077`, `chmod 600/700`). Na VPS Linux vale.
- **Não há PostgreSQL real.** O que não foi provado: que o `pg_dump` de verdade
  produz este formato, que `psql -f` reconstrói constraints, índices, sequences e
  FKs, e que extensões e ownership voltam.
- **Continua sendo questão de operador:** upload offsite (o backup mora no mesmo
  host do volume do banco), agendamento versionado (o cron é comentário), e o
  restore contra PostgreSQL real.

## Comando do operador — restore de verdade

Na VPS, contra um banco **descartável**, nunca o de produção. Note o `-i` **sem**
`-t` — a mesma armadilha do TTY, no sentido inverso:

```bash
BKP=/opt/backups/bna-postgres/bna_postgres_YYYYmmdd_HHMMSS.sql.gz
( cd "$(dirname "$BKP")" && sha256sum -c "$(basename "$BKP").sha256" )
docker exec -i crm_postgres psql -U crm_user -d postgres -c 'CREATE DATABASE restore_teste;'
gzip -dc "$BKP" | docker exec -i crm_postgres psql -v ON_ERROR_STOP=1 -U crm_user -d restore_teste
for t in users leads conversations messages; do
  printf '%-15s prod=%s restore=%s\n' "$t" \
    "$(docker exec crm_postgres psql -tAU crm_user -d crm_atacama   -c "select count(*) from $t")" \
    "$(docker exec crm_postgres psql -tAU crm_user -d restore_teste -c "select count(*) from $t")"
done
docker exec -i crm_postgres psql -U crm_user -d postgres -c 'DROP DATABASE restore_teste;'
```

## Consequência para os backups já existentes

Não muda: **todo backup gerado antes desta branch continua suspeito**, agora por
dois motivos em vez de um. Se algum foi gerado com o script da Fase 1 tal como
estava, ele nem existe — o script abortava. Se foi gerado com o script original
(com `-t`), está corrompido com CRLF. Trate ambos como não confiáveis até que um
restore contra PostgreSQL real prove o contrário.
