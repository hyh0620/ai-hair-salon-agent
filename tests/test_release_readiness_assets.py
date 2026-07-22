from pathlib import Path
import re
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = PROJECT_ROOT / "docs" / "RELEASE_CHECKLIST.md"
RELEASE_NOTES = PROJECT_ROOT / "docs" / "RELEASE_NOTES_V1.0.md"
READINESS_SCRIPT = PROJECT_ROOT / "scripts" / "check_release_readiness.sh"


@pytest.mark.parametrize(
    "relative_path",
    [
        "docs/RELEASE_CHECKLIST.md",
        "docs/RELEASE_NOTES_V1.0.md",
        "scripts/check_release_readiness.sh",
    ],
)
def test_release_asset_exists(relative_path: str) -> None:
    assert (PROJECT_ROOT / relative_path).is_file()


@pytest.mark.parametrize(
    "required_text",
    [
        "set -euo pipefail",
        "scripts/test_hermetic.sh",
        '"$PYTHON_BIN" -m pip check',
        '"$PYTHON_BIN" -m compileall',
    ],
)
def test_readiness_script_runs_required_checks(required_text: str) -> None:
    assert required_text in READINESS_SCRIPT.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "forbidden_command",
    [
        re.compile(r"(?m)^\s*(?:source|\.)\s+[^\n]*\.env(?:\s|$)"),
        re.compile(r"(?m)^\s*git\s+tag(?:\s|$)"),
        re.compile(r"(?m)^\s*gh\s+release(?:\s|$)"),
    ],
)
def test_readiness_script_does_not_run_release_or_secret_commands(
    forbidden_command: re.Pattern[str],
) -> None:
    content = READINESS_SCRIPT.read_text(encoding="utf-8")
    assert forbidden_command.search(content) is None


@pytest.mark.parametrize(
    "forbidden_pattern",
    [
        re.compile("/" + "Users/"),
        re.compile(r"(?:ghp_|github_pat_|sk-)[A-Za-z0-9_\-]{20,}"),
    ],
)
def test_release_notes_exclude_local_paths_and_credentials(
    forbidden_pattern: re.Pattern[str],
) -> None:
    content = RELEASE_NOTES.read_text(encoding="utf-8")
    assert forbidden_pattern.search(content) is None


def test_checklist_requires_provider_validation_before_tagging() -> None:
    content = CHECKLIST.read_text(encoding="utf-8")
    provider_gate = content.index("## F. 真实 Provider 验收")
    release_operations = content.index("## H. 发布操作")

    assert provider_gate < release_operations
    assert "只有 A 至 G 全部通过后" in content
    assert "本发布准备 PR 不执行 Tag 或 GitHub Release 命令" in content


def test_readme_links_release_and_demo_documents() -> None:
    content = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    required_links = (
        "docs/RELEASE_CHECKLIST.md",
        "docs/RELEASE_NOTES_V1.0.md",
        "docs/DEMO_GUIDE.md",
        "https://github.com/hyh0620/ai-hair-salon-agent/releases",
    )

    assert all(link in content for link in required_links)
    assert "## v1.0 发布与验收" in content
    assert "当前仓库正在准备 v1.0 release candidate" not in content
    assert f"{383} passed" not in content
    assert "383 个自动化测试" not in content
    assert "真实 Provider 验收在显式允许外部调用的隔离流程中执行，不属于 Hermetic CI" in content


def test_tracked_text_does_not_contain_stale_test_baselines() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files"], cwd=PROJECT_ROOT, text=True
    ).splitlines()
    stale_counts = tuple(f"{value} passed" for value in (181, 262, 292, 338, 383))
    findings: list[str] = []

    for relative_path in tracked:
        try:
            content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(stale_count in content for stale_count in stale_counts):
            findings.append(relative_path)

    assert findings == []


def test_public_start_commands_disable_proxy_headers() -> None:
    for relative_path in (
        "README.md",
        "docs/DEMO_GUIDE.md",
        "docs/RELEASE_NOTES_V1.0.md",
    ):
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert "--no-proxy-headers" in content


def test_public_docs_do_not_claim_production_deployment() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    release_notes = RELEASE_NOTES.read_text(encoding="utf-8")

    assert "不声称已经完成生产部署" in readme
    assert "不代表已经生产部署" in release_notes


def test_public_docs_distinguish_auth_and_chat_sessions() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    release_notes = RELEASE_NOTES.read_text(encoding="utf-8")

    assert "Auth Session 管理登录凭据" in readme
    assert "Auth Session 管理登录凭据" in release_notes
    assert "Chat Session" in readme and "Chat Session" in release_notes


def test_public_docs_distinguish_mcp_from_rag_responsibilities() -> None:
    release_notes = RELEASE_NOTES.read_text(encoding="utf-8")

    assert "MCP 是跨进程工具调用协议" in release_notes
    assert "RAG 是独立知识服务内部" in release_notes
    assert "二者不是同一组件" in release_notes
    assert "MCP/RAG 只处理知识咨询" in release_notes
    assert "不参与真实预约事实判断" in release_notes


def test_public_docs_distinguish_sqlite_from_distributed_transactions() -> None:
    release_notes = RELEASE_NOTES.read_text(encoding="utf-8")

    assert "不是分布式事务" in release_notes
