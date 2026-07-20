"""Model provider factory for chat LLMs and embeddings.

Supports Azure OpenAI and OpenAI-compatible providers such as Qwen,
DeepSeek, Zhipu, and OpenAI by switching environment variables.
"""

from __future__ import annotations

import logging
import os
from typing import NoReturn

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import (
    AzureChatOpenAI,
    AzureOpenAIEmbeddings,
    ChatOpenAI,
    OpenAIEmbeddings,
)
from pydantic import SecretStr

load_dotenv()

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

    timeout = _positive_float_env("LLM_TIMEOUT_SECONDS", DEFAULT_LLM_TIMEOUT_SECONDS)
    max_retries = _bounded_int_env("LLM_MAX_RETRIES", DEFAULT_LLM_MAX_RETRIES, 0, 3)

    try:
        if provider == "azure":
            return AzureChatOpenAI(
                azure_deployment=_env("AZURE_OPENAI_DEPLOYMENT"),
                api_version=_env("AZURE_OPENAI_VERSION"),
                temperature=temperature,
                azure_endpoint=_env("AZURE_OPENAI_ENDPOINT"),
                api_key=SecretStr(_env("AZURE_OPENAI_API_KEY", "") or ""),
                timeout=timeout,
                max_retries=max_retries,
            )

        if provider in CHAT_PROVIDERS:
            return ChatOpenAI(
                model=_env("LLM_MODEL", "qwen-plus") or "qwen-plus",
                api_key=SecretStr(_env("LLM_API_KEY", "") or ""),
                base_url=_env("LLM_BASE_URL"),
                temperature=temperature,
                timeout=timeout,
                max_retries=max_retries,
            )
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

    if provider == "azure":
        return AzureOpenAIEmbeddings(
            azure_deployment=_env("AZURE_OPENAI_DEPLOYMENT_EMBEDDING"),
            api_key=SecretStr(_env("AZURE_OPENAI_API_KEY", "") or ""),
            api_version=_env("AZURE_OPENAI_EMBEDDING_VERSION", "2023-05-15"),
            azure_endpoint=_env("AZURE_OPENAI_ENDPOINT_EMBEDDING"),
        )

    if provider in EMBEDDING_PROVIDERS:
        return OpenAIEmbeddings(
            model=_env("EMBEDDING_MODEL", "text-embedding-v3") or "text-embedding-v3",
            api_key=SecretStr(_env("EMBEDDING_API_KEY") or _env("LLM_API_KEY", "") or ""),
            base_url=_env("EMBEDDING_BASE_URL") or _env("LLM_BASE_URL"),
            # OpenAI-compatible providers like DashScope (Qwen) only accept raw
            # strings; disable token-id batching to send plain text.
            check_embedding_ctx_length=False,
        )

    raise ValueError(
        f"Unsupported EMBEDDING_PROVIDER={provider!r}. "
        "Use azure, qwen, zhipu, openai, or openai-compatible."
    )


def _usable_env(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value and not value.startswith("your_") and "YOUR_" not in value)


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
