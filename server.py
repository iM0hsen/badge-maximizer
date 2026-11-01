#!/usr/bin/env python3
"""Standalone hosted server for the Badge Maximizer panel.

Runs the web interface behind a password gate so it can live on a VPS or a
free cloud VM instead of the local desktop launcher. The Discord token and
cf_clearance cookie still live only in this process memory.

Environment:
  BADGE_PANEL_PASSWORD  required, at least 12 characters
  PORT                  listen port (default 8080)
  BADGE_HOST            bind address (default 0.0.0.0)
  BADGE_DOMAIN          public hostname, e.g. badges.example.com (recommended)
  BADGE_DATA_DIR        directory for non-sensitive progress files (default: app dir)
  BADGE_TRUST_PROXY     "1" when behind Caddy/nginx (default 1)
  BADGE_COOKIE_SECURE   "1" to force Secure cookies (default 1 in server mode)
"""
from __future__ import annotations

import os
import signal
import sys
import threading

from web_backend import (
    MIN_PANEL_PASSWORD_LENGTH,
    AppState,
    create_server,
    resource_dir,
)

PLACEHOLDER_PASSWORDS = {
    "change-me",
    "changeme",
    "password",
    "password123",
    "your-strong-password",
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _fail(message: str) -> None:
    print(f"[badge-maximizer] {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def _stop_engines(state: AppState) -> None:
    with state.lock:
        engines = [state.engine, state.viewer_engine]
        state.engine = None
        state.viewer_engine = None
        state.viewer_generation += 1
    for engine in engines:
        if engine is not None:
            engine.running = False


def main() -> int:
    password = os.environ.get("BADGE_PANEL_PASSWORD", "")
    if len(password) < MIN_PANEL_PASSWORD_LENGTH:
        _fail(
            "BADGE_PANEL_PASSWORD is missing or shorter than "
            f"{MIN_PANEL_PASSWORD_LENGTH} characters."
        )
    if password.strip().lower() in PLACEHOLDER_PASSWORDS:
        _fail("BADGE_PANEL_PASSWORD is still a placeholder value.")

    host = os.environ.get("BADGE_HOST", "0.0.0.0").strip() or "0.0.0.0"
    try:
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        _fail("PORT must be a number.")

    data_dir = os.environ.get("BADGE_DATA_DIR", "").strip()
    if data_dir:
        try:
            os.makedirs(data_dir, exist_ok=True)
        except OSError as exc:
            _fail(f"Cannot create BADGE_DATA_DIR {data_dir}: {exc}")

    domain = os.environ.get("BADGE_DOMAIN", "").strip().lower()
    allowed_hosts = [part.strip() for part in domain.split(",") if part.strip()]
    trust_proxy = _env_bool("BADGE_TRUST_PROXY", True)
    cookie_secure = _env_bool("BADGE_COOKIE_SECURE", True)

    state = AppState()
    try:
        server = create_server(
            state,
            resource_dir(),
            public_mode=True,
            password=password,
            host=host,
            port=port,
            allowed_hosts=allowed_hosts,
            trust_proxy=trust_proxy,
            cookie_secure=cookie_secure,
        )
    except OSError as exc:
        _fail(f"Cannot bind {host}:{port}: {exc}")

    def _shutdown(signum, _frame) -> None:
        print(f"[badge-maximizer] signal {signum} — shutting down", flush=True)
        _stop_engines(state)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    label = domain.split(",")[0] if domain else f"{host}:{port}"
    print(
        "[badge-maximizer] panel ready on "
        f"http://{host}:{port} "
        f"(public entry: {label or 'not set'})",
        flush=True,
    )
    print(
        "[badge-maximizer] token/cookie storage: memory only; "
        f"data dir: {data_dir or 'application directory'}",
        flush=True,
    )
    state.logs.add("info", "بدأت لوحة الاستضافة — بانتظار تسجيل الدخول.")

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        _stop_engines(state)
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
