"""Offline registry-to-picker regression without the user's installed plugin or login."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def test_isolated_registry_resolves_legacy_local_cli_and_refreshes_catalog(tmp_path):
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    plugins = hermes_home / "plugins"
    plugins.mkdir(parents=True)
    (plugins / "cursor-acp").symlink_to(ROOT, target_is_directory=True)
    legacy = home / ".local" / "bin" / "agent"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("#!/bin/sh\nexit 0\n")
    legacy.chmod(0o755)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    env = {
        "HOME": str(home), "HERMES_HOME": str(hermes_home), "PATH": str(empty_path),
        "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
        "EXPECTED_COMMAND": str(legacy),
    }
    code = textwrap.dedent('''
        import contextlib
        import json
        import os
        from providers import get_provider_profile
        from hermes_cli.auth import resolve_external_process_provider_credentials
        from hermes_cli.models import provider_model_ids
        from agent.copilot_acp_client import CopilotACPClient

        profile = get_provider_profile("cursor-acp")
        assert profile is not None
        creds = resolve_external_process_provider_credentials("cursor-acp")
        assert creds["command"] == os.environ["EXPECTED_COMMAND"], creds
        catalog = [{"modelId": "family[effort=high]"}, {"modelId": "family[effort=low]"}]
        sessions = []

        @contextlib.contextmanager
        def scripted_session(self, timeout_seconds, **kwargs):
            assert self._acp_command == os.environ["EXPECTED_COMMAND"]
            assert self._acp_args == ["acp"]
            assert kwargs.get("allow_file_requests") is False
            sessions.append(True)
            yield {"sessionId": "offline", "models": {"availableModels": catalog}}, None

        # Only the transport is scripted. Registry, credential lookup, actual list_models,
        # profile.fetch_models and Hermes picker merging all run their production code.
        CopilotACPClient._session = scripted_session
        first = provider_model_ids("cursor-acp", force_refresh=True)
        assert first == ["family[effort=high]", "family[effort=low]"], first
        catalog.append({"modelId": "new-release[fast=false]"})
        second = provider_model_ids("cursor-acp", force_refresh=True)
        assert second == [*first, "new-release[fast=false]"], second
        assert len(sessions) == 2
        print(json.dumps({"first": first, "second": second, "legacy_resolved": True}))
    ''')
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["legacy_resolved"] is True
    assert receipt["second"] == [*receipt["first"], "new-release[fast=false]"]
