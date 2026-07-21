import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HERMETIC_SCRIPT = PROJECT_ROOT / "scripts" / "test_hermetic.sh"
VALIDATION_SCRIPT = PROJECT_ROOT / "scripts" / "run_isolated_validation.sh"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def test_hermetic_script_sets_policy_and_never_sources_private_env():
    text = HERMETIC_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert "EXTERNAL_CALL_POLICY=deny" in text
    assert "LLM_API_KEY=" in text
    assert "EMBEDDING_API_KEY=" in text
    assert "RAG_MCP_ENABLED=false" in text
    assert "WEATHER_ENABLED=false" in text
    assert "-m pytest -W error::DeprecationWarning" in text
    assert "source " not in text
    assert ".env" not in text


def test_ci_uses_hermetic_script_without_provider_secrets():
    text = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "name: Python 3.12" in text
    assert "EXTERNAL_CALL_POLICY: deny" in text
    assert 'LLM_API_KEY: ""' in text
    assert 'EMBEDDING_API_KEY: ""' in text
    assert 'RAG_MCP_ENABLED: "false"' in text
    assert 'WEATHER_ENABLED: "false"' in text
    assert "run: bash scripts/test_hermetic.sh" in text
    assert "contents: read" in text


def test_isolated_validation_script_is_syntax_valid_and_cleans_runtime(tmp_path):
    fake_python = tmp_path / "fake-python"
    capture_file = tmp_path / "database-url.txt"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "if [[ \"${1:-}\" == \"-c\" ]]; then\n"
        "  printf '%s\\n' 'generated-test-secret-not-for-runtime-use-1234567890'\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s' \"${DATABASE_URL:?}\" > \"${CAPTURE_FILE:?}\"\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "PYTHON_BIN": str(fake_python),
            "CAPTURE_FILE": str(capture_file),
            "PORT": "8765",
        }
    )

    syntax = subprocess.run(
        ["bash", "-n", str(VALIDATION_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        ["bash", str(VALIDATION_SCRIPT)],
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert syntax.returncode == 0
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [
        "Isolated validation mode",
        "External providers: denied",
        "Database: temporary",
        "Host: 127.0.0.1",
        "Port: 8765",
    ]
    database_url = capture_file.read_text(encoding="utf-8")
    database_path = Path(database_url.removeprefix("sqlite:///"))
    assert not database_path.parent.exists()
    assert "generated-test-secret" not in completed.stdout
    assert "sqlite:///" not in completed.stdout


def test_isolated_validation_script_disables_every_real_provider():
    text = VALIDATION_SCRIPT.read_text(encoding="utf-8")

    assert "set -euo pipefail" in text
    assert "EXTERNAL_CALL_POLICY=deny" in text
    assert "LLM_API_KEY=" in text
    assert "EMBEDDING_API_KEY=" in text
    assert "RAG_MCP_ENABLED=false" in text
    assert "WEATHER_ENABLED=false" in text
    assert "AUTH_ENABLED=true" in text
    assert "mktemp -d" in text
    assert "trap cleanup EXIT" in text
    assert "--host \"$HOST\"" in text
    assert "source " not in text
    assert ".env" not in text
