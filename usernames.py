"""Short Discord username availability checks and renames.

Checks use Discord's public username-attempt endpoint; renames use the
in-memory account token and talk to discord.com only.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request

import stream as stream_engine

ATTEMPT_URL = "https://discord.com/api/v9/unique-username/username-attempt-unauthed"
ME_URL = "https://discord.com/api/v9/users/@me"
USERNAME_RE = re.compile(r"^[a-z0-9._]{2,32}$")
RESERVED = ("discord", "everyone", "here")

HEADERS = {
    "content-type": "application/json",
    "accept": "*/*",
    "user-agent": stream_engine.USER_AGENT,
    "origin": "https://discord.com",
    "referer": "https://discord.com/",
}


def _json_body(payload) -> str:
    if isinstance(payload, dict):
        errors = payload.get("errors") or {}
        messages: list[str] = []

        def walk(node) -> None:
            if isinstance(node, dict):
                for item in node.get("_errors") or []:
                    text = item.get("message") or item.get("code")
                    if text:
                        messages.append(str(text))
                for key, value in node.items():
                    if key != "_errors":
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(errors)
        if messages:
            return "، ".join(dict.fromkeys(messages))
        return str(payload.get("message") or "")
    return ""


def _post(url: str, body: dict, headers: dict | None = None, method: str = "POST"):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method=method,
        headers={**HEADERS, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {"message": raw[:200].decode("utf-8", "replace")}
        return exc.code, payload
    except Exception as exc:
        return 0, {"message": str(exc)}


def check_availability(name: str) -> tuple[bool | None, float, str]:
    """Return (available, retry_after, error). available=None means unknown."""
    status, payload = _post(ATTEMPT_URL, {"username": name})
    if status == 200:
        return not bool(payload.get("taken")), 0.0, ""
    if status == 429:
        try:
            retry = float(payload.get("retry_after") or 5)
        except (TypeError, ValueError):
            retry = 5.0
        return None, retry, "ديسكورد حدّ الطلبات مؤقتاً."
    if status == 400:
        return None, 0.0, _json_body(payload) or "اسم غير صالح."
    return None, 0.0, _json_body(payload) or f"HTTP {status}"


def valid_username(name: str) -> str:
    value = str(name or "").strip().lower()
    if not USERNAME_RE.match(value):
        raise ValueError(
            "اليوزر يجب أن يكون 2–32 حرفاً إنجليزياً صغيراً أو رقماً أو . أو _."
        )
    if any(word in value for word in RESERVED):
        raise ValueError("هذا الاسم محجوز من ديسكورد.")
    if value.startswith(".") or value.endswith(".") or ".." in value:
        raise ValueError("لا يمكن أن تبدأ أو تنتهي بنقطة أو تحتوي نقطتين متتاليتين.")
    return value


class UsernameTool:
    def __init__(self, logs) -> None:
        self.logs = logs
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        self.results: list[str] = []
        self.error = ""
        self.checked = 0
        self.length = 3
        self.digits = True
        self.started = 0.0
        self.finished = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "results": list(self.results),
                "error": self.error,
                "checked": self.checked,
                "length": self.length,
                "digits": self.digits,
                "startedAt": self.started,
                "finishedAt": self.finished,
            }

    def start_scan(self, length: int, count: int, digits: bool) -> None:
        try:
            length = int(length)
            count = int(count)
        except (TypeError, ValueError):
            raise ValueError("قيم البحث غير صحيحة.") from None
        if length < 2 or length > 8:
            raise ValueError("الطول يجب أن يكون بين 2 و 8.")
        count = max(1, min(count, 30))
        with self._lock:
            if self.running:
                raise RuntimeError("يوجد بحث جارٍ بالفعل.")
            self.running = True
            self.results = []
            self.error = ""
            self.checked = 0
            self.length = length
            self.digits = bool(digits)
            self.started = time.time()
            self.finished = 0.0
            self._stop.clear()
        self._thread = threading.Thread(
            target=self._scan, args=(length, count, bool(digits)), daemon=True, name="name-scan"
        )
        self._thread.start()
        self.logs.add(
            "info",
            f"بدء فحص {count} يوزر بطول {length} — الفحص يتم ببطء لتجنب الحظر.",
        )

    def _scan(self, length: int, count: int, digits: bool) -> None:
        alphabet = "abcdefghijklmnopqrstuvwxyz" + ("0123456789" if digits else "")
        seen: set[str] = set()
        try:
            for _ in range(count):
                if self._stop.is_set():
                    break
                name = ""
                for _attempt in range(20):
                    candidate = "".join(random.choice(alphabet) for _ in range(length))
                    if candidate not in seen:
                        name = candidate
                        break
                if not name:
                    break
                seen.add(name)
                available, retry, error = check_availability(name)
                with self._lock:
                    self.checked += 1
                if available is True:
                    with self._lock:
                        self.results.append(name)
                    self.logs.add("ok", f"متاح: {name}")
                elif available is None and retry:
                    self.logs.add("warn", f"حد الطلبات — انتظار {int(retry)} ثانية.")
                    wait = min(retry, 25.0)
                    if retry > 30:
                        with self._lock:
                            self.error = "ديسكورد حظر الفحص مؤقتاً — حاول لاحقاً."
                        break
                    if self._stop.wait(wait):
                        break
                    continue
                elif available is None and error and not retry and "صالح" in error:
                    self.logs.add("warn", f"تخطي {name}: {error}")
                time.sleep(random.uniform(1.1, 1.9))
        finally:
            with self._lock:
                self.running = False
                self.finished = time.time()
                found = len(self.results)
                checked = self.checked
            if not self.error:
                self.logs.add(
                    "info", f"انتهى الفحص: {found} متاح من {checked} اسم تم فحصه."
                )

    def check_one(self, name: str) -> bool:
        value = valid_username(name)
        with self._lock:
            if self.running:
                raise RuntimeError("انتظر انتهاء البحث الحالي أولاً.")
        available, retry, error = check_availability(value)
        if available is None:
            raise RuntimeError(error or "تعذر فحص الاسم.")
        if available and value not in self.results:
            with self._lock:
                self.results.insert(0, value)
        return available

    def apply(self, name: str, token: str) -> dict:
        value = valid_username(name)
        if not token:
            raise RuntimeError("سجّل الدخول أولاً.")
        status, payload = _post(
            ME_URL,
            {"username": value},
            headers={"authorization": token},
            method="PATCH",
        )
        if status not in (200, 201):
            raise RuntimeError("تعذر تغيير الاسم: " + (_json_body(payload) or f"HTTP {status}"))
        with self._lock:
            self.results = [item for item in self.results if item != value]
        self.logs.add("ok", f"تم تغيير اسم الحساب إلى {value}.")
        return {
            "username": str(payload.get("username") or value),
            "globalName": payload.get("global_name"),
        }
