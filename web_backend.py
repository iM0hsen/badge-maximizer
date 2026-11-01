"""HTTP backend for the Badge Maximizer web interface.

Two modes share this module:

* Local mode (desktop launcher): binds to 127.0.0.1 on a random port and
  requires a per-launch key on every API call.
* Public mode (hosted server): binds to 0.0.0.0 behind a TLS proxy, requires a
  panel password plus a session cookie before the interface or any API call is
  reachable, and rate-limits login attempts.

In both modes the Discord token and Cloudflare cookie live in process memory
only. Nothing sensitive is ever written to disk or returned to the browser.
"""
from __future__ import annotations

import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import games
import stream as stream_engine

DISCORD_LOGIN_URL = "https://discord.com/login"
MAX_BODY_BYTES = 64 * 1024
MAX_LOG_LINES = 600
PING_TIMEOUT_SECONDS = 75

SESSION_COOKIE = "bdm_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600
MAX_LOGIN_FAILURES = 8
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_PENALTY_SECONDS = 0.4
MIN_PANEL_PASSWORD_LENGTH = 12


class AuthManager:
    """Password gate for public mode with in-memory sessions and lockout."""

    def __init__(self, password: str, ttl: int = SESSION_TTL_SECONDS) -> None:
        self._password = str(password or "")
        self._ttl = ttl
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._password)

    def _locked_until(self, ip: str, now: float) -> float:
        stamps = [
            stamp
            for stamp in self._failures.get(ip, [])
            if now - stamp < LOGIN_WINDOW_SECONDS
        ]
        if stamps:
            self._failures[ip] = stamps
        else:
            self._failures.pop(ip, None)
        if len(stamps) < MAX_LOGIN_FAILURES:
            return 0.0
        return LOGIN_WINDOW_SECONDS - (now - stamps[0])

    def lockout_remaining(self, ip: str) -> float:
        with self._lock:
            return self._locked_until(ip, time.time())

    def login(self, password: str, ip: str) -> str | None:
        supplied = str(password or "")
        with self._lock:
            now = time.time()
            if self._locked_until(ip, now) > 0:
                return None
            expected = self._password.encode("utf-8")
            if not hmac.compare_digest(supplied.encode("utf-8"), expected):
                self._failures.setdefault(ip, []).append(now)
                if len(self._failures) > 2048:
                    cutoff = now - LOGIN_WINDOW_SECONDS
                    self._failures = {
                        key: [stamp for stamp in stamps if stamp > cutoff]
                        for key, stamps in self._failures.items()
                    }
                    self._failures = {
                        key: stamps for key, stamps in self._failures.items() if stamps
                    }
                token = None
            else:
                self._failures.pop(ip, None)
                for known, expiry in list(self._sessions.items()):
                    if expiry <= now:
                        self._sessions.pop(known, None)
                token = secrets.token_urlsafe(32)
                self._sessions[token] = now + self._ttl
        if token is None:
            time.sleep(LOGIN_PENALTY_SECONDS)
        return token

    def verify(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            now = time.time()
            expiry = self._sessions.get(token)
            if not expiry or expiry <= now:
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = now + self._ttl
            return True

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)


def normalize_token(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    lowered = text.lower()
    for prefix in ("authorization:", "authorization", "bearer "):
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text.strip().strip("'\"")


def normalize_cookie(raw: str) -> str:
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    match = re.search(r"(?:^|[;\s])cf_clearance=([^;\s]+)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    return text.strip().strip(";").strip("'\"")


def _snowflake_age(account_id: str) -> int | None:
    try:
        stamp = (int(account_id) >> 22) + 1420070400000
    except (TypeError, ValueError):
        return None
    return int(stamp)


def _avatar_url(user: dict) -> str:
    account_id = str(user.get("id", "0"))
    avatar = user.get("avatar")
    if not avatar:
        try:
            index = (int(account_id) >> 22) % 6
        except (TypeError, ValueError):
            index = 0
        return f"https://cdn.discordapp.com/embed/avatars/{index}.png"
    ext = "gif" if str(avatar).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/avatars/{account_id}/{avatar}.{ext}?size=160"


def _banner_url(user: dict) -> str | None:
    account_id = str(user.get("id", "0"))
    banner = user.get("banner")
    if not banner:
        return None
    ext = "gif" if str(banner).startswith("a_") else "png"
    return f"https://cdn.discordapp.com/banners/{account_id}/{banner}.{ext}?size=600"


class LogStore:
    def __init__(self, limit: int = MAX_LOG_LINES) -> None:
        self._limit = limit
        self._lines: list[dict] = []
        self._next_id = 1
        self._lock = threading.Lock()

    def add(self, level: str, text: str) -> None:
        with self._lock:
            entry = {
                "id": self._next_id,
                "level": level,
                "time": time.strftime("%H:%M:%S"),
                "text": str(text),
            }
            self._next_id += 1
            self._lines.append(entry)
            if len(self._lines) > self._limit:
                del self._lines[: len(self._lines) - self._limit]

    def since(self, after: int) -> list[dict]:
        with self._lock:
            return [entry for entry in self._lines if entry["id"] > after]


class WebSink:
    """Console-compatible adapter that feeds engine output to the UI log."""

    RED = ORANGE = GREEN = YELLOW = CYAN = WHITE = GREY = MAGENTA = RESET = ""
    RULE = "─" * 48

    def __init__(self, logs: LogStore, prefix: str = "") -> None:
        self.logs = logs
        self.prefix = prefix

    def _emit(self, level: str, target: str, message: str) -> None:
        text = f"{target} · {message}" if target else str(message)
        self.logs.add(level, f"{self.prefix}{text}")

    def ok(self, target: str, detail: str) -> None:
        self._emit("ok", target, detail)

    def sent(self, target: str, detail: str) -> None:
        self._emit("sent", target, detail)

    def live(self, target: str, detail: str) -> None:
        self._emit("live", target, detail)

    def warn(self, target: str, detail: str) -> None:
        self._emit("warn", target, detail)

    def err(self, target: str, detail: str) -> None:
        self._emit("error", target, detail)

    def info(self, detail: str) -> None:
        self._emit("info", "", detail)

    def log(self, message: str) -> None:
        self._emit("info", "", message)

    def success(self, target: str, message: str) -> None:
        self._emit("ok", target, message)

    def dispatched(self, target: str, message: str) -> None:
        self._emit("sent", target, message)

    def failure(self, target: str, message: str) -> None:
        self._emit("error", target, message)

    def warning(self, target: str, message: str) -> None:
        self._emit("warn", target, message)

    def separator(self) -> None:
        self.logs.add("muted", "─" * 46)

    def rule(self) -> None:
        self.logs.add("muted", "─" * 46)

    def clear_viewport(self) -> None:
        pass


class AppState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.logs = LogStore()
        self.sink = WebSink(self.logs)
        self.viewer_sink = WebSink(self.logs, prefix="[حساب 2] ")
        self.token = ""
        self.cookie = ""
        self.account: dict | None = None
        self.alt_token = ""
        self.alt_account: dict | None = None
        self.engine: stream_engine.GatewayStreamEngine | None = None
        self.viewer_engine: stream_engine.GatewayStreamEngine | None = None
        self.viewer_generation = 0
        self.claim = {"running": False, "done": False, "total": 0, "accepted": 0, "error": ""}
        self.stream = {
            "running": False,
            "status": "غير متصل",
            "startedAt": 0.0,
            "guild": "",
            "channel": "",
        }
        self.viewer = {
            "running": False,
            "status": "غير متصل",
            "startedAt": 0.0,
            "guild": "",
            "channel": "",
        }
        self.pinged = False
        self.last_ping = 0.0
        self.close_requested_at = 0.0
        self.closed = threading.Event()
        self._scrub_state_files()

    def _scrub_state_files(self) -> None:
        """Remove credential fields left behind by older versions."""
        try:
            games.AccountSessionState.restore().persist()
        except Exception:
            pass
        try:
            stream_engine.State.load().save()
        except Exception:
            pass

    # ------------------------------------------------------------ credentials
    def _ensure_idle(self) -> None:
        if self.claim["running"]:
            raise RuntimeError("انتظر انتهاء المطالبة قبل تغيير بيانات الدخول.")
        if self.stream["running"]:
            raise RuntimeError("أوقف البث قبل تغيير بيانات الدخول.")

    def set_token(self, raw: str) -> dict:
        token = normalize_token(raw)
        if not token:
            raise ValueError("التوكن فارغ.")
        with self.lock:
            self._ensure_idle()
        account = self._fetch_me(token)
        with self.lock:
            self.token = token
            self.account = account
        self.logs.add("ok", f"تم التحقق من الحساب: {account['displayName']} ({account['id']}).")
        return account

    def set_cookie(self, raw: str) -> None:
        cookie = normalize_cookie(raw)
        if not cookie:
            raise ValueError("قيمة cf_clearance فارغة.")
        with self.lock:
            self._ensure_idle()
            self.cookie = cookie
        self.logs.add("info", "تم حفظ كوكي cf_clearance في الذاكرة.")

    def _ensure_viewer_idle(self) -> None:
        if self.viewer["running"]:
            raise RuntimeError("أوقف المشاهد أولاً قبل تغيير بيانات الحساب الثاني.")

    def set_alt_token(self, raw: str) -> dict:
        token = normalize_token(raw)
        if not token:
            raise ValueError("توكن الحساب الثاني فارغ.")
        with self.lock:
            self._ensure_viewer_idle()
            main_id = (self.account or {}).get("id")
        account = self._fetch_me(token)
        with self.lock:
            if main_id and account["id"] == main_id:
                raise ValueError("هذا نفس الحساب الأساسي — استخدم حساب ديسكورد مختلفاً للمشاهدة.")
            self.alt_token = token
            self.alt_account = account
        self.logs.add(
            "ok", f"تم التحقق من حساب المشاهدة: {account['displayName']} ({account['id']})."
        )
        return account

    def clear_alt(self) -> None:
        with self.lock:
            self._ensure_viewer_idle()
            self.alt_token = ""
            self.alt_account = None
        self.logs.add("warn", "تم مسح بيانات الحساب الثاني من الذاكرة.")

    def clear(self) -> None:
        with self.lock:
            if self.claim["running"]:
                raise RuntimeError("انتظر انتهاء المطالبة أو أعد المحاولة بعدها قبل مسح البيانات.")
            self.token = ""
            self.cookie = ""
            self.account = None
            self.alt_token = ""
            self.alt_account = None
            engine = self.engine
            self.engine = None
            viewer_engine = self.viewer_engine
            self.viewer_engine = None
            self.viewer_generation += 1
        if engine is not None:
            engine.running = False
        if viewer_engine is not None:
            viewer_engine.running = False
        self.stream.update({"running": False, "status": "غير متصل", "startedAt": 0.0})
        self.viewer.update({"running": False, "status": "غير متصل", "startedAt": 0.0})
        self.logs.add("warn", "تم مسح بيانات الدخول من الذاكرة.")

    def _fetch_me(self, token: str) -> dict:
        request = urllib.request.Request(
            f"{games.DISCORD_USERS_API}/@me",
            headers={
                "authorization": token,
                "user-agent": games.USER_AGENT_IDENTIFIER,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("رفض ديسكورد هذا التوكن (401). تأكد أنك نسخته كاملاً.") from exc
            raise RuntimeError(f"تعذر التحقق من التوكن (HTTP {exc.code}).") from exc
        except Exception as exc:
            raise RuntimeError(f"تعذر الاتصال بديسكورد: {exc}") from exc

        username = payload.get("username") or "?"
        return {
            "id": str(payload.get("id", "")),
            "username": username,
            "globalName": payload.get("global_name"),
            "displayName": payload.get("global_name") or username,
            "avatarUrl": _avatar_url(payload),
        }

    # ---------------------------------------------------------------- profile
    def fetch_profile(self) -> dict:
        with self.lock:
            token = self.token
            account = self.account
        if not token:
            raise RuntimeError("سجّل الدخول أولاً.")

        account_id = games.extract_account_snowflake(token) or (account or {}).get("id", "")
        if not account_id:
            raise RuntimeError("تعذر قراءة معرف الحساب من التوكن.")

        url = (
            f"{games.DISCORD_USERS_API}/{account_id}/profile"
            "?with_mutual_guilds=false&with_mutual_friends_count=false"
        )
        request = urllib.request.Request(
            url,
            headers={
                "authorization": token,
                "user-agent": games.USER_AGENT_IDENTIFIER,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("رفض ديسكورد التوكن أثناء جلب البروفايل (401).") from exc
            raise RuntimeError(f"تعذر جلب البروفايل (HTTP {exc.code}).") from exc
        except Exception as exc:
            raise RuntimeError(f"تعذر الاتصال بديسكورد: {exc}") from exc

        user = payload.get("user") or {}
        details = payload.get("user_profile") or {}
        username = user.get("username") or (account or {}).get("username") or "?"
        global_name = user.get("global_name")

        badges = []
        for badge in payload.get("badges") or []:
            badge_id = str(badge.get("id") or "badge")
            badges.append({
                "id": badge_id,
                "description": str(badge.get("description") or badge_id),
            })

        guild_badges = []
        for badge in payload.get("guild_badges") or []:
            badge_id = str(badge.get("id") or "badge")
            guild_badges.append({
                "id": badge_id,
                "description": str(badge.get("description") or badge_id),
            })

        connected = []
        for entry in payload.get("connected_accounts") or []:
            connected.append({
                "type": str(entry.get("type") or ""),
                "name": str(entry.get("name") or ""),
                "verified": bool(entry.get("verified")),
            })

        accent = user.get("accent_color")
        if accent is None:
            accent = details.get("accent_color")

        profile = {
            "id": str(user.get("id") or account_id),
            "username": username,
            "globalName": global_name,
            "displayName": global_name or username,
            "avatarUrl": _avatar_url(user) if user else (account or {}).get("avatarUrl", ""),
            "bannerUrl": _banner_url(user) if user else None,
            "accentColor": accent,
            "bio": str(details.get("bio") or ""),
            "pronouns": str(details.get("pronouns") or ""),
            "badges": badges,
            "guildBadges": guild_badges,
            "connected": connected,
            "premiumSince": payload.get("premium_since"),
            "createdAt": _snowflake_age(account_id),
        }
        self.logs.add("ok", f"تم جلب البروفايل: {profile['displayName']} · {len(badges)} بادج.")
        return profile

    # ------------------------------------------------------------------ claim
    def start_claim(self, count: int, hours: float) -> None:
        if count < 0 or hours <= 0:
            raise ValueError("أدخل عدد ألعاب وساعات صحيحة (أكبر من صفر).")
        with self.lock:
            if self.claim["running"]:
                raise RuntimeError("هناك عملية مطالبة قيد التنفيذ.")
            token, cookie = self.token, self.cookie
            if not token or not cookie:
                raise RuntimeError("أدخل التوكن وكوكي cf_clearance أولاً.")
            self.claim = {"running": True, "done": False, "total": 0, "accepted": 0, "error": ""}
        threading.Thread(
            target=self._claim_worker, args=(token, cookie, count, hours), daemon=True
        ).start()

    def _claim_worker(self, token: str, cookie: str, count: int, hours: float) -> None:
        accepted = 0
        total = 0
        error = ""
        try:
            self.logs.add("info", "تحميل كتالوج الألعاب القابلة للكشف...")
            catalog = games.load_game_catalog()
            if not catalog:
                raise RuntimeError("تعذر تحميل كتالوج الألعاب من ديسكورد.")
            self.logs.add("ok", f"تم تحميل {len(catalog)} لعبة.")

            state = games.AccountSessionState.restore()
            state.token = token
            state.cookie = cookie
            context = games.RuntimeClientContext.initialize()

            self.logs.add("info", "مصادقة الحساب وجلب analytics token...")
            games.synchronize_credentials(state, context.encoded_properties)
            self.logs.add("ok", "تمت المصادقة.")

            fabricator = games.TelemetryEventFabricator(state, context)
            controller = games.ApplicationController()
            controller.state = state
            controller.runtime_context = context
            controller.fabricator = fabricator
            controller.catalog = catalog

            target = min(count, len(catalog)) if count else len(catalog)
            selected = controller._filter_title_selection(target)
            total = len(selected)
            with self.lock:
                self.claim["total"] = total
            duration_ms = int(hours * 3600 * 1000)
            self.logs.add(
                "info",
                f"إرسال {len(selected)} لعبة × {hours:g} ساعة ({len(selected) * hours:,.0f} ساعة إجمالاً)...",
            )

            pipeline = games.IngestionPipeline(fabricator, self.sink)
            accepted = pipeline.execute_stream(
                selected,
                lambda title: fabricator.assemble_complete_session(title, duration_ms),
            )
            state.persist()
            self.logs.add(
                "ok",
                f"انتهى: {accepted}/{total} لعبة ({accepted * hours:,.0f} ساعة). "
                "تحديث البادج يستغرق عادة 24-48 ساعة.",
            )
        except Exception as exc:
            error = str(exc)
            self.logs.add("error", f"فشلت العملية: {exc}")
        finally:
            with self.lock:
                self.claim.update({
                    "running": False,
                    "done": True,
                    "accepted": accepted,
                    "total": total,
                    "error": error,
                })

    # ----------------------------------------------------------------- stream
    def start_stream(self, guild_id: str, channel_id: str, with_viewer: bool = True) -> None:
        guild_id = str(guild_id or "").strip()
        channel_id = str(channel_id or "").strip()
        if not guild_id.isdigit() or not channel_id.isdigit():
            raise ValueError("أدخل Server ID و Voice Channel ID كأرقام فقط.")
        viewer_token = ""
        viewer_generation = 0
        with self.lock:
            if self.stream["running"]:
                raise RuntimeError("البث يعمل بالفعل.")
            token, cookie = self.token, self.cookie
            if not token or not cookie:
                raise RuntimeError("أدخل التوكن وكوكي cf_clearance أولاً.")
            self.stream.update({
                "running": True,
                "status": "جاري الاتصال...",
                "startedAt": time.time(),
                "guild": guild_id,
                "channel": channel_id,
            })
            if with_viewer:
                if self.alt_token:
                    if self.viewer["running"]:
                        self.logs.add("info", "المشاهد يعمل بالفعل — سيستمر في نفس الروم.")
                    else:
                        viewer_token = self.alt_token
                        self.viewer_generation += 1
                        viewer_generation = self.viewer_generation
                        self.viewer.update({
                            "running": True,
                            "status": "جاري اتصال المشاهد...",
                            "startedAt": time.time(),
                            "guild": guild_id,
                            "channel": channel_id,
                        })
                else:
                    self.logs.add(
                        "warn",
                        "لا يوجد حساب مشاهدة ثانٍ — أضفه من بطاقة الحساب الثاني "
                        "لضمان احتساب ساعات البث.",
                    )
        if viewer_token:
            threading.Thread(
                target=self._viewer_worker,
                args=(viewer_token, guild_id, channel_id, viewer_generation),
                daemon=True,
            ).start()
        threading.Thread(
            target=self._stream_worker,
            args=(token, cookie, guild_id, channel_id),
            daemon=True,
        ).start()

    def _viewer_worker(
        self, token: str, guild_id: str, channel_id: str, generation: int
    ) -> None:
        try:
            state = stream_engine.State()
            state.token = token
            session = stream_engine.Session.new()

            ok, detail = stream_engine.validate_stream_target(
                token,
                session.super_props,
                guild_id,
                channel_id,
                self.viewer_sink,
                require_stream=False,
            )
            if not ok:
                raise RuntimeError(f"تعذر تجهيز حساب المشاهدة ({detail}).")

            engine = stream_engine.GatewayStreamEngine(
                state, session, self.viewer_sink, viewer_only=True
            )
            with self.lock:
                if generation != self.viewer_generation or not self.viewer["running"]:
                    return
                self.viewer_engine = engine
                self.viewer["status"] = "متصل — مشاهد"
                self.viewer["startedAt"] = time.time()
            self.logs.add("ok", "الحساب الثاني دخل الروم كمشاهد — ساعات البث ستُحتسب.")
            engine.start_stream(guild_id, channel_id, wait_for_input=False)
        except Exception as exc:
            self.logs.add("error", f"توقف المشاهد: {exc}")
        finally:
            with self.lock:
                if generation != self.viewer_generation:
                    return
                self.viewer_engine = None
                self.viewer.update({
                    "running": False,
                    "status": "غير متصل",
                    "startedAt": 0.0,
                })

    def _stop_viewer_locked(self) -> None:
        engine = self.viewer_engine
        self.viewer_engine = None
        if engine is not None:
            engine.running = False

    def _stream_worker(self, token: str, cookie: str, guild_id: str, channel_id: str) -> None:
        try:
            state = stream_engine.State()
            state.token = token
            state.cookie = cookie
            session = stream_engine.Session.new()

            ok, detail = stream_engine.validate_stream_target(
                token, session.super_props, guild_id, channel_id, self.sink
            )
            if not ok:
                raise RuntimeError(detail)

            engine = stream_engine.GatewayStreamEngine(state, session, self.sink)
            with self.lock:
                self.engine = engine
                self.stream["status"] = "متصل — LIVE"
                self.stream["startedAt"] = time.time()
            engine.start_stream(guild_id, channel_id, wait_for_input=False)
        except Exception as exc:
            self.logs.add("error", f"توقف البث: {exc}")
        finally:
            with self.lock:
                self.engine = None
                self.stream.update({
                    "running": False,
                    "status": "غير متصل",
                    "startedAt": 0.0,
                })
                # The viewer only exists to support the broadcast, so it leaves
                # the room together with the streamer.
                self.viewer_generation += 1
                self._stop_viewer_locked()
                self.viewer.update({
                    "running": False,
                    "status": "غير متصل",
                    "startedAt": 0.0,
                })

    def stop_stream(self) -> None:
        with self.lock:
            engine = self.engine
            viewer_engine = self.viewer_engine
            if engine is None and viewer_engine is None and not self.stream["running"]:
                raise RuntimeError("لا يوجد بث نشط.")
            self.stream["status"] = "جاري الإيقاف..."
            self.viewer_generation += 1
            self._stop_viewer_locked()
            self.viewer.update({"running": False, "startedAt": 0.0})
        self.logs.add("warn", "جاري إيقاف البث...")
        if engine is not None:
            engine.running = False

    # ------------------------------------------------------------------ state
    def mark_ping(self) -> None:
        with self.lock:
            self.pinged = True
            self.last_ping = time.time()
            self.close_requested_at = 0.0

    def request_quit(self) -> None:
        # A reload also fires pagehide; the launcher only exits if no ping
        # clears this request within its grace window.
        with self.lock:
            self.close_requested_at = time.time()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "authenticated": bool(self.token),
                "hasCookie": bool(self.cookie),
                "account": self.account,
                "altAuthenticated": bool(self.alt_token),
                "altAccount": self.alt_account,
                "claim": dict(self.claim),
                "stream": dict(self.stream),
                "viewer": dict(self.viewer),
            }


class AppRequestHandler(BaseHTTPRequestHandler):
    server_version = "BadgeMaximizer/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        pass

    @property
    def state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    # ------------------------------------------------------------- request meta
    def _client_ip(self) -> str:
        if self.server.trust_proxy:  # type: ignore[attr-defined]
            # Caddy appends the real client IP to any incoming header, so the
            # last entry is the one a client cannot spoof.
            forwarded = [
                part.strip()
                for part in (self.headers.get("X-Forwarded-For") or "").split(",")
                if part.strip()
            ]
            if forwarded:
                return forwarded[-1]
        return self.client_address[0] if self.client_address else "unknown"

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _session_ok(self) -> bool:
        auth = self.server.auth  # type: ignore[attr-defined]
        if auth is None:
            return True
        return bool(auth.verify(self._cookie(SESSION_COOKIE)))

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        if host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            return True
        if not self.server.public_mode:  # type: ignore[attr-defined]
            self._send_json(403, {"error": "forbidden"})
            return False
        allowed = self.server.allowed_hosts  # type: ignore[attr-defined]
        if not allowed:
            return True
        if host.split(":")[0] in allowed:
            return True
        self._send_json(403, {"error": "forbidden"})
        return False

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        allowed = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
        if self.server.public_mode:  # type: ignore[attr-defined]
            hosts = set(self.server.allowed_hosts)  # type: ignore[attr-defined]
            if not hosts:
                hosts = {(self.headers.get("Host") or "").split(":")[0].lower()}
            for host in hosts:
                if host:
                    allowed.add(f"https://{host}")
                    allowed.add(f"http://{host}")
        if origin in allowed:
            return True
        self._send_json(403, {"error": "forbidden"})
        return False

    def _key_ok(self) -> bool:
        supplied = self.headers.get("X-App-Key") or ""
        if secrets.compare_digest(supplied, self.server.app_key):  # type: ignore[attr-defined]
            return True
        self._send_json(403, {"error": "invalid app key"})
        return False

    def _api_guard(self) -> bool:
        if not self._session_ok():
            self._drain_body()
            self._send_json(401, {"error": "unauthorized"})
            return False
        if not self._key_ok():
            self._drain_body()
            return False
        return True

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ValueError("طلب غير صالح.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON غير صالح.") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON غير صالح.")
        return payload

    def _drain_body(self) -> None:
        # Keep-alive connections reuse the socket, so any unread body would be
        # parsed as the start of the next request.
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except Exception:
            self.close_connection = True

    def _security_headers(self, nonce: str = "") -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if nonce:
            script_src = f"'self' 'nonce-{nonce}'"
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; object-src 'none'; "
                "frame-ancestors 'none'; form-action 'self'; "
                f"script-src {script_src}; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https://cdn.discordapp.com; "
                "connect-src 'self'; font-src 'self'",
            )
        if self.server.cookie_secure and (  # type: ignore[attr-defined]
            not self.server.trust_proxy  # type: ignore[attr-defined]
            or (self.headers.get("X-Forwarded-Proto") or "").lower() == "https"
        ):
            self.send_header("Strict-Transport-Security", "max-age=31536000")

    def _session_cookie(self, token: str) -> None:
        value = token
        age = SESSION_TTL_SECONDS
        if not token:
            value = ""
            age = 0
        parts = [
            f"{SESSION_COOKIE}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
            f"Max-Age={age}",
        ]
        if self.server.cookie_secure:  # type: ignore[attr-defined]
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_name: str, content_type: str) -> None:
        path = os.path.join(self.server.ui_dir, file_name)  # type: ignore[attr-defined]
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._security_headers()
        self.end_headers()

    def _serve_page(self, file_name: str, *, app_key: bool = False) -> None:
        path = os.path.join(self.server.ui_dir, file_name)  # type: ignore[attr-defined]
        try:
            with open(path, encoding="utf-8") as handle:
                html = handle.read()
        except OSError:
            self._send_json(500, {"error": "ui missing"})
            return
        nonce = secrets.token_urlsafe(16)
        html = html.replace("__CSP_NONCE__", nonce)
        if app_key:
            html = html.replace('"__APP_KEY__"', json.dumps(self.server.app_key))  # type: ignore[attr-defined]
            html = html.replace(
                "__PUBLIC_MODE_VALUE__",
                "true" if self.server.public_mode else "false",  # type: ignore[attr-defined]
            )
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(nonce)
        self.end_headers()
        self.wfile.write(body)

    def _serve_index(self) -> None:
        self._serve_page("index.html", app_key=True)

    def _serve_login(self) -> None:
        self._serve_page("login.html")

    def _handle_login(self) -> None:
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        auth = self.server.auth  # type: ignore[attr-defined]
        ip = self._client_ip()
        wait = auth.lockout_remaining(ip)
        if wait > 0:
            minutes = max(1, math.ceil(wait / 60))
            self._send_json(
                429, {"error": f"محاولات دخول كثيرة — حاول بعد {minutes} دقيقة."}
            )
            return
        token = auth.login(str(payload.get("password") or ""), ip)
        if not token:
            self._send_json(401, {"error": "كلمة المرور غير صحيحة."})
            return
        self.state.logs.add("ok", f"تم فتح لوحة التحكم من {ip}.")
        body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._session_cookie(token)
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/healthz":
            self._send_json(
                200, {"ok": True, "public": bool(self.server.public_mode)}  # type: ignore[attr-defined]
            )
            return
        if not self._host_ok():
            return

        public = bool(self.server.public_mode)  # type: ignore[attr-defined]
        if path in ("/", "/index.html"):
            if public and not self._session_ok():
                self._redirect("/login")
                return
            self._serve_index()
            return
        if path == "/login":
            if not public:
                self._redirect("/")
                return
            if self._session_ok():
                self._redirect("/")
                return
            self._serve_login()
            return
        if path in ("/styles.css", "/app.js", "/favicon.svg"):
            content_type = {
                "/styles.css": "text/css; charset=utf-8",
                "/app.js": "application/javascript; charset=utf-8",
                "/favicon.svg": "image/svg+xml",
            }[path]
            self._send_file(path.lstrip("/"), content_type)
            return
        if path == "/api/state":
            if not self._api_guard():
                return
            self._send_json(200, self.state.snapshot())
            return
        if path == "/api/profile":
            if not self._api_guard():
                return
            try:
                self._send_json(200, {"profile": self.state.fetch_profile()})
            except (ValueError, RuntimeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception:
                self._send_json(500, {"error": "حدث خطأ غير متوقع."})
            return
        if path == "/api/logs":
            if not self._api_guard():
                return
            try:
                after = int(parse_qs(urlparse(self.path).query).get("after", ["0"])[0])
            except ValueError:
                after = 0
            self._send_json(200, {"lines": self.state.logs.since(after)})
            return
        self._send_json(404, {"error": "not found"})

    # ------------------------------------------------------------------- POST
    def do_POST(self) -> None:
        if not self._origin_ok() or not self._host_ok():
            self._drain_body()
            return
        path = urlparse(self.path).path
        public = bool(self.server.public_mode)  # type: ignore[attr-defined]

        if path == "/api/login":
            if not public:
                self._drain_body()
                self._send_json(404, {"error": "not found"})
                return
            self._handle_login()
            return
        if not path.startswith("/api/"):
            self._drain_body()
            self._send_json(404, {"error": "not found"})
            return
        if not self._api_guard():
            return

        try:
            if path == "/api/logout":
                self._drain_body()
                token = self._cookie(SESSION_COOKIE)
                if public:
                    self.server.auth.revoke(token)  # type: ignore[attr-defined]
                body = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                if public:
                    self._session_cookie("")
                self._security_headers()
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/ping":
                self._drain_body()
                self.state.mark_ping()
                self._send_json(200, {"ok": True})
                return
            if path == "/api/quit":
                self._drain_body()
                self._send_json(200, {"ok": True})
                if not public:
                    self.state.request_quit()
                return

            payload = self._read_json()

            if path == "/api/token":
                account = self.state.set_token(str(payload.get("token") or ""))
                self._send_json(200, {"account": account})
                return
            if path == "/api/cookie":
                self.state.set_cookie(str(payload.get("cookie") or ""))
                self._send_json(200, {"ok": True})
                return
            if path == "/api/alt/token":
                account = self.state.set_alt_token(str(payload.get("token") or ""))
                self._send_json(200, {"account": account})
                return
            if path == "/api/alt/clear":
                self.state.clear_alt()
                self._send_json(200, {"ok": True})
                return
            if path == "/api/clear":
                self.state.clear()
                self._send_json(200, {"ok": True})
                return
            if path == "/api/open-login":
                if public:
                    # The panel runs on a remote host, so the browser must open
                    # Discord on the client machine instead.
                    self._send_json(200, {"ok": True, "url": DISCORD_LOGIN_URL})
                else:
                    threading.Thread(
                        target=webbrowser.open, args=(DISCORD_LOGIN_URL,), daemon=True
                    ).start()
                    self._send_json(200, {"ok": True})
                return
            if path == "/api/claim":
                count = int(payload.get("count") or 0)
                hours = float(payload.get("hours") or 0)
                self.state.start_claim(count, hours)
                self._send_json(200, {"ok": True})
                return
            if path == "/api/stream/start":
                self.state.start_stream(
                    str(payload.get("guildId") or ""),
                    str(payload.get("channelId") or ""),
                    with_viewer=bool(payload.get("withViewer", True)),
                )
                self._send_json(200, {"ok": True})
                return
            if path == "/api/stream/stop":
                self.state.stop_stream()
                self._send_json(200, {"ok": True})
                return

            self._send_json(404, {"error": "not found"})
        except (ValueError, RuntimeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            self._send_json(500, {"error": "حدث خطأ غير متوقع."})


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionError, TimeoutError)):
            return
        try:
            self.app_state.logs.add(  # type: ignore[attr-defined]
                "error", f"خطأ في الخادم المحلي: {error.__class__.__name__}: {error}"
            )
        except Exception:
            pass


def create_server(
    state: AppState,
    ui_dir: str,
    *,
    public_mode: bool = False,
    password: str = "",
    host: str = "127.0.0.1",
    port: int = 0,
    allowed_hosts: list[str] | None = None,
    trust_proxy: bool = False,
    cookie_secure: bool | None = None,
) -> ThreadingHTTPServer:
    if public_mode and len(password) < MIN_PANEL_PASSWORD_LENGTH:
        raise ValueError(
            f"كلمة مرور اللوحة يجب أن تكون {MIN_PANEL_PASSWORD_LENGTH} حرفاً على الأقل."
        )
    server = LocalHTTPServer((host, port), AppRequestHandler)
    server.app_state = state  # type: ignore[attr-defined]
    server.app_key = secrets.token_urlsafe(24)  # type: ignore[attr-defined]
    server.ui_dir = ui_dir  # type: ignore[attr-defined]
    server.public_mode = bool(public_mode)  # type: ignore[attr-defined]
    server.auth = AuthManager(password) if public_mode else None  # type: ignore[attr-defined]
    server.allowed_hosts = frozenset(  # type: ignore[attr-defined]
        host_name.strip().lower()
        for host_name in (allowed_hosts or [])
        if host_name and host_name.strip()
    )
    server.trust_proxy = bool(trust_proxy)  # type: ignore[attr-defined]
    server.cookie_secure = (  # type: ignore[attr-defined]
        bool(public_mode) if cookie_secure is None else bool(cookie_secure)
    )
    state.server = server
    return server


def resource_dir() -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "webui")
