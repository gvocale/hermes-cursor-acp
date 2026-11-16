# hermes-cursor-acp

Use Cursor subscription models in [Hermes Agent](https://github.com/NousResearch/hermes-agent) through the Cursor CLI's ACP server.
Hermes keeps its tools, approvals, memory, and skills. Cursor supplies the model.

## Install

Requires macOS or Linux, Python 3.11+, Node.js 18+, Hermes on `PATH`, and a Cursor account with model access.
Install [Hermes](https://hermes-agent.nousresearch.com/docs) and the [Cursor CLI](https://cursor.com/cli) first, then:

```bash
cursor-agent login
npx hermes-cursor-acp
```

The npm command delegates plugin installation and enablement to Hermes; it does not handle API keys.
Cursor manages your login. Older Cursor installations may use `agent` instead of `cursor-agent`.
For a direct install without npm:

```bash
hermes plugins install https://github.com/neerazz/hermes-cursor-acp --enable
```

## GitHub Packages (optional)

`npx hermes-cursor-acp` from npm remains the primary install and needs no GitHub login.
The [GitHub package](https://github.com/neerazz/hermes-cursor-acp/pkgs/npm/hermes-cursor-acp) uses the separate name `@neerazz/hermes-cursor-acp`.
GitHub requires authentication even for public package downloads: use a personal access token (classic) with `read:packages` as the password when prompted.
Authenticate through npm, then run either scoped command:

```bash
npm login --scope=@neerazz --auth-type=legacy --registry=https://npm.pkg.github.com
npx --registry=https://npm.pkg.github.com @neerazz/hermes-cursor-acp
# Equivalent npm exec command:
npm exec --registry=https://npm.pkg.github.com --package=@neerazz/hermes-cursor-acp -- hermes-cursor-acp
```

Keep the token in npm's authentication configuration; never pass it to the plugin or commit it.
The plugin does not collect GitHub credentials. See [GitHub's npm registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-npm-registry) for authentication details.

## Choose a model

Open `/model` in Hermes and choose **Cursor Subscription (ACP)**.
The plugin discovers exact model IDs from the live ACP catalog, including variant parameters; it does not maintain a fixed model list.
Use an ID shown there with `hermes --provider cursor-acp -m 'MODEL_ID'`, replacing `MODEL_ID` and keeping the quotes.
Existing bare model names still select the first advertised variant. A qualified ID must match exactly; unknown IDs fail instead of silently selecting another variant.
CLI `models` output can contain marketing aliases that ACP does not accept.
If discovery fails, the plugin warns and returns an empty list rather than inventing fallback models.

## What works, and what does not

Hermes executes bridged tool calls under its own approvals. Some models refuse the bridge or emit unusable calls; task completion is not guaranteed.
Cursor's native tools are denied through `.cursor/cli.json` in a private workspace.
This is a CLI permission boundary, **not an OS sandbox**, and future Cursor changes could break it.
Conversation text, tool schemas, and tool results sent to the model pass through Cursor.
Each turn starts a new ACP session, which adds latency. Cursor provides no token or cost counts, so Hermes usage reports undercount.

## Troubleshoot

```bash
npx hermes-cursor-acp status
cursor-agent models
```

`status` shows the selected executable and plugin state; `models` checks CLI access, not ACP ID compatibility.
Executable lookup order: `HERMES_CURSOR_ACP_COMMAND`, `CURSOR_AGENT_PATH`, `cursor-agent` on `PATH`, `~/.local/bin/cursor-agent`, legacy `agent` on `PATH`, then `~/.local/bin/agent`.
If `agent` belongs to another product, set `HERMES_CURSOR_ACP_COMMAND` to the absolute Cursor CLI path before running the installer and Hermes.
For an empty catalog, confirm that executable, run its `login` command, and retry discovery in Hermes.
Do not allow Cursor native tools to work around a bridge failure; report a redacted reproduction instead.

| Optional environment variable | Default / effect |
| --- | --- |
| `HERMES_CURSOR_ACP_ARGS` | `acp`; arguments passed to the Cursor CLI |
| `HERMES_CURSOR_ACP_MODE` | `agent`; Cursor session mode |
| `HERMES_CURSOR_ACP_CWD` | `$HERMES_HOME/cache/cursor-acp-workspace`; keep this separate from your projects because the plugin rewrites its `.cursor/cli.json` |

Remove the plugin with `npx hermes-cursor-acp uninstall`.

## Contribute or report a problem

See [CONTRIBUTING.md](CONTRIBUTING.md) for local tests and pull requests.
Use [bug reports and feature requests](https://github.com/neerazz/hermes-cursor-acp/issues/new/choose) for non-sensitive issues.
Report vulnerabilities privately through [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE).
