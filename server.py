#!/usr/bin/env python3
"""Local model-usage panel. Listens on 127.0.0.1 only.

  python3 server.py
  python3 server.py --check
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
PANEL = ROOT / "index.html"
DEMO_DATA = ROOT / "demo-data"
VENDOR_ICONS = ROOT / "vendor-icons.js"
FONT = ROOT / "assets" / "zhuque.woff2"
DATA_DIR = Path(os.environ.get("USAGE_DATA_DIR", ROOT / ".data")).expanduser()
HISTORY_DB = DATA_DIR / "quota-history.db"
MULTI_AUTH_EXT = Path(os.environ.get("PI_CONFIG_DIR", ROOT / ".omp")).expanduser() / "plugins/node_modules/omp-multi-auth/extensions/multi-auth.ts"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
STALE_GAP_MS = 45 * 60_000
CACHE_TTL_S = 45
LOOKBACK_MS = 6 * HOUR_MS
RESET_DROP = 0.15
HISTORY_KEEP_MS = 30 * DAY_MS
SAMPLE_INTERVAL_S = 300
TOKEN_RETRY_S = 1.5
NOTIFY_USED = 0.80
FILL_SOON_HOURS = 6
EXHAUSTED = 0.995
NOTICE_CYCLE_SLACK_MS = 10 * 60_000
NOTICE_NO_RESET_QUIET_MS = 6 * HOUR_MS
TAILSCALE = os.environ.get("TAILSCALE_BIN", "tailscale")
DISPATCH_DIR = Path(os.environ.get("USAGE_DISPATCH_DIR", DEMO_DATA / "dispatch")).expanduser()
TASK_TABLE = DISPATCH_DIR / "任务类型表.md"
MODEL_CARD_DIR = DISPATCH_DIR / "模型卡"
STATUS_DIR = DISPATCH_DIR
STATUS_PATH = STATUS_DIR / "当前状态.md"
READING_STALE_MS = 6 * HOUR_MS
STATUS_MAX_LINES = 40
TOKEN_FRESH_MS = 120_000
EFFORT_RANK = {"ultra": 6, "max": 5, "xhigh": 4, "high": 3, "medium": 2, "low": 1, "minimal": 0, "non-reasoning": -1}
AUTH_STATE_DIR = Path(os.environ.get("USAGE_AUTH_DIR", ROOT / ".state")).expanduser()
AUTH_FILE = AUTH_STATE_DIR / "auth.json"
SESSION_COOKIE = "usage_session"
SESSION_TTL_S = 30 * 86_400
LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_FAILS = 5
PBKDF2_ITERATIONS = 240_000
PROVIDER_LABEL = {
    "openai-codex": "Codex",
    "anthropic": "Claude",
    "cursor": "Cursor",
    "xai-oauth": "Grok",
    "xai-oauth-2": "Grok 2",
    "google-antigravity": "Antigravity",
    "google-antigravity-2": "Antigravity 2",
    "google-antigravity-3": "Antigravity 3",
    "google-antigravity-4": "Antigravity 4",
}

_lock = threading.Lock()
_collect_lock = threading.Lock()
_state = {"at": 0.0, "bundle": None}
_notify_enabled = True
_demo_mode = False
_status_lock = threading.Lock()
_sampler_stop = threading.Event()

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    provider TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    account_id TEXT NOT NULL DEFAULT '',
    window_label TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    used REAL NOT NULL,
    resets_at INTEGER,
    recorded_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_account
    ON samples (provider, email, account_id, recorded_at);
CREATE TABLE IF NOT EXISTS notifications (
    provider TEXT NOT NULL,
    account_key TEXT NOT NULL,
    window_label TEXT NOT NULL,
    window_kind TEXT NOT NULL,
    resets_at INTEGER NOT NULL,
    reason TEXT NOT NULL,
    notified_at INTEGER NOT NULL,
    PRIMARY KEY (provider, account_key, window_label, window_kind, resets_at, reason)
);
"""


def omp_home() -> Path:
    raw = os.environ.get("PI_CONFIG_DIR")
    return Path(raw).expanduser() if raw else ROOT / ".omp"


def stats_db() -> Path:
    return omp_home() / "stats.db"


def omp_argv(args: list[str]) -> list[str]:
    if MULTI_AUTH_EXT.exists() and args and args[0] in {"usage", "token"}:
        return ["omp", "-e", str(MULTI_AUTH_EXT), *args]
    return ["omp", *args]


def parse_omp_json(raw: str) -> dict:
    start = raw.find("{")
    if start < 0:
        raise ValueError("omp 输出里没有 JSON")
    return json.loads(raw[start:])


def scrub(text: str) -> str:
    import re

    cleaned = re.sub(r"(?i)(bearer\s+|sk-|api[_-]?key[=:\s]+)\S+", r"\1…", text or "")
    return cleaned.strip().replace("\n", " ")[:240]


def run_omp(args: list[str], timeout: int) -> tuple[dict | None, str | None]:
    try:
        proc = subprocess.run(
            omp_argv(args),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, "找不到 omp"
    except subprocess.TimeoutExpired:
        return None, "超时"
    raw = proc.stdout or ""
    if "{" not in raw:
        return None, "没有 JSON"
    try:
        return parse_omp_json(raw), None
    except json.JSONDecodeError:
        return None, "JSON 解析失败"


def parse_time(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def provider_label(provider: str) -> str:
    if provider in PROVIDER_LABEL:
        return PROVIDER_LABEL[provider]
    match = re.fullmatch(r"google-antigravity-(\d+)", provider or "")
    if match:
        return f"Antigravity {match.group(1)}"
    match = re.fullmatch(r"xai-oauth-(\d+)", provider or "")
    if match:
        return f"Grok {match.group(1)}"
    return provider

def collapse_limits(limits: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for limit in limits or []:
        scope = limit.get("scope") or {}
        key = scope.get("sharedGroup") or limit.get("id") or limit.get("label")
        if key in seen:
            continue
        seen.add(key)
        out.append(limit)
    return out


def used_fraction(limit: dict) -> float | None:
    amount = limit.get("amount") or {}
    fraction = amount.get("usedFraction")
    if isinstance(fraction, (int, float)):
        return float(fraction)
    used = amount.get("used")
    limit_n = amount.get("limit")
    if isinstance(used, (int, float)) and isinstance(limit_n, (int, float)) and limit_n:
        return float(used) / float(limit_n)
    if isinstance(used, (int, float)) and amount.get("unit") == "percent" and 0 <= used <= 100:
        return float(used) / 100
    return None


def account_identity(meta: dict, fallback: dict | None = None) -> dict:
    meta = meta or {}
    fallback = fallback or {}
    return {
        "email": meta.get("email") or fallback.get("email") or "",
        "accountId": meta.get("accountId") or fallback.get("accountId") or "",
        "plan": meta.get("planType") or "",
        "org": meta.get("orgName") or "",
    }


def reset_summary(block: dict | None) -> dict | None:
    if not block:
        return None
    credits = block.get("credits") or []
    available = block.get("availableCount")
    if available is None:
        available = sum(1 for credit in credits if credit.get("status") == "available")
    usable = block.get("redeemableCount")
    expiries = [parse_time(credit.get("expiresAt")) for credit in credits if credit.get("status") == "available"]
    expiries = [item for item in expiries if item]
    return {
        "available": int(available or 0),
        "usable": None if usable is None else int(usable),
        "soonest": min(expiries) if expiries else None,
    }


def display_window(label: str, window_label: str) -> str:
    zh = {"5 Hour": "5 小时", "7 Day": "7 天", "7 days": "7 天", "Weekly": "周", "Monthly": "月"}
    text = label or window_label or "窗口"
    if text in zh:
        return zh[text]
    duration = zh.get(window_label, "")
    if duration and window_label not in text and duration not in text:
        return f"{text} · {duration}"
    return text


def fills_before_reset(used: float | None, slope_per_hour: float | None, resets_in_hours: float | None) -> bool:
    if used is None or slope_per_hour is None or slope_per_hour <= 0.002 or used >= EXHAUSTED:
        return False
    hours_full = (1 - used) / slope_per_hour
    if resets_in_hours is None:
        return hours_full < 48
    return hours_full <= resets_in_hours + 0.05


def hours_to_full(used: float | None, slope: float | None) -> float | None:
    if used is None or slope is None or slope <= 0.002 or used >= EXHAUSTED:
        return None
    return (1 - used) / slope


def describe_pace(used: float | None, slope_per_hour: float | None, resets_in_hours: float | None) -> str:
    if used is None or slope_per_hour is None:
        return "近 6 小时样本不够"
    pace_points = slope_per_hour * 100
    if slope_per_hour <= 0.002:
        return "近 6 小时几乎没动"
    pace = f"近 6 小时每小时约 {pace_points:.1f} 个百分点"
    if used >= EXHAUSTED:
        return pace + "，窗口已满"
    hours_full = (1 - used) / slope_per_hour
    if resets_in_hours is not None and hours_full > resets_in_hours + 0.05:
        return pace + "，重置会先到"
    if hours_full < 1:
        return pace + "，不到 1 小时后打满"
    return pace + f"，大约 {hours_full:.0f} 小时后打满"


def slope_per_hour(points: list[dict], now_ms: int) -> float | None:
    if len(points) < 2:
        return None
    trimmed = points
    for index in range(1, len(points)):
        if points[index]["used"] < points[index - 1]["used"] - RESET_DROP:
            trimmed = points[index:]
    recent = [point for point in trimmed if point["t"] >= now_ms - LOOKBACK_MS]
    sample = recent if len(recent) >= 2 else trimmed[-2:]
    if len(sample) < 2:
        return None
    hours = (sample[-1]["t"] - sample[0]["t"]) / HOUR_MS
    if hours < 0.5:
        return None
    return (sample[-1]["used"] - sample[0]["used"]) / hours


def history_series(entries: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for entry in entries or []:
        key = (
            entry.get("provider") or "",
            entry.get("email") or "",
            entry.get("label") or "",
            entry.get("windowLabel") or "",
        )
        grouped.setdefault(key, []).append(entry)
    series = []
    for (provider, email, label, window_label), rows in grouped.items():
        by_time: dict[int, float] = {}
        for row in rows:
            recorded = row.get("recordedAt")
            used = row.get("usedFraction")
            if not isinstance(recorded, (int, float)) or not isinstance(used, (int, float)):
                continue
            by_time[int(recorded)] = float(used)
        points = [{"t": stamp, "used": by_time[stamp]} for stamp in sorted(by_time)]
        if not points:
            continue
        series.append(
            {
                "provider": provider,
                "email": email,
                "label": label,
                "windowLabel": window_label,
                "points": points,
                "lastAt": points[-1]["t"],
                "used": points[-1]["used"],
            }
        )
    return series


def combine_entries(omp_entries: list[dict] | None, own_entries: list[dict] | None) -> list[dict]:
    own = list(own_entries or [])
    if not omp_entries:
        return own
    if not own:
        return list(omp_entries)
    own_times: dict[tuple, list[int]] = {}
    for entry in own:
        key = (entry.get("provider") or "", entry.get("label") or "", entry.get("windowLabel") or "")
        recorded = entry.get("recordedAt")
        if isinstance(recorded, (int, float)):
            own_times.setdefault(key, []).append(int(recorded))
    merged = list(own)
    for entry in omp_entries:
        key = (entry.get("provider") or "", entry.get("label") or "", entry.get("windowLabel") or "")
        recorded = entry.get("recordedAt")
        stamps = own_times.get(key) or []
        if isinstance(recorded, (int, float)) and any(abs(int(recorded) - stamp) < 15 * 60_000 for stamp in stamps):
            continue
        merged.append(entry)
    return merged


def match_series(series: list[dict], provider: str, email: str, label: str, window_label: str) -> dict | None:
    exact = [
        item
        for item in series
        if item["provider"] == provider
        and item["label"] == label
        and item["windowLabel"] == window_label
        and (not email or not item["email"] or item["email"] == email)
    ]
    if exact:
        return max(exact, key=lambda item: item["lastAt"])
    loose = [
        item
        for item in series
        if item["provider"] == provider and item["label"] == label and item["windowLabel"] == window_label
    ]
    return max(loose, key=lambda item: item["lastAt"]) if loose else None


def normalize_window(limit: dict, now_ms: int, series: dict | None) -> dict:
    window = limit.get("window") or {}
    used = used_fraction(limit)
    resets_at = parse_time(window.get("resetsAt"))
    resets_in_hours = None if resets_at is None else (resets_at - now_ms) / HOUR_MS
    slope = slope_per_hour(series["points"], now_ms) if series else None
    label = limit.get("label") or window.get("label") or "窗口"
    window_label = window.get("label") or ""
    return {
        "id": limit.get("id") or "",
        "label": label,
        "windowLabel": window_label,
        "name": display_window(label, window_label),
        "used": used,
        "resetsAt": resets_at,
        "resetsInHours": resets_in_hours,
        "status": limit.get("status") or "ok",
        "pace": describe_pace(used, slope, resets_in_hours),
        "urgent": fills_before_reset(used, slope, resets_in_hours),
        "hoursToFull": hours_to_full(used, slope),
        "detail": limit.get("detail") or "",
        "points": (series or {}).get("points") or [],
        "stored": False,
        "recordedAt": None,
    }


def same_used(left, right) -> bool:
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return False
    return round(float(left), 4) == round(float(right), 4)


def merge_same_windows(windows: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for window in windows:
        host = None
        for item in merged:
            if item.get("resetsAt") == window.get("resetsAt") and same_used(item.get("used"), window.get("used")):
                host = item
                break
        if host is None:
            copied = dict(window)
            copied["names"] = [window.get("name") or window.get("label") or "窗口"]
            merged.append(copied)
            continue
        name = window.get("name") or window.get("label") or "窗口"
        if name not in host["names"]:
            host["names"].append(name)
        if len(window.get("points") or []) > len(host.get("points") or []):
            host["points"] = window.get("points") or []
            host["pace"] = window.get("pace") or host.get("pace")
            host["hoursToFull"] = window.get("hoursToFull")
            host["urgent"] = window.get("urgent")
    for item in merged:
        names = item.pop("names")
        if len(names) > 1:
            item["name"] = "、".join(names)
    return merged


def window_kind(resets_at: int | None, now_ms: int) -> tuple[str, str]:
    if not resets_at:
        return "window", "窗口"
    hours = (resets_at - now_ms) / HOUR_MS
    if hours <= 8:
        return "5 Hour", "5 小时"
    if hours <= 30:
        return "24h", "日"
    return "Weekly", "周"


def limit_row(limit_id: str, label: str, window_label: str, used_fraction: float, resets_at: int | None, detail: str = "") -> dict:
    used = max(0.0, min(1.0, used_fraction))
    return {
        "id": limit_id,
        "label": label,
        "detail": detail,
        "scope": {},
        "window": {"label": window_label, "resetsAt": resets_at},
        "amount": {"usedFraction": used, "unit": "percent"},
        "status": "exhausted" if used >= EXHAUSTED else "ok",
    }


def parse_xai_billing(data: dict, now_ms: int) -> list[dict]:
    config = (data or {}).get("config") or {}
    percent = config.get("creditUsagePercent")
    if not isinstance(percent, (int, float)):
        return []
    period = config.get("currentPeriod") or {}
    resets_at = parse_time(period.get("end") or config.get("billingPeriodEnd"))
    products = []
    for item in config.get("productUsage") or []:
        name = item.get("product")
        value = item.get("usagePercent")
        if name and isinstance(value, (int, float)) and value > 0:
            products.append(f"{name} {value:g}%")
    return [limit_row("xai-clone:credits:1w", "SuperGrok Weekly Credits", "Weekly", float(percent) / 100, resets_at, "，".join(products))]


def parse_antigravity_models(data: dict, now_ms: int) -> list[dict]:
    buckets: dict[str, dict] = {}
    for key, model in ((data or {}).get("models") or {}).items():
        if key.startswith(("chat_", "tab_")) or model.get("isInternal") is True:
            continue
        quota = model.get("quotaInfo") or {}
        remaining = quota.get("remainingFraction")
        resets_at = parse_time(quota.get("resetTime"))
        if not isinstance(remaining, (int, float)) or not resets_at:
            continue
        provider = model.get("modelProvider") or ""
        family = "google" if "GOOGLE" in provider else "shared" if "ANTHROPIC" in provider or "OPENAI" in provider else ""
        if not family:
            continue
        used = 1 - float(remaining)
        current = buckets.get(family)
        if current is None or used > current["used"]:
            buckets[family] = {"used": used, "resetsAt": resets_at}
    limits = []
    if "google" in buckets:
        kind, _name = window_kind(buckets["google"]["resetsAt"], now_ms)
        limits.append(limit_row("ag-clone:gemini", "Gemini", kind, buckets["google"]["used"], buckets["google"]["resetsAt"]))
    if "shared" in buckets:
        kind, _name = window_kind(buckets["shared"]["resetsAt"], now_ms)
        limits.append(limit_row("ag-clone:shared", "Claude & GPT (shared)", kind, buckets["shared"]["used"], buckets["shared"]["resetsAt"]))
    return limits


def parse_antigravity_summary(data: dict, now_ms: int) -> list[dict]:
    window_labels = {"weekly": "Weekly", "5h": "5 Hour", "daily": "24h"}
    limits = []
    for group in (data or {}).get("groups") or []:
        group_name = (group.get("displayName") or "").lower()
        for bucket in group.get("buckets") or []:
            if bucket.get("disabled") is True:
                continue
            remaining = bucket.get("remainingFraction")
            if not isinstance(remaining, (int, float)):
                continue
            bucket_id = (bucket.get("bucketId") or "").lower()
            if bucket_id.startswith("gemini-argon"):
                label = "Gemini (Argon)"
            elif bucket_id.startswith("gemini") or "gemini" in group_name:
                label = "Gemini"
            elif bucket_id.startswith("3p") or any(word in group_name for word in ("claude", "gpt", "third")):
                label = "Claude & GPT (shared)"
            else:
                continue
            kind = window_labels.get(bucket.get("window") or "", bucket.get("window") or "window")
            limits.append(
                limit_row(
                    f"ag-summary:{bucket_id or label}:{kind}",
                    label,
                    kind,
                    1 - float(remaining),
                    parse_time(bucket.get("resetTime")),
                )
            )
    return limits


def _amount_number(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        inner = value.get("val")
        if isinstance(inner, (int, float)):
            return float(inner)
    return None


def parse_xai_monthly(data: dict, now_ms: int) -> list[dict]:
    config = (data or {}).get("config") or {}
    limit = _amount_number(config.get("monthlyLimit"))
    used = _amount_number(config.get("used"))
    if limit is None or limit <= 0 or used is None:
        return []
    return [
        limit_row(
            "xai-clone:included:1mo",
            "SuperGrok Monthly Included",
            "Monthly",
            min(1.0, used / limit),
            parse_time(config.get("billingPeriodEnd")),
        )
    ]


def auth_db() -> Path:
    return omp_home() / "agent" / "agent.db"


def clone_provider_names() -> list[str]:
    names: set[str] = set()
    config = omp_home() / "agent" / "multi-auth.json"
    if config.exists():
        try:
            data = json.loads(config.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        for item in data.get("subscriptions") or []:
            base = item.get("provider") or ""
            index = item.get("index")
            if base and isinstance(index, int):
                names.add(f"{base}-{index}")
    path = auth_db()
    if path.exists():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            rows = con.execute("SELECT provider FROM auth_credentials").fetchall()
            con.close()
        except sqlite3.Error:
            rows = []
        for (provider,) in rows:
            if re.fullmatch(r"(xai-oauth|google-antigravity)-\d+", provider or ""):
                names.add(provider)
    return sorted(names)


def load_stored_login(provider: str) -> dict | None:
    path = auth_db()
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = con.execute("SELECT data FROM auth_credentials WHERE provider = ?", (provider,)).fetchone()
        con.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    return {
        "access": data.get("access") or "",
        "refresh": data.get("refresh") or "",
        "expires": data.get("expires") if isinstance(data.get("expires"), (int, float)) else 0,
        "email": data.get("email") or "",
        "accountId": data.get("accountId") or "",
        "projectId": data.get("projectId") or "",
    }


def clone_access(provider: str, now_ms: int) -> tuple[str | None, dict, str]:
    login = load_stored_login(provider) or {}
    access = login.get("access") or ""
    expires = login.get("expires") or 0
    if access and expires > now_ms + TOKEN_FRESH_MS:
        return access, login, ""
    if not login:
        return None, {}, "没有保存登录"
    # 只读：面板不自行刷新令牌，也不写 OMP 的登录库。令牌过期后等 OMP 下次使用该账号时刷新。
    return None, login, "登录待 OMP 刷新"



def read_provider_token(provider: str) -> str:
    for attempt in (0, 1):
        try:
            proc = subprocess.run(
                omp_argv(["token", provider]),
                capture_output=True,
                text=True,
                timeout=40,
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode == 0 and (proc.stdout or "").strip():
            return (proc.stdout or "").strip()
        if attempt == 0:
            time.sleep(TOKEN_RETRY_S)
    raise RuntimeError("token")


def fetch_json(url: str, bearer: str, method: str = "GET", body: dict | None = None, headers: dict | None = None) -> dict:
    req_headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        req_headers["Content-Type"] = "application/json"
    request = Request(url, data=payload, headers=req_headers, method=method)
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode())


def fetch_clone_report(provider: str, gap: dict, now_ms: int) -> dict | None:
    token, login, reason = clone_access(provider, now_ms)
    if login:
        for field in ("email", "accountId", "projectId"):
            if not gap.get(field):
                gap[field] = login.get(field) or ""
    if not token:
        gap["note"] = reason or "没有保存登录"
        return None
    project = gap.get("projectId") or "aicode-consumers"
    try:
        if provider.startswith("xai-oauth"):
            data = fetch_json(
                "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
                token,
                headers={"X-XAI-Token-Auth": "xai-grok-cli"},
            )
            limits = parse_xai_billing(data, now_ms)
            if not limits:
                monthly = fetch_json(
                    "https://cli-chat-proxy.grok.com/v1/billing",
                    token,
                    headers={"X-XAI-Token-Auth": "xai-grok-cli"},
                )
                limits = parse_xai_monthly(monthly, now_ms)
        elif provider.startswith("google-antigravity"):
            summary = fetch_json(
                "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                token,
                method="POST",
                body={"project": project},
                headers={"User-Agent": "antigravity/1.11.9 windows/amd64"},
            )
            limits = parse_antigravity_summary(summary, now_ms)
            covered = {item["label"] for item in limits}
            models = fetch_json(
                "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                token,
                method="POST",
                body={"project": project},
                headers={"User-Agent": "antigravity/1.11.9 windows/amd64"},
            )
            for item in parse_antigravity_models(models, now_ms):
                if item["label"] not in covered:
                    limits.append(item)
        else:
            return None
    except (OSError, HTTPError, URLError, json.JSONDecodeError, ValueError, RuntimeError):
        gap["note"] = "额度接口没有返回窗口"
        return None
    if not limits:
        gap["note"] = "额度接口没有返回窗口"
        return None
    return {
        "provider": provider,
        "fetchedAt": now_ms,
        "metadata": {
            "email": gap.get("email") or "",
            "accountId": gap.get("accountId") or "",
            "projectId": gap.get("projectId") or "",
        },
        "limits": limits,
    }


def attach_clone_reports(usage: dict | None, extra_gaps: list[dict], failed: set[str], now_ms: int) -> dict:
    usage = usage or {"reports": [], "accountsWithoutUsage": []}
    reported = {report.get("provider") for report in usage.get("reports") or []}
    for provider in clone_provider_names():
        if provider in reported:
            continue
        gap = next((item for item in extra_gaps if item.get("provider") == provider), None)
        if gap is None:
            gap = {"provider": provider, "email": "", "accountId": ""}
            extra_gaps.append(gap)
        report = fetch_clone_report(provider, gap, now_ms)
        if report:
            usage.setdefault("reports", []).append(report)
            reported.add(provider)
        else:
            failed.add(provider)
    return usage



def load_rollup(path: Path) -> tuple[list[dict], str | None]:
    if not path.exists():
        return [], "还没有用量账本"
    uri = f"file:{path}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return [], "打不开用量账本"
    try:
        rows = con.execute(
            """
            SELECT bucket, provider, model, agent_type, requests, failed,
                   input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                   cost_total, first_ts, last_ts
            FROM message_rollup
            """
        ).fetchall()
    except sqlite3.Error:
        return [], "用量账本读失败"
    finally:
        con.close()
    out = []
    for row in rows:
        out.append(
            {
                "bucket": row[0],
                "provider": row[1],
                "model": row[2],
                "agentType": row[3],
                "requests": row[4],
                "failed": row[5],
                "inputTokens": row[6],
                "outputTokens": row[7],
                "cacheReadTokens": row[8],
                "cacheWriteTokens": row[9],
                "cost": row[10],
                "firstTs": row[11],
                "lastTs": row[12],
            }
        )
    return out, None


def local_day(bucket_ms: int) -> str:
    return datetime.fromtimestamp(bucket_ms / 1000).astimezone().date().isoformat()


def spend_view(rows: list[dict], range_id: str, now_ms: int) -> dict:
    if range_id == "24h":
        start = now_ms - DAY_MS
    elif range_id == "7d":
        start = now_ms - 7 * DAY_MS
    else:
        start = 0
    chosen = [row for row in rows if (row.get("bucket") or 0) >= start]
    by_model: dict[tuple, dict] = {}
    by_day: dict[str, dict] = {}
    by_agent: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    first = None
    last = None
    for row in chosen:
        key = (row["provider"], row["model"])
        cell = by_model.setdefault(
            key,
            {
                "provider": row["provider"],
                "providerLabel": provider_label(row["provider"]),
                "model": row["model"],
                "requests": 0,
                "failed": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
                "cost": 0.0,
            },
        )
        for field in ("requests", "failed", "inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens"):
            cell[field] += row.get(field) or 0
        cell["cost"] += row.get("cost") or 0
        day = local_day(row["bucket"])
        day_cell = by_day.setdefault(day, {"day": day, "requests": 0, "cost": 0.0})
        day_cell["requests"] += row.get("requests") or 0
        day_cell["cost"] += row.get("cost") or 0
        agent = by_agent.setdefault(row.get("agentType") or "main", {"agentType": row.get("agentType") or "main", "requests": 0, "cost": 0.0})
        agent["requests"] += row.get("requests") or 0
        agent["cost"] += row.get("cost") or 0
        prov = by_provider.setdefault(
            row["provider"],
            {"requests": 0, "cost": 0.0, "outputTokens": 0},
        )
        prov["requests"] += row.get("requests") or 0
        prov["cost"] += row.get("cost") or 0
        prov["outputTokens"] += row.get("outputTokens") or 0
        if row.get("firstTs"):
            first = row["firstTs"] if first is None else min(first, row["firstTs"])
        if row.get("lastTs"):
            last = row["lastTs"] if last is None else max(last, row["lastTs"])
    models = sorted(by_model.values(), key=lambda item: item["cost"], reverse=True)
    billed = sum(item["cacheReadTokens"] + item["inputTokens"] for item in models)
    cache_read = sum(item["cacheReadTokens"] for item in models)
    return {
        "range": range_id,
        "from": first,
        "to": last,
        "requests": sum(item["requests"] for item in models),
        "failed": sum(item["failed"] for item in models),
        "cost": sum(item["cost"] for item in models),
        "inputTokens": sum(item["inputTokens"] for item in models),
        "outputTokens": sum(item["outputTokens"] for item in models),
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": sum(item["cacheWriteTokens"] for item in models),
        "cacheRate": (cache_read / billed) if billed else None,
        "byModel": models,
        "byDay": [by_day[key] for key in sorted(by_day)],
        "byAgent": sorted(by_agent.values(), key=lambda item: item["cost"], reverse=True),
        "byProvider": by_provider,
    }


def clock_label(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).astimezone().strftime("%H:%M")


def missed_sentence(label: str, recorded_at: int | None) -> str:
    if recorded_at:
        return f"{label} 这次没读到，显示的是 {clock_label(recorded_at)} 的数据"
    return f"{label} 这次没读到"


def sanitize_errors(errors: list[str]) -> list[str]:
    banned = ("credential", "traceback", "exception", "oauth", "no active", "bearer", "error:")
    out = []
    for item in errors:
        text = (item or "").strip()
        if not text or any(token in text.lower() for token in banned):
            continue
        out.append(text)
    return out


def open_history(path: Path | None = None) -> sqlite3.Connection:
    target = path or HISTORY_DB
    target.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(target), timeout=10)
    con.executescript(SCHEMA)
    return con


def load_own_entries(path: Path | None = None, now_ms: int | None = None) -> list[dict]:
    target = path or HISTORY_DB
    if not target.exists():
        return []
    cutoff = 0 if now_ms is None else now_ms - HISTORY_KEEP_MS
    try:
        con = open_history(target)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            """
            SELECT provider, email, account_id, window_label, window_kind, used, resets_at, recorded_at
            FROM samples
            WHERE recorded_at >= ?
            ORDER BY recorded_at
            """,
            (cutoff,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    entries = []
    for row in rows:
        entries.append(
            {
                "provider": row[0],
                "email": row[1] or "",
                "accountId": row[2] or "",
                "label": row[3] or "",
                "windowLabel": row[4] or "",
                "usedFraction": row[5],
                "resetsAt": row[6],
                "recordedAt": row[7],
            }
        )
    return entries


def latest_stored(entries: list[dict]) -> dict[tuple, list[dict]]:
    best: dict[tuple, dict] = {}
    for entry in entries:
        if not isinstance(entry.get("recordedAt"), (int, float)) or not isinstance(entry.get("usedFraction"), (int, float)):
            continue
        if not entry.get("resetsAt") and entry.get("resetsAt") != 0:
            # omp history has no reset time; last-good windows come from our samples.
            if "resetsAt" not in entry:
                continue
        key = (
            entry.get("provider") or "",
            entry.get("email") or "",
            entry.get("accountId") or "",
            entry.get("label") or "",
            entry.get("windowLabel") or "",
        )
        current = best.get(key)
        if current is None or entry["recordedAt"] > current["recordedAt"]:
            best[key] = entry
    grouped: dict[tuple, list[dict]] = {}
    for entry in best.values():
        account_key = (entry.get("provider") or "", entry.get("email") or "", entry.get("accountId") or "")
        grouped.setdefault(account_key, []).append(entry)
    return grouped


def match_stored(grouped: dict[tuple, list[dict]], account: dict) -> list[dict]:
    provider = account.get("provider") or ""
    email = account.get("email") or ""
    account_id = account.get("accountId") or ""
    provider_rows: list[dict] = []
    exact: list[dict] = []
    identities = set()
    for (prov, row_email, row_id), rows in grouped.items():
        if prov != provider:
            continue
        provider_rows.extend(rows)
        identities.add((row_email, row_id))
        email_ok = not email or not row_email or email == row_email
        id_ok = not account_id or not row_id or account_id == row_id
        if email_ok and id_ok and ((email and email == row_email) or (account_id and account_id == row_id) or (not email and not account_id)):
            exact.extend(rows)
    if exact:
        return exact
    if len(identities) == 1:
        return provider_rows
    return []


def windows_from_stored(rows: list[dict], series: list[dict], now_ms: int, email: str) -> list[dict]:
    windows = []
    for row in rows:
        limit = limit_row(
            f"stored:{row.get('label')}:{row.get('windowLabel')}",
            row.get("label") or "窗口",
            row.get("windowLabel") or "",
            float(row.get("usedFraction") or 0),
            row.get("resetsAt"),
        )
        matched = match_series(series, row.get("provider") or "", email, limit["label"], limit["window"]["label"])
        window = normalize_window(limit, now_ms, matched)
        window["stored"] = True
        window["recordedAt"] = row.get("recordedAt")
        windows.append(window)
    return merge_same_windows(windows)


def tight_sort_key(item: dict) -> tuple:
    used = item["used"] if isinstance(item.get("used"), (int, float)) else -1.0
    label = item.get("label") or ""
    if used >= EXHAUSTED:
        return (0, 0.0, -used, label)
    hours = item.get("hoursToFull")
    if item.get("urgent") and isinstance(hours, (int, float)):
        return (1, float(hours), -used, label)
    return (2, -used, 0.0, label)


def merge_duplicate_logins(accounts: list[dict]) -> list[dict]:
    """同一渠道下邮箱相同的多个登录是同一份额度，只留编号最小的一个，其余记在 aliases。"""

    def rank(provider: str) -> tuple[str, int]:
        match = re.fullmatch(r"(.+)-(\d+)", provider)
        return (match.group(1), int(match.group(2))) if match else (provider, 0)

    kept: dict[tuple[str, str], dict] = {}
    result = []
    for account in sorted(accounts, key=lambda item: rank(item["provider"])[1]):
        email = (account.get("email") or "").strip().lower()
        key = (rank(account["provider"])[0], email)
        if email and key in kept:
            kept[key].setdefault("aliases", []).append(account["label"])
            continue
        if email:
            kept[key] = account
        result.append(account)
    order = {id(account): index for index, account in enumerate(accounts)}
    result.sort(key=lambda item: order[id(item)])
    return result


def build_snapshot(
    usage: dict | None,
    account_list: list[dict],
    history: dict | None,
    extra_gaps: list[dict],
    rollup: list[dict],
    now_ms: int,
    range_id: str,
    errors: list[str],
    own_entries: list[dict] | None = None,
    failed_providers: set[str] | None = None,
    usage_failed: bool = False,
) -> dict:
    reports = (usage or {}).get("reports") or []
    gaps = list((usage or {}).get("accountsWithoutUsage") or []) + list(extra_gaps or [])
    entries = combine_entries((history or {}).get("entries") or [], own_entries)
    series = history_series(entries)
    stored = latest_stored(own_entries or [])
    failed = set(failed_providers or [])
    newest_by_provider: dict[str, int] = {}
    for item in series:
        newest_by_provider[item["provider"]] = max(newest_by_provider.get(item["provider"], 0), item["lastAt"])

    accounts = []
    seen = set()
    for report in reports:
        meta = report.get("metadata") or {}
        ident = account_identity(meta)
        key = (report.get("provider"), ident["accountId"] or ident["email"])
        seen.add(key)
        windows = []
        for limit in collapse_limits(report.get("limits") or []):
            window = limit.get("window") or {}
            matched = match_series(
                series,
                report.get("provider") or "",
                ident["email"],
                limit.get("label") or "",
                window.get("label") or "",
            )
            windows.append(normalize_window(limit, now_ms, matched))
        windows = merge_same_windows(windows)
        windows.sort(key=lambda item: item["used"] if item["used"] is not None else -1, reverse=True)
        accounts.append(
            {
                "provider": report.get("provider") or "",
                "label": provider_label(report.get("provider") or ""),
                **ident,
                "fetchedAt": report.get("fetchedAt"),
                "dataAt": report.get("fetchedAt"),
                "quota": "ok",
                "stored": False,
                "resets": reset_summary(report.get("resetCredits")),
                "windows": windows,
                "note": "",
            }
        )

    for gap in gaps:
        ident = account_identity({}, gap)
        key = (gap.get("provider"), ident["accountId"] or ident["email"])
        if key in seen or not gap.get("provider"):
            continue
        seen.add(key)
        known = {item["provider"] for item in account_list}
        if account_list and gap.get("provider") not in known and gap not in extra_gaps:
            continue
        accounts.append(
            {
                "provider": gap.get("provider") or "",
                "label": provider_label(gap.get("provider") or ""),
                **ident,
                "fetchedAt": None,
                "dataAt": None,
                "quota": "missing",
                "stored": False,
                "resets": None,
                "windows": [],
                "note": gap.get("note") or "额度接口没有返回窗口",
            }
        )

    for item in account_list or []:
        provider = item.get("provider")
        if not provider or any(account["provider"] == provider for account in accounts):
            continue
        accounts.append(
            {
                "provider": provider,
                "label": provider_label(provider),
                "email": "",
                "accountId": "",
                "plan": item.get("orgName") or "",
                "org": item.get("orgName") or "",
                "fetchedAt": None,
                "dataAt": None,
                "quota": "missing",
                "stored": False,
                "resets": None,
                "windows": [],
                "note": "额度接口没有返回窗口",
            }
        )

    accounts = merge_duplicate_logins(accounts)

    account_errors = []
    for account in accounts:
        if account["windows"]:
            account["dataAt"] = account.get("fetchedAt")
            continue
        rows = match_stored(stored, account)
        should_note = usage_failed or account["provider"] in failed or bool(rows)
        if rows:
            recorded = max(int(row["recordedAt"]) for row in rows)
            account["windows"] = windows_from_stored(rows, series, now_ms, account.get("email") or "")
            account["quota"] = "ok"
            account["stored"] = True
            account["fetchedAt"] = recorded
            account["dataAt"] = recorded
            account["note"] = missed_sentence(account["label"], recorded)
            account_errors.append(account["note"])
        elif should_note:
            account["note"] = missed_sentence(account["label"], None)
            account["dataAt"] = None
            account_errors.append(account["note"])

    live_keys = {
        (account["provider"], window["label"], window["windowLabel"])
        for account in accounts
        for window in account["windows"]
        if not window.get("stored")
    }
    stale = []
    for item in series:
        key = (item["provider"], item["label"], item["windowLabel"])
        if key in live_keys:
            continue
        if item["used"] < 0.05:
            continue
        newest = newest_by_provider.get(item["provider"], item["lastAt"])
        if newest - item["lastAt"] < STALE_GAP_MS and now_ms - item["lastAt"] < STALE_GAP_MS:
            continue
        stale.append(
            {
                "provider": item["provider"],
                "label": provider_label(item["provider"]),
                "email": item["email"],
                "window": item["label"],
                "windowLabel": item["windowLabel"],
                "used": item["used"],
                "lastAt": item["lastAt"],
                "points": item["points"],
            }
        )
    stale.sort(key=lambda item: item["used"], reverse=True)

    spend = spend_view(rollup, range_id, now_ms)
    for account in accounts:
        extra = spend["byProvider"].get(account["provider"], {})
        account["sessionRequests"] = extra.get("requests", 0)
        account["sessionCost"] = extra.get("cost", 0.0)
        account["sessionOutput"] = extra.get("outputTokens", 0)
    accounts.sort(
        key=lambda account: (
            account["quota"] != "ok",
            -(max((window["used"] or 0) for window in account["windows"]) if account["windows"] else 0),
            account["label"],
        )
    )

    tight = []
    for account in accounts:
        for window in account["windows"]:
            if window["used"] is None:
                continue
            tight.append(
                {
                    "provider": account["provider"],
                    "label": account["label"],
                    "email": account["email"],
                    "plan": account["plan"],
                    "window": window["name"],
                    "used": window["used"],
                    "resetsAt": window["resetsAt"],
                    "pace": window["pace"],
                    "urgent": window["urgent"],
                    "hoursToFull": window.get("hoursToFull"),
                    "status": window["status"],
                    "resets": account["resets"],
                }
            )
    ranked = sorted(tight, key=tight_sort_key)
    nonzero = [item for item in ranked if item["used"] > 0]
    tight = (nonzero or ranked)[:4]

    return {
        "fetchedAt": now_ms,
        "range": range_id,
        "errors": sanitize_errors(list(errors) + account_errors),
        "tight": tight,
        "accounts": accounts,
        "stale": stale,
        "spend": {key: value for key, value in spend.items() if key != "byProvider"},
    }


def collect() -> dict:
    errors = []
    failed: set[str] = set()
    usage, usage_error = run_omp(["usage", "--json"], 90)
    accounts_doc, accounts_error = run_omp(["usage", "accounts", "--json"], 40)
    history, history_error = run_omp(["usage", "--history", "--days", "7", "--json"], 120)
    stats_doc, stats_error = run_omp(["stats", "--json"], 180)
    usage_failed = bool(usage_error)
    if accounts_error:
        errors.append("账号列表这次没读到")
    if history_error:
        errors.append("额度走势这次没读到")
    if stats_error or stats_doc is None:
        errors.append("会话同步这次没读到")

    account_list = (accounts_doc or {}).get("accounts") or []
    reported = {report.get("provider") for report in (usage or {}).get("reports") or []}
    missing = [item.get("provider") for item in account_list if item.get("provider") and item.get("provider") not in reported]
    extra_gaps = []
    for provider in missing:
        gap_doc, gap_error = run_omp(["usage", "--provider", provider, "--json"], 60)
        if gap_error:
            failed.add(provider)
            extra_gaps.append({"provider": provider, "email": "", "accountId": ""})
            continue
        found = (gap_doc or {}).get("accountsWithoutUsage") or []
        reports = (gap_doc or {}).get("reports") or []
        if reports:
            usage = usage or {"reports": [], "accountsWithoutUsage": []}
            usage.setdefault("reports", []).extend(reports)
        elif found:
            extra_gaps.extend(found)
        else:
            failed.add(provider)
            extra_gaps.append({"provider": provider})
    rollup, rollup_error = load_rollup(stats_db())
    if rollup_error:
        errors.append(rollup_error)
    now_ms = int(time.time() * 1000)
    usage = attach_clone_reports(usage, extra_gaps, failed, now_ms)
    kept_gaps = extra_gaps
    usage = usage or {"reports": []}
    if usage_failed and not account_list and not (usage.get("reports") or []):
        errors.append("额度这次没读到")
    return {
        "usage": usage,
        "account_list": account_list,
        "history": history,
        "extra_gaps": kept_gaps,
        "rollup": rollup,
        "errors": errors,
        "failed_providers": failed,
        "usage_failed": usage_failed,
        "collectedAt": now_ms,
    }


def parse_task_table(text: str) -> dict:
    order = []
    rows = []
    section = ""
    for line in (text or "").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section == "选型顺序":
            match = re.match(r"\d+\.\s+(.*)", line)
            if match:
                order.append(match.group(1).strip())
        if section == "路由表" and line.startswith("|"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if not cells or not cells[0].isdigit():
                continue
            while len(cells) < 7:
                cells.append("")
            rows.append(
                {
                    "index": int(cells[0]),
                    "task": cells[1],
                    "floor": cells[2],
                    "preferred": cells[3],
                    "alternate": cells[4],
                    "avoid": cells[5],
                    "basis": cells[6],
                }
            )
    return {"order": order, "rows": rows}


def preferred_providers(text: str) -> list[str]:
    return re.findall(r"`([A-Za-z0-9][A-Za-z0-9._-]*)/", text or "")


def hot_windows_for(accounts: list[dict], providers: list[str]) -> list[str]:
    found = []
    wanted = set(providers)
    for account in accounts or []:
        if account.get("provider") not in wanted:
            continue
        for window in account.get("windows") or []:
            used = window.get("used")
            if isinstance(used, (int, float)) and used >= NOTIFY_USED:
                name = window.get("name") or window.get("label") or "窗口"
                if name not in found:
                    found.append(name)
    return found


def mark_routing_rows(rows: list[dict], accounts: list[dict]) -> list[dict]:
    marked = []
    for row in rows:
        providers = preferred_providers(row.get("preferred") or "")
        hot = hot_windows_for(accounts, providers)
        pref_used = None
        pref_provider = providers[0] if providers else ""
        if pref_provider:
            pref_used, _ = tightest_window(accounts, pref_provider)
        marked.append(
            {
                "index": row.get("index"),
                "task": row.get("task") or "",
                "floor": row.get("floor") or "",
                "preferred": row.get("preferred") or "",
                "alternate": row.get("alternate") or "",
                "avoid": row.get("avoid") or "",
                "hot": bool(hot),
                "hotWindows": hot,
                "preferredProvider": pref_provider,
                "preferredUsed": pref_used,
            }
        )
    return marked


def normalize_model_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def parse_aa_table(text: str) -> list[dict]:
    rows = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        name = parts[0].strip()
        effort = ""
        stem = name
        match = re.search(r"\(([^)]+)\)\s*$", name)
        if match:
            effort = match.group(1).strip().lower()
            stem = name[: match.start()].strip()
        rows.append(
            {
                "name": name,
                "key": normalize_model_key(stem),
                "effort": effort,
                "index": parts[3].strip() if len(parts) > 3 else "",
                "cost": parts[4].strip() if len(parts) > 4 else "",
                "minutes": parts[5].strip() if len(parts) > 5 else "",
            }
        )
    return rows


def aa_efforts_for(rows: list[dict], model_id: str) -> list[dict]:
    key = normalize_model_key(model_id)
    matched = [row for row in rows if row.get("key") == key]
    if not matched:
        return []
    ordered = sorted(matched, key=lambda row: EFFORT_RANK.get(row.get("effort") or "", -2), reverse=True)
    out = []
    seen_efforts: set[str] = set()
    for row in ordered:
        eff = row.get("effort") or "default"
        if eff in seen_efforts:
            continue
        seen_efforts.add(eff)
        out.append(
            {
                "effort": eff,
                "index": blank_metric(row.get("index") or ""),
                "cost": blank_metric(row.get("cost") or ""),
                "minutes": blank_metric(row.get("minutes") or ""),
            }
        )
    return out


def select_aa_row(rows: list[dict], model_id: str) -> dict | None:
    key = normalize_model_key(model_id)
    matched = [row for row in rows if row.get("key") == key]
    if not matched:
        return None
    high = [row for row in matched if row.get("effort") == "high"]
    if high:
        return high[0]
    return max(matched, key=lambda row: EFFORT_RANK.get(row.get("effort") or "", -2))

def latest_aa_file(directory: Path | None = None) -> Path | None:
    folder = directory or STATUS_DIR
    if not folder.exists():
        return None
    files = sorted(folder.glob("AA排行榜-*.tsv"))
    return files[-1] if files else None


def aa_snapshot_date(path: Path | None) -> str:
    if not path:
        return ""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    return match.group(1) if match else ""


def derive_fallback_tags(suited: str, unsuited: str) -> list[str]:
    tags = []
    for chunk in re.split(r"[、，；。]", suited or ""):
        clean = re.sub(r"（[^）]*）|`[^`]*`", "", chunk).strip()
        if clean and clean != "无" and len(clean) <= 10:
            tags.append(clean)
        if len(tags) >= 3:
            break
    for chunk in re.split(r"[、，；。]", unsuited or ""):
        clean = re.sub(r"（[^）]*）|`[^`]*`", "", chunk).strip()
        if clean and len(clean) <= 8:
            tags.append(f"× {clean}")
            break
    return tags


def parse_model_card(text: str, filename: str) -> dict:
    model = filename.removesuffix(".md")
    selector = ""
    tier = ""
    suited = ""
    unsuited = ""
    measured = ""
    tags: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("- 选择器："):
            match = re.search(r"`([^`]+)`", line)
            selector = match.group(1) if match else ""
        elif line.startswith("- 档位："):
            tier = line.split("：", 1)[1].strip()
        elif line.startswith("- 本机实测："):
            measured = line.split("：", 1)[1].strip()
        elif line.startswith("- 适合派："):
            suited = line.split("：", 1)[1].strip()
        elif line.startswith("- 不适合派："):
            unsuited = line.split("：", 1)[1].strip()
        elif line.startswith("- 标签："):
            raw_tags = line.split("：", 1)[1].strip()
            tags = [item.strip() for item in re.split(r"[、，]", raw_tags) if item.strip()]
    if not tags:
        tags = derive_fallback_tags(suited, unsuited)
    provider = selector.split("/", 1)[0] if "/" in selector else ""
    model_id = selector.split("/", 1)[1] if "/" in selector else model
    return {
        "model": model_id or model,
        "file": model,
        "selector": selector,
        "provider": provider,
        "tier": tier,
        "measured": measured,
        "suited": suited,
        "unsuited": unsuited,
        "tags": tags,
    }


def load_model_cards(directory: Path | None = None) -> list[dict]:
    folder = directory or MODEL_CARD_DIR
    if not folder.exists():
        return []
    cards = []
    for path in sorted(folder.glob("*.md")):
        try:
            cards.append(parse_model_card(path.read_text(), path.name))
        except OSError:
            continue
    return cards


def usage_by_model(rollup: list[dict], now_ms: int) -> dict[str, dict]:
    start = now_ms - 7 * DAY_MS
    out: dict[str, dict] = {}
    for row in rollup or []:
        if (row.get("bucket") or 0) < start:
            continue
        model = row.get("model") or ""
        cell = out.setdefault(model, {"mainRequests": 0, "mainFailed": 0, "subRequests": 0, "subFailed": 0})
        prefix = "sub" if row.get("agentType") == "subagent" else "main"
        cell[f"{prefix}Requests"] += row.get("requests") or 0
        cell[f"{prefix}Failed"] += row.get("failed") or 0
    return out


def tightest_window(accounts: list[dict], provider: str) -> tuple[float | None, str]:
    best = None
    name = ""
    for account in accounts or []:
        if account.get("provider") != provider:
            continue
        for window in account.get("windows") or []:
            used = window.get("used")
            if not isinstance(used, (int, float)):
                continue
            if best is None or used > best:
                best = used
                name = window.get("name") or window.get("label") or ""
    return best, name


def reading_status(recorded_at: int | None, now_ms: int) -> str:
    if not recorded_at:
        return "empty"
    if now_ms - int(recorded_at) > READING_STALE_MS:
        return "stale"
    return "fresh"

def sampling_rows(accounts: list[dict], now_ms: int) -> list[dict]:
    rows = []
    for account in accounts or []:
        recorded = account.get("dataAt") or account.get("fetchedAt")
        rows.append(
            {
                "provider": account.get("provider") or "",
                "label": account.get("label") or provider_label(account.get("provider") or ""),
                "email": account.get("email") or "",
                "recordedAt": recorded,
                "status": reading_status(recorded, now_ms),
                "note": account.get("note") or "",
            }
        )
    return rows


def blank_metric(value: str) -> str:
    text = (value or "").strip()
    if not text or text == "--":
        return ""
    return text


def build_channel_strip(accounts: list[dict]) -> list[dict]:
    specs = [
        ("Grok Heavy", "xai-oauth", ""),
        ("Gemini Web", "gemini-web", "ultra"),
        ("Antigravity Ultra", "google-antigravity-3", "ultra"),
        ("Claude", "anthropic", ""),
        ("Codex", "openai-codex", "至 11-30"),
        ("Cursor", "cursor", ""),
    ]
    strip = []
    for rank, (name, provider, badge) in enumerate(specs, start=1):
        used, window = tightest_window(accounts, provider)
        if used is None and provider == "google-antigravity-3":
            for acc in accounts or []:
                if "Antigravity 3" in (acc.get("aliases") or []):
                    used, window = tightest_window([acc], acc.get("provider") or "")
                    break
        strip.append(
            {
                "rank": rank,
                "name": name,
                "provider": provider,
                "used": used,
                "window": window,
                "badge": badge,
            }
        )
    return strip


def build_dispatch(accounts: list[dict], rollup: list[dict], now_ms: int, task_text: str | None = None, cards: list[dict] | None = None, aa_rows: list[dict] | None = None, aa_date: str | None = None) -> dict:
    if task_text is None:
        try:
            task_text = TASK_TABLE.read_text() if TASK_TABLE.exists() else ""
        except OSError:
            task_text = ""
    table = parse_task_table(task_text)
    if cards is None:
        cards = load_model_cards()
    if aa_rows is None:
        aa_path = latest_aa_file()
        aa_date = aa_snapshot_date(aa_path)
        try:
            aa_rows = parse_aa_table(aa_path.read_text()) if aa_path else []
        except OSError:
            aa_rows = []
    usage = usage_by_model(rollup, now_ms)
    capabilities = []
    for card in cards:
        picked = select_aa_row(aa_rows or [], card["model"])
        efforts = aa_efforts_for(aa_rows or [], card["model"])
        prov = card.get("provider") or ""
        used, window = tightest_window(accounts, prov)
        if used is None and prov == "google-antigravity-3":
            for acc in accounts or []:
                if "Antigravity 3" in (acc.get("aliases") or []):
                    used, window = tightest_window([acc], acc.get("provider") or "")
                    break
        counts = usage.get(card["model"], {})
        is_ultra = prov == "google-antigravity-3" or "ultra" in (card.get("tier") or "").lower()
        capabilities.append(
            {
                "model": card["model"],
                "providerLabel": provider_label(prov) if prov else "",
                "tier": card.get("tier") or "",
                "isUltra": is_ultra,
                "aaEffort": (picked.get("effort") or "default") if picked else "",
                "aaIndex": blank_metric(picked.get("index") or "") if picked else "",
                "aaCost": blank_metric(picked.get("cost") or "") if picked else "",
                "aaMinutes": blank_metric(picked.get("minutes") or "") if picked else "",
                "aaListed": bool(picked),
                "efforts": efforts,
                "mainRequests": counts.get("mainRequests", 0),
                "mainFailed": counts.get("mainFailed", 0),
                "subRequests": counts.get("subRequests", 0),
                "subFailed": counts.get("subFailed", 0),
                "tightUsed": used,
                "tightWindow": window,
                "tags": card.get("tags") or [],
                "measured": card.get("measured") or "",
                "suited": card.get("suited") or "",
                "unsuited": card.get("unsuited") or "",
                "provider": prov,
            }
        )
    return {
        "order": table["order"],
        "channels": build_channel_strip(accounts),
        "routes": mark_routing_rows(table["rows"], accounts),
        "capabilities": capabilities,
        "aaDate": aa_date or "",
        "sampling": sampling_rows(accounts, now_ms),
    }


def short_date(ms: int) -> str:
    stamp = datetime.fromtimestamp(ms / 1000).astimezone()
    return f"{stamp.month}-{stamp.day}"


def percent_text(used: float) -> str:
    pct = used * 100
    text = str(round(pct)) if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}"
    return text + "%"


def status_account_name(account: dict) -> str:
    provider = account.get("provider") or ""
    if provider == "xai-oauth":
        return "xai-oauth（Heavy）"
    return provider or account.get("label") or "账号"


def fresh_reading(account: dict, now_ms: int) -> bool:
    recorded = account.get("dataAt") or account.get("fetchedAt")
    return bool(recorded) and reading_status(recorded, now_ms) == "fresh"

def render_agent_status(payload: dict, now_ms: int | None = None) -> str:
    stamp = now_ms if now_ms is not None else int(payload.get("fetchedAt") or time.time() * 1000)
    when = datetime.fromtimestamp(stamp / 1000).astimezone()
    date = when.date().isoformat()
    clock = when.strftime("%H:%M")
    items = []
    hints = []
    for account in payload.get("accounts") or []:
        name = status_account_name(account)
        recorded = account.get("dataAt") or account.get("fetchedAt")
        fresh = fresh_reading(account, stamp)
        windows = [window for window in account.get("windows") or [] if isinstance(window.get("used"), (int, float))]
        max_used = max((window["used"] for window in windows), default=0)
        if fresh and windows:
            bits = []
            for window in windows:
                bit = f"{window.get('name') or window.get('label') or '窗口'}：已用 {percent_text(window['used'])}"
                if window.get("resetsAt"):
                    bit += f"，{short_date(window['resetsAt'])} 重置"
                bits.append(bit)
                if window["used"] >= NOTIFY_USED:
                    hints.append(f"- {name} {window.get('name') or '窗口'}已用 {percent_text(window['used'])}，已到 80%")
                if window.get("urgent"):
                    hints.append(f"- {name} {window.get('name') or '窗口'}按当前速度会在重置前用尽")
            line = f"- {name} {'；'.join(bits)}，读数 {clock_label(recorded)}"
        elif recorded:
            line = f"- {name}：读数过期（{short_date(recorded)}），按未知处理"
            hints.append(f"- {name}：读数过期（{short_date(recorded)}）")
        else:
            line = f"- {name}：无读数，按未知处理"
        items.append({"line": line, "fresh": fresh, "maxUsed": max_used})
    if not hints:
        hints = ["- 无"]

    def document(chosen: list[dict], omitted: int) -> list[str]:
        lines = [
            "来源: 用量面板自动生成",
            f"日期: {date}",
            "",
            f"# 当前状态（{date} {clock}）",
            "",
            "## 余量",
        ]
        lines.extend(item["line"] for item in chosen)
        if omitted:
            lines.append(f"- 省略了 {omitted} 个低用量账号")
        lines.extend(["", "## 提示", *hints])
        return lines

    chosen = list(items)
    omitted = 0
    lines = document(chosen, omitted)
    if len(lines) > STATUS_MAX_LINES:
        kept = []
        for item in chosen:
            if item["fresh"] and item["maxUsed"] < 0.20:
                omitted += 1
                continue
            kept.append(item)
        chosen = kept
        lines = document(chosen, omitted)
    while len(lines) > STATUS_MAX_LINES:
        droppable = [index for index, item in enumerate(chosen) if item["fresh"] and item["maxUsed"] < NOTIFY_USED]
        if not droppable:
            break
        drop = min(droppable, key=lambda index: chosen[index]["maxUsed"])
        chosen.pop(drop)
        omitted += 1
        lines = document(chosen, omitted)
    if len(lines) > STATUS_MAX_LINES:
        lines = lines[: STATUS_MAX_LINES - 1] + [f"- 省略了 {omitted} 个低用量账号"]
    return "\n".join(lines) + "\n"


def write_agent_status(payload: dict, path: Path | None = None, now_ms: int | None = None) -> Path:
    target = path or STATUS_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(render_agent_status(payload, now_ms), encoding="utf-8")
    os.replace(temporary, target)
    return target


def snapshot_from_bundle(bundle: dict, range_id: str, now_ms: int | None = None) -> dict:
    if range_id not in {"24h", "7d", "all"}:
        range_id = "7d"
    stamp = now_ms if now_ms is not None else int(time.time() * 1000)
    payload = build_snapshot(
        bundle.get("usage"),
        bundle.get("account_list") or [],
        bundle.get("history"),
        bundle.get("extra_gaps") or [],
        bundle.get("rollup") or [],
        stamp,
        range_id,
        bundle.get("errors") or [],
        own_entries=load_own_entries(now_ms=stamp),
        failed_providers=set(bundle.get("failed_providers") or []),
        usage_failed=bool(bundle.get("usage_failed")),
    )
    payload["dispatch"] = build_dispatch(payload.get("accounts") or [], bundle.get("rollup") or [], stamp)
    return payload



def refresh_cache(force: bool) -> dict:
    with _lock:
        age = time.time() - _state["at"]
        if _state["bundle"] is not None and ((not force and age <= CACHE_TTL_S) or (force and age <= 5)):
            return _state["bundle"]
    with _collect_lock:
        with _lock:
            age = time.time() - _state["at"]
            if _state["bundle"] is not None and ((not force and age <= CACHE_TTL_S) or (force and age <= 5)):
                return _state["bundle"]
        bundle = collect()
        with _lock:
            _state["bundle"] = bundle
            _state["at"] = time.time()
        return bundle


def snapshot(range_id: str, fresh: bool) -> dict:
    if _demo_mode:
        if range_id not in {"24h", "7d", "all"}:
            range_id = "7d"
        payload = json.loads((DEMO_DATA / "snapshots" / f"{range_id}.json").read_text(encoding="utf-8"))
        counts = {row["model"]: row for row in payload.get("dispatch", {}).get("capabilities", [])}
        stamp = int(payload.get("fetchedAt") or time.time() * 1000)
        dispatch = build_dispatch(payload.get("accounts") or [], [], stamp)
        for row in dispatch["capabilities"]:
            previous = counts.get(row["model"], {})
            for key in ("mainRequests", "mainFailed", "subRequests", "subFailed"):
                if key in previous:
                    row[key] = previous[key]
        payload["dispatch"] = dispatch
        with _status_lock:
            write_agent_status(payload, now_ms=stamp)
        return payload
    bundle = refresh_cache(fresh or _state["bundle"] is None)
    return snapshot_from_bundle(bundle, range_id)


def record_samples(path: Path, payload: dict, recorded_at: int | None = None) -> int:
    stamp = recorded_at if recorded_at is not None else int(payload.get("fetchedAt") or time.time() * 1000)
    wrote = 0
    con = open_history(path)
    try:
        for account in payload.get("accounts") or []:
            if account.get("stored"):
                continue
            for window in account.get("windows") or []:
                if window.get("stored") or not isinstance(window.get("used"), (int, float)):
                    continue
                con.execute(
                    """
                    INSERT INTO samples (
                        provider, email, account_id, window_label, window_kind, used, resets_at, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account.get("provider") or "",
                        account.get("email") or "",
                        account.get("accountId") or "",
                        window.get("label") or "",
                        window.get("windowLabel") or "",
                        float(window["used"]),
                        window.get("resetsAt"),
                        stamp,
                    ),
                )
                wrote += 1
        con.execute("DELETE FROM samples WHERE recorded_at < ?", (stamp - HISTORY_KEEP_MS,))
        con.commit()
    finally:
        con.close()
    return wrote


def account_notice_key(account: dict) -> str:
    return account.get("accountId") or account.get("email") or account.get("provider") or ""


def applicable_reasons(window: dict) -> list[str]:
    used = window.get("used")
    if not isinstance(used, (int, float)) or window.get("stored"):
        return []
    reasons = []
    if used >= EXHAUSTED:
        reasons.append("exhausted")
    hours = window.get("hoursToFull")
    if window.get("urgent") and isinstance(hours, (int, float)) and hours < FILL_SOON_HOURS:
        reasons.append("fill6h")
    if NOTIFY_USED <= used < EXHAUSTED:
        reasons.append("pct80")
    return reasons


def reset_phrase(hours: float | None) -> str:
    if hours is None:
        return "重置时间未知"
    if hours <= 0:
        return "已过重置点"
    if hours < 1:
        return f"{max(1, round(hours * 60))} 分钟后重置"
    if hours < 48:
        return f"{hours:.0f} 小时后重置"
    return f"{hours / 24:.0f} 天后重置"


def notice_text(account: dict, window: dict) -> str:
    label = account.get("label") or provider_label(account.get("provider") or "")
    name = window.get("name") or window.get("label") or "窗口"
    used = float(window.get("used") or 0)
    pct = used * 100
    pct_text = str(round(pct)) if abs(pct - round(pct)) < 0.05 else f"{pct:.1f}"
    pct_text += "%"
    tail = reset_phrase(window.get("resetsInHours"))
    if used >= EXHAUSTED:
        return f"{label} {name}已用 {pct_text}，窗口已满，{tail}"
    hours = window.get("hoursToFull")
    if isinstance(hours, (int, float)):
        full = "不到 1 小时后打满" if hours < 1 else f"大约 {hours:.0f} 小时后打满"
        return f"{label} {name}已用 {pct_text}，{full}，{tail}"
    return f"{label} {name}已用 {pct_text}，{tail}"


def notice_identity(account: dict, window: dict, reason: str) -> tuple:
    return (
        account.get("provider") or "",
        account_notice_key(account),
        window.get("label") or "",
        window.get("windowLabel") or "",
        int(window.get("resetsAt") or 0),
        reason,
    )


def notice_key(identity: tuple) -> tuple:
    return (*identity[:4], identity[5])


def load_seen(path: Path) -> dict[tuple, list[tuple[int, int]]]:
    if not path.exists():
        return {}
    con = open_history(path)
    try:
        rows = con.execute(
            "SELECT provider, account_key, window_label, window_kind, resets_at, reason, notified_at FROM notifications"
        ).fetchall()
    finally:
        con.close()
    seen: dict[tuple, list[tuple[int, int]]] = {}
    for row in rows:
        seen.setdefault(notice_key(tuple(row[:6])), []).append((int(row[4] or 0), int(row[6] or 0)))
    return seen


def already_notified(seen: dict[tuple, list[tuple[int, int]]], identity: tuple, now_ms: int) -> bool:
    # Providers report a window's reset time with a few seconds of jitter, so the
    # cycle is "until the reset time we already notified about has passed".
    for resets_at, notified_at in seen.get(notice_key(identity), []):
        if resets_at:
            if now_ms < resets_at + NOTICE_CYCLE_SLACK_MS:
                return True
        elif now_ms - notified_at < NOTICE_NO_RESET_QUIET_MS:
            return True
    return False


def unseen_notices(accounts: list[dict], seen: dict[tuple, list[tuple[int, int]]], now_ms: int) -> list[dict]:
    pending = []
    for account in accounts:
        if account.get("stored"):
            continue
        for window in account.get("windows") or []:
            unseen = [
                reason
                for reason in applicable_reasons(window)
                if not already_notified(seen, notice_identity(account, window, reason), now_ms)
            ]
            if not unseen:
                continue
            reason = unseen[0]
            pending.append(
                {
                    "account": account,
                    "window": window,
                    "reason": reason,
                    "identity": notice_identity(account, window, reason),
                    "text": notice_text(account, window),
                }
            )
    return pending


def remember_notices(path: Path, notices: list[dict], now_ms: int) -> None:
    if not notices and not path.exists():
        return
    con = open_history(path)
    try:
        for notice in notices:
            identity = notice["identity"]
            con.execute(
                """
                INSERT OR IGNORE INTO notifications (
                    provider, account_key, window_label, window_kind, resets_at, reason, notified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (*identity, now_ms),
            )
        con.execute("DELETE FROM notifications WHERE notified_at < ?", (now_ms - HISTORY_KEEP_MS,))
        con.commit()
    finally:
        con.close()


def consider_notices(path: Path, accounts: list[dict], now_ms: int, send=None) -> list[str]:
    pending = unseen_notices(accounts, load_seen(path), now_ms)
    sent = []
    remembered = []
    for notice in pending:
        if send is not None:
            if not send(notice["text"]):
                continue
        sent.append(notice["text"])
        remembered.append(notice)
    remember_notices(path, remembered, now_ms)
    return sent


def send_notification(text: str) -> bool:
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'display notification "{safe}" with title "模型用量"'
    try:
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def sample_once() -> None:
    bundle = refresh_cache(True)
    payload = snapshot_from_bundle(bundle, "7d", bundle.get("collectedAt"))
    record_samples(HISTORY_DB, payload, bundle.get("collectedAt"))
    try:
        write_agent_status(payload, now_ms=bundle.get("collectedAt"))
    except OSError:
        print("当前状态这次没写成", flush=True)
    if _notify_enabled:
        consider_notices(HISTORY_DB, payload.get("accounts") or [], int(time.time() * 1000), send_notification)



def sampler_loop() -> None:
    while not _sampler_stop.is_set():
        try:
            sample_once()
        except Exception:
            print("额度采样这次没完成", flush=True)
        if _sampler_stop.wait(SAMPLE_INTERVAL_S):
            return


def summary_lines(payload: dict) -> list[str]:
    lines = [f"fetched {datetime.fromtimestamp(payload['fetchedAt']/1000).astimezone():%H:%M}"]
    for item in payload["tight"]:
        used = item["used"] * 100 if item["used"] is not None else float("nan")
        hours = item.get("hoursToFull")
        hours_text = "" if hours is None else f" full={hours:.1f}h"
        lines.append(f"tight {item['label']} {item['window']} {used:.1f}%{hours_text}")
    for account in payload["accounts"]:
        mark = "ok" if account["quota"] == "ok" else "missing"
        lines.append(f"account {account['label']} {mark} windows={len(account['windows'])}")
    for item in payload["stale"]:
        lines.append(f"stale {item['label']} {item['window']} {item['used']*100:.1f}%")
    spend = payload["spend"]
    lines.append(
        f"spend {spend['range']} requests={spend['requests']} cost={spend['cost']:.2f} models={len(spend['byModel'])}"
    )
    for error in payload["errors"]:
        lines.append(f"error {error}")
    return lines


def allow_request(client_host: str, login: str | None, headers: dict | None = None) -> bool:
    hdrs = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if any(k in hdrs for k in ("cf-connecting-ip", "cf-ray", "cf-visitor")):
        return False
    if login:
        allowed = os.environ.get("USAGE_TAILSCALE_LOGIN", "")
        return bool(allowed) and hmac.compare_digest(login, allowed) and client_host in {"127.0.0.1", "::1"}
    return client_host in {"127.0.0.1", "::1"}


def hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


def save_auth_config(password: str, path: Path | None = None) -> Path:
    if not password or len(password) < 10:
        raise ValueError("密码至少 10 个字符")
    target = path or AUTH_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    salt = secrets.token_bytes(16)
    doc = {
        "salt": salt.hex(),
        "hash": hash_password(password, salt, PBKDF2_ITERATIONS),
        "iterations": PBKDF2_ITERATIONS,
        "sessionSecret": secrets.token_hex(32),
        "updatedAt": int(time.time()),
    }
    tmp = target.with_name(target.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    os.replace(tmp, target)
    return target


def load_auth_config(path: Path | None = None) -> dict | None:
    target = path or AUTH_FILE
    if not target.exists():
        return None
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not all(isinstance(doc.get(k), str) and doc.get(k) for k in ("salt", "hash", "sessionSecret")):
        return None
    return doc


def verify_password(password: str, config: dict | None) -> bool:
    if not password or not config:
        return False
    try:
        salt = bytes.fromhex(config["salt"])
        iters = int(config.get("iterations") or PBKDF2_ITERATIONS)
    except (ValueError, TypeError):
        return False
    candidate = hash_password(password, salt, iters)
    return hmac.compare_digest(candidate, config["hash"])


def issue_session_token(config: dict, now_s: int | None = None, ttl_s: int = SESSION_TTL_S) -> str:
    stamp = int(time.time()) if now_s is None else int(now_s)
    exp = stamp + int(ttl_s)
    nonce = secrets.token_hex(12)
    payload = f"{exp}.{nonce}"
    sig = hmac.new(config["sessionSecret"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str | None, config: dict | None, now_s: int | None = None) -> bool:
    if not token or not config:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    exp_str, nonce, sig = parts
    if not exp_str.isdigit() or not nonce or not sig:
        return False
    stamp = int(time.time()) if now_s is None else int(now_s)
    if int(exp_str) <= stamp:
        return False
    payload = f"{exp_str}.{nonce}"
    expected = hmac.new(config["sessionSecret"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


class LoginRateLimiter:
    def __init__(self, window_s: int = LOGIN_WINDOW_S, max_fails: int = LOGIN_MAX_FAILS) -> None:
        self.window_s = window_s
        self.max_fails = max_fails
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}

    def _prune(self, key: str, now_s: float) -> list[float]:
        cutoff = now_s - self.window_s
        recent = [ts for ts in self._attempts.get(key, []) if ts > cutoff]
        if recent:
            self._attempts[key] = recent
        else:
            self._attempts.pop(key, None)
        return recent

    def blocked(self, key: str, now_s: float | None = None) -> bool:
        stamp = time.time() if now_s is None else now_s
        with self._lock:
            return len(self._prune(key, stamp)) >= self.max_fails

    def record_failure(self, key: str, now_s: float | None = None) -> int:
        stamp = time.time() if now_s is None else now_s
        with self._lock:
            if len(self._attempts) > 2048 and key not in self._attempts:
                oldest = next(iter(self._attempts))
                self._attempts.pop(oldest, None)
            recent = self._prune(key, stamp)
            recent.append(stamp)
            self._attempts[key] = recent
            return len(recent)

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


_login_limiter = LoginRateLimiter()


def mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or ""
    return email.split("@", 1)[0]


def mask_public_payload(payload: dict) -> dict:
    cloned = json.loads(json.dumps(payload, ensure_ascii=False))
    for item in cloned.get("tight") or []:
        item["email"] = mask_email(item.get("email") or "")
    for acc in cloned.get("accounts") or []:
        acc["email"] = mask_email(acc.get("email") or "")
    dispatch = cloned.get("dispatch") or {}
    for row in dispatch.get("sampling") or []:
        row["email"] = mask_email(row.get("email") or "")
    cloned["publicSession"] = True
    return cloned


def render_login_page(message: str = "", status_ready: bool = True) -> bytes:
    msg_html = f'<p class="err" role="alert">{message}</p>' if message else ""
    hint = "" if status_ready else '<p class="err">尚未配置密码，请在 Mac Mini 运行 server.py --set-password</p>'
    disabled = "" if status_ready else " disabled"
    html = f"""<!DOCTYPE html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>登录 · 模型用量</title>
<style>
@font-face {{
  font-family: "Zhuque Fangsong";
  font-style: normal;
  font-weight: 400;
  font-display: swap;
  src: url("/font/zhuque.woff2") format("woff2");
}}
:root {{
  --ink: #1c1915;
  --muted: #6f675e;
  --line: #dcd3c6;
  --paper: #f6f1e8;
  --card: #fffdf8;
  --hot: #9c3b2e;
  color-scheme: light dark;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --ink: #f4efe6;
    --muted: #b7aea2;
    --line: #3c352c;
    --paper: #141210;
    --card: #1d1a16;
    --hot: #e7a093;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 24px;
  background: var(--paper);
  color: var(--ink);
  font-family: "Times New Roman", Times, "Zhuque Fangsong", serif;
  font-size: 16px;
}}
.box {{
  width: 100%;
  max-width: 360px;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 28px 24px;
}}
h1 {{ font-size: 28px; font-weight: 400; margin: 0 0 6px; }}
.sub {{ color: var(--muted); font-size: 14px; margin: 0 0 20px; }}
label {{ display: block; font-size: 14px; color: var(--muted); margin-bottom: 6px; }}
input {{
  width: 100%;
  min-height: 44px;
  padding: 10px 12px;
  font: inherit;
  font-size: 16px;
  color: var(--ink);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 6px;
}}
input:focus-visible, button:focus-visible {{ outline: 2px solid var(--ink); outline-offset: 2px; }}
button {{
  margin-top: 14px;
  width: 100%;
  min-height: 44px;
  font: inherit;
  font-size: 16px;
  background: var(--ink);
  color: var(--paper);
  border: 1px solid var(--ink);
  border-radius: 6px;
  cursor: pointer;
}}
button:disabled {{ opacity: 0.45; cursor: not-allowed; }}
.err {{ color: var(--hot); font-size: 14px; margin: 0 0 14px; }}
</style>
</head>
<body>
  <form class="box" method="POST" action="/auth/login">
    <h1>模型用量</h1>
    <p class="sub">usage.example.com</p>
    {hint}{msg_html}
    <label for="pw">访问密码</label>
    <input id="pw" name="password" type="password" autocomplete="current-password" required autofocus{disabled}>
    <button type="submit"{disabled}>进入面板</button>
  </form>
</body>
</html>"""
    return html.encode("utf-8")

def tailscale_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    env.setdefault("TERM", "xterm-256color")
    return env


def tailnet_origin() -> str:
    if not (Path(TAILSCALE).exists() or shutil.which(TAILSCALE)):
        return ""
    try:
        proc = subprocess.run(
            [TAILSCALE, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
            env=tailscale_env(),
        )
        raw = proc.stdout or ""
        start = raw.find("{")
        if start < 0:
            return ""
        name = json.loads(raw[start:]).get("Self", {}).get("DNSName", "").rstrip(".")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return ""
    return f"https://{name}" if name else ""


def publish_tailnet(port: int) -> str:
    origin = tailnet_origin()
    if not origin:
        return ""
    try:
        proc = subprocess.run(
            [TAILSCALE, "serve", "--bg", "--yes", "--https", "443", f"http://127.0.0.1:{port}"],
            capture_output=True,
            text=True,
            timeout=30,
            env=tailscale_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        print("Tailscale 发布失败", flush=True)
        return ""
    return origin



class Handler(BaseHTTPRequestHandler):
    server_version = "usage-panel/2"
    public_listener = False
    auth_file: Path | None = None

    def log_message(self, fmt: str, *args) -> None:
        return

    def _is_https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        if proto == "https":
            return True
        visitor = self.headers.get("Cf-Visitor") or ""
        return '"https"' in visitor.lower()

    def _client_key(self) -> str:
        return self.client_address[0]

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if self._is_https():
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    def _send(self, status: int, body: bytes, content_type: str, cache: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self._security_headers()
        for key, val in extra_headers or []:
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _redirect(self, location: str, extra_headers: list[tuple[str, str]] | None = None) -> None:
        headers = [("Location", location), *(extra_headers or [])]
        self._send(303, b"", "text/plain; charset=utf-8", "no-store", headers)

    def _session_valid(self) -> bool:
        config = load_auth_config(self.auth_file)
        if not config:
            return False
        raw_cookie = self.headers.get("Cookie") or ""
        if not raw_cookie:
            return False
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            return False
        morsel = jar.get(SESSION_COOKIE)
        return verify_session_token(morsel.value if morsel else None, config)

    def _authorized(self) -> bool:
        if self.public_listener:
            return self._session_valid()
        login = self.headers.get("Tailscale-User-Login")
        return allow_request(self.client_address[0], login, dict(self.headers.items()))

    def do_TRACE(self) -> None:
        self._send(405, b"", "text/plain", "no-store")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self.public_listener:
            self._send(405, b"", "text/plain", "no-store")
            return
        sec_site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if sec_site and sec_site not in {"same-origin", "none"}:
            self._send(403, "跨站请求已拒绝".encode("utf-8"), "text/plain; charset=utf-8", "no-store")
            return
        if parsed.path == "/auth/logout":
            secure = "; Secure" if self._is_https() else ""
            cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"
            self._redirect("/login", [("Set-Cookie", cookie)])
            return
        if parsed.path != "/auth/login":
            self._send(404, b"", "text/plain", "no-store")
            return
        key = self._client_key()
        if _login_limiter.blocked(key):
            self._send(429, render_login_page("尝试次数过多，请 15 分钟后再试"), "text/html; charset=utf-8", "no-store")
            return
        config = load_auth_config(self.auth_file)
        if not config:
            self._send(503, render_login_page(status_ready=False), "text/html; charset=utf-8", "no-store")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            self._send(400, render_login_page("请求无效"), "text/html; charset=utf-8", "no-store")
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = parse_qs(raw, keep_blank_values=True)
        password = (form.get("password") or [""])[0]
        if not verify_password(password, config):
            _login_limiter.record_failure(key)
            if _login_limiter.blocked(key):
                self._send(429, render_login_page("尝试次数过多，请 15 分钟后再试"), "text/html; charset=utf-8", "no-store")
            else:
                self._send(401, render_login_page("密码错误"), "text/html; charset=utf-8", "no-store")
            return
        _login_limiter.clear(key)
        token = issue_session_token(config)
        secure = "; Secure" if self._is_https() else ""
        cookie = f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_S}; HttpOnly; SameSite=Strict{secure}"
        self._redirect("/", [("Set-Cookie", cookie)])

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/font/zhuque.woff2", "/assets/zhuque.woff2"}:
            if not FONT.exists():
                self._send(404, b"", "text/plain", "no-store")
                return
            self._send(200, FONT.read_bytes(), "font/woff2", "public, max-age=86400")
            return
        if parsed.path == "/vendor-icons.js":
            if not VENDOR_ICONS.exists():
                self._send(404, b"", "text/plain", "no-store")
                return
            self._send(200, VENDOR_ICONS.read_bytes(), "application/javascript; charset=utf-8", "public, max-age=3600")
            return
        if parsed.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon", "no-store")
            return
        if self.public_listener and parsed.path == "/login":
            if self._session_valid():
                self._redirect("/")
                return
            ready = load_auth_config(self.auth_file) is not None
            self._send(200, render_login_page(status_ready=ready), "text/html; charset=utf-8", "no-store")
            return
        if not self._authorized():
            if self.public_listener:
                if parsed.path.startswith("/api/"):
                    self._send(401, json.dumps({"error": "unauthorized"}).encode("utf-8"), "application/json; charset=utf-8", "no-store")
                else:
                    self._redirect("/login")
                return
            self._send(403, "这个面板只开给 Owner 的节点".encode("utf-8"), "text/plain; charset=utf-8", "no-store")
            return
        login = self.headers.get("Tailscale-User-Login")
        if not self.public_listener and login and not getattr(Handler, "_seen_login", False):
            Handler._seen_login = True
            print(f"tailscale login {login}", flush=True)
        if parsed.path in {"/", "/index.html"}:
            if not PANEL.exists():
                self._send(500, "面板文件缺失".encode("utf-8"), "text/plain; charset=utf-8", "no-store")
                return
            self._send(200, PANEL.read_bytes(), "text/html; charset=utf-8", "no-store")
            return
        if parsed.path == "/demo-data/demo-snapshots.js":
            path = DEMO_DATA / "demo-snapshots.js"
            self._send(200, path.read_bytes(), "application/javascript; charset=utf-8", "no-store")
            return
        if parsed.path == "/api/snapshot":
            query = parse_qs(parsed.query)
            range_id = (query.get("range") or ["7d"])[0]
            fresh = (query.get("fresh") or ["0"])[0] == "1"
            try:
                payload = snapshot(range_id, fresh)
                if self.public_listener:
                    payload = mask_public_payload(payload)
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8", "no-store")
            except Exception:
                body = json.dumps({"errors": ["面板这次没读成"], "accounts": [], "tight": []}, ensure_ascii=False).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8", "no-store")
            return
        self._send(404, b"", "text/plain", "no-store")


class PublicHandler(Handler):
    public_listener = True


def serve(port: int, open_browser: bool, public_port: int = 3928, publish: bool = False) -> None:
    server = None
    last_error = None
    chosen = port
    for candidate in range(port, port + 8):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            chosen = candidate
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise SystemExit(f"端口 {port} 起 8 个都被占用: {last_error}")
    pub_server = None
    if public_port > 0:
        try:
            pub_server = ThreadingHTTPServer(("127.0.0.1", public_port), PublicHandler)
            threading.Thread(target=pub_server.serve_forever, name="usage-public-http", daemon=True).start()
            print(f"公网源站  http://127.0.0.1:{public_port} (强制密码鉴权)", flush=True)
        except OSError as exc:
            print(f"公网端口 {public_port} 未启动: {exc}", flush=True)
    url = f"http://127.0.0.1:{chosen}"
    remote = publish_tailnet(chosen) if publish else ""
    print(f"模型用量  {url}", flush=True)
    if remote:
        print(f"Tailnet  {remote}", flush=True)
    if not _demo_mode:
        thread = threading.Thread(target=sampler_loop, name="usage-sampler", daemon=True)
        thread.start()
    if open_browser and os.uname().sysname == "Darwin":
        subprocess.Popen(["open", url])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停了", flush=True)
    finally:
        _sampler_stop.set()
        if pub_server is not None:
            pub_server.server_close()
        server.server_close()


def main() -> None:
    global _notify_enabled, _demo_mode
    import argparse

    parser = argparse.ArgumentParser(description="本机模型用量面板")
    parser.add_argument("--port", type=int, default=3927)
    parser.add_argument("--public-port", type=int, default=None, help="本地密码鉴权源站端口（0 关闭）")
    parser.add_argument("--demo", action="store_true", help="使用本地脱敏快照")
    parser.add_argument("--publish-tailnet", action="store_true", help="显式修改 Tailscale Serve 配置")
    parser.add_argument("--set-password", action="store_true", help="交互设置公网访问密码（保存 PBKDF2 哈希）")
    parser.add_argument("--check", action="store_true", help="拉一次快照并打印摘要")
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    _demo_mode = args.demo
    public_port = args.public_port if args.public_port is not None else (0 if args.demo else 3928)
    if args.set_password:
        first = getpass.getpass("输入新密码（至少 10 位）: ")
        second = getpass.getpass("再次确认新密码: ")
        if first != second:
            raise SystemExit("两次输入不一致")
        saved = save_auth_config(first)
        print(f"密码哈希已写入 {saved}")
        return
    _notify_enabled = not args.no_notify
    if args.check:
        payload = snapshot("7d", True)
        print("\n".join(summary_lines(payload)))
        return
    serve(args.port, open_browser=not args.no_open, public_port=public_port, publish=args.publish_tailnet)

if __name__ == "__main__":
    main()
