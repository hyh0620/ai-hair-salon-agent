from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = PROJECT_ROOT / "docs" / "RELEASE_CHECKLIST.md"
RELEASE_NOTES = PROJECT_ROOT / "docs" / "RELEASE_NOTES_V1.0.md"
DEMO_GUIDE = PROJECT_ROOT / "docs" / "DEMO_GUIDE.md"
DEMO_RUNBOOK = PROJECT_ROOT / "docs" / "DEMO_RUNBOOK.md"
ARCHITECTURE = PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
ARCHITECTURE_SVG = PROJECT_ROOT / "architecture.svg"
EVALUATION = PROJECT_ROOT / "docs" / "EVALUATION.md"
AGENT_GUIDE = PROJECT_ROOT / "AGENTS.md"
RUN_DEMO_SKILL = PROJECT_ROOT / ".github" / "skills" / "run-demo" / "SKILL.md"
SETUP_SKILL = PROJECT_ROOT / ".github" / "skills" / "setup-environment" / "SKILL.md"
READINESS_SCRIPT = PROJECT_ROOT / "scripts" / "check_release_readiness.sh"
PUBLIC_PRESENTATION_FILES = (
    PROJECT_ROOT / "README.md",
    DEMO_GUIDE,
    DEMO_RUNBOOK,
    PROJECT_ROOT / "docs" / "ARCHITECTURE.md",
    PROJECT_ROOT / "docs" / "EVALUATION.md",
    PROJECT_ROOT / "docs" / "RAG_SERVICE_INTEGRATION.md",
    CHECKLIST,
    RELEASE_NOTES,
    PROJECT_ROOT / "eval" / "README.md",
)


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
    provider_gate = content.index("## F. 真实外部服务验收")
    release_operations = content.index("## H. 发布操作")

    assert provider_gate < release_operations
    assert "只有 A 至 G 全部通过后" in content
    assert "可重复使用" in content
    assert "空复选框不代表当前版本验收失败" in content
    assert "本发布准备 PR" not in content


def test_readme_links_release_and_demo_documents() -> None:
    content = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    required_links = (
        "docs/RELEASE_CHECKLIST.md",
        "docs/RELEASE_NOTES_V1.0.md",
        "docs/DEMO_GUIDE.md",
        "docs/DEMO_RUNBOOK.md",
        "docs/PROJECT_EVIDENCE.md",
        "docs/ROADMAP.md",
        "https://github.com/hyh0620/ai-hair-salon-agent/releases",
    )

    assert all(link in content for link in required_links)
    assert "## 为什么不是普通预约表单或固定工作流？" in content
    assert "## 项目阅读导航" in content
    assert "## 面试与演示导航" not in content
    assert "面试现场默认先看" not in content
    assert "[5 分钟项目演示](docs/DEMO_GUIDE.md)" in content
    assert "[本地运行与深度演示手册](docs/DEMO_RUNBOOK.md)" in content
    assert "## v1.0 发布与验收" in content
    assert "413 passed" in content
    assert "当前仓库正在准备 v1.0 release candidate" not in content
    assert f"{383} passed" not in content
    assert "383 个自动化测试" not in content
    assert "真实外部服务验收在显式允许外部调用的隔离流程中执行" in content

    evidence = (PROJECT_ROOT / "docs" / "PROJECT_EVIDENCE.md").read_text(
        encoding="utf-8"
    )
    roadmap = (PROJECT_ROOT / "docs" / "ROADMAP.md").read_text(
        encoding="utf-8"
    )
    assert "简历可用表述" in evidence
    assert "当前限制" in evidence
    assert "不是分布式多 Agent 平台" in evidence
    assert "Planned / 规划中" in roadmap
    assert "当前已经实现" in roadmap

    guide = DEMO_GUIDE.read_text(encoding="utf-8")
    runbook = DEMO_RUNBOOK.read_text(encoding="utf-8")
    agent_guide = AGENT_GUIDE.read_text(encoding="utf-8")
    run_demo_skill = RUN_DEMO_SKILL.read_text(encoding="utf-8")
    setup_skill = SETUP_SKILL.read_text(encoding="utf-8")
    fixed_demo_date = f"{2026}-09-01"

    assert "# 5 分钟项目演示" in guide
    assert "# 5 分钟面试演示" not in guide
    assert "技术追问备用演示" not in guide
    assert "## 常见技术问题" in guide
    assert "pip install" not in guide
    assert "ingest.py" not in guide
    assert "Token 重放" not in guide
    assert "# 本地运行与深度演示手册" in runbook
    assert "环境准备" in runbook
    assert "认证会话深度演示" in runbook
    assert "MCP 故障注入" in runbook
    assert "### Booking 可用但 Consultation 返回 503" not in runbook
    assert "### Consultation 可用但没有 Citations" not in runbook
    assert "### 预约功能正常，但知识咨询返回 HTTP 503" in runbook
    assert "### 知识咨询可用，但没有返回引用来源" in runbook
    assert "/" + "Users/" not in runbook
    assert fixed_demo_date not in guide
    assert fixed_demo_date not in runbook
    assert fixed_demo_date not in run_demo_skill
    assert "disabled by default" not in agent_guide
    assert "WEATHER_ENABLED=false" not in run_demo_skill
    assert "start MCP Knowledge Service, then start FastAPI" not in setup_skill
    assert "FastAPI lifespan" in setup_skill
    assert "FastAPI 应用生命周期（lifespan）" in guide
    assert "FastAPI 应用生命周期（lifespan）" in runbook
    assert "主项目固定使用 Python 3.12" in guide
    assert "知识服务声明支持 Python 3.11+" in guide
    assert "主要开发、运行和 CI 版本为 Python 3.12" in runbook
    assert "项目元数据声明支持 Python 3.11+" in runbook
    assert all(
        fixed_demo_date not in path.read_text(encoding="utf-8")
        for path in PUBLIC_PRESENTATION_FILES
    )

    release_notes = RELEASE_NOTES.read_text(encoding="utf-8")
    release_demo_url = (
        "https://github.com/hyh0620/ai-hair-salon-agent/"
        "blob/v1.0.0/docs/DEMO_GUIDE.md"
    )
    release_runbook_url = (
        "https://github.com/hyh0620/ai-hair-salon-agent/"
        "blob/v1.0.0/docs/DEMO_RUNBOOK.md"
    )

    assert release_demo_url in release_notes
    assert release_runbook_url in release_notes
    assert "](DEMO_GUIDE.md)" not in release_notes
    assert "](DEMO_RUNBOOK.md)" not in release_notes
    assert "blob/main/docs/DEMO_GUIDE.md" not in release_notes
    assert "blob/main/docs/DEMO_RUNBOOK.md" not in release_notes
    assert "默认配置关闭真实外部 Provider" not in release_notes
    assert "创建 v1.0.0 Tag 前应" not in release_notes
    assert "403 passed" in release_notes
    assert "功能契约 | 28 / 28" in release_notes
    assert "## 真实外部服务验收" in release_notes
    assert "## 已知限制" in release_notes
    assert "--no-proxy-headers" in release_notes
    assert "https://github.com/hyh0620/mcp-knowledge-service" in release_notes

    expected_release_headings = (
        "## 项目概述",
        "## 核心能力",
        "## 工程边界",
        "## 验证结果",
        "## 真实外部服务验收",
        "## 升级说明",
        "## 已知限制",
        "## 快速开始",
        "## 演示材料",
        "## 相关仓库",
    )
    forbidden_release_headings = (
        "## Overview",
        "## Highlights",
        "## Engineering Boundaries",
        "## Validation",
        "## Upgrade Note",
        "## Known Limitations",
        "## Quick Start",
        "## Demo",
        "## Related Repository",
    )
    assert all(heading in release_notes for heading in expected_release_headings)
    assert all(heading not in release_notes for heading in forbidden_release_headings)

    evaluation = EVALUATION.read_text(encoding="utf-8")
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    assert "## 基准用例集分类" in evaluation
    assert "功能契约（Functional Contract）" in evaluation
    assert "RAG 用例" in evaluation
    assert "## 会话与预约归属边界" in architecture
    assert "不是分布式锁、分布式事务或跨数据库事务" in architecture

    old_public_phrases = (
        "Booking 链路",
        "Consultation 返回",
        "没有 Citations",
        "Runbook：用于",
        "面试现场默认",
    )
    assert all(
        phrase not in path.read_text(encoding="utf-8")
        for path in PUBLIC_PRESENTATION_FILES
        for phrase in old_public_phrases
    )

    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "name: Python 3.12" in workflow
    ET.parse(ARCHITECTURE_SVG)


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
        "docs/DEMO_RUNBOOK.md",
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
    guide = DEMO_GUIDE.read_text(encoding="utf-8")

    assert "认证会话管理登录凭据的有效性" in readme
    assert "认证会话管理登录凭据的有效性" in release_notes
    assert "对话会话" in readme and "对话会话" in release_notes
    assert "游客预约归属是兼容的业务范围，不是安全认证" in guide
    assert "游客 `anonymous_owner_id` 是可伪造的业务范围，不是安全认证" in release_notes


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
