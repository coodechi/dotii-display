#!/usr/bin/env python3
"""LAN bridge and loopback-only management UI for Dotii.

The ESP32-facing schema v1 endpoint is intentionally kept compatible with the
current firmware. Administrative data and static UI assets are only available
from the local computer.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urlparse

from bambu_client import BambuConfigStore, BambuService
from bluetooth_bridge import BluetoothBridge
from firmware_flasher import FirmwareFlasher
from connection_settings import ConnectionSettings
from usb_bridge import UsbBridge
from localization import localize_admin
from platforms import current_platform
from runtime_paths import (
    application_root,
    is_frozen,
    project_root,
    project_version,
    resource_path,
    runtime_folder,
    sibling_executable,
    tools_root,
)


SCHEMA_VERSION = 1
APP_VERSION = project_version()
MAX_BODY = 32 * 1024
CUSTOM_FRAME_WIDTH = 466
CUSTOM_FRAME_HEIGHT = 466
CUSTOM_FRAME_SIZE = CUSTOM_FRAME_WIDTH * CUSTOM_FRAME_HEIGHT * 2
MAX_CUSTOM_SOURCE_SIZE = 4 * 1024 * 1024
CUSTOM_DEFAULTS = {
    "enabled": True,
    "revision": 0,
    "title": "我的页面",
    "value": "你好，Dotii",
    "body": "在电脑端编辑内容，保存后会自动同步到设备。",
    "footer": "自定义内容",
    "accent": "#F2C66D",
    "title_visible": True,
    "title_color": "#8E9B97",
    "value_visible": True,
    "value_color": "#F2C66D",
    "body_visible": True,
    "body_color": "#D6DFDC",
    "footer_visible": True,
    "footer_color": "#8E9B97",
    "background_image": False,
    "image_fit": "cover",
    "image_opacity": 70,
    "ring_enabled": True,
    "ring_start": "#F2C66D",
    "ring_end": "#5DA9FF",
}
MODULE_DEFAULTS = {"weather": False, "bambu": False, "dotii": True}


def resolve_ffmpeg(runtime_folder: Path) -> str | None:
    """Resolve the bundled FFmpeg executable, with legacy user fallbacks."""
    platform = current_platform()
    candidates = platform.ffmpeg_candidates(runtime_folder, tools_root())
    candidate = next((path for path in candidates if path.is_file()), None)
    if candidate:
        return str(candidate.resolve())
    executable_name = "ffmpeg.exe" if platform.name == "windows" else "ffmpeg"
    return shutil.which(executable_name)
DOTII_EXPRESSION_IDS = (
    "idle_breath", "blink", "curious", "sleepy_yawn",
    "touch_response", "connecting", "working", "complete", "failure",
)
DOTII_EXPRESSIONS = set(DOTII_EXPRESSION_IDS)
DOTII_LEGACY_EXPRESSIONS = {"happy"}
DOTII_FIXED_EXPRESSION_IDS = (
    "idle_breath", "blink", "sleepy_yawn", "touch_response", "connecting",
)
DOTII_BUSINESS_EXPRESSION_IDS = (
    "curious", "working", "complete", "failure",
)
DOTII_STATE_GROUPS = (
    {"id": "bambu", "label": "Bambu 状态"},
)
DOTII_FIXED_STATE_DEFINITIONS = (
    {"id": "idle", "label": "空闲", "expression": "idle_breath"},
    {"id": "blink", "label": "待机眨眼", "expression": "blink"},
    {"id": "long_idle", "label": "长时间无操作", "expression": "sleepy_yawn"},
    {"id": "touch", "label": "触摸屏幕", "expression": "touch_response"},
    {"id": "connecting", "label": "连接中", "expression": "connecting"},
)
DOTII_BUSINESS_STATE_DEFINITIONS = (
    {"id": "bambu_paused", "label": "已暂停", "group": "bambu"},
    {"id": "bambu_printing", "label": "打印中", "group": "bambu"},
    {"id": "bambu_completed", "label": "打印完成", "group": "bambu"},
    {"id": "bambu_failure", "label": "故障或异常", "group": "bambu"},
)
DOTII_FIXED_STATE_EXPRESSIONS = {
    definition["id"]: definition["expression"]
    for definition in DOTII_FIXED_STATE_DEFINITIONS
}
DOTII_BUSINESS_STATE_LABELS = {
    definition["id"]: definition["label"]
    for definition in DOTII_BUSINESS_STATE_DEFINITIONS
}
DOTII_LEGACY_STATE_EXPANSIONS = {
    "waiting_user": ("bambu_paused",),
    "working": ("bambu_printing",),
    "print_completed": ("bambu_completed",),
    "failure": ("bambu_failure",),
}
DOTII_DEFAULT_BUSINESS_ASSIGNMENTS = {
    "curious": ("bambu_paused",),
    "working": ("bambu_printing",),
    "complete": ("bambu_completed",),
    "failure": ("bambu_failure",),
}
DOTII_DEFAULT_BUSINESS_EXPRESSION = {
    state_id: expression_id
    for expression_id, state_ids in DOTII_DEFAULT_BUSINESS_ASSIGNMENTS.items()
    for state_id in state_ids
}
DOTII_LOOP_DURATIONS_MS = {
    expression_id: 1200 if expression_id == "touch_response" else 800
    for expression_id in DOTII_EXPRESSION_IDS
}
DOTII_STATE_DURATION_OPTIONS = (
    {"value": 1000, "label": "1 秒"},
    {"value": 3000, "label": "3 秒"},
    {"value": 5000, "label": "5 秒"},
    {"value": 30000, "label": "30 秒"},
    {"value": 0, "label": "保持"},
)
DOTII_STATE_DURATION_VALUES = {
    option["value"] for option in DOTII_STATE_DURATION_OPTIONS
}
DISPLAY_DEFAULTS = {
    "revision": 0,
    "docked_rotation_tenths": 900,
    "screen_off_timeout_seconds": 60,
    "sleep_timeout_seconds": 300,
    "charging_screen_off_timeout_seconds": 60,
    "charging_sleep_timeout_seconds": 300,
    "screen_off_page": "none",
    "clock_style": "classic",
}
DISPLAY_TIMEOUT_OPTIONS = {0, 10, 30, 60, 180, 300, 600, 1800}
SCREEN_OFF_PAGE_OPTIONS = {"none", "custom", "dotii"}
CLOCK_STYLE_OPTIONS = {"classic", "minimal", "sunset", "mint"}
CUSTOM_COLOR_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
WEB_ROOT = resource_path("web")
MANAGEMENT_CENTER_EXECUTABLE = (
    "DotiiManagementCenter.exe" if os.name == "nt" else "DotiiManagementCenter"
)

WEATHER_DEFAULTS = {
    "enabled": False,
    "latitude": None,
    "longitude": None,
    "province": "",
    "city": "",
}
WEATHER_REFRESH_SECONDS = 600.0
WEATHER_REQUEST_TIMEOUT = 8.0
# Domestic (mainland-hosted) weather provider. Tencent accepts a province plus
# city pair, so the management centre keeps working without overseas endpoints.
WEATHER_DOMESTIC_URL = "https://wis.qq.com/weather/common"
# Coordinates are the user-facing input, so province/city names are resolved
# from them instead of being typed by hand.
WEATHER_GEOCODE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
WIND_LEVEL_KPH = {1: 2, 2: 7, 3: 16, 4: 24, 5: 34, 6: 44, 7: 56, 8: 68, 9: 80}
LOCATION_CACHE: dict[tuple[int, int], tuple[str, str]] = {}
LOCATION_CACHE_LOCK = threading.Lock()
LUNAR_TEXT_MAX_BYTES = 64
LUNAR_CACHE: dict[str, str] = {}
LUNAR_CACHE_LOCK = threading.Lock()


def _bounded_text(value: Any, maximum: int, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value.encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


def startup_command(python_executable: Path, app_script: Path) -> str:
    return current_platform().startup_command(python_executable, app_script)


def packaged_startup_command(management_center: Path) -> str:
    """Build the login command for the tray host, never the bridge backend."""
    return current_platform().packaged_startup_command(management_center)


def current_startup_command() -> str:
    return current_platform().current_startup_command(
        frozen=is_frozen(),
        executable=Path(sys.executable),
        app_script=application_root() / "bridge" / "bridge_app.py",
        management_center=sibling_executable(MANAGEMENT_CENTER_EXECUTABLE),
    )


def startup_enabled() -> bool:
    return current_platform().startup_enabled()


def set_startup(enabled: bool) -> bool:
    command = current_startup_command() if enabled else ""
    return current_platform().set_startup(enabled, command)


HOST_PID_ENV = "DOTII_HOST_PID"
HOST_WATCH_MISSES = 5  # 2 s poll -> 10 s grace before shutting the server down


def host_watch_pids() -> list[int]:
    """Resolve stable host processes that should bound this backend.

    Only the IDE main process (VSCODE_PID) is watched automatically. Per-window
    utility helpers are recycled by the IDE on their own, so watching them
    stopped healthy servers. Opt into a specific host with DOTII_HOST_PID.
    """
    pids: list[int] = []
    for name in (HOST_PID_ENV, "VSCODE_PID"):
        for part in re.split(r"[,\s]+", os.environ.get(name, "").strip()):
            try:
                pid = int(part)
            except ValueError:
                continue
            if pid > 1 and pid not in pids:
                pids.append(pid)
    return pids


def watch_parent(parent_pid: int | list[int] | tuple[int, ...], server: ThreadingHTTPServer, stop: threading.Event) -> None:
    """Stop the backend after a host has been continuously absent for 10 s."""
    pids = (parent_pid,) if isinstance(parent_pid, int) else tuple(parent_pid)
    missing = 0
    while not stop.wait(2.0):
        missing_pid = 0
        for pid in pids:
            if pid <= 1:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                if missing_pid == 0:
                    missing_pid = pid
            except PermissionError:
                continue
        if missing_pid == 0:
            missing = 0
            continue
        missing += 1
        if missing >= HOST_WATCH_MISSES:
            logging.warning("宿主进程 %s 已退出，正在停止后台服务", missing_pid)
            server.shutdown()
            return


def load_or_create_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        token = ""
    if token:
        return token
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    return token


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def local_ipv4() -> str:
    """Return the LAN address used in the device connection hint."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        address = sock.getsockname()[0]
        return address if address and not _is_loopback(address) else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _unbounded_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    result = int(value)
    if result < 0:
        raise ValueError(f"{field} must be non-negative")
    return result


def _optional_float(value: Any, field: str, low: float, high: float) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    result = float(value)
    if not low <= result <= high:
        raise ValueError(f"{field} must be between {low} and {high}")
    return result


def _bounded_int(value: Any, field: str, low: int, high: int, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    result = int(value)
    if not low <= result <= high:
        raise ValueError(f"{field} must be between {low} and {high}")
    return result


def validate_weather_config(payload: Any) -> dict[str, Any]:
    if payload is None:
        return dict(WEATHER_DEFAULTS)
    if not isinstance(payload, dict):
        raise ValueError("weather config must be an object")
    enabled = payload.get("enabled", WEATHER_DEFAULTS["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("weather.enabled must be a boolean")
    latitude = _optional_float(
        payload.get("latitude", WEATHER_DEFAULTS["latitude"]), "weather.latitude", -90.0, 90.0
    )
    longitude = _optional_float(
        payload.get("longitude", WEATHER_DEFAULTS["longitude"]), "weather.longitude", -180.0, 180.0
    )
    province = _bounded_text(
        payload.get("province", WEATHER_DEFAULTS["province"]), 32, "weather.province"
    )
    city = _bounded_text(payload.get("city", WEATHER_DEFAULTS["city"]), 32, "weather.city")
    if enabled and (latitude is None or longitude is None):
        raise ValueError("启用天气模块前请先设置经纬度")
    return {
        "enabled": enabled,
        "latitude": latitude,
        "longitude": longitude,
        "province": province,
        "city": city,
    }


def validate_weather_status(status: Any) -> dict[str, Any]:
    if not isinstance(status, dict):
        raise ValueError("weather must be an object")
    available = bool(status.get("available", False))
    return {
        "enabled": bool(status.get("enabled", False)),
        "available": available,
        "fetched_at_epoch": _non_negative_int(
            status.get("fetched_at_epoch", 0), "weather.fetched_at_epoch"
        ),
        "city": _bounded_text(status.get("city", ""), 32, "weather.city"),
        "condition_code": _bounded_int(
            status.get("condition_code"), "weather.condition_code", 0, 99, 0
        ) if available else 0,
        "temperature_c": _bounded_int(
            status.get("temperature_c"), "weather.temperature_c", -100, 100, 0
        ) if available else 0,
        "temp_low_c": _bounded_int(
            status.get("temp_low_c"), "weather.temp_low_c", -100, 100, 0
        ) if available else 0,
        "temp_high_c": _bounded_int(
            status.get("temp_high_c"), "weather.temp_high_c", -100, 100, 0
        ) if available else 0,
        "humidity_percent": _bounded_int(
            status.get("humidity_percent"), "weather.humidity_percent", 0, 100, 0
        ) if available else 0,
        "wind_kph": _bounded_int(
            status.get("wind_kph"), "weather.wind_kph", 0, 500, 0
        ) if available else 0,
    }


def _leading_int(value: Any, default: int = 0) -> int:
    """Parse "45%", "6km/h" or "2级" style values into the leading integer."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(round(value))
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return int(match.group())
    return default


def resolve_location_names(latitude: float, longitude: float) -> tuple[str, str]:
    """Resolve Chinese province/city names from coordinates, cached per ~1 km."""
    key = (int(round(float(latitude) * 100)), int(round(float(longitude) * 100)))
    with LOCATION_CACHE_LOCK:
        cached = LOCATION_CACHE.get(key)
    if cached is not None:
        return cached
    query = urlencode({
        "latitude": f"{float(latitude):.4f}",
        "longitude": f"{float(longitude):.4f}",
        "localityLanguage": "zh",
    })
    request = urllib.request.Request(
        f"{WEATHER_GEOCODE_URL}?{query}",
        headers={"User-Agent": "DotiiManagementCenter"},
    )
    with urllib.request.urlopen(request, timeout=WEATHER_REQUEST_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    province = str(payload.get("principalSubdivision") or "").strip()
    city = str(payload.get("city") or payload.get("locality") or "").strip() or province
    result = (province, city)
    with LOCATION_CACHE_LOCK:
        if len(LOCATION_CACHE) > 32:
            LOCATION_CACHE.clear()
        LOCATION_CACHE[key] = result
    return result


def domestic_condition_code(description: str) -> int:
    """Translate a Chinese weather description into the WMO code the firmware uses."""
    text = description or ""
    if "雷" in text:
        return 96 if "冰雹" in text else 95
    if "冰雹" in text:
        return 96
    if "雪" in text:
        if "暴" in text:
            return 75
        if "中" in text:
            return 73
        return 71
    if "雨" in text:
        if "暴" in text or "大" in text:
            return 65
        if "中" in text:
            return 63
        if "阵" in text or "毛" in text:
            return 80
        return 61
    if "雾" in text or "霾" in text or "沙" in text or "尘" in text:
        return 45
    if "阴" in text:
        return 3
    if "云" in text:
        return 2
    if "晴" in text:
        return 0
    return 2


def domestic_wind_kph(level_value: Any) -> int:
    """Convert the reported Beaufort level ("3-4") into an approximate km/h."""
    return WIND_LEVEL_KPH.get(_leading_int(level_value, 0), 0)


def lunar_date_text(now: float | None = None) -> str:
    """Return the Chinese lunar date with sexagenary cycle, computed locally."""
    now = time.time() if now is None else now
    day_key = time.strftime("%Y-%m-%d", time.localtime(now))
    with LUNAR_CACHE_LOCK:
        cached = LUNAR_CACHE.get(day_key)
    if cached is not None:
        return cached
    try:
        from cnlunar import Lunar

        lunar = Lunar(datetime.fromtimestamp(now), godType="8char")
        month_cn = str(lunar.lunarMonthCn)
        if month_cn and month_cn[-1] in {"大", "小"}:
            month_cn = month_cn[:-1]
        text = _bounded_text(
            f"农历{lunar.year8Char}年{month_cn}{lunar.lunarDayCn} {lunar.day8Char}日",
            LUNAR_TEXT_MAX_BYTES,
            "date.lunar_text",
        )
    except Exception as error:  # noqa: BLE001 - lunar display must never break the snapshot
        logging.warning("农历计算失败：%s", error)
        return ""
    with LUNAR_CACHE_LOCK:
        if len(LUNAR_CACHE) > 8:
            LUNAR_CACHE.clear()
        LUNAR_CACHE[day_key] = text
    return text


def _custom_text(payload: dict[str, Any], field: str, maximum: int, *, required: bool = False) -> str:
    value = payload.get(field, CUSTOM_DEFAULTS[field])
    if not isinstance(value, str):
        raise ValueError(f"custom.{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"custom.{field} is required")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"custom.{field} exceeds {maximum} UTF-8 bytes")
    return value


def _custom_bool(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field, CUSTOM_DEFAULTS[field])
    if not isinstance(value, bool):
        raise ValueError(f"custom.{field} must be a boolean")
    return value


def _custom_color(payload: dict[str, Any], field: str, fallback: str | None = None) -> str:
    value = payload.get(field, fallback if fallback is not None else CUSTOM_DEFAULTS[field])
    if not isinstance(value, str) or CUSTOM_COLOR_PATTERN.fullmatch(value) is None:
        raise ValueError(f"custom.{field} must be a #RRGGBB color")
    return value.upper()


def validate_custom_config(payload: Any) -> dict[str, Any]:
    if payload is None:
        return dict(CUSTOM_DEFAULTS)
    if not isinstance(payload, dict):
        raise ValueError("custom must be an object")
    enabled = payload.get("enabled", CUSTOM_DEFAULTS["enabled"])
    if not isinstance(enabled, bool):
        raise ValueError("custom.enabled must be a boolean")
    accent = payload.get("accent", CUSTOM_DEFAULTS["accent"])
    if not isinstance(accent, str) or CUSTOM_COLOR_PATTERN.fullmatch(accent) is None:
        raise ValueError("custom.accent must be a #RRGGBB color")
    title_visible = _custom_bool(payload, "title_visible")
    value_visible = _custom_bool(payload, "value_visible")
    body_visible = _custom_bool(payload, "body_visible")
    footer_visible = _custom_bool(payload, "footer_visible")
    title_color = _custom_color(payload, "title_color")
    value_color = _custom_color(payload, "value_color", accent)
    body_color = _custom_color(payload, "body_color")
    footer_color = _custom_color(payload, "footer_color")
    background_image = payload.get("background_image", False)
    ring_enabled = payload.get("ring_enabled", True)
    if not isinstance(background_image, bool):
        raise ValueError("custom.background_image must be a boolean")
    if not isinstance(ring_enabled, bool):
        raise ValueError("custom.ring_enabled must be a boolean")
    image_fit = payload.get("image_fit", "cover")
    if image_fit not in {"cover", "contain"}:
        raise ValueError("custom.image_fit must be cover or contain")
    image_opacity = _non_negative_int(payload.get("image_opacity", 70), "custom.image_opacity")
    if image_opacity > 100:
        raise ValueError("custom.image_opacity must be between 0 and 100")
    ring_start = payload.get("ring_start", accent)
    ring_end = payload.get("ring_end", ring_start)
    if not isinstance(ring_start, str) or CUSTOM_COLOR_PATTERN.fullmatch(ring_start) is None:
        raise ValueError("custom.ring_start must be a #RRGGBB color")
    if not isinstance(ring_end, str) or CUSTOM_COLOR_PATTERN.fullmatch(ring_end) is None:
        raise ValueError("custom.ring_end must be a #RRGGBB color")
    return {
        "enabled": enabled,
        "revision": _non_negative_int(payload.get("revision", 0), "custom.revision"),
        "title": _custom_text(payload, "title", 48),
        "value": _custom_text(payload, "value", 96),
        "body": _custom_text(payload, "body", 256),
        "footer": _custom_text(payload, "footer", 96),
        "accent": accent.upper(),
        "title_visible": title_visible,
        "title_color": title_color,
        "value_visible": value_visible,
        "value_color": value_color,
        "body_visible": body_visible,
        "body_color": body_color,
        "footer_visible": footer_visible,
        "footer_color": footer_color,
        "background_image": background_image,
        "image_fit": image_fit,
        "image_opacity": image_opacity,
        "ring_enabled": ring_enabled,
        "ring_start": ring_start.upper(),
        "ring_end": ring_end.upper(),
    }


def validate_module_config(payload: Any) -> dict[str, bool]:
    if payload is None:
        return dict(MODULE_DEFAULTS)
    if not isinstance(payload, dict):
        raise ValueError("modules must be an object")
    result: dict[str, bool] = {}
    for module_id, default in MODULE_DEFAULTS.items():
        enabled = payload.get(module_id, default)
        if not isinstance(enabled, bool):
            raise ValueError(f"modules.{module_id} must be a boolean")
        result[module_id] = enabled
    return result


def default_dotii_config() -> dict[str, Any]:
    return {
        "revision": 0,
        "animations": {
            expression_id: {
                "states": list(DOTII_DEFAULT_BUSINESS_ASSIGNMENTS.get(expression_id, ())),
                "state_duration_ms": 0,
            }
            for expression_id in DOTII_EXPRESSION_IDS
        },
        "available_states": [
            dict(definition) for definition in DOTII_BUSINESS_STATE_DEFINITIONS
        ],
        "state_groups": [dict(group) for group in DOTII_STATE_GROUPS],
        "fixed_states": [
            {
                **definition,
                "duration_ms": DOTII_LOOP_DURATIONS_MS[definition["expression"]],
            }
            for definition in DOTII_FIXED_STATE_DEFINITIONS
        ],
        "duration_options": [dict(option) for option in DOTII_STATE_DURATION_OPTIONS],
    }


def validate_dotii_config(payload: Any) -> dict[str, Any]:
    defaults = default_dotii_config()
    if payload is None:
        return defaults
    if not isinstance(payload, dict):
        raise ValueError("dotii config must be an object")
    animations = payload.get("animations")
    if not isinstance(animations, dict):
        raise ValueError("dotii.animations must be an object")

    unknown_animations = set(animations) - DOTII_EXPRESSIONS - DOTII_LEGACY_EXPRESSIONS
    if unknown_animations:
        raise ValueError(f"unsupported Dotii animations: {', '.join(sorted(unknown_animations))}")

    candidates: dict[str, dict[str, Any]] = {}
    assigned_states: dict[str, str] = {}
    for expression_id in (*DOTII_EXPRESSION_IDS, *sorted(DOTII_LEGACY_EXPRESSIONS)):
        candidate = animations.get(expression_id)
        if expression_id in DOTII_LEGACY_EXPRESSIONS and candidate is None:
            continue
        if not isinstance(candidate, dict):
            raise ValueError(f"dotii.animations.{expression_id} must be an object")
        states = candidate.get("states")
        if not isinstance(states, list):
            raise ValueError(f"dotii.animations.{expression_id}.states must be a list")
        candidates[expression_id] = candidate
        for legacy_state_id in states:
            if not isinstance(legacy_state_id, str):
                raise ValueError(f"unsupported Dotii state in {expression_id}")
            expanded_states = DOTII_LEGACY_STATE_EXPANSIONS.get(
                legacy_state_id, (legacy_state_id,)
            )
            for state_id in expanded_states:
                if state_id in DOTII_FIXED_STATE_EXPRESSIONS:
                    continue
                if state_id not in DOTII_BUSINESS_STATE_LABELS:
                    raise ValueError(f"unsupported Dotii state in {expression_id}")
                if state_id in assigned_states:
                    raise ValueError(
                        f"Dotii state {state_id} is assigned to both "
                        f"{assigned_states[state_id]} and {expression_id}"
                    )
                assigned_states[state_id] = expression_id

    validated_animations: dict[str, Any] = {
        expression_id: {"states": [], "state_duration_ms": 0}
        for expression_id in DOTII_EXPRESSION_IDS
    }
    for state_id, source_expression in assigned_states.items():
        expression_id = source_expression if source_expression in DOTII_BUSINESS_EXPRESSION_IDS else (
            DOTII_DEFAULT_BUSINESS_EXPRESSION[state_id]
        )
        validated_animations[expression_id]["states"].append(state_id)

    for expression_id in DOTII_BUSINESS_EXPRESSION_IDS:
        candidate = candidates[expression_id]
        state_duration_ms = candidate.get("state_duration_ms")
        if state_duration_ms is None and "duration_ms" in candidate:
            state_duration_ms = 0
        if state_duration_ms is None:
            state_duration_ms = 0
        if isinstance(state_duration_ms, bool) or not isinstance(state_duration_ms, int):
            raise ValueError(
                f"dotii.animations.{expression_id}.state_duration_ms must be an integer"
            )
        if state_duration_ms not in DOTII_STATE_DURATION_VALUES:
            raise ValueError(
                f"dotii.animations.{expression_id}.state_duration_ms must be "
                "0, 1000, 3000, 5000 or 30000"
            )
        validated_animations[expression_id] = {
            "states": validated_animations[expression_id]["states"],
            "state_duration_ms": state_duration_ms,
        }

    return {
        "revision": _non_negative_int(payload.get("revision", 0), "dotii.revision"),
        "animations": validated_animations,
        "available_states": defaults["available_states"],
        "state_groups": defaults["state_groups"],
        "fixed_states": defaults["fixed_states"],
        "duration_options": defaults["duration_options"],
    }


def _dotii_assignment_for_state(config: dict[str, Any], state_id: str) -> tuple[str, bool]:
    fixed_expression = DOTII_FIXED_STATE_EXPRESSIONS.get(state_id)
    if fixed_expression is not None:
        return fixed_expression, True
    for expression_id in DOTII_BUSINESS_EXPRESSION_IDS:
        if state_id in config["animations"][expression_id]["states"]:
            return expression_id, True
    return "idle_breath", False


def dotii_state(bambu: dict[str, Any], enabled: bool,
                config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve the device companion expression from public module state only."""
    config = validate_dotii_config(config)
    bambu_status = bambu.get("status", "offline")
    bambu_connected = bool(bambu.get("configured")) and bool(bambu.get("connected"))

    if bambu_connected and bambu_status == "fault":
        state_id, reason = "bambu_failure", "Bambu 打印机需要处理"
    elif bambu_connected and bambu_status in {"preparing", "printing", "cancelling"}:
        state_id, reason = "bambu_printing", "Bambu 正在打印"
    elif bambu_connected and bambu_status == "completed":
        state_id, reason = "bambu_completed", "Bambu 打印已经完成"
    elif bambu_connected and bambu_status == "paused":
        state_id, reason = "bambu_paused", "Bambu 打印已暂停"
    else:
        state_id, reason = "idle", "当前处于空闲状态"

    expression, state_assigned = _dotii_assignment_for_state(config, state_id)
    animations = config["animations"]

    def local_state(local_id: str) -> dict[str, Any]:
        local_expression = DOTII_FIXED_STATE_EXPRESSIONS[local_id]
        return {
            "expression": local_expression,
            "duration_ms": DOTII_LOOP_DURATIONS_MS[local_expression],
        }

    state_duration_ms = (
        animations[expression]["state_duration_ms"]
        if state_assigned and state_id in DOTII_BUSINESS_STATE_LABELS
        else 0
    )

    return {
        "enabled": enabled,
        "expression": expression,
        "state": state_id,
        "reason": reason,
        "state_assigned": state_assigned,
        "state_duration_ms": state_duration_ms,
        "state_hold": state_assigned and state_duration_ms == 0,
        "state_token": zlib.crc32(state_id.encode("utf-8")) & 0xFFFFFFFF,
        "duration_ms": DOTII_LOOP_DURATIONS_MS[expression],
        "durations_ms": dict(DOTII_LOOP_DURATIONS_MS),
        "local_states": {
            "touch": local_state("touch"),
            "blink": local_state("blink"),
            "long_idle": local_state("long_idle"),
        },
        "config_revision": config["revision"],
        "available_expressions": list(DOTII_EXPRESSION_IDS),
    }


def _validate_timeout_pair(payload: dict[str, Any], screen_off_key: str, sleep_key: str,
                           fallback_screen_off: int, fallback_sleep: int) -> tuple[int, int]:
    if screen_off_key not in payload and sleep_key not in payload:
        return fallback_screen_off, fallback_sleep
    sleep_timeout = payload.get(sleep_key, fallback_sleep)
    if isinstance(sleep_timeout, bool) or not isinstance(sleep_timeout, int):
        raise ValueError(f"display.{sleep_key} must be an integer")
    if sleep_timeout == 5:
        sleep_timeout = 10
    if sleep_timeout not in DISPLAY_TIMEOUT_OPTIONS:
        raise ValueError(f"display.{sleep_key} must be one of 0, 10, 30, 60, 180, 300, 600, 1800")
    screen_off_timeout = payload.get(screen_off_key)
    if screen_off_timeout is None:
        if sleep_timeout == 0:
            screen_off_timeout = fallback_screen_off
        else:
            earlier_options = [value for value in DISPLAY_TIMEOUT_OPTIONS if 0 < value <= sleep_timeout]
            screen_off_timeout = max(earlier_options) if earlier_options else 10
            if not earlier_options:
                sleep_timeout = 10
    if isinstance(screen_off_timeout, bool) or not isinstance(screen_off_timeout, int):
        raise ValueError(f"display.{screen_off_key} must be an integer")
    if screen_off_timeout == 5:
        screen_off_timeout = 10
    if screen_off_timeout not in DISPLAY_TIMEOUT_OPTIONS:
        raise ValueError(f"display.{screen_off_key} must be one of 0, 10, 30, 60, 180, 300, 600, 1800")
    if screen_off_timeout == 0 and sleep_timeout != 0:
        raise ValueError(f"display.{sleep_key} must be never when {screen_off_key} is never")
    if screen_off_timeout != 0 and sleep_timeout != 0 and sleep_timeout < screen_off_timeout:
        raise ValueError(f"display.{sleep_key} must be greater than or equal to {screen_off_key}")
    return screen_off_timeout, sleep_timeout


def validate_display_config(payload: Any) -> dict[str, Any]:
    if payload is None:
        return dict(DISPLAY_DEFAULTS)
    if not isinstance(payload, dict):
        raise ValueError("display must be an object")
    angle = payload.get("docked_rotation_tenths", DISPLAY_DEFAULTS["docked_rotation_tenths"])
    if isinstance(angle, bool) or not isinstance(angle, int) or not 800 <= angle <= 1000:
        raise ValueError("display.docked_rotation_tenths must be an integer between 800 and 1000")
    screen_off_page = payload.get("screen_off_page", DISPLAY_DEFAULTS["screen_off_page"])
    if screen_off_page not in SCREEN_OFF_PAGE_OPTIONS:
        raise ValueError("display.screen_off_page must be none, custom or dotii")
    clock_style = payload.get("clock_style", DISPLAY_DEFAULTS["clock_style"])
    if clock_style not in CLOCK_STYLE_OPTIONS:
        raise ValueError("display.clock_style must be classic, minimal, sunset or mint")
    screen_off_timeout, sleep_timeout = _validate_timeout_pair(
        payload, "screen_off_timeout_seconds", "sleep_timeout_seconds",
        DISPLAY_DEFAULTS["screen_off_timeout_seconds"], DISPLAY_DEFAULTS["sleep_timeout_seconds"],
    )
    charging_screen_off_timeout, charging_sleep_timeout = _validate_timeout_pair(
        payload, "charging_screen_off_timeout_seconds", "charging_sleep_timeout_seconds",
        screen_off_timeout, sleep_timeout,
    )
    return {
        "revision": _non_negative_int(payload.get("revision", 0), "display.revision"),
        "docked_rotation_tenths": angle,
        "screen_off_timeout_seconds": screen_off_timeout,
        "sleep_timeout_seconds": sleep_timeout,
        "charging_screen_off_timeout_seconds": charging_screen_off_timeout,
        "charging_sleep_timeout_seconds": charging_sleep_timeout,
        "screen_off_page": screen_off_page,
        "clock_style": clock_style,
    }


def validate_snapshot(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("schema_version must be 1")
    date = payload.get("date")
    if date is not None and not isinstance(date, dict):
        raise ValueError("date must be an object")
    weather = payload.get("weather")
    if weather is not None and not isinstance(weather, dict):
        raise ValueError("weather must be an object")
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_epoch": _non_negative_int(
            payload.get("generated_at_epoch", int(time.time())), "generated_at_epoch"
        ),
        "preview_data": bool(payload.get("preview_data", False)),
        "source": _bounded_text(payload.get("source", "manual"), 32, "source"),
        "date": {
            "lunar_text": _bounded_text(
                (date or {}).get("lunar_text", ""), LUNAR_TEXT_MAX_BYTES, "date.lunar_text"
            ),
        },
        "weather": validate_weather_status(weather or {}),
    }


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self.lock:
            return validate_snapshot(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, snapshot: dict[str, Any]) -> None:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(snapshot, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)


class WeatherConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self.lock:
            if not self.path.is_file():
                return dict(WEATHER_DEFAULTS)
            return validate_weather_config(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, config: dict[str, Any]) -> dict[str, Any]:
        validated = validate_weather_config(config)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix="weather-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(validated, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return validated


class CustomConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self.lock:
            if not self.path.is_file():
                return dict(CUSTOM_DEFAULTS)
            return validate_custom_config(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, config: dict[str, Any]) -> dict[str, Any]:
        validated = validate_custom_config(config)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix="custom-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(validated, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return validated


class CustomAssetStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.render_path = folder / "custom-screen.rgb565"
        self.pending_path = folder / "custom-screen.pending"
        self.source_path = folder / "custom-source.bin"
        self.source_meta_path = folder / "custom-source.json"
        self.lock = threading.RLock()

    @staticmethod
    def _atomic_bytes(path: Path, body: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary_name = tempfile.mkstemp(prefix=path.stem + "-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(body)
            os.replace(temporary_name, path)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def write_source(self, body: bytes, content_type: str) -> dict[str, Any]:
        if content_type == "image/png":
            if not body.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("PNG 文件签名无效")
        elif content_type == "image/jpeg":
            if len(body) < 4 or not body.startswith(b"\xff\xd8") or not body.endswith(b"\xff\xd9"):
                raise ValueError("JPEG 文件签名无效")
        else:
            raise ValueError("仅支持 PNG 或 JPEG 图片")
        with self.lock:
            self._atomic_bytes(self.source_path, body)
            self._atomic_bytes(
                self.source_meta_path,
                json.dumps({
                    "content_type": content_type,
                    "revision": zlib.crc32(body) & 0xFFFFFFFF,
                }, ensure_ascii=False).encode("utf-8"),
            )
            return self.source_info()

    def source_info(self) -> dict[str, Any]:
        with self.lock:
            if not self.source_path.is_file() or not self.source_meta_path.is_file():
                return {"source_available": False, "source_revision": 0, "source_mime": ""}
            meta = json.loads(self.source_meta_path.read_text(encoding="utf-8"))
            return {
                "source_available": True,
                "source_revision": int(meta.get("revision", 0)),
                "source_mime": meta.get("content_type", "application/octet-stream"),
            }

    def read_source(self) -> tuple[bytes, str]:
        info = self.source_info()
        if not info["source_available"]:
            raise FileNotFoundError("custom source image not found")
        return self.source_path.read_bytes(), info["source_mime"]

    def stage_render(self, body: bytes) -> str:
        if len(body) != CUSTOM_FRAME_SIZE:
            raise ValueError(f"自定义画面必须为 {CUSTOM_FRAME_SIZE} 字节 RGB565")
        token = hashlib.sha256(body).hexdigest()
        with self.lock:
            self._atomic_bytes(self.pending_path, body)
        return token

    def commit_render(self, token: str) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) != 64:
            raise ValueError("render_token 无效")
        with self.lock:
            if not self.pending_path.is_file():
                raise ValueError("待保存画面不存在，请重新生成")
            body = self.pending_path.read_bytes()
            if not secrets.compare_digest(hashlib.sha256(body).hexdigest(), token):
                raise ValueError("待保存画面校验失败")
            os.replace(self.pending_path, self.render_path)
            return self.render_info()

    def render_info(self) -> dict[str, Any]:
        with self.lock:
            if not self.render_path.is_file() or self.render_path.stat().st_size != CUSTOM_FRAME_SIZE:
                return {"image_available": False, "image_revision": 0, "image_size": 0}
            body = self.render_path.read_bytes()
            return {
                "image_available": True,
                "image_revision": zlib.crc32(body) & 0xFFFFFFFF,
                "image_size": len(body),
            }

    def read_render(self) -> bytes:
        with self.lock:
            body = self.render_path.read_bytes()
            if len(body) != CUSTOM_FRAME_SIZE:
                raise ValueError("stored custom frame has invalid size")
            return body


class ModuleConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, bool]:
        with self.lock:
            if not self.path.is_file():
                return dict(MODULE_DEFAULTS)
            return validate_module_config(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, config: dict[str, Any]) -> dict[str, bool]:
        validated = validate_module_config(config)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix="modules-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(validated, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return validated


class DotiiConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self.lock:
            if not self.path.is_file():
                return default_dotii_config()
            return validate_dotii_config(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, config: dict[str, Any]) -> dict[str, Any]:
        validated = validate_dotii_config(config)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix="dotii-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(validated, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return validated


class DisplayConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self.lock:
            if not self.path.is_file():
                return dict(DISPLAY_DEFAULTS)
            return validate_display_config(json.loads(self.path.read_text(encoding="utf-8")))

    def write(self, config: dict[str, Any]) -> dict[str, Any]:
        validated = validate_display_config(config)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary_name = tempfile.mkstemp(prefix="display-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(validated, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary_name, self.path)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        return validated


class RuntimeStatus:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started_at_epoch = int(time.time())
        self.collector_state = "disabled"
        self.collector_detail = "后台数据源未启用"
        self.collector_updated_epoch = self.started_at_epoch

    def report(self, state: str, detail: str) -> None:
        with self.lock:
            self.collector_state = state
            self.collector_detail = detail[:240]
            self.collector_updated_epoch = int(time.time())

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "started_at_epoch": self.started_at_epoch,
                "collector_state": self.collector_state,
                "collector_detail": self.collector_detail,
                "collector_updated_epoch": self.collector_updated_epoch,
            }


class WeatherService:
    """Fetch weather from a mainland provider on demand, falling back to Open-Meteo.

    Failures keep the previous values but mark them unavailable so the UI never
    shows stale numbers as live data.
    """

    def __init__(self, config_store: WeatherConfigStore, enabled_fn: Any) -> None:
        self.config_store = config_store
        self.enabled_fn = enabled_fn
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.refresh_now = threading.Event()
        self.status: dict[str, Any] = {
            "enabled": False,
            "available": False,
            "fetched_at_epoch": 0,
            "city": "",
            "condition_code": 0,
            "temperature_c": 0,
            "temp_low_c": 0,
            "temp_high_c": 0,
            "humidity_percent": 0,
            "wind_kph": 0,
        }
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        self.worker = threading.Thread(target=self._run, name="weather-service", daemon=True)
        self.worker.start()

    def stop_service(self) -> None:
        self.stop.set()
        self.refresh_now.set()

    def reconfigure(self) -> None:
        self.refresh_now.set()

    def _run(self) -> None:
        # Fetch immediately on startup and on every reconfigure, then keep the
        # 10 minute cadence. Waiting for the deadline first made a freshly saved
        # location look like it had never been applied.
        while not self.stop.is_set():
            self.refresh()
            deadline = time.monotonic() + WEATHER_REFRESH_SECONDS
            while not self.stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if self.refresh_now.wait(min(2.0, remaining)):
                    self.refresh_now.clear()
                    break

    def refresh(self) -> None:
        config = self.config_store.read()
        module_enabled = bool(self.enabled_fn())
        active = module_enabled and bool(config.get("enabled")) \
            and config.get("latitude") is not None and config.get("longitude") is not None
        if not active:
            with self.lock:
                self.status = {
                    "enabled": module_enabled and bool(config.get("enabled")),
                    "available": False,
                    "fetched_at_epoch": 0,
                    "city": config.get("city", ""),
                    "condition_code": 0,
                    "temperature_c": 0,
                    "temp_low_c": 0,
                    "temp_high_c": 0,
                    "humidity_percent": 0,
                    "wind_kph": 0,
                }
            return
        config = self._config_with_resolved_location(config)
        try:
            data = self._fetch(config)
            with self.lock:
                self.status = validate_weather_status({
                    "enabled": True,
                    "available": True,
                    "fetched_at_epoch": int(time.time()),
                    "city": config.get("city", ""),
                    **data,
                })
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            logging.warning("天气数据获取失败：%s", error)
            with self.lock:
                previous = dict(self.status)
                previous["enabled"] = True
                previous["available"] = False
                previous["city"] = config.get("city", "")
                self.status = previous

    def _config_with_resolved_location(self, config: dict[str, Any]) -> dict[str, Any]:
        """Treat coordinates as the source of truth for the displayed location."""
        latitude = config.get("latitude")
        longitude = config.get("longitude")
        if latitude is None or longitude is None:
            return config
        try:
            province, city = resolve_location_names(latitude, longitude)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            logging.warning("经纬度反查城市失败：%s", error)
            return config
        if not city:
            return config
        if config.get("province") == province and config.get("city") == city:
            return config
        resolved = {**config, "province": province, "city": city}
        try:
            self.config_store.write(resolved)
        except (OSError, ValueError) as error:
            logging.warning("回写城市名称失败：%s", error)
        return resolved

    def _fetch(self, config: dict[str, Any]) -> dict[str, Any]:
        """Query the domestic provider first and keep Open-Meteo as a fallback."""
        city = str(config.get("city") or "").strip()
        province = str(config.get("province") or "").strip() or city
        if city:
            try:
                return self._fetch_domestic(province, city)
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
                logging.warning("国内天气接口获取失败，改用 Open-Meteo：%s", error)
        return self._fetch_open_meteo(config)

    def _fetch_domestic(self, province: str, city: str) -> dict[str, Any]:
        query = urlencode({
            "source": "pc",
            "weather_type": "observe|forecast_24h",
            "province": province,
            "city": city,
        })
        request = urllib.request.Request(
            f"{WEATHER_DOMESTIC_URL}?{query}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.qq.com/"},
        )
        with urllib.request.urlopen(request, timeout=WEATHER_REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("status") not in (200, None):
            raise ValueError("国内天气接口返回失败")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("国内天气接口缺少 data 字段")
        observe = data.get("observe")
        if not isinstance(observe, dict) or not str(observe.get("degree", "")).strip():
            raise ValueError("国内天气接口未返回该城市的实况")
        today = (data.get("forecast_24h") or {}).get("0") or {}
        current = _leading_int(observe.get("degree"))
        return {
            "condition_code": domestic_condition_code(
                str(today.get("day_weather") or observe.get("weather", ""))
            ),
            "temperature_c": current,
            "temp_high_c": _leading_int(today.get("max_degree"), current),
            "temp_low_c": _leading_int(today.get("min_degree"), current),
            "humidity_percent": max(0, min(100, _leading_int(observe.get("humidity")))),
            "wind_kph": max(0, domestic_wind_kph(observe.get("wind_power"))),
        }

    def _fetch_open_meteo(self, config: dict[str, Any]) -> dict[str, Any]:
        query = urlencode({
            "latitude": f"{config['latitude']:.4f}",
            "longitude": f"{config['longitude']:.4f}",
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "forecast_days": 1,
            "timezone": "auto",
        })
        request = urllib.request.Request(
            f"https://api.open-meteo.com/v1/forecast?{query}",
            headers={"User-Agent": "DotiiManagementCenter"},
        )
        with urllib.request.urlopen(request, timeout=WEATHER_REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        current = payload["current"]
        daily = payload["daily"]
        return {
            "condition_code": int(current["weather_code"]),
            "temperature_c": int(round(float(current["temperature_2m"]))),
            "temp_high_c": int(round(float(daily["temperature_2m_max"][0]))),
            "temp_low_c": int(round(float(daily["temperature_2m_min"][0]))),
            "humidity_percent": int(round(float(current["relative_humidity_2m"]))),
            "wind_kph": int(round(float(current["wind_speed_10m"]))),
        }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.status)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = f"StateDisplayBridge/{APP_VERSION}"

    @property
    def bridge(self) -> "BridgeServer":
        return self.server  # type: ignore[return-value]

    def _authorized(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), self.bridge.token)

    def _local_admin(self) -> bool:
        return _is_loopback(self.client_address[0])

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; "
                "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            logging.debug("客户端在响应写入完成前断开：%s", self.client_address[0])

    def _send_json(self, status: int, payload: Any) -> None:
        if urlparse(self.path).path.startswith("/api/v1/admin/") and self.bridge.connections.read()["language"] == "en":
            payload = localize_admin(payload)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _deny_nonlocal(self) -> bool:
        if self._local_admin():
            return False
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "management interface is local only"})
        return True

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_BODY:
            raise ValueError("invalid request size")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _read_bytes(self, maximum: int, *, exact: int | None = None) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > maximum or (exact is not None and length != exact):
            raise ValueError("invalid request size")
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError("incomplete request body")
        return body

    def _read_admin_action(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("management actions require application/json")
        payload = self._read_json()
        if not isinstance(payload, dict):
            raise ValueError("request body must be an object")
        return payload

    def _serve_static(self, request_path: str) -> None:
        if self._deny_nonlocal():
            return
        relative = "index.html" if request_path == "/" else unquote(request_path).lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not target.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "image/svg+xml"}:
            content_type += "; charset=utf-8"
        self._send_bytes(HTTPStatus.OK, target.read_bytes(), content_type)

    def _overview(self) -> dict[str, Any]:
        snapshot = self.bridge.snapshot()
        address = local_ipv4()
        runtime = self.bridge.runtime.snapshot()
        return {
            "app": {"name": "Dotii 管理中心", "version": APP_VERSION},
            "bridge": {
                "online": True,
                "local_url": f"http://127.0.0.1:{self.bridge.server_port}",
                "device_url": f"http://{address}:{self.bridge.server_port}/api/v1/snapshot",
                "token": self.bridge.token,
                "port": self.bridge.server_port,
                "auto_start": startup_enabled(),
                "startup_available": current_platform().startup_available,
                **runtime,
            },
            "snapshot": snapshot,
            "weather_config": self.bridge.weather_config.read(),
            "dotii_config": self.bridge.dotii.read(),
            "display_config": self.bridge.display.read(),
            "bambu_config": self.bridge.bambu_config.public(),
            "connection_settings": self.bridge.connections.read(),
            "usb": self.bridge.usb.snapshot(),
            "wifi_last_seen": self.bridge.wifi_seen.get(self.bridge.connections.read()["device_id"], 0),
            "wifi_data_last_seen": self.bridge.wifi_data_seen.get(self.bridge.connections.read()["device_id"], 0),
            "bluetooth": self.bridge.bluetooth.snapshot(),
            "firmware": self.bridge.firmware.snapshot(),
            "modules": [
                {"id": "weather", "name": "天气", "enabled": snapshot["modules"]["weather"], "available": True, "locked": False},
                {"id": "bambu", "name": "Bambu", "enabled": snapshot["modules"]["bambu"], "available": True, "locked": False},
                {"id": "custom", "name": "自定义", "enabled": snapshot["custom"]["enabled"], "available": True, "locked": False},
                {"id": "dotii", "name": "Dotii", "enabled": snapshot["modules"]["dotii"], "available": True, "locked": False},
            ],
        }

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "schema_version": SCHEMA_VERSION,
                "parent_pid": self.bridge.host_parent_pid,
            })
        elif path == "/api/v1/device/ping":
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bridge token"})
                return
            device_id = self.headers.get("X-Dotii-Device", "")[:64]
            if device_id:
                self.bridge.wifi_seen[device_id] = int(time.time())
            self._send_json(HTTPStatus.OK, {"ok": True})
        elif path == "/api/v1/snapshot":
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bridge token"})
                return
            try:
                device_id = self.headers.get("X-Dotii-Device", "")[:64]
                if device_id:
                    self.bridge.wifi_seen[device_id] = int(time.time())
                    self.bridge.wifi_data_seen[device_id] = int(time.time())
                self._send_json(HTTPStatus.OK, self.bridge.snapshot())
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        elif path == "/api/v1/admin/overview":
            if self._deny_nonlocal():
                return
            try:
                self._send_json(HTTPStatus.OK, self._overview())
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        elif path == "/api/v1/admin/custom/source":
            if self._deny_nonlocal():
                return
            try:
                body, content_type = self.bridge.assets.read_source()
                self._send_bytes(HTTPStatus.OK, body, content_type)
            except FileNotFoundError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "尚未上传背景图片"})
            except (OSError, ValueError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
        elif path == "/api/v1/admin/bambu/camera.jpg":
            if self._deny_nonlocal():
                return
            body = self.bridge.bambu.get_camera_jpeg()
            if body:
                self._send_bytes(HTTPStatus.OK, body, "image/jpeg")
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "打印机相机暂不可用"})
        elif path == "/api/v1/bambu-camera.rgb565":
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bridge token"})
                return
            body = self.bridge.bambu.get_camera_rgb565()
            if body:
                self._send_bytes(HTTPStatus.OK, body, "application/octet-stream")
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "camera unavailable"})
        elif path == "/api/v1/custom-screen.rgb565":
            if not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bridge token"})
                return
            try:
                self._send_bytes(HTTPStatus.OK, self.bridge.assets.read_render(), "application/octet-stream")
            except (OSError, ValueError) as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
        else:
            self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in {"/api/v1/admin/connection", "/api/v1/admin/usb/configure"}:
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                if path.endswith("/configure"):
                    self.bridge.usb.configure(payload.get("ssid"), payload.get("password", ""), payload.get("preserve_password", False))
                else:
                    if set(payload) - {"language", "device_language", "mode", "port"}:
                        raise ValueError("Invalid connection settings")
                    self.bridge.connections.write(payload)
                self._send_json(HTTPStatus.OK, {"ok": True, "connection_settings": self.bridge.connections.read()})
            except (OSError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/weather/config":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                current = self.bridge.weather_config.read()
                candidate = {**current, **payload}
                if payload.get("enabled") is True:
                    candidate["enabled"] = True
                config = self.bridge.weather_config.write(candidate)
                self.bridge.weather.reconfigure()
                self._send_json(HTTPStatus.OK, {"ok": True, "weather_config": config})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/weather/refresh":
            if self._deny_nonlocal():
                return
            try:
                self._read_admin_action()
                self.bridge.weather.reconfigure()
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/bluetooth/install":
            if self._deny_nonlocal():
                return
            try:
                self._read_admin_action()
                started = self.bridge.bluetooth.start_install()
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json(status, {"ok": started, "bluetooth": self.bridge.bluetooth.snapshot()})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/bluetooth/scan":
            if self._deny_nonlocal():
                return
            try:
                self._read_admin_action()
                started = self.bridge.bluetooth.start_scan()
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json(status, {"ok": started})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/bluetooth/configure":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                address = local_ipv4()
                bridge_url = f"http://{address}:{self.bridge.server_port}/api/v1/snapshot"
                started = self.bridge.bluetooth.start_configure(
                    address=payload.get("address"), ssid=payload.get("ssid"),
                    password=payload.get("password"), bridge_url=bridge_url,
                    bridge_token=self.bridge.token,
                    current_token=payload.get("current_token", ""),
                )
                status = HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT
                self._send_json(status, {"ok": started})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/firmware/refresh":
            if self._deny_nonlocal():
                return
            try:
                self._read_admin_action()
                self.bridge.firmware.refresh()
                self._send_json(HTTPStatus.OK, {"ok": True, "firmware": self.bridge.firmware.snapshot()})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/firmware/flash":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                if payload.get("confirmation") != "FLASH_DOTII":
                    raise ValueError("需要确认后才能烧录")
                with self.bridge.usb.flash_lock:
                    if self.bridge.firmware.operation_state == "running":
                        self._send_json(HTTPStatus.CONFLICT, {"error": "已有烧录任务正在运行"})
                        return
                    self.bridge.usb.pause()
                    try:
                        started = self.bridge.firmware.start_flash(payload.get("port"))
                    except Exception:
                        self.bridge.usb.resume()
                        raise
                    if not started:
                        self.bridge.usb.resume()
                    else:
                        def resume_usb():
                            self.bridge.firmware.worker.join()
                            self.bridge.usb.resume()
                        threading.Thread(target=resume_usb, daemon=True, name="usb-after-flash").start()
                    if not started:
                        self._send_json(HTTPStatus.CONFLICT, {"error": "已有烧录任务正在运行"})
                    else:
                        self._send_json(HTTPStatus.ACCEPTED, {"ok": True})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/bambu/config":
            if self._deny_nonlocal():
                return
            try:
                config = self.bridge.bambu_config.write(self._read_admin_action(), preserve_secret=True)
                self.bridge.bambu.reconfigure()
                self._send_json(HTTPStatus.OK, {"ok": True, "bambu_config": config})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/bambu/refresh":
            if self._deny_nonlocal():
                return
            try:
                self._read_admin_action()
                self.bridge.bambu.reconfigure()
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path in {"/api/v1/admin/bambu/command", "/api/v1/bambu/command"}:
            if path.startswith("/api/v1/admin/"):
                if self._deny_nonlocal():
                    return
            elif not self._authorized():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bridge token"})
                return
            try:
                payload = self._read_admin_action()
                result = self.bridge.bambu.command(payload.get("action"))
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, **result})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/custom/source":
            if self._deny_nonlocal():
                return
            try:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                info = self.bridge.assets.write_source(
                    self._read_bytes(MAX_CUSTOM_SOURCE_SIZE), content_type
                )
                self._send_json(HTTPStatus.OK, {"ok": True, **info})
            except (OSError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/custom/render":
            if self._deny_nonlocal():
                return
            try:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/octet-stream":
                    raise ValueError("自定义画面必须使用 application/octet-stream")
                token = self.bridge.assets.stage_render(
                    self._read_bytes(CUSTOM_FRAME_SIZE, exact=CUSTOM_FRAME_SIZE)
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "render_token": token})
            except (OSError, ValueError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/custom":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                current = self.bridge.custom.read()
                modules = self.bridge.modules.read()
                if payload.get("enabled") is False and not any(modules.values()):
                    raise ValueError("至少保留一个启用页面")
                payload["revision"] = current["revision"] + 1
                render_token = payload.pop("render_token", None)
                validated = validate_custom_config(payload)
                if render_token is not None:
                    self.bridge.assets.commit_render(render_token)
                custom = self.bridge.custom.write(validated)
                custom.update(self.bridge.assets.source_info())
                custom.update(self.bridge.assets.render_info())
                self._send_json(HTTPStatus.OK, {"ok": True, "custom": custom})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/modules":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                module_id = payload.get("id")
                enabled = payload.get("enabled")
                if module_id not in MODULE_DEFAULTS or not isinstance(enabled, bool):
                    raise ValueError("id must be weather, bambu or dotii and enabled must be boolean")
                modules = self.bridge.modules.read()
                candidate = {**modules, module_id: enabled}
                if not any(candidate.values()) and not self.bridge.custom.read()["enabled"]:
                    raise ValueError("至少保留一个启用页面")
                saved = self.bridge.modules.write(candidate)
                if module_id == "weather":
                    self.bridge.weather.reconfigure()
                elif module_id == "bambu":
                    self.bridge.bambu.set_enabled(enabled)
                self._send_json(HTTPStatus.OK, {"ok": True, "modules": saved})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/dotii":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                current = self.bridge.dotii.read()
                candidate = {**payload, "revision": current["revision"] + 1}
                config = self.bridge.dotii.write(candidate)
                self._send_json(HTTPStatus.OK, {"ok": True, "dotii_config": config})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/display":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_admin_action()
                current = self.bridge.display.read()
                candidate = {**current, **payload, "revision": current["revision"] + 1}
                display = self.bridge.display.write(candidate)
                self._send_json(HTTPStatus.OK, {"ok": True, "display_config": display})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path == "/api/v1/admin/settings":
            if self._deny_nonlocal():
                return
            try:
                payload = self._read_json()
                if not isinstance(payload, dict) or not isinstance(payload.get("auto_start"), bool):
                    raise ValueError("auto_start must be a boolean")
                enabled = set_startup(payload["auto_start"])
                self._send_json(HTTPStatus.OK, {"ok": True, "auto_start": enabled})
            except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        if path != "/api/v1/snapshot":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid bridge token"})
            return
        try:
            snapshot = validate_snapshot(self._read_json())
            self.bridge.store.write(snapshot)
            self._send_json(HTTPStatus.OK, {"ok": True, "generated_at_epoch": snapshot["generated_at_epoch"]})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def log_message(self, format_string: str, *args: Any) -> None:
        logging.info("%s %s", self.client_address[0], format_string % args)


class BridgeServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store: StateStore,
        token: str,
        runtime: RuntimeStatus,
        custom: CustomConfigStore,
        modules: ModuleConfigStore,
        dotii: DotiiConfigStore,
        display: DisplayConfigStore,
        weather_config: WeatherConfigStore,
        weather: WeatherService,
        assets: CustomAssetStore,
        bambu_config: BambuConfigStore,
        bambu: BambuService,
        bluetooth: BluetoothBridge,
        firmware: FirmwareFlasher,
        runtime_folder: Path,
        application_folder: Path,
        host_parent_pid: int = 0,
    ) -> None:
        super().__init__(address, BridgeHandler)
        self.store = store
        self.token = token
        self.runtime = runtime
        self.custom = custom
        self.modules = modules
        self.dotii = dotii
        self.display = display
        self.weather_config = weather_config
        self.weather = weather
        self.assets = assets
        self.bambu_config = bambu_config
        self.bambu = bambu
        self.bluetooth = bluetooth
        self.firmware = firmware
        self.runtime_folder = runtime_folder
        self.application_folder = application_folder
        self.host_parent_pid = host_parent_pid
        self.connections = ConnectionSettings(runtime_folder / "connection.json")
        self.usb = UsbBridge(self)
        self.wifi_seen = {}
        self.wifi_data_seen = {}

    def device_url(self):
        return f"http://{local_ipv4()}:{self.server_port}/api/v1/snapshot"

    def snapshot(self) -> dict[str, Any]:
        snapshot = self.store.read()
        custom = self.custom.read()
        custom.update(self.assets.source_info())
        custom.update(self.assets.render_info())
        snapshot["custom"] = custom
        snapshot["bambu"] = self.bambu.snapshot()
        modules = self.modules.read()
        snapshot["modules"] = {**modules, "custom": custom["enabled"]}
        snapshot["date"] = {"lunar_text": lunar_date_text()}
        snapshot["weather"] = self.weather.snapshot()
        snapshot["dotii"] = dotii_state(
            snapshot["bambu"], modules["dotii"], self.dotii.read()
        )
        snapshot["display"] = {**self.display.read(), **self.connections.device()}
        return snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description="LAN bridge for the Dotii status display")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8787, type=int)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--token", default=os.environ.get("STATE_DISPLAY_BRIDGE_TOKEN"))
    parser.add_argument("--parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--watch-pid", type=int, action="append", default=[], help=argparse.SUPPRESS)
    parser.add_argument("--no-parent-watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--development", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--app-server", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Preserve the user's enabled/disabled choice while repairing stale paths
    # and releases that incorrectly registered DotiiBridge.exe as the login app.
    try:
        if not arguments.development and startup_enabled():
            set_startup(True)
    except OSError as error:
        logging.warning("无法修复登录自启动命令：%s", error)
    writable_root = runtime_folder()
    writable_root.mkdir(parents=True, exist_ok=True)
    state_path = arguments.state or writable_root / "state.json"
    if not state_path.exists():
        shutil.copyfile(resource_path("state.json"), state_path)

    token = arguments.token or load_or_create_token(writable_root / "bridge-token")
    store = StateStore(state_path)
    runtime = RuntimeStatus()
    stop = threading.Event()
    custom = CustomConfigStore(writable_root / "custom.json")
    modules = ModuleConfigStore(writable_root / "modules.json")
    dotii = DotiiConfigStore(writable_root / "dotii.json")
    display = DisplayConfigStore(writable_root / "display.json")
    weather_config = WeatherConfigStore(writable_root / "weather.json")
    assets = CustomAssetStore(writable_root)
    bambu_config = BambuConfigStore(writable_root / "bambu.json")
    bambu = BambuService(
        bambu_config,
        lambda: modules.read()["bambu"],
        lambda: resolve_ffmpeg(writable_root),
    )
    bambu.start()
    weather = WeatherService(weather_config, lambda: modules.read()["weather"])
    weather.start()
    bluetooth = BluetoothBridge(writable_root)
    bluetooth.start()
    firmware = FirmwareFlasher(project_root(), writable_root)
    if arguments.no_parent_watch:
        watch_pids: list[int] = []
    elif arguments.parent_pid > 1:
        watch_pids = [arguments.parent_pid]
    else:
        watch_pids = host_watch_pids()
    for pid in arguments.watch_pid:
        if pid > 1 and pid not in watch_pids:
            watch_pids.append(pid)
    server = BridgeServer((arguments.host, arguments.port), store, token, runtime,
                          custom, modules, dotii, display, weather_config, weather, assets,
                          bambu_config, bambu, bluetooth, firmware,
                          writable_root, application_root(), watch_pids[0] if watch_pids else 0)
    server.usb.start()
    if watch_pids:
        logging.info("宿主进程监控已启用：%s", ", ".join(str(pid) for pid in watch_pids))
        threading.Thread(
            target=watch_parent,
            args=(watch_pids, server, stop),
            name="dotii-parent-watchdog",
            daemon=True,
        ).start()
    runtime.report("disabled", "后台数据源未启用")

    address = local_ipv4()
    logging.info("Dotii 管理中心：http://127.0.0.1:%s", arguments.port)
    logging.info("ESP32 地址：http://%s:%s/api/v1/snapshot", address, arguments.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.usb.stop()
        weather.stop_service()
        bambu.stop()
        bluetooth.stop()
        firmware.stop()
        server.server_close()


if __name__ == "__main__":
    if is_frozen() and len(sys.argv) > 1 and sys.argv[1] == "--esptool":
        import esptool

        sys.argv = ["esptool", *sys.argv[2:]]
        raise SystemExit(esptool.main())
    main()
