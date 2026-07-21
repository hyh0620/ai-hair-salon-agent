"""Model provider factory for chat LLMs and embeddings.

Supports Azure OpenAI and OpenAI-compatible providers such as Qwen,
DeepSeek, Zhipu, and OpenAI by switching environment variables.
"""

from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator, Iterator, NoReturn

from langchain_core.embeddings import Embeddings
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import (
    AzureChatOpenAI,
    AzureOpenAIEmbeddings,
    ChatOpenAI,
    OpenAIEmbeddings,
)
from pydantic import PrivateAttr, SecretStr

from config.external_calls import assert_external_call_allowed, load_runtime_dotenv

load_runtime_dotenv()

logger = logging.getLogger(__name__)


CHAT_PROVIDERS = {"openai", "qwen", "deepseek", "zhipu", "openai-compatible"}
EMBEDDING_PROVIDERS = {"openai", "qwen", "zhipu", "openai-compatible"}
CHAT_MODEL_FAILURE_REASONS = {
    "not_configured",
    "timeout",
    "provider_unavailable",
    "invalid_credentials",
    "unexpected_error",
}
DEFAULT_LLM_TIMEOUT_SECONDS = 15.0
DEFAULT_LLM_MAX_RETRIES = 1


class ChatModelError(RuntimeError):
    """Stable model failure used across streaming chat boundaries."""

    def __init__(self, reason: str):
        normalized = reason if reason in CHAT_MODEL_FAILURE_REASONS else "unexpected_error"
        super().__init__(normalized)
        self.reason = normalized


class UnavailableChatModel(BaseChatModel):
    """Non-networking placeholder that keeps deterministic chat flows available."""

    reason: str = "not_configured"

    @property
    def _llm_type(self) -> str:
        return "unavailable-chat-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        del messages, stop, run_manager, kwargs
        raise ChatModelError(self.reason)


class GuardedChatModel(BaseChatModel):
    """Delegate to a real provider while enforcing policy at every call path."""

    provider_name: str
    call_location: str = "config.model_provider.GuardedChatModel"
    _delegate: BaseChatModel = PrivateAttr()

    def __init__(self, delegate: BaseChatModel, provider_name: str):
        super().__init__(provider_name=provider_name)
        self._delegate = delegate

    @property
    def _llm_type(self) -> str:
        return f"guarded-{self.provider_name}-chat-model"

    def _guard(self, operation: str) -> None:
        assert_external_call_allowed(
            f"llm:{self.provider_name}",
            f"{self.call_location}.{operation}",
        )

    def invoke(self, input: Any, config=None, *, stop=None, **kwargs):
        self._guard("invoke")
        return self._delegate.invoke(input, config=config, stop=stop, **kwargs)

    async def ainvoke(self, input: Any, config=None, *, stop=None, **kwargs):
        self._guard("ainvoke")
        return await self._delegate.ainvoke(input, config=config, stop=stop, **kwargs)

    def stream(self, input: Any, config=None, *, stop=None, **kwargs) -> Iterator[Any]:
        self._guard("stream")
        return self._delegate.stream(input, config=config, stop=stop, **kwargs)

    async def astream(
        self,
        input: Any,
        config=None,
        *,
        stop=None,
        **kwargs,
    ) -> AsyncIterator[Any]:
        self._guard("astream")
        async for chunk in self._delegate.astream(
            input,
            config=config,
            stop=stop,
            **kwargs,
        ):
            yield chunk

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._guard("generate")
        return self._delegate._generate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._guard("agenerate")
        return await self._delegate._agenerate(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        self._guard("stream")
        yield from self._delegate._stream(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        )

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._guard("astream")
        async for chunk in self._delegate._astream(
            messages,
            stop=stop,
            run_manager=run_manager,
            **kwargs,
        ):
            yield chunk


class GuardedEmbeddings(Embeddings):
    """Guard every operation delegated to a real embedding provider."""

    def __init__(self, delegate: Embeddings, provider_name: str):
        self._delegate = delegate
        self.provider_name = provider_name

    def _guard(self, operation: str) -> None:
        assert_external_call_allowed(
            f"embedding:{self.provider_name}",
            f"config.model_provider.GuardedEmbeddings.{operation}",
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._guard("embed_documents")
        return self._delegate.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        self._guard("embed_query")
        return self._delegate.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        self._guard("aembed_documents")
        return await self._delegate.aembed_documents(texts)

    async def aembed_query(self, text: str) -> list[float]:
        self._guard("aembed_query")
        return await self._delegate.aembed_query(text)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def get_model_provider() -> str:
    """Return configured provider, defaulting to Azure for backward compatibility."""
    return (_env("MODEL_PROVIDER", "azure") or "azure").strip().lower()


def chat_model_configuration_status() -> str:
    """Return configured, not_configured, or provider_unavailable."""
    provider = get_model_provider()
    if provider == "azure":
        keys = (
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "AZURE_OPENAI_DEPLOYMENT",
            "AZURE_OPENAI_VERSION",
        )
    elif provider in CHAT_PROVIDERS:
        keys = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
    else:
        return "provider_unavailable"
    return "configured" if all(_usable_env(key) for key in keys) else "not_configured"


def is_chat_model_configured() -> bool:
    return chat_model_configuration_status() == "configured"


def classify_chat_model_error(exc: BaseException) -> str:
    """Map provider exceptions to a small, user-safe status vocabulary."""
    if isinstance(exc, ChatModelError):
        return exc.reason

    status_code = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if isinstance(exc, (TimeoutError,)) or "timeout" in name or "timed out" in message:
        return "timeout"
    if "missing credentials" in message or "api key must be set" in message:
        return "not_configured"
    if status_code in {401, 403} or "authentication" in name or "permissiondenied" in name:
        return "invalid_credentials"
    if any(term in message for term in ("invalid api key", "incorrect api key", "unauthorized")):
        return "invalid_credentials"
    if (
        status_code in {408, 409, 425, 429}
        or isinstance(status_code, int) and status_code >= 500
        or any(term in name for term in ("connection", "ratelimit", "serviceunavailable"))
    ):
        return "provider_unavailable"
    return "unexpected_error"


def raise_chat_model_error(exc: BaseException) -> NoReturn:
    if isinstance(exc, ChatModelError):
        raise exc
    raise ChatModelError(classify_chat_model_error(exc)) from exc


def chat_model_user_message(reason: str) -> str:
    messages = {
        "not_configured": (
            "当前未配置语言模型，无法完成这条自然语言解析。"
            "请配置 LLM_API_KEY 后重试，或通过 Swagger 使用结构化预约接口。"
        ),
        "timeout": "语言模型响应超时，请稍后重试。",
        "provider_unavailable": "语言模型服务暂时不可用，请稍后重试。",
        "invalid_credentials": "语言模型凭据无效，请检查模型配置后重试。",
        "unexpected_error": "处理消息时发生异常，请稍后重试。",
    }
    return messages.get(reason, messages["unexpected_error"])


def create_chat_model(temperature: float = 0) -> BaseChatModel:
    """Create a chat model from environment configuration.

    Azure-compatible env vars:
        MODEL_PROVIDER=azure
        AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT,
        AZURE_OPENAI_VERSION

    OpenAI-compatible env vars:
        MODEL_PROVIDER=qwen|deepseek|zhipu|openai|openai-compatible
        LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
    """
    provider = get_model_provider()
    configuration_status = chat_model_configuration_status()
    if configuration_status != "configured":
        return UnavailableChatModel(reason=configuration_status)

    assert_external_call_allowed(
        f"llm:{provider}",
        "config.model_provider.create_chat_model",
    )

    timeout = _positive_float_env("LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS)
    max_retries = _bounded_int_env("LLM_MAX_RETRIES", DEFAULT_LLM_MAX_RETRIES, 0, 3)

    try:
        if provider == "azure":
            model = AzureChatOpenAI(
                azure_deployment=_env("AZURE_OPENAI_DEPLOYMENT"),
                api_version=_env("AZURE_OPENAI_VERSION"),
                temperature=temperature,
                azure_endpoint=_env("AZURE_OPENAI_ENDPOINT"),
                api_key=SecretStr(_env("AZURE_OPENAI_API_KEY", "") or ""),
                timeout=timeout,
                max_retries=max_retries,
            )
            return GuardedChatModel(model, provider)

        if provider in CHAT_PROVIDERS:
            model = ChatOpenAI(
                model=_env("LLM_MODEL", "qwen-plus") or "qwen-plus",
                api_key=SecretStr(_env("LLM_API_KEY", "") or ""),
                base_url=_env("LLM_BASE_URL"),
                temperature=temperature,
                timeout=timeout,
                max_retries=max_retries,
                **_chat_http_clients(),
            )
            return GuardedChatModel(model, provider)
    except Exception as exc:
        reason = classify_chat_model_error(exc)
        logger.warning(
            "chat_model_initialization_failed provider=%s status=%s exception_type=%s",
            provider,
            reason,
            type(exc).__name__,
        )
        return UnavailableChatModel(reason=reason)

    return UnavailableChatModel(reason="provider_unavailable")


def create_embedding_model():
    """Create an embedding model from environment configuration."""
    provider = (_env("EMBEDDING_PROVIDER") or get_model_provider()).strip().lower()

    assert_external_call_allowed(
        f"embedding:{provider}",
        "config.model_provider.create_embedding_model",
    )

    if provider == "azure":
        model = AzureOpenAIEmbeddings(
            azure_deployment=_env("AZURE_OPENAI_DEPLOYMENT_EMBEDDING"),
            api_key=SecretStr(_env("AZURE_OPENAI_API_KEY", "") or ""),
            api_version=_env("AZURE_OPENAI_EMBEDDING_VERSION", "2023-05-15"),
            azure_endpoint=_env("AZURE_OPENAI_ENDPOINT_EMBEDDING"),
        )
        return GuardedEmbeddings(model, provider)

    if provider in EMBEDDING_PROVIDERS:
        model = OpenAIEmbeddings(
            model=_env("EMBEDDING_MODEL", "text-embedding-v3") or "text-embedding-v3",
            api_key=SecretStr(_env("EMBEDDING_API_KEY") or _env("LLM_API_KEY", "") or ""),
            base_url=_env("EMBEDDING_BASE_URL") or _env("LLM_BASE_URL"),
            # OpenAI-compatible providers like DashScope (Qwen) only accept raw
            # strings; disable token-id batching to send plain text.
            check_embedding_ctx_length=False,
        )
        return GuardedEmbeddings(model, provider)

    raise ValueError(
        f"Unsupported EMBEDDING_PROVIDER={provider!r}. "
        "Use azure, qwen, zhipu, openai, or openai-compatible."
    )


def _usable_env(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value and not value.startswith("your_") and "YOUR_" not in value)


def _chat_http_clients() -> dict[str, object]:
    """Build optional clients for hosts that require an explicit local address."""
    local_address = _env("LLM_HTTP_LOCAL_ADDRESS")
    if not local_address:
        return {}

    assert_external_call_allowed(
        f"llm:{get_model_provider()}",
        "config.model_provider._chat_http_clients",
    )

    import httpx

    return {
        "http_client": httpx.Client(
            transport=httpx.HTTPTransport(local_address=local_address)
        ),
        "http_async_client": httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(local_address=local_address)
        ),
    }


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(max(value, minimum), maximum)
