#!/usr/bin/env node
/**
 * PostToolUse advisory: um teste que usa o app do Conversas precisa conter a
 * string `CONVERSAS_DIR`. O CI (.github/workflows/test.yml) usa exatamente essa
 * string para separar os arquivos em dois jobs — Python 3.12 (Conversas) e 3.11
 * (CRM). Sem ela o teste roda no ambiente errado e so falha depois do push.
 *
 * Advisory: nunca bloqueia (exit 0 sempre), so escreve o aviso no stdout.
 */
import fs from "node:fs";
import path from "node:path";
import { parseHookEvent, readStdinRaw } from "./hook-io.mjs";

const event = parseHookEvent(readStdinRaw()) ?? {};

const file =
  event?.tool_input?.file_path || event?.tool_input?.path || "";
if (typeof file !== "string" || !file) process.exit(0);

const base = path.basename(file);
if (!base.startsWith("test_") || !base.endsWith(".py")) process.exit(0);
if (path.basename(path.dirname(file)) !== "tests") process.exit(0);

let source = "";
try {
  source = fs.readFileSync(file, "utf8");
} catch {
  process.exit(0);
}

// Sinais de que o teste sobe o app do Conversas, e nao o do CRM.
const usesConversas = /conversas/i.test(source);
if (!usesConversas) process.exit(0);
if (source.includes("CONVERSAS_DIR")) process.exit(0);

process.stdout.write(
  `conversas-test-marker: ${base} referencia o Conversas mas nao contem a string ` +
    `\`CONVERSAS_DIR\`. O CI usa essa string para mandar o arquivo ao job Python 3.12; ` +
    `sem ela o teste roda no job do CRM (3.11) com os pins errados. ` +
    `Insira \`conversas/\` no sys.path via uma variavel chamada CONVERSAS_DIR, ` +
    `como fazem os outros testes do subsistema.\n`,
);
process.exit(0);
