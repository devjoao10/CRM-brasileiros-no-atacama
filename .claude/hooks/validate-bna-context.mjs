#!/usr/bin/env node
/**
 * PostToolUse advisory: quando um arquivo de `bna_agent_context/` e editado,
 * roda o validador do vault da Bia (scripts/validate_bna_agent_context.py) e
 * reporta a saida se ele falhar.
 *
 * Advisory: nunca bloqueia (exit 0 sempre). O validador nao acessa rede.
 */
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { parseHookEvent, readStdinRaw } from "./hook-io.mjs";

const event = parseHookEvent(readStdinRaw()) ?? {};

const file = event?.tool_input?.file_path || event?.tool_input?.path || "";
if (typeof file !== "string" || !file) process.exit(0);
if (!/[\\/]bna_agent_context[\\/]/.test(file)) process.exit(0);

// Purpose A: diretorio operacional segue o cwd vivo da sessao.
const cwdArg = typeof event.cwd === "string" && event.cwd ? event.cwd : "";
const projectDir = cwdArg || process.env.CLAUDE_PROJECT_DIR || process.cwd();
const script = path.join(projectDir, "scripts", "validate_bna_agent_context.py");
if (!fs.existsSync(script)) process.exit(0);

const python = process.platform === "win32" ? "python" : "python3";
try {
  execFileSync(python, [script], {
    cwd: projectDir,
    stdio: "pipe",
    windowsHide: true,
    timeout: 20000,
  });
} catch (err) {
  const out = `${err?.stdout ?? ""}${err?.stderr ?? ""}`.trim();
  // Binario ausente / alias da Store no Windows: nao e falha do vault.
  if (err?.code === "ENOENT") process.exit(0);
  process.stdout.write(
    `validate-bna-context: o vault da Bia esta invalido apos esta edicao.\n${out.slice(0, 2000)}\n`,
  );
}
process.exit(0);
