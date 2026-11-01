"""Discord quest helpers driven by the in-memory account token.

Everything here talks to discord.com only. No credential is written to disk.
"""
from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

import stream as stream_engine

API_BASE = "https://discord.com/api/v9"

VIDEO_TASKS = {"WATCH_VIDEO", "WATCH_VIDEO_ON_MOBILE"}
ACTIVITY_TASKS = {"PLAY_ACTIVITY", "ACHIEVEMENT_IN_ACTIVITY"}
DESKTOP_TASKS = {"PLAY_ON_DESKTOP", "STREAM_ON_DESKTOP"}

TYPE_LABELS = {
    "WATCH_VIDEO": "مشاهدة فيديو",
    "WATCH_VIDEO_ON_MOBILE": "مشاهدة فيديو (جوال)",
    "PLAY_ON_DESKTOP": "تشغيل لعبة على الحاسوب",
    "STREAM_ON_DESKTOP": "بث اللعبة",
    "PLAY_ACTIVITY": "نشاط داخل ديسكورد",
    "ACHIEVEMENT_IN_ACTIVITY": "إنجاز داخل نشاط",
}


def _kind(key: str) -> str:
    if key in VIDEO_TASKS or "VIDEO" in key:
        return "video"
    if key in ACTIVITY_TASKS or "ACHIEVEMENT" in key:
        return "activity"
    if key.startswith("PLAY_ON_DESKTOP") or key == "PLAY_ON_DESKTOP":
        return "game"
    if key.startswith("STREAM_ON_DESKTOP") or "STREAM" in key:
        return "stream"
    return "unsupported"


def _expiry_seconds(value) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _progress_value(payload: dict, key: str) -> float:
    if not isinstance(payload, dict):
        return 0.0
    status = payload.get("userStatus")
    if isinstance(status, dict):
        payload = status
    progress = payload.get("progress")
    if isinstance(progress, dict):
        entry = progress.get(key)
        if isinstance(entry, dict):
            try:
                return float(entry.get("value") or 0)
            except (TypeError, ValueError):
                return 0.0
    stream_seconds = payload.get("streamProgressSeconds")
    if stream_seconds is not None:
        try:
            return float(stream_seconds or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _error_text(payload, status: int) -> str:
    parts: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for item in node.get("_errors") or []:
                message = item.get("message") or item.get("code")
                if message:
                    parts.append(str(message))
            for key, value in node.items():
                if key != "_errors":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(payload, dict):
        walk(payload.get("errors") or {})
    if parts:
        return "، ".join(dict.fromkeys(parts))
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or "")
    if not message:
        message = f"HTTP {status}"
    retry = payload.get("retry_after") if isinstance(payload, dict) else None
    if retry:
        message += f" (انتظر {int(float(retry))} ثانية)"
    return message


def _request(
    method: str,
    path: str,
    token: str,
    super_props: str = "",
    body: dict | None = None,
    timeout: int = 25,
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "authorization": token,
        "user-agent": stream_engine.USER_AGENT,
        "accept": "*/*",
    }
    if super_props:
        headers["x-super-properties"] = super_props
    if data is not None:
        headers["content-type"] = "application/json"
    request = urllib.request.Request(
        API_BASE + path, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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


TASK_PRIORITY = (
    "WATCH_VIDEO",
    "WATCH_VIDEO_ON_MOBILE",
    "PLAY_ACTIVITY",
    "ACHIEVEMENT_IN_ACTIVITY",
    "PLAY_ON_DESKTOP",
    "STREAM_ON_DESKTOP",
)


def _select_tasks(config: dict) -> dict:
    # taskConfigV2 is authoritative when it carries tasks; legacy taskConfig can
    # point at stale task/app metadata for the same quest.
    for source in (config.get("taskConfigV2"), config.get("taskConfig")):
        tasks = source.get("tasks") if isinstance(source, dict) else None
        if isinstance(tasks, dict) and tasks:
            return tasks
    return {}


def _pick_key(tasks: dict) -> str:
    for key in TASK_PRIORITY:
        if key in tasks:
            return key
    for key in tasks:
        if _kind(str(key)) != "unsupported":
            return str(key)
    return str(next(iter(tasks), ""))


def _parse_quest(raw: dict) -> dict:
    config = raw.get("config") or {}
    status = raw.get("userStatus") or {}
    tasks = _select_tasks(config)
    key = _pick_key(tasks)
    task = tasks.get(key) or {}
    application = config.get("application") or {}
    if not application:
        applications = task.get("applications") or []
        application = applications[0] if applications else {}
    try:
        target = float(task.get("target") or 0)
    except (TypeError, ValueError):
        target = 0.0
    progress_map = status.get("progress") or {}
    progress = 0.0
    if isinstance(progress_map.get(key), dict):
        try:
            progress = float(progress_map[key].get("value") or 0)
        except (TypeError, ValueError):
            progress = 0.0
    elif status.get("streamProgressSeconds") is not None:
        try:
            progress = float(status.get("streamProgressSeconds") or 0)
        except (TypeError, ValueError):
            progress = 0.0
    completed = bool(status.get("completedAt"))
    claimed = bool(status.get("claimedAt"))
    rew = config.get("rewardsConfig") or {}
    rewards = rew.get("rewards") if isinstance(rew.get("rewards"), list) else []
    reward = ""
    if rewards:
        first = rewards[0] or {}
        reward = str((first.get("messages") or {}).get("name") or first.get("name") or "")
    name = str(
        (config.get("messages") or {}).get("questName")
        or application.get("name")
        or "مهمة"
    )
    task_app = (task.get("applications") or [{}])[0] or {}
    app_id = str(task_app.get("id") or application.get("id") or "")
    kind = _kind(key) if key else "unsupported"
    return {
        "id": str(raw.get("id") or ""),
        "name": name,
        "appId": app_id,
        "appName": str(task_app.get("name") or application.get("name") or ""),
        "taskKey": key,
        "typeLabel": TYPE_LABELS.get(key, key or "غير معروف"),
        "kind": kind,
        "target": target,
        "progress": progress,
        "enrolled": bool(status.get("enrolledAt")),
        "completed": completed,
        "claimed": claimed,
        "claimable": completed and not claimed,
        "supported": kind != "unsupported" and not completed,
        "expiresAt": _expiry_seconds(config.get("expiresAt")),
        "reward": reward,
    }


class QuestManager:
    def __init__(self, logs, get_token) -> None:
        self.logs = logs
        self._get_token = get_token
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._session = None
        self._channel = ""
        self.quests: list[dict] = []
        self.refreshed = 0.0
        self.active: dict = {}

    # ------------------------------------------------------------------ helpers
    def _token(self) -> str:
        token = (self._get_token() or "").strip()
        if not token:
            raise RuntimeError("سجّل الدخول أولاً.")
        return token

    def _props(self) -> str:
        with self._lock:
            if self._session is None:
                try:
                    self._session = stream_engine.Session.new()
                except Exception:
                    self._session = False
        if not self._session:
            return ""
        return self._session.super_props

    def _find(self, quest_id: str) -> dict | None:
        with self._lock:
            for quest in self.quests:
                if quest["id"] == quest_id:
                    return dict(quest)
        return None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "quests": [dict(quest) for quest in self.quests],
                "refreshedAt": self.refreshed,
                "active": dict(self.active),
                "running": self.running,
            }

    # -------------------------------------------------------------------- list
    def refresh(self) -> list[dict]:
        token = self._token()
        status, payload = _request("GET", "/quests/@me", token, self._props())
        if status != 200:
            raise RuntimeError("تعذر جلب المهام: " + _error_text(payload, status))
        raw_quests = payload.get("quests") if isinstance(payload, dict) else None
        quests: list[dict] = []
        now = time.time()
        for raw in raw_quests or []:
            try:
                quest = _parse_quest(raw)
            except Exception:
                continue
            if not quest["id"]:
                continue
            if quest["expiresAt"] and quest["expiresAt"] < now and not quest["completed"]:
                continue
            quests.append(quest)
        quests.sort(
            key=lambda quest: (
                quest["completed"],
                not quest["enrolled"],
                quest["expiresAt"] or 0,
            )
        )
        with self._lock:
            self.quests = quests
            self.refreshed = time.time()
        self.logs.add("info", f"تم تحديث المهام: {len(quests)} مهمة متاحة.")
        return quests

    # ------------------------------------------------------------------ enroll
    def enroll(self, quest_id: str) -> None:
        token = self._token()
        status, payload = _request(
            "POST",
            f"/quests/{quest_id}/enroll",
            token,
            self._props(),
            {
                "location": 11,
                "is_targeted": False,
                "metadata_sealed": None,
                "traffic_metadata_sealed": None,
            },
        )
        if status not in (200, 201, 204):
            raise RuntimeError("تعذر قبول المهمة: " + _error_text(payload, status))
        self.logs.add("ok", "تم قبول المهمة.")
        try:
            self.refresh()
        except RuntimeError:
            pass

    # ------------------------------------------------------------------- run
    def start(self, quest_id: str) -> None:
        quest = self._find(quest_id)
        if quest is None:
            raise RuntimeError("المهمة غير موجودة — اضغط تحديث المهام.")
        if quest["completed"]:
            raise RuntimeError("هذه المهمة مكتملة بالفعل.")
        if self.running:
            raise RuntimeError("توجد مهمة قيد التنفيذ — أوقفها أولاً.")
        token = self._token()
        if not quest["enrolled"]:
            self.enroll(quest_id)
            quest = self._find(quest_id) or quest
        if quest["kind"] == "unsupported":
            raise RuntimeError("نوع هذه المهمة غير مدعوم حالياً.")
        self._stop.clear()
        with self._lock:
            self.active = {
                "id": quest["id"],
                "name": quest["name"],
                "typeLabel": quest["typeLabel"],
                "progress": quest["progress"],
                "target": quest["target"],
                "status": "قيد التنفيذ",
                "startedAt": time.time(),
            }
        self._thread = threading.Thread(
            target=self._run, args=(quest, token), daemon=True, name="discord-quest"
        )
        self._thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        self._stop.set()
        with self._lock:
            if self.active:
                self.active["status"] = "جاري الإيقاف..."
        self.logs.add("warn", "تم طلب إيقاف المهمة.")

    def _update(self, progress: float, target: float) -> None:
        with self._lock:
            if not self.active:
                return
            self.active["progress"] = round(progress, 1)
            self.active["target"] = target
            self.active["status"] = "قيد التنفيذ"

    def _run(self, quest: dict, token: str) -> None:
        quest_id = quest["id"]
        target = quest["target"] or 1
        key = quest["taskKey"]
        progress = float(quest["progress"] or 0)
        props = self._props()
        self.logs.add("info", f"بدء مهمة: {quest['name']} — {quest['typeLabel']}.")
        error = ""
        try:
            if quest["kind"] == "video":
                progress = self._run_video(quest_id, key, token, props, progress, target)
            elif quest["kind"] == "activity":
                progress = self._run_heartbeats(
                    quest, token, props, progress, target, terminal_wait=20.0
                )
            else:
                progress = self._run_desktop(quest, token, props, progress, target)
            if self._stop.is_set():
                error = "تم الإيقاف يدوياً."
            elif progress < target:
                error = "لم يكتمل التقدم قبل التوقف."
            else:
                self.logs.add("ok", f"اكتملت المهمة: {quest['name']} — اضغط استلام الجائزة.")
        except RuntimeError as exc:
            error = str(exc)
            self.logs.add("error", f"{quest['name']}: {error}")
        finally:
            with self._lock:
                if self.active.get("id") == quest_id:
                    self.active["status"] = error or "مكتملة"
                    self.active["progress"] = round(min(progress, target), 1)
                    self.active["target"] = target
            self._stop.clear()
            try:
                self.refresh()
            except Exception:
                pass

    def _run_video(self, quest_id, key, token, props, progress, target) -> float:
        stagnant = 0
        while not self._stop.is_set():
            delay = random.uniform(7.0, 9.5)
            if self._stop.wait(delay):
                break
            progress = min(target, progress + delay)
            status, payload = _request(
                "POST",
                f"/quests/{quest_id}/video-progress",
                token,
                props,
                {"timestamp": round(progress, 6)},
            )
            if status == 429:
                raise RuntimeError(_error_text(payload, status))
            if status not in (200, 201):
                raise RuntimeError("تعذر إرسال التقدم: " + _error_text(payload, status))
            server = _progress_value(payload, key)
            if server > progress:
                progress = min(target, server)
                stagnant = 0
            else:
                stagnant += 1
            self._update(progress, target)
            if payload.get("completed_at") or progress >= target:
                return target
            if stagnant >= 8:
                raise RuntimeError(
                    "ديسكورد لا يحتسب تقدم الفيديو — جرّب فتح المهمة مرة من التطبيق."
                )
        return progress

    def _run_heartbeats(
        self, quest, token, props, progress, target, terminal_wait=20.0
    ) -> float:
        quest_id = quest["id"]
        key = quest["taskKey"]
        channel = self._call_channel(token, props)
        if not channel:
            raise RuntimeError("تعذر إيجاد روم أو محادثة لإرسال النبضات.")
        body = {
            "stream_key": f"call:{channel}:1",
            "application_id": str(quest.get("appId") or ""),
            "terminal": False,
        }
        stagnant = 0
        while not self._stop.is_set():
            status, payload = _request(
                "POST", f"/quests/{quest_id}/heartbeat", token, props, body
            )
            if status == 429:
                raise RuntimeError(_error_text(payload, status))
            if status not in (200, 201):
                raise RuntimeError("تعذر إرسال نبضة المهمة: " + _error_text(payload, status))
            server = _progress_value(payload, key)
            if server > progress:
                progress = min(target, server)
                stagnant = 0
            else:
                stagnant += 1
            self._update(progress, target)
            if progress >= target:
                _request(
                    "POST",
                    f"/quests/{quest_id}/heartbeat",
                    token,
                    props,
                    {**body, "terminal": True},
                )
                return target
            if stagnant >= 6:
                raise RuntimeError(
                    "ديسكورد لا يحتسب النبضات — قد تتطلب المهمة اللعب الفعلي أو البث."
                )
            if self._stop.wait(terminal_wait):
                break
        return progress

    def _run_desktop(self, quest, token, props, progress, target) -> float:
        # Discord normally drives these quests from its desktop client after
        # detecting the game process. Try the same heartbeat the client sends;
        # if the server ignores it, stop with an explanation.
        try:
            return self._run_heartbeats(
                quest, token, props, progress, target, terminal_wait=20.0
            )
        except RuntimeError as exc:
            raise RuntimeError(
                str(exc)
                + " — مهام تشغيل/بث اللعبة تحتاج Discord Desktop مع اللعبة (أو إضافتها كعملية)."
            ) from exc

    def _call_channel(self, token: str, props: str) -> str:
        with self._lock:
            if self._channel:
                return self._channel
        status, payload = _request("GET", "/users/@me/channels", token, props)
        if status == 200 and isinstance(payload, list):
            for channel in payload:
                if channel.get("type") in (1, 3) and channel.get("id"):
                    with self._lock:
                        self._channel = str(channel["id"])
                    return self._channel
        status, guilds = _request("GET", "/users/@me/guilds", token, props)
        if status == 200 and isinstance(guilds, list):
            for guild in guilds[:5]:
                gid = guild.get("id")
                if not gid:
                    continue
                status, channels = _request(
                    "GET", f"/guilds/{gid}/channels", token, props
                )
                if status != 200 or not isinstance(channels, list):
                    continue
                for channel in channels:
                    if channel.get("type") in (2, 13) and channel.get("id"):
                        with self._lock:
                            self._channel = str(channel["id"])
                        return self._channel
        return ""

    # ------------------------------------------------------------------ claim
    def claim(self, quest_id: str) -> None:
        token = self._token()
        status, payload = _request(
            "POST",
            f"/quests/{quest_id}/claim-reward",
            token,
            self._props(),
            {
                "platform": 0,
                "location": 11,
                "is_targeted": False,
                "metadata_sealed": None,
                "traffic_metadata_sealed": None,
            },
        )
        if status not in (200, 201, 204):
            raise RuntimeError("تعذر استلام الجائزة: " + _error_text(payload, status))
        self.logs.add("ok", "تم استلام جائزة المهمة.")
        try:
            self.refresh()
        except RuntimeError:
            pass
