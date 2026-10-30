import ipaddress
import os

import uvicorn

from .api import create_app
from .config import Settings


def _is_loopback(host: str) -> bool:
    if host in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_app():
    return create_app(Settings.from_env())


def main():
    settings = Settings.from_env()
    host = os.environ.get("AUTOFLY_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("PORT", "8000"))
    if not _is_loopback(host) and not settings.inbound_token:
        raise RuntimeError("AUTOFLY_ENGINE_TOKEN is required for public network binding.")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

