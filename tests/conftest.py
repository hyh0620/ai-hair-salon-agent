"""Hermetic pytest bootstrap installed before application module collection."""

from __future__ import annotations

import atexit
import ipaddress
import os
import re
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


_TEST_RUNTIME_DIR = Path(tempfile.mkdtemp(prefix="ai-hair-salon-pytest-"))


def _cleanup_test_runtime() -> None:
    shutil.rmtree(_TEST_RUNTIME_DIR, ignore_errors=True)


atexit.register(_cleanup_test_runtime)

# These values must exist before app, Agent, config, or provider imports. Empty
# credentials prevent python-dotenv from restoring private local values even if
# a future module bypasses the centralized dotenv loader.
_HERMETIC_ENV = {
    "EXTERNAL_CALL_POLICY": "deny",
    "MODEL_PROVIDER": "qwen",
    "LLM_API_KEY": "",
    "LLM_BASE_URL": "",
    "LLM_MODEL": "",
    "LLM_HTTP_LOCAL_ADDRESS": "",
    "EMBEDDING_PROVIDER": "qwen",
    "EMBEDDING_API_KEY": "",
    "EMBEDDING_BASE_URL": "",
    "EMBEDDING_MODEL": "",
    "AZURE_OPENAI_API_KEY": "",
    "AZURE_OPENAI_ENDPOINT": "",
    "AZURE_OPENAI_DEPLOYMENT": "",
    "AZURE_OPENAI_VERSION": "",
    "AZURE_OPENAI_DEPLOYMENT_EMBEDDING": "",
    "AZURE_OPENAI_ENDPOINT_EMBEDDING": "",
    "AZURE_OPENAI_EMBEDDING_VERSION": "",
    "RAG_MCP_ENABLED": "false",
    "RAG_MCP_SERVER_PYTHON": "",
    "RAG_MCP_SERVER_CWD": "",
    "WEATHER_ENABLED": "false",
    "OPENWEATHER_API_KEY": "",
    "AUTH_ENABLED": "true",
    "AUTH_JWT_SECRET": "pytest-only-jwt-secret-never-use-outside-tests",
    "DATABASE_URL": f"sqlite:///{_TEST_RUNTIME_DIR / 'pytest-default.db'}",
}
os.environ.update(_HERMETIC_ENV)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ExternalNetworkBlockedError(RuntimeError):
    """Raised before hermetic tests can resolve or connect to an external host."""


_ORIGINAL_SOCKET = socket.socket
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_GETHOSTBYNAME = socket.gethostbyname
_ORIGINAL_GETHOSTBYNAME_EX = socket.gethostbyname_ex
_ORIGINAL_GETHOSTBYADDR = socket.gethostbyaddr
_ORIGINAL_GETNAMEINFO = socket.getnameinfo


def _safe_host(host: Any) -> str:
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    value = str(host if host is not None else "none")
    if "://" in value:
        value = urlsplit(value).hostname or "unknown"
    elif "@" in value:
        value = value.rsplit("@", 1)[-1]
    value = value.split("?", 1)[0].split("#", 1)[0].split("/", 1)[0]
    value = re.sub(r"[^A-Za-z0-9_.:%-]+", "_", value)
    return value[:128] or "unknown"


def _is_loopback_host(host: Any) -> bool:
    if host in (None, "", b""):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    value = str(host).strip().lower()
    if value == "localhost" or value.endswith(".localhost"):
        return True
    candidate = value.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _blocked(host: Any, port: Any = "unknown") -> ExternalNetworkBlockedError:
    return ExternalNetworkBlockedError(
        "External network access blocked during hermetic test: "
        f"host={_safe_host(host)} port={port}"
    )


def _assert_local_address(family: int, address: Any) -> None:
    if family == socket.AF_UNIX:
        return
    if not isinstance(address, tuple) or not address:
        raise _blocked("unknown")
    host = address[0]
    port = address[1] if len(address) > 1 else "unknown"
    if not _is_loopback_host(host):
        raise _blocked(host, port)


class HermeticSocket(_ORIGINAL_SOCKET):
    """Socket subclass that permits only loopback and Unix-domain traffic."""

    def connect(self, address: Any) -> None:
        _assert_local_address(self.family, address)
        return super().connect(address)

    def connect_ex(self, address: Any) -> int:
        _assert_local_address(self.family, address)
        return super().connect_ex(address)

    def sendto(self, data: bytes, *args: Any) -> int:
        if not args:
            raise TypeError("sendto requires a destination address")
        _assert_local_address(self.family, args[-1])
        return super().sendto(data, *args)

    if hasattr(_ORIGINAL_SOCKET, "sendmsg"):

        def sendmsg(
            self,
            buffers: Any,
            ancdata: Any = (),
            flags: int = 0,
            address: Any = None,
        ) -> int:
            if address is not None:
                _assert_local_address(self.family, address)
                return super().sendmsg(buffers, ancdata, flags, address)
            return super().sendmsg(buffers, ancdata, flags)


def _create_connection(address: Any, *args: Any, **kwargs: Any):
    host = address[0] if isinstance(address, tuple) and address else "unknown"
    port = address[1] if isinstance(address, tuple) and len(address) > 1 else "unknown"
    if not _is_loopback_host(host):
        raise _blocked(host, port)
    return _ORIGINAL_CREATE_CONNECTION(address, *args, **kwargs)


def _getaddrinfo(host: Any, *args: Any, **kwargs: Any):
    if not _is_loopback_host(host):
        port = args[0] if args else kwargs.get("port", "unknown")
        raise _blocked(host, port)
    return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)


def _gethostbyname(host: Any):
    if not _is_loopback_host(host):
        raise _blocked(host)
    return _ORIGINAL_GETHOSTBYNAME(host)


def _gethostbyname_ex(host: Any):
    if not _is_loopback_host(host):
        raise _blocked(host)
    return _ORIGINAL_GETHOSTBYNAME_EX(host)


def _gethostbyaddr(host: Any):
    if not _is_loopback_host(host):
        raise _blocked(host)
    return _ORIGINAL_GETHOSTBYADDR(host)


def _getnameinfo(sockaddr: Any, flags: int):
    host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else "unknown"
    port = sockaddr[1] if isinstance(sockaddr, tuple) and len(sockaddr) > 1 else "unknown"
    if not _is_loopback_host(host):
        raise _blocked(host, port)
    return _ORIGINAL_GETNAMEINFO(sockaddr, flags)


# Installed during conftest import, before pytest imports any application module.
socket.socket = HermeticSocket
socket.create_connection = _create_connection
socket.getaddrinfo = _getaddrinfo
socket.gethostbyname = _gethostbyname
socket.gethostbyname_ex = _gethostbyname_ex
socket.gethostbyaddr = _gethostbyaddr
socket.getnameinfo = _getnameinfo
