# Security

Report vulnerabilities privately through [GitHub vulnerability reporting](https://github.com/neerazz/hermes-cursor-acp/security/advisories/new).
Do not post exploit details, credentials, private prompts, or account data in public issues.
Include the affected commit, Hermes and Cursor CLI versions, impact, and a minimal reproduction with synthetic data.
If private reporting is unavailable, do not publish sensitive details in a bug report.

## Trust boundary

The npm wrapper invokes Hermes for plugin installation and does not handle API keys.
Cursor manages authentication. Prompts, tool schemas, and supplied tool results pass through Cursor's service.
Do not send data your Cursor account or organization is not authorized to process.

The plugin writes a native-tool deny list to `.cursor/cli.json` in a private workspace.
That list relies on Cursor honoring its permissions; it is not an OS sandbox or a guarantee against future CLI changes.
Hermes executes bridged calls through its own tools and approvals. Those tools retain their normal host access.
Keep approvals enabled and use a separate OS account or container when stronger isolation is needed.

`HERMES_CURSOR_ACP_COMMAND` and `CURSOR_AGENT_PATH` select executable code. Set them only to trusted Cursor binaries.
Keep `HERMES_CURSOR_ACP_CWD` separate from project directories: the plugin rewrites that workspace's `.cursor/cli.json`.
Never loosen native-tool permissions to bypass a failed task.

## Safe evidence

Use temporary canary files and disposable workspaces for permission tests.
Redact tokens, account identifiers, sensitive paths, prompts, and tool results before sharing logs.
Offline tests check permission configuration and mocked behavior; only explicit live tests exercise the installed Cursor CLI.
