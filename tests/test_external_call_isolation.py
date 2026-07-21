import asyncio
import os
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import model_provider
from config.external_calls import (
    ExternalCallBlockedError,
    ExternalCallPolicy,
    ExternalCallPolicyError,
    assert_external_call_allowed,
    get_external_call_policy,
    load_runtime_dotenv,
)
from config.model_provider import GuardedChatModel, GuardedEmbeddings
from services import mcp_knowledge_gateway
from services.mcp_knowledge_gateway import MCPKnowledgeGateway


def test_pytest_bootstrap_is_hermetic_before_application_imports():
    assert os.environ["EXTERNAL_CALL_POLICY"] == "deny"
    assert os.environ["LLM_API_KEY"] == ""
    assert os.environ["EMBEDDING_API_KEY"] == ""
    assert os.environ["RAG_MCP_ENABLED"] == "false"
    assert os.environ["WEATHER_ENABLED"] == "false"
    assert os.environ["DATABASE_URL"].startswith("sqlite:////")
    assert "ai-hair-salon-pytest-" in os.environ["DATABASE_URL"]


def test_unset_policy_preserves_normal_allow_behavior(monkeypatch):
    monkeypatch.delenv("EXTERNAL_CALL_POLICY", raising=False)

    assert get_external_call_policy() is ExternalCallPolicy.ALLOW


@pytest.mark.parametrize("value", ["", "invalid", "enabled", "0"])
def test_invalid_policy_fails_closed_with_configuration_error(monkeypatch, value):
    monkeypatch.setenv("EXTERNAL_CALL_POLICY", value)

    with pytest.raises(ExternalCallPolicyError, match="Invalid EXTERNAL_CALL_POLICY"):
        get_external_call_policy()


def test_denied_runtime_does_not_read_project_dotenv(monkeypatch):
    import dotenv

    monkeypatch.setattr(
        dotenv,
        "load_dotenv",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("private .env must not be read")
        ),
    )

    assert load_runtime_dotenv() is False


def test_allow_runtime_preserves_normal_dotenv_loading(monkeypatch, tmp_path):
    import dotenv

    loaded = []
    monkeypatch.setenv("EXTERNAL_CALL_POLICY", "allow")
    monkeypatch.setattr(
        dotenv,
        "load_dotenv",
        lambda *args, **kwargs: loaded.append((args, kwargs)) or True,
    )
    dotenv_path = tmp_path / ".env"

    assert load_runtime_dotenv(dotenv_path) is True
    assert loaded[0][1]["dotenv_path"] == dotenv_path


def test_blocked_error_is_sanitized_and_contains_no_credentials(monkeypatch):
    sentinel = "test-sentinel-never-send"
    monkeypatch.setenv("LLM_API_KEY", sentinel)
    monkeypatch.setenv("EMBEDDING_API_KEY", sentinel)
    monkeypatch.setenv("AUTHORIZATION", f"Bearer {sentinel}")
    monkeypatch.setenv("COOKIE", f"session={sentinel}")

    with pytest.raises(ExternalCallBlockedError) as captured:
        assert_external_call_allowed("llm:qwen", "tests.provider_boundary")

    message = str(captured.value)
    assert "provider=llm:qwen" in message
    assert "policy=deny" in message
    assert "location=tests.provider_boundary" in message
    assert sentinel not in message
    assert "Authorization" not in message
    assert "Cookie" not in message


def _configure_sentinel_llm(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "qwen")
    monkeypatch.setenv("LLM_API_KEY", "test-sentinel-never-send")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example.invalid/v1")
    monkeypatch.setenv("LLM_MODEL", "test-model")


def test_deny_blocks_real_llm_before_client_construction(monkeypatch):
    _configure_sentinel_llm(monkeypatch)
    constructed = []
    monkeypatch.setattr(
        model_provider,
        "ChatOpenAI",
        lambda *args, **kwargs: constructed.append((args, kwargs)),
    )

    with pytest.raises(ExternalCallBlockedError, match="provider=llm:qwen"):
        model_provider.create_chat_model()

    assert constructed == []


class _ChatDelegateSpy:
    def __init__(self):
        self.calls = []

    def invoke(self, *args, **kwargs):
        self.calls.append("invoke")

    async def ainvoke(self, *args, **kwargs):
        self.calls.append("ainvoke")

    def stream(self, *args, **kwargs):
        self.calls.append("stream")
        return iter(())

    async def astream(self, *args, **kwargs):
        self.calls.append("astream")
        if False:
            yield None


def test_deny_blocks_llm_invoke_ainvoke_and_stream_before_delegate():
    delegate = _ChatDelegateSpy()
    model = GuardedChatModel(delegate, "qwen")

    with pytest.raises(ExternalCallBlockedError, match=r"\.invoke"):
        model.invoke("hello")
    with pytest.raises(ExternalCallBlockedError, match=r"\.ainvoke"):
        asyncio.run(model.ainvoke("hello"))
    with pytest.raises(ExternalCallBlockedError, match=r"\.stream"):
        model.stream("hello")

    async def consume_astream():
        async for _ in model.astream("hello"):
            pass

    with pytest.raises(ExternalCallBlockedError, match=r"\.astream"):
        asyncio.run(consume_astream())

    assert delegate.calls == []


def test_plain_fake_llm_is_not_blocked_by_provider_policy():
    fake = _ChatDelegateSpy()

    fake.invoke("hello")
    asyncio.run(fake.ainvoke("hello"))
    list(fake.stream("hello"))

    assert fake.calls == ["invoke", "ainvoke", "stream"]


def test_allow_policy_preserves_guarded_llm_delegate_behavior(monkeypatch):
    monkeypatch.setenv("EXTERNAL_CALL_POLICY", "allow")
    delegate = _ChatDelegateSpy()
    model = GuardedChatModel(delegate, "qwen")

    model.invoke("hello")
    asyncio.run(model.ainvoke("hello"))
    list(model.stream("hello"))

    assert delegate.calls == ["invoke", "ainvoke", "stream"]


def test_deny_blocks_real_embedding_before_client_construction(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "qwen")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-sentinel-never-send")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://provider.example.invalid/v1")
    constructed = []
    monkeypatch.setattr(
        model_provider,
        "OpenAIEmbeddings",
        lambda *args, **kwargs: constructed.append((args, kwargs)),
    )

    with pytest.raises(ExternalCallBlockedError, match="provider=embedding:qwen"):
        model_provider.create_embedding_model()

    assert constructed == []


class _EmbeddingDelegateSpy:
    def __init__(self):
        self.calls = []

    def embed_query(self, text):
        self.calls.append(("query", text))
        return [1.0]

    def embed_documents(self, texts):
        self.calls.append(("documents", texts))
        return [[1.0] for _ in texts]

    async def aembed_query(self, text):
        self.calls.append(("aquery", text))
        return [1.0]

    async def aembed_documents(self, texts):
        self.calls.append(("adocuments", texts))
        return [[1.0] for _ in texts]


def test_deny_blocks_embedding_query_and_documents_before_delegate():
    delegate = _EmbeddingDelegateSpy()
    embeddings = GuardedEmbeddings(delegate, "qwen")

    with pytest.raises(ExternalCallBlockedError, match="embed_query"):
        embeddings.embed_query("hello")
    with pytest.raises(ExternalCallBlockedError, match="embed_documents"):
        embeddings.embed_documents(["hello"])
    with pytest.raises(ExternalCallBlockedError, match="aembed_query"):
        asyncio.run(embeddings.aembed_query("hello"))
    with pytest.raises(ExternalCallBlockedError, match="aembed_documents"):
        asyncio.run(embeddings.aembed_documents(["hello"]))

    assert delegate.calls == []


def test_plain_fake_embeddings_are_not_blocked_by_provider_policy():
    fake = _EmbeddingDelegateSpy()

    assert fake.embed_query("hello") == [1.0]
    assert fake.embed_documents(["hello"]) == [[1.0]]


def test_allow_policy_preserves_guarded_embedding_delegate_behavior(monkeypatch):
    monkeypatch.setenv("EXTERNAL_CALL_POLICY", "allow")
    delegate = _EmbeddingDelegateSpy()
    embeddings = GuardedEmbeddings(delegate, "qwen")

    assert embeddings.embed_query("hello") == [1.0]
    assert embeddings.embed_documents(["hello"]) == [[1.0]]

    assert delegate.calls == [
        ("query", "hello"),
        ("documents", ["hello"]),
    ]


def test_deny_blocks_mcp_before_any_subprocess_or_sensitive_path(monkeypatch):
    process_calls = []

    def forbidden_popen(*args, **kwargs):
        process_calls.append("popen")
        raise AssertionError("subprocess.Popen must not be called")

    async def forbidden_async_subprocess(*args, **kwargs):
        process_calls.append("asyncio")
        raise AssertionError("asyncio.create_subprocess_exec must not be called")

    def forbidden_stdio(*args, **kwargs):
        process_calls.append("stdio")
        raise AssertionError("MCP stdio client must not be created")

    monkeypatch.setattr(subprocess, "Popen", forbidden_popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async_subprocess)
    monkeypatch.setattr(mcp_knowledge_gateway, "stdio_client", forbidden_stdio)
    gateway = MCPKnowledgeGateway(
        True,
        "/private/test-sentinel/server-python",
        "private.module",
        "/private/test-sentinel/server-data",
        "salon_knowledge",
        4,
    )

    with pytest.raises(ExternalCallBlockedError) as captured:
        asyncio.run(gateway.start())

    assert process_calls == []
    assert "/private/test-sentinel" not in str(captured.value)


def test_deny_overrides_enabled_mcp_environment_before_start(monkeypatch):
    monkeypatch.setenv("RAG_MCP_ENABLED", "true")
    monkeypatch.setenv("RAG_MCP_SERVER_PYTHON", "/private/provider-python")
    monkeypatch.setenv("RAG_MCP_SERVER_CWD", "/private/provider-data")
    gateway = MCPKnowledgeGateway.from_env()

    assert gateway.enabled is True
    with pytest.raises(ExternalCallBlockedError, match="provider=mcp:knowledge-service"):
        asyncio.run(gateway.start())


def test_deny_blocks_an_existing_real_mcp_session_before_tool_call():
    class SessionSpy:
        def __init__(self):
            self.calls = 0

        async def call_tool(self, name, args):
            self.calls += 1

    session = SessionSpy()
    gateway = MCPKnowledgeGateway(True, "python", "module", "/tmp", "salon_knowledge", 4)
    gateway._session = session
    gateway._session_is_external = True

    with pytest.raises(ExternalCallBlockedError, match="query_knowledge"):
        asyncio.run(gateway.query_knowledge("test query"))

    assert session.calls == 0


def test_non_loopback_tcp_udp_and_external_dns_are_blocked(monkeypatch):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        with pytest.raises(RuntimeError, match=r"host=203\.0\.113\.10 port=443"):
            client.connect(("203.0.113.10", 443))

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        with pytest.raises(RuntimeError, match=r"host=203\.0\.113\.10 port=53"):
            client.sendto(b"test", ("203.0.113.10", 53))

    with pytest.raises(RuntimeError, match=r"host=example\.com"):
        socket.getaddrinfo("example.com", 443)

    with pytest.raises(RuntimeError) as sanitized:
        socket.getaddrinfo(
            "https://user:private-value@example.com/path?token=private-value",
            443,
        )
    assert "host=example.com" in str(sanitized.value)
    assert "private-value" not in str(sanitized.value)

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:8080")
    with pytest.raises(RuntimeError, match=r"host=proxy\.example\.invalid"):
        socket.create_connection(("proxy.example.invalid", 8080))


def test_localhost_and_ipv4_loopback_are_allowed():
    ready = threading.Event()
    received = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def accept_one():
            ready.set()
            connection, _ = server.accept()
            with connection:
                received.append(connection.recv(4))

        thread = threading.Thread(target=accept_one)
        thread.start()
        ready.wait(timeout=1)
        with socket.create_connection(("localhost", port), timeout=1) as client:
            client.sendall(b"test")
        thread.join(timeout=1)

    assert received == [b"test"]


def test_ipv6_loopback_is_not_blocked():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as client:
        try:
            client.connect(("::1", 9))
        except OSError:
            pass


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_unix_domain_socket_is_allowed():
    with tempfile.TemporaryDirectory(prefix="salon-sock-", dir="/tmp") as directory:
        path = Path(directory) / "test.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(path))
                connection, _ = server.accept()
                connection.close()


def test_fastapi_testclient_and_httpx_asgi_transport_still_work():
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

    async def call_asgi():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            return await client.get("/health")

    assert asyncio.run(call_asgi()).json() == {"status": "ok"}
