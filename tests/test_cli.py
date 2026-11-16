"""Exercise the shipped Node entrypoint with disposable executables, never a login."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(tmp_path):
    node = shutil.which("node")
    assert node, "Node is required to test the npm entrypoint"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls"
    env = {"HOME": str(tmp_path), "PATH": f"{bindir}:/usr/bin:/bin", "CALL_LOG": str(log)}

    def executable(name, body):
        target = bindir / name
        target.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$CALL_LOG"\n' + body + "\n")
        target.chmod(0o755)
        return target

    def run(*args, extra_env=None):
        return subprocess.run([node, str(ROOT / "bin/cli.js"), *args],
                              env={**env, **(extra_env or {})}, capture_output=True, text=True, timeout=15)

    return executable, run, log


@pytest.mark.parametrize("command", ["help", "--help", "-h"])
def test_help_needs_no_hermes_or_cursor(cli, command):
    _, run, log = cli
    result = run(command)
    assert result.returncode == 0 and "Usage:" in result.stdout
    assert not log.exists()


def test_unknown_command_is_an_error(cli):
    _, run, _ = cli
    result = run("made-up-command")
    assert result.returncode == 2


def test_missing_hermes_is_actionable(cli):
    _, run, _ = cli
    result = run("status")
    assert result.returncode != 0 and "Hermes Agent not found" in result.stderr


@pytest.mark.parametrize("command", ["status", "install"])
def test_missing_cursor_is_not_reported_as_success(cli, command):
    executable, run, log = cli
    executable("hermes", "exit 0")
    result = run(command)
    assert result.returncode != 0 and "Cursor CLI" in result.stderr
    assert not log.exists(), "Do not install or inspect plugins before finding Cursor"


@pytest.mark.parametrize("command", ["status", "install"])
def test_broken_cursor_override_never_uses_another_agent(cli, command):
    executable, run, log = cli
    executable("hermes", "exit 0")
    executable("cursor-agent", "exit 0")
    result = run(command, extra_env={"HERMES_CURSOR_ACP_COMMAND": "/missing/cursor"})
    assert result.returncode != 0
    assert "Done!" not in result.stdout
    assert "authenticated and active" not in result.stdout
    assert "cursor-agent" not in (log.read_text() if log.exists() else "")


@pytest.mark.parametrize("command", ["status", "install"])
def test_cursor_discovery_failure_is_actionable(cli, command):
    executable, run, _ = cli
    executable("hermes", "exit 0")
    executable("cursor-agent", "exit 9")
    result = run(command)
    assert result.returncode != 0
    assert "login" in result.stderr.lower()
    assert "Done!" not in result.stdout


def test_install_passes_pin_and_force_to_hermes(cli):
    executable, run, log = cli
    executable("hermes", "exit 0")
    executable("cursor-agent", "exit 0")
    sha = "a" * 40
    result = run("install", sha, "--force")
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert any(f"--ref {sha}" in line and "--force" in line and "plugins install" in line for line in calls)
    assert any(line.endswith("cursor-agent models") for line in calls)


@pytest.mark.parametrize("pin", ["abc1234", "g" * 40, "a" * 39, "a" * 41])
def test_install_rejects_invalid_pin_before_running_any_command(cli, pin):
    _, run, log = cli
    result = run("install", pin)
    assert result.returncode != 0
    assert "40" in result.stderr and "SHA" in result.stderr
    assert not log.exists()


def test_model_listing_does_not_claim_login_or_acp_access(cli):
    executable, run, _ = cli
    executable("hermes", "exit 0")
    executable("cursor-agent", "printf 'Available models\\n'; exit 0")
    result = run("install")
    assert result.returncode == 0, result.stderr
    assert "authenticated" not in result.stdout.lower()
    assert "verify ACP access" in result.stdout


def test_status_finds_legacy_cursor_outside_sparse_path(cli):
    executable, run, log = cli
    executable("hermes", "exit 0")
    legacy = executable("agent", "printf 'LEGACY_CURSOR_OK\\n'")
    local = log.parent / ".local" / "bin"
    local.mkdir(parents=True)
    legacy.rename(local / "agent")
    result = run("status")
    assert result.returncode == 0, result.stderr
    assert "LEGACY_CURSOR_OK" in result.stdout


def test_failed_hermes_install_does_not_claim_success(cli):
    executable, run, log = cli
    executable("hermes", "exit 7")
    executable("cursor-agent", "exit 0")
    result = run("install")
    assert result.returncode != 0 and "plugins install" in result.stderr
    assert "Done!" not in result.stdout
    assert not any(line.endswith("cursor-agent models") for line in log.read_text().splitlines())


def test_uninstall_removes_only_this_plugin_and_preserves_exit_code(cli):
    executable, run, log = cli
    executable("hermes", "exit 7")
    result = run("uninstall")
    assert result.returncode == 7
    assert log.read_text().strip().endswith("hermes plugins remove cursor-acp")


def test_package_includes_the_cli_entrypoint():
    package = json.loads((ROOT / "package.json").read_text())
    assert package["bin"]["hermes-cursor-acp"] == "bin/cli.js"
    assert "bin/cli.js" in package["files"]
    assert "__init__.py" not in package["files"], "Hermes installs Python from Git, not npm"
