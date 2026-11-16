#!/usr/bin/env node
// npx hermes-cursor-acp [install|status|uninstall]
// Thin wrapper over the official CLIs: `hermes plugins install` and `cursor-agent`.
// No credentials or tokens pass through this script.
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "package.json"), "utf8"));
let REPO = pkg.repository?.url || "https://github.com/neerazz/hermes-cursor-acp";
if (REPO.startsWith("git+")) REPO = REPO.slice(4);
if (REPO.endsWith(".git")) REPO = REPO.slice(0, -4);
const PLUGIN = pkg.name.replace(/^hermes-/, "");
const CURSOR_INSTALLER = "https://cursor.com/install";
const HERMES_DOCS = "https://hermes-agent.nousresearch.com/docs";

const [, , cmd = "install", ...rest] = process.argv;
const flags = new Set(rest);
const yes = flags.has("--yes") || flags.has("-y");

function which(bin) {
  const r = spawnSync(process.platform === "win32" ? "where" : "which", [bin], { encoding: "utf8" });
  return r.status === 0 ? r.stdout.split(/\r?\n/)[0].trim() : null;
}

function run(bin, args, opts = {}) {
  console.log(`$ ${bin} ${args.join(" ")}`);
  const r = spawnSync(bin, args, { stdio: "inherit", shell: process.platform === "win32", ...opts });
  if (r.error) throw r.error;
  return r.status ?? 1;
}

function die(msg, code = 1) {
  console.error(`\n✗ ${msg}`);
  process.exit(code);
}

function requireHermes() {
  if (!which("hermes")) {
    die(`Hermes Agent not found on PATH. Install it first: ${HERMES_DOCS}`);
  }
}

function cursorCommand() {
  for (const name of ["HERMES_CURSOR_ACP_COMMAND", "CURSOR_AGENT_PATH"]) {
    const value = (process.env[name] || "").trim();
    if (value) return value.startsWith("~/") ? path.join(os.homedir(), value.slice(2)) : value;
  }
  for (const name of ["cursor-agent", "agent"]) {
    const found = which(name);
    if (found) return found;
    const local = path.join(os.homedir(), ".local", "bin", name);
    try {
      if (fs.statSync(local).isFile()) {
        fs.accessSync(local, fs.constants.X_OK);
        return local;
      }
    } catch (_) { /* Not installed in the default location. */ }
  }
  return null;
}

async function ensureCursorCli() {
  const existing = cursorCommand();
  if (existing) return existing;
  if (!yes) {
    die(`Cursor CLI (agent) not found.\n  Install it with:\n    curl ${CURSOR_INSTALLER} -fsS | bash\n  or re-run with --yes to install it now.`);
  }
  console.log(`Installing Cursor CLI from ${CURSOR_INSTALLER}...`);
  const code = run("bash", ["-c", `curl ${CURSOR_INSTALLER} -fsS | bash`]);
  const installed = cursorCommand();
  if (code !== 0 || !installed) {
    die("Cursor CLI installation failed or cursor-agent is not on PATH. Open a new terminal and re-run.");
  }
  return installed;
}

async function install() {
  const positional = rest.filter((arg) => !["--yes", "-y", "--force"].includes(arg));
  if (positional.length > 1 || positional.some((arg) => !/^[0-9a-f]{40}$/.test(arg))) {
    die("Pass one full 40-character lowercase hexadecimal commit SHA, or omit the pin. Supported flags: --yes, -y, --force.");
  }
  const ref = positional[0];
  requireHermes();
  const cursor = await ensureCursorCli();
  const args = ["plugins", "install", REPO, "--enable"];
  if (ref) args.push("--ref", ref);
  if (flags.has("--force")) args.push("--force");

  console.log(`Installing ${PLUGIN} into Hermes...`);
  if (run("hermes", args) !== 0) die("`hermes plugins install` failed (see output above).");

  console.log("\nVerifying Cursor CLI connection...");
  const modelsCheck = spawnSync(cursor, ["models"], { encoding: "utf8" });
  if (modelsCheck.status !== 0) {
    die(`Plugin installed, but Cursor model discovery failed. Check ${cursor} and run \`${cursor} login\`; then retry status.`);
  } else {
    console.log("Cursor CLI model listing is accessible. Use /model in Hermes to verify ACP access.");
  }

  console.log("\nPlugin installation finished. Select 'Cursor Subscription (ACP)' from /model inside Hermes.");
}

function status() {
  requireHermes();
  console.log("Hermes: " + (which("hermes") || "not found"));
  const cursor = cursorCommand();
  console.log("Cursor CLI: " + (cursor || "not found"));
  if (!cursor) die("Cursor CLI not found. Install Cursor or set HERMES_CURSOR_ACP_COMMAND.");
  console.log("\nChecking Cursor models:");
  if (run(cursor, ["models"]) !== 0) die("Cursor model discovery failed; check the CLI and login.");
  console.log("\nHermes plugin status:");
  process.exit(run("hermes", ["plugins", "list"]));
}

function uninstall() {
  requireHermes();
  process.exit(run("hermes", ["plugins", "remove", PLUGIN]));
}

function help() {
  console.log(`hermes-cursor-acp — Cursor Subscription models for Hermes Agent

Usage: npx hermes-cursor-acp [command] [flags]

Commands:
  install     Install + enable the Hermes plugin, verify Cursor CLI (default)
  status      Show CLI and plugin status
  uninstall   Remove the plugin from Hermes

Flags:
  --yes, -y      Install the Cursor CLI automatically if missing
  --force        Reinstall over an existing copy
  <40-hex sha>   Pin the plugin to an exact commit

Platform: ${os.platform()} ${os.arch()}`);
}

const commands = { install, status, uninstall, help, "--help": help, "-h": help };
Promise.resolve((commands[cmd] || (() => { help(); process.exit(2); }))()).catch((err) => die(err.message || String(err)));
