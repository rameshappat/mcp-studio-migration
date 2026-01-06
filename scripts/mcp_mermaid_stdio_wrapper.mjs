#!/usr/bin/env node
/**
 * Wrapper for mcp-mermaid stdio servers that (incorrectly) print human logs to stdout.
 *
 * The MCP Python stdio client expects stdout to be JSON-RPC messages only.
 * This wrapper runs `npx -y mcp-mermaid` and forwards only JSON-looking lines
 * (starting with '{' or '[') to stdout. Everything else goes to stderr.
 */

import { spawn } from "node:child_process";

const child = spawn(
  "npx",
  ["-y", "mcp-mermaid", ...process.argv.slice(2)],
  {
    stdio: ["pipe", "pipe", "pipe"],
    env: process.env,
  }
);

// Forward stdin to the child
process.stdin.pipe(child.stdin);

let stdoutBuf = "";
child.stdout.setEncoding("utf8");
child.stdout.on("data", (chunk) => {
  stdoutBuf += chunk;

  let idx;
  while ((idx = stdoutBuf.indexOf("\n")) !== -1) {
    const line = stdoutBuf.slice(0, idx + 1);
    stdoutBuf = stdoutBuf.slice(idx + 1);

    const trimmed = line.trimStart();
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      process.stdout.write(line);
    } else if (trimmed.length > 0) {
      process.stderr.write(line);
    }
  }
});

child.stdout.on("end", () => {
  const trimmed = stdoutBuf.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    process.stdout.write(stdoutBuf);
  } else if (trimmed.length > 0) {
    process.stderr.write(stdoutBuf);
  }
});

// Always forward child stderr to our stderr
child.stderr.pipe(process.stderr);

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
  }
  process.exit(code ?? 1);
});
