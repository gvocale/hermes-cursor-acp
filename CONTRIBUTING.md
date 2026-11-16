# Contributing

Keep changes small enough to reproduce and review. Bug fixes should include a regression test that fails before the fix.
Discuss new behavior in an issue first, especially model selection, tool routing, or permissions.

## Local setup

Use macOS or Linux with Python 3.11+ and Node.js 18+. From a fresh clone:

```bash
git clone https://github.com/neerazz/hermes-cursor-acp.git
cd hermes-cursor-acp
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
git clone https://github.com/NousResearch/hermes-agent.git .hermes-agent
git -C .hermes-agent checkout 3b421578588243f920886e6fe79a6faa87ea026e
export PYTHONPATH="$PWD/.hermes-agent:$PWD"
```

Hermes is a source dependency: installing this plugin alone does not supply its ACP shim.
The checkout above pins Hermes v0.23.1, matching `.github/workflows/ci.yml`.
Record that commit (`git -C .hermes-agent rev-parse HEAD`) in your PR when reporting compatibility results.

## Offline checks

```bash
unset CURSOR_LIVE
python -m pytest tests -q -o addopts= --cov=. --cov-report=term-missing
node --check bin/cli.js
node --check scripts/next-version.js
node bin/cli.js help
npm pack --dry-run
```

The default tests mock ACP responses and use local fake executables. They need no Cursor login or API keys.
They check parsing, model resolution, executable selection, and permission configuration, not real model reliability.
`npm pack --dry-run` checks package contents; it does not publish anything.
Do not add credentials or enable live tests in pull-request CI.
CI runs these checks on Linux and macOS with Python 3.11/Node 18 and Python 3.12/Node 24.
The provider's line-coverage floor is 90%; coverage is not evidence that every model works.

## Where to change things

| Change | Owner |
| --- | --- |
| Discovery, ACP model selection, tool bridge or native-tool fence | `__init__.py` and `tests/test_cursor_acp.py` |
| Registry, credential lookup and picker compatibility | `tests/test_registry_consumer.py` (isolated home; only ACP transport is scripted) |
| npm installation or diagnostics | `bin/cli.js` and `tests/test_cli.py` |
| Offline checks or publication | `.github/workflows/ci.yml` or `.github/workflows/release.yml` |

## Live checks (explicit opt-in)

Live tests contact Cursor, send prompts, and may consume paid subscription quota or incur charges under your plan.
Use a test account and non-sensitive files. Authenticate with the selected Cursor CLI (`cursor-agent login` by default).
With the virtual environment and `PYTHONPATH` above still active:

```bash
CURSOR_LIVE=1 python -m pytest tests -q -o addopts= -k test_live_
```

Some live tests name specific models. For the configurable two-step test, use a bare family (the text before `[` in an ID from Hermes' live ACP catalog):

```bash
CURSOR_LIVE=1 CURSOR_LIVE_MODELS="MODEL_NAME" CURSOR_LIVE_TRIALS=1   python -m pytest tests -q -o addopts= -k live_two_step
```

Replace `MODEL_NAME`; CLI marketing aliases are not necessarily ACP model IDs.
Report the model, Cursor version, trial count, and failures. A skipped live test is not evidence of live compatibility.

## Change boundaries

Preserve bare model names. Exact ACP variant requests must not silently select a different variant.
Discovery must keep complete ACP IDs, including variant parameters; deduplicate exact IDs only.
Do not add a stale fallback catalog when live discovery fails.
Keep Python and npm executable lookup consistent, with explicit overrides before `cursor-agent` and legacy `agent` last.
Cursor's native tool deny list must remain in place; never loosen it to make a test pass.
It is not an OS sandbox. Changes to permissions or tool routing need failure-path tests and a security explanation.
The npm wrapper delegates installation to Hermes and must not collect credentials.

## Pull requests

Fill out the PR template with the user-visible change and actual check results.
Include failure cases, supported-platform effects, and docs changes; explain checks you did not run.
Keep unrelated cleanup out. Do not include tokens, account details, private prompts, or unredacted tool output.
Maintainers handle publishing; opening a PR does not require a version bump, npm credentials, or a release.
`main` requires pull requests and one approval; only repository owner `neerazz` is exempt from the review and update-access restrictions.
Other accounts retain those restrictions. Authors cannot approve their own PRs.
Required CI, up-to-date branches, resolved conversations, and code-quality checks still apply to everyone, including the owner; force pushes and branch deletion remain blocked.

After merge, a maintainer explicitly runs the `release` workflow on `main` and selects `registry`: `npm` (default) or `github`.
A push alone does not publish. Both targets require successful CI for the selected commit.
The `npm` target publishes the unscoped `hermes-cursor-acp` package and creates its tag and GitHub release.
The `github` target publishes `@neerazz/hermes-cursor-acp` to GitHub Packages using `GITHUB_TOKEN` with `packages: write`; it does not publish to npm or create a tag or GitHub release.
For GitHub Packages, the workflow chooses a version above the latest npm and GitHub versions and changes only the runner's manifest, preserving repository, homepage, license, description, and executable metadata.
The source package stays unscoped; GitHub package downloads require separate npm authentication as described in the [README](README.md#github-packages-optional).
For sensitive reports, use [SECURITY.md](SECURITY.md), not a public issue.
