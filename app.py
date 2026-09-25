"""GPT 生图工坊 - 本地内部服务。

职责：
  * 从上游 /models 发现 gpt-image-* 生图模型，并按模型能力组织请求参数。
  * 上游地址与 API key 只从客户端输入，本服务不读 .env、不读环境变量。
  * 文生图走 /images/generations（优先异步任务 + 轮询），图生图优先 /chat/completions，
    失败回退 /images/edits。
  * 保存返回图片到 outputs/generated/，历史记录写入 outputs/history.json。
  * 只监听 127.0.0.1，供桌面客户端与本地脚本调用。
"""

from __future__ import annotations

import atexit
import base64
import binascii
import io
import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory
from PIL import Image


# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
IS_FROZEN = bool(getattr(sys, "frozen", False))

if IS_FROZEN:
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    APP_DIR = Path(sys.executable).resolve().parent
else:
    RESOURCE_DIR = Path(__file__).resolve().parent
    APP_DIR = RESOURCE_DIR

OUTPUT_DIR = APP_DIR / "outputs" / "generated"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MODEL = "gpt-image-2"
# 默认模型优先顺序：文档里的 GPT 型号，最后回落到任意 gpt-image-*。
DEFAULT_MODEL_PREFERENCE = [
    "gpt-image-2",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5",
]

MISSING_BASE_URL_ERROR = "未填写提供商地址。请在客户端填写中转站 Base URL（通常以 /v1 结尾）。"
MISSING_KEY_ERROR = "未填写 API key。请在客户端填写中转站 API key。"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
HTTP = requests.Session()
HTTP.headers.update(_BROWSER_HEADERS)

# GPT Image 的官方能力固定为 10 档比例 × 1K / 2K / 4K；不支持 auto 与自定义像素。
GPT_RATIOS = [
    "1:1",
    "16:9",
    "9:16",
    "3:2",
    "2:3",
    "4:3",
    "3:4",
    "5:4",
    "4:5",
    "21:9",
]
GPT_K_LEVELS = ["1K", "2K", "4K"]
# 上游按 quality 判定像素档：1K -> standard、2K -> hd、4K -> 4k。
GPT_QUALITY_BY_K = {"1K": "standard", "2K": "hd", "4K": "4k"}
GPT_QUALITIES = ["standard", "hd", "4k"]
# 官方推荐标准像素表。
GPT_TIER_SIZES = {
    "1K": {
        "1:1": "1024x1024",
        "16:9": "1280x720",
        "9:16": "720x1280",
        "3:2": "1536x1024",
        "2:3": "1024x1536",
        "4:3": "1152x864",
        "3:4": "864x1152",
        "5:4": "1120x896",
        "4:5": "896x1120",
        "21:9": "1456x624",
    },
    "2K": {
        "1:1": "2048x2048",
        "16:9": "2560x1440",
        "9:16": "1440x2560",
        "3:2": "2496x1664",
        "2:3": "1664x2496",
        "4:3": "2304x1728",
        "3:4": "1728x2304",
        "5:4": "2240x1792",
        "4:5": "1792x2240",
        "21:9": "3024x1296",
    },
    "4K": {
        "1:1": "2480x2480",
        "16:9": "3328x1872",
        "9:16": "1872x3328",
        "3:2": "3056x2032",
        "2:3": "2032x3056",
        "4:3": "2880x2160",
        "3:4": "2160x2880",
        "5:4": "2784x2224",
        "4:5": "2224x2784",
        "21:9": "3808x1632",
    },
}
GPT_MAX_N = 4
GPT_MAX_REFERENCES = 4
# 参考图通道：chat（/chat/completions，首选）与 edits（/images/edits，回退）。
GPT_REFERENCE_TRANSPORTS = ("chat", "edits")

OUTPUTS_ROOT = APP_DIR / "outputs"
HISTORY_FILE = OUTPUTS_ROOT / "history.json"
HISTORY_LIMIT = 100
HISTORY_LOCK = threading.RLock()

# 上游 HTTP 参数。
SUBMIT_TIMEOUT = 300
# 异步提交成功时上游可能返回 200 / 201 / 202。
SUBMIT_OK_STATUS = {200, 201, 202}
IMAGE_DOWNLOAD_TIMEOUT = 120
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 异步任务参数：每 3 秒轮询一次上游，总预算 15 分钟，完成任务保留 30 分钟。
POLL_INTERVAL_SECONDS = 3
TASK_BUDGET_SECONDS = 900
# 同步型中转站的预期耗时（用于把已用时间换算成百分比）与估算门槛。
TASK_EXPECTED_SECONDS = 120
TASK_ESTIMATE_THRESHOLD = 5
TASK_TTL_SECONDS = 1800
TASK_POLL_SLEEP_SECONDS = 0.2
TASKS: dict[str, dict[str, Any]] = {}
TASKS_LOCK = threading.RLock()

TASK_PENDING = "pending"
TASK_RUNNING = "processing"
TASK_DONE = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_TERMINAL = (TASK_DONE, TASK_FAILED, TASK_CANCELLED)

_STATUS_ALIASES = {
    "pending": TASK_PENDING,
    "queued": TASK_PENDING,
    "submitted": TASK_PENDING,
    "created": TASK_PENDING,
    "in_queue": TASK_PENDING,
    "processing": TASK_RUNNING,
    "running": TASK_RUNNING,
    "in_progress": TASK_RUNNING,
    "generating": TASK_RUNNING,
    "completed": TASK_DONE,
    "complete": TASK_DONE,
    "succeeded": TASK_DONE,
    "success": TASK_DONE,
    "done": TASK_DONE,
    "failed": TASK_FAILED,
    "failure": TASK_FAILED,
    "error": TASK_FAILED,
    "cancelled": TASK_CANCELLED,
    "canceled": TASK_CANCELLED,
}

# 模型家族顺序：越小越靠前。
_GPT_MODEL_FAMILIES = [
    ("gpt-image-2-5-flare", 10, "GPT Image 2.5 Flare"),
    ("gpt-image-2-5-sunburst", 11, "GPT Image 2.5 Sunburst"),
    ("gpt-image-2-5", 12, "GPT Image 2.5"),
    ("gpt-image-2", 20, "GPT Image 2"),
    ("gpt-image-1-5", 30, "GPT Image 1.5"),
    ("gpt-image-1-mini", 40, "GPT Image 1 mini"),
    ("gpt-image-1", 50, "GPT Image 1"),
    ("gpt-image", 60, "GPT Image"),
]


# ---------------------------------------------------------------------------
# 模型能力
# ---------------------------------------------------------------------------
def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _normalized_model_id(model_id: str) -> str:
    # 仅用于能力匹配；保留原始模型 ID 作为上游请求参数。
    return model_id.strip().lower().replace("_", "-").replace(".", "-")


def is_gpt_image_model(model_id: str) -> bool:
    """只接受 gpt-image-* 生图模型。"""
    return _normalized_model_id(model_id).startswith("gpt-image")


def size_for_gpt(ratio: str, k: str) -> str | None:
    """按官方推荐像素表由画幅与档位派生宽 x 高。"""
    return GPT_TIER_SIZES.get(k, {}).get(ratio)


def derived_gpt_sizes() -> list[str]:
    sizes: list[str] = []
    for k in GPT_K_LEVELS:
        for ratio in GPT_RATIOS:
            size = size_for_gpt(ratio, k)
            if size and size not in sizes:
                sizes.append(size)
    return sizes


def _reference_transport(endpoint_types: Any) -> str:
    """上游声明支持 openai（chat）时才首选 chat，否则直接用 edits。"""
    if isinstance(endpoint_types, (list, tuple, set)):
        names = {str(item).strip().lower() for item in endpoint_types}
        return "chat" if "openai" in names else "edits"
    return "chat"


def gpt_capabilities(*, edit_transport: str = "chat") -> dict[str, Any]:
    if edit_transport not in GPT_REFERENCE_TRANSPORTS:
        edit_transport = "chat"
    return {
        "edits": True,
        "edit_transport": edit_transport,
        "ratios": list(GPT_RATIOS),
        "k_levels": list(GPT_K_LEVELS),
        "derived_sizes": derived_gpt_sizes(),
        "qualities": list(GPT_QUALITIES),
        "max_n": GPT_MAX_N,
        "max_references": GPT_MAX_REFERENCES,
        "async": True,
    }


def model_identity(model_id: str, endpoint_types: Any = None) -> dict[str, Any]:
    """返回 gpt-image-* 模型的标签、排序与能力；其它模型返回空字典。"""
    original = (model_id or "").strip()
    if not is_gpt_image_model(original):
        return {}

    normalized = _normalized_model_id(original)
    order = 900
    label = original
    for prefix, prefix_order, prefix_label in _GPT_MODEL_FAMILIES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            order, label = prefix_order, prefix_label
            break

    return {
        "id": original,
        "label": label,
        "family": "gpt-image",
        "group": "recommended",
        "order": order,
        "capabilities": gpt_capabilities(
            edit_transport=_reference_transport(endpoint_types),
        ),
    }


def list_image_models(
    model_ids: list[str],
    endpoint_types: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """只保留 gpt-image-* 模型，并按家族顺序排序。"""
    types_map = endpoint_types or {}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model_id in model_ids:
        if not isinstance(model_id, str):
            continue
        cleaned = model_id.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        identity = model_identity(cleaned, types_map.get(cleaned))
        if identity:
            items.append(identity)
    return sorted(items, key=lambda item: (int(item["order"]), item["id"].lower()))


def _default_model_for_items(items: list[dict[str, Any]]) -> str | None:
    ids = [item["id"] for item in items]
    for candidate in DEFAULT_MODEL_PREFERENCE:
        if candidate in ids:
            return candidate
    return ids[0] if ids else None


def resolve_base_url(override: str | None) -> str:
    """去尾斜杠；漏写协议头时按 https 补全。"""
    candidate = (override or "").strip().rstrip("/")
    if not candidate:
        return ""
    if candidate.startswith(("http://", "https://")):
        return candidate
    return f"https://{candidate}"


def _resolve_api_key(override: str | None) -> str:
    return (override or "").strip()


def resolve_size(payload: Any, capabilities: dict[str, Any]) -> tuple[str | None, str | None]:
    """返回 (size, error)。GPT 只支持 10 档比例 × 1K / 2K / 4K。"""
    ratios = capabilities["ratios"]
    k_levels = capabilities["k_levels"]

    ratio = _clean_text(payload.get("ratio")) or ratios[0]
    if ratio not in ratios:
        return None, (
            f"画幅 {ratio!r} 不受支持。该模型只支持：{'、'.join(ratios)}"
            "（不支持 auto 与自定义像素）。"
        )
    k = _clean_text(payload.get("k")) or k_levels[0]
    if k not in k_levels:
        return None, f"分辨率 {k!r} 不受支持。可选：{'、'.join(k_levels)}"

    derived = size_for_gpt(ratio, k)
    if not derived:
        return None, "无法从画幅与分辨率档派生尺寸。"

    requested = _clean_text(payload.get("size"))
    if requested and requested != derived:
        return None, (
            f"尺寸 {requested!r} 与该档推荐像素不一致。"
            f"{ratio} + {k} 对应像素为 {derived}。"
        )
    return derived, None


def resolve_quality(payload: Any, capabilities: dict[str, Any]) -> tuple[str | None, str | None]:
    """quality 由分辨率档决定：1K -> standard、2K -> hd、4K -> 4k。"""
    allowed = capabilities["qualities"]
    k = _clean_text(payload.get("k")) or capabilities["k_levels"][0]
    quality = _clean_text(payload.get("quality"))
    if not quality or quality == "auto":
        quality = GPT_QUALITY_BY_K.get(k, "")
    if quality not in allowed:
        return None, f"质量 {quality!r} 不受支持。可选：{'、'.join(allowed)}"
    return quality, None


def resolve_count(
    payload: Any,
    capabilities: dict[str, Any],
    has_reference: bool,
) -> tuple[int, str | None]:
    """带参考图时上游 chat 通道不支持 n，固定为 1。"""
    if has_reference:
        return 1, None
    try:
        n = int(payload.get("n") or 1)
    except (TypeError, ValueError):
        n = 1
    max_n = int(capabilities.get("max_n") or 1)
    if n < 1 or n > max_n:
        return 1, f"数量只能在 1 到 {max_n} 之间。"
    return n, None
# ---------------------------------------------------------------------------
# Flask 应用与历史记录
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.get("/api/download/<path:filename>")
def download_output(filename: str):
    name = Path(filename).name
    target = OUTPUT_DIR / name
    if not target.is_file():
        return jsonify({"ok": False, "error": "文件不存在或已被删除。"}), 404
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True, download_name=name)


def _load_history() -> list[dict[str, Any]]:
    with HISTORY_LOCK:
        if not HISTORY_FILE.exists():
            return []
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
        return data if isinstance(data, list) else []


def _save_history(records: list[dict[str, Any]]) -> None:
    with HISTORY_LOCK:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = HISTORY_FILE.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(HISTORY_FILE)


def _append_history(records: list[dict[str, Any]]) -> None:
    with HISTORY_LOCK:
        current = _load_history()
        _save_history((records + current)[:HISTORY_LIMIT])


@app.get("/api/history")
def history():
    items = _load_history()
    return jsonify({"ok": True, "count": len(items), "items": items})


@app.delete("/api/history")
def clear_history():
    _save_history([])
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 健康检查 / 模型发现
# ---------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return jsonify(
        {
            "ok": True,
            "config_source": "ui",
            "model_mode": "gpt-image",
            "default_model": DEFAULT_MODEL,
            "ratios": GPT_RATIOS,
            "k_levels": GPT_K_LEVELS,
            "qualities_by_k": GPT_QUALITY_BY_K,
            "max_n": GPT_MAX_N,
            "max_references": GPT_MAX_REFERENCES,
            "task": {
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "budget_seconds": TASK_BUDGET_SECONDS,
                "ttl_seconds": TASK_TTL_SECONDS,
            },
        }
    )


def _upstream_endpoint_types(raw_models: list[Any]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for item in raw_models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id.strip():
            mapping[model_id.strip()] = item.get("supported_endpoint_types")
    return mapping


@app.get("/api/models")
def models():
    """发现上游 GPT 生图模型，并返回前端所需的完整能力信息。"""
    key = _resolve_api_key(request.headers.get("X-Api-Key"))
    if not key:
        return jsonify({"ok": False, "error": MISSING_KEY_ERROR}), 400

    target_base = resolve_base_url(request.args.get("base_url"))
    if not target_base:
        return jsonify({"ok": False, "error": MISSING_BASE_URL_ERROR}), 400

    try:
        resp = HTTP.get(
            f"{target_base}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=30,
        )
    except requests.RequestException as exc:
        return jsonify({"ok": False, "error": f"无法连接中转站：{exc.__class__.__name__}"}), 502

    if resp.status_code != 200:
        return jsonify(
            {"ok": False, "status": resp.status_code, "error": _summarize_http_error(resp)}
        ), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({"ok": False, "error": "中转站返回了非 JSON 响应。"}), 502

    raw_models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw_models, list):
        return jsonify({"ok": False, "error": "中转站模型列表格式不正确。"}), 502

    ids = [
        item.get("id")
        for item in raw_models
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    items = list_image_models(ids, _upstream_endpoint_types(raw_models))
    default_model = _default_model_for_items(items)

    return jsonify(
        {
            "ok": True,
            "default_model": default_model,
            "available": default_model is not None,
            "count": len(ids),
            "image_count": len(items),
            "models": ids,
            "items": items,
            "base_url": target_base,
        }
    )


# ---------------------------------------------------------------------------
# 生成任务：上游提交 / 轮询 / 出图
# ---------------------------------------------------------------------------
def _new_task(model: str, prompt: str, n: int, size: str, quality: str) -> dict[str, Any]:
    now = time.time()
    task = {
        "id": uuid.uuid4().hex,
        "state": TASK_PENDING,
        "progress": 0,
        "created_at": now,
        "updated_at": now,
        "deadline": now + TASK_BUDGET_SECONDS,
        "elapsed": 0.0,
        "cancelled": threading.Event(),
        "finished": threading.Event(),
        "model": model,
        "prompt": prompt,
        "n": n,
        "size": size,
        "quality": quality,
        "reference": False,
        "images": [],
        "error": None,
    }
    with TASKS_LOCK:
        TASKS[task["id"]] = task
    return task


def _estimated_progress(task: dict[str, Any], now: float | None = None) -> int:
    """上游没给进度（同步型中转站）时按已用时间估算，最多到 95%。"""
    moment = now if now is not None else time.time()
    elapsed = max(0.0, moment - float(task["created_at"]))
    return max(1, min(95, int(elapsed / TASK_EXPECTED_SECONDS * 100)))


def _task_public(task: dict[str, Any]) -> dict[str, Any]:
    progress = int(task.get("progress") or 0)
    if task["state"] == TASK_RUNNING and progress < TASK_ESTIMATE_THRESHOLD:
        # 同步型中转站不会回报百分比，用已用时间给出可读进度。
        progress = _estimated_progress(task)
    return {
        "task_id": task["id"],
        "state": task["state"],
        "progress": progress,
        "elapsed": round(float(task.get("elapsed") or 0), 1),
        "model": task.get("model", ""),
        "size": task.get("size", ""),
        "quality": task.get("quality", ""),
        "count": task.get("n", 1),
        "reference": bool(task.get("reference")),
        "images": list(task.get("images") or []),
        "error": task.get("error"),
    }


def _cleanup_tasks(now: float | None = None) -> None:
    """清理超过 TTL 的终态任务；运行中的任务不清理。"""
    moment = now if now is not None else time.time()
    with TASKS_LOCK:
        expired = [
            task_id
            for task_id, task in TASKS.items()
            if task["state"] in TASK_TERMINAL
            and moment - float(task["updated_at"]) > TASK_TTL_SECONDS
        ]
        for task_id in expired:
            TASKS.pop(task_id, None)


def clear_tasks() -> None:
    """进程退出或测试时清空任务表。"""
    with TASKS_LOCK:
        TASKS.clear()


atexit.register(clear_tasks)


def _lookup_task(task_id: str) -> dict[str, Any] | None:
    _cleanup_tasks()
    with TASKS_LOCK:
        return TASKS.get(task_id)


def _normalize_status(value: Any) -> str:
    text = _clean_text(value).lower()
    return _STATUS_ALIASES.get(text, "")


def _task_status_error(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("code")
        if message:
            return str(message).strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return None


def _poll_upstream_task(
    task: dict[str, Any],
    status_url: str,
    fallback_url: str,
    poll_url: str | None,
    headers: dict[str, str],
) -> tuple[str, str | None, Any]:
    """轮询上游任务；返回 (state, error, payload)。"""
    candidates = [url for url in (status_url, fallback_url, poll_url) if url]
    consecutive_failures = 0
    max_failures = 5

    while True:
        if task["cancelled"].is_set():
            return TASK_CANCELLED, None, None
        if time.time() > task["deadline"]:
            return (
                TASK_FAILED,
                f"生成超时（已等待 {int(TASK_BUDGET_SECONDS / 60)} 分钟）。"
                "可改用 1K 档或更简短的提示词后重试。",
                None,
            )

        payload: Any = None
        fetch_error: str | None = None
        for url in candidates:
            try:
                resp = HTTP.get(url, headers=headers, timeout=60)
            except requests.RequestException as exc:
                fetch_error = f"轮询任务状态失败：{exc.__class__.__name__}。"
                continue
            if resp.status_code != 200:
                fetch_error = _summarize_http_error(resp)
                continue
            try:
                payload = resp.json()
            except ValueError:
                fetch_error = "任务状态返回了非 JSON 响应。"
                continue
            break

        if isinstance(payload, dict):
            consecutive_failures = 0
            data = payload.get("data")
            state = _normalize_status(payload.get("status"))
            if state == TASK_DONE or (isinstance(data, list) and data):
                return TASK_DONE, None, payload
            if state == TASK_FAILED:
                return TASK_FAILED, _task_status_error(payload) or "上游任务生成失败。", None

            task["state"] = TASK_RUNNING
            progress = payload.get("progress")
            try:
                task["progress"] = max(1, min(99, int(progress)))
            except (TypeError, ValueError):
                used = time.time() - float(task["created_at"])
                task["progress"] = max(1, min(95, int(used / TASK_BUDGET_SECONDS * 100)))
        else:
            consecutive_failures += 1
            fetch_error = fetch_error or "上游任务状态无法识别。"
            task["state"] = TASK_RUNNING
            if consecutive_failures >= max_failures:
                return TASK_FAILED, fetch_error, None

        if task["cancelled"].wait(POLL_INTERVAL_SECONDS):
            return TASK_CANCELLED, None, None


def _save_images(items: list[Any], task: dict[str, Any]) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        filename, actual_size = _save_image(item, task.get("size"), index)
        saved.append(
            {
                "url": f"/outputs/{filename}",
                "filename": filename,
                "size": actual_size,
                "quality": task.get("quality"),
                "model": task.get("model"),
            }
        )
    return saved


def _run_generation_task(task: dict[str, Any], request_spec: dict[str, Any]) -> None:
    """在线程中完成上游提交、轮询与保存；状态写入 task。"""
    try:
        if task["cancelled"].is_set():
            return
        task["state"] = TASK_RUNNING
        task["progress"] = 1
        task["reference"] = bool(request_spec.get("references"))
        payload = _submit_upstream(task, request_spec)
        if payload is None:
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        items = data if isinstance(data, list) else []
        if not items:
            task["state"] = TASK_FAILED
            task["error"] = "上游返回的 data 为空，没有生成图片。"
            return
        item_error = _extract_image_error(items)
        if item_error:
            task["state"] = TASK_FAILED
            task["error"] = item_error
            return
        if task["cancelled"].is_set():
            return
        task["images"] = _save_images(items, task)
        task["progress"] = 100
        task["state"] = TASK_DONE
    except (requests.RequestException, OSError, ValueError, binascii.Error) as exc:
        task["state"] = TASK_FAILED
        task["error"] = f"生成失败：{exc.__class__.__name__}。请稍后重试。"
    except Exception as exc:  # pragma: no cover - 兜底，避免线程静默退出
        task["state"] = TASK_FAILED
        task["error"] = f"生成失败：{exc.__class__.__name__}。"
    finally:
        task["elapsed"] = time.time() - float(task["created_at"])
        task["updated_at"] = time.time()
        task["finished"].set()


def _submit_upstream(task: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any] | None:
    """把请求提交给上游；同步完成时直接返回图片，异步时轮询到完成。"""
    headers = {
        "Authorization": f"Bearer {spec['api_key']}",
        "Content-Type": "application/json",
        "Prefer": "respond-async",
    }
    references = list(spec.get("references") or [])

    if references:
        resp = _submit_with_references(task, spec, headers, references)
        if resp is None:
            return None
        data = _payload_from_response(resp)
        if data is not None:
            return data
        return _await_task(task, data_source=resp, headers=headers, spec=spec)

    body: dict[str, Any] = {
        "model": spec["model"],
        "prompt": spec["prompt"],
        "n": spec["n"],
        "size": spec["size"],
        "quality": spec["quality"],
    }
    resp = _post_with_retry(
        f"{spec['base_url']}/images/generations",
        headers=headers,
        params={"async": 1},
        json=body,
        timeout=SUBMIT_TIMEOUT,
    )
    if resp.status_code not in SUBMIT_OK_STATUS:
        task["state"] = TASK_FAILED
        task["error"] = _summarize_http_error(resp)
        return None

    data = _payload_from_response(resp)
    if data is not None:
        return data
    return _await_task(task, data_source=resp, headers=headers, spec=spec)


def _payload_from_response(resp: requests.Response) -> dict[str, Any] | None:
    """同步返回且已带图片时返回 payload；异步任务则返回 None。"""
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if isinstance(items, list) and items:
        return data
    if data.get("task_id") or data.get("status_url") or data.get("poll_url"):
        return None
    return None


def _extract_task_refs(payload: Any) -> tuple[str, str, str | None]:
    if not isinstance(payload, dict):
        return "", "", None
    task_id = _clean_text(payload.get("task_id"))
    status_url = _clean_text(payload.get("status_url"))
    poll_url = _clean_text(payload.get("poll_url")) or None
    return task_id, status_url, poll_url


def _await_task(
    task: dict[str, Any],
    *,
    data_source: requests.Response,
    headers: dict[str, str],
    spec: dict[str, Any],
) -> dict[str, Any] | None:
    """按上游返回的 task_id / status_url 轮询到完成。"""
    try:
        submitted = data_source.json()
    except ValueError:
        task["state"] = TASK_FAILED
        task["error"] = "中转站返回了非 JSON 响应。"
        return None

    task_id, status_url, poll_url = _extract_task_refs(submitted)
    base = spec["base_url"]
    if not status_url:
        status_url = f"{base}/images/generations/{task_id}" if task_id else ""
    if not status_url and not poll_url:
        task["state"] = TASK_FAILED
        task["error"] = "上游既没有返回图片，也没有返回任务 ID。"
        return None
    fallback = f"{base}/images/generations/{task_id}" if task_id else status_url

    state, error, payload = _poll_upstream_task(
        task, status_url, fallback, poll_url, headers
    )
    if state == TASK_CANCELLED:
        return None
    if state == TASK_FAILED:
        task["state"] = TASK_FAILED
        task["error"] = error or "上游任务生成失败。"
        return None
    if not isinstance(payload, dict):
        task["state"] = TASK_FAILED
        task["error"] = "上游任务完成但没有返回内容。"
        return None
    return payload


def _files_to_data_urls(files: list[Any]) -> list[str]:
    result: list[str] = []
    for item in files:
        raw = item.read()
        if not raw:
            continue
        encoded = base64.b64encode(raw).decode("ascii")
        result.append(f"data:{_data_url_mime(raw, item.mimetype)};base64,{encoded}")
    return result


def _data_url_mime(raw: bytes, content_type: str | None) -> str:
    """data URL 的 MIME：优先用上传类型，缺失或过于笼统时按图片内容推断。"""
    ctype = (content_type or "").strip().lower()
    if ctype.startswith("image/"):
        return ctype
    return _IMAGE_MIME_BY_EXTENSION[_image_extension(raw)]



_IMAGE_URL_PATTERN = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+")


def _images_from_chat_text(text: str) -> list[dict[str, Any]]:
    """从 chat 响应文本里提取图片：data URL 或 markdown 图片链接。"""
    found: list[dict[str, Any]] = []
    for match in _IMAGE_URL_PATTERN.findall(text or ""):
        found.append({"b64_json": match.split(",", 1)[1]})
    if found:
        return found

    for url in re.findall(r"!\[[^\]]*\]\((https?://[^\s)]+)\)", text or ""):
        found.append({"url": url})
    if found:
        return found

    for url in re.findall(r"https?://[^\s)\]\"']+\.(?:png|jpe?g|webp)", text or ""):
        candidate = {"url": url}
        if candidate not in found:
            found.append(candidate)
    return found


def _chat_response_items(payload: Any) -> list[Any]:
    """解析 chat/completions 响应，取出图片项。"""
    if not isinstance(payload, dict):
        return []

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str):
                found = _images_from_chat_text(content)
                if found:
                    return found
            elif isinstance(content, list):
                items = [
                    {"url": part["image_url"]["url"]}
                    for part in content
                    if isinstance(part, dict)
                    and isinstance(part.get("image_url"), dict)
                    and isinstance(part["image_url"].get("url"), str)
                ]
                if items:
                    return items

    data = payload.get("data")
    if isinstance(data, list):
        return data
    return []


def _parse_sse_text(text: str) -> list[Any]:
    """解析 SSE 流，把每个 chunk 的文本拼接后再提取图片。"""
    chunks: list[str] = []
    images: list[Any] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("data:"):
            continue
        body = stripped[5:].strip()
        if not body or body == "[DONE]":
            continue
        try:
            payload = json.loads(body)
        except ValueError:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list) and data:
            images.extend(data)
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(
                        part.get("image_url"), dict
                    ):
                        url = part["image_url"].get("url")
                        if isinstance(url, str):
                            images.append({"url": url})
    if images:
        return images
    joined = "".join(chunks)
    if joined:
        return _images_from_chat_text(joined)
    return _images_from_chat_text(text or "")


def _submit_with_references(
    task: dict[str, Any],
    spec: dict[str, Any],
    headers: dict[str, str],
    references: list[str],
) -> requests.Response | None:
    """图生图：优先 chat/completions，失败回退 images/edits（multipart）。"""
    order = list(GPT_REFERENCE_TRANSPORTS)
    if spec.get("edit_transport") == "edits":
        order = ["edits", "chat"]

    first_error: str | None = None
    for transport in order:
        if task["cancelled"].is_set():
            return None
        if transport == "chat":
            resp = _post_with_retry(
                f"{spec['base_url']}/chat/completions",
                headers=headers,
                json=_chat_body(spec, references),
                timeout=SUBMIT_TIMEOUT,
                retry_status={429, 500, 502, 503, 504},
            )
            if resp.status_code in SUBMIT_OK_STATUS:
                items = _chat_response_items(_safe_json(resp))
                if not items:
                    items = _parse_sse_text(resp.text)
                if items:
                    return _make_sync_response(items, spec)
                first_error = "chat 通道没有返回图片。"
                continue
            first_error = _summarize_http_error(resp)
            continue

        resp, edits_error = _submit_edits(spec, headers)
        if resp is not None:
            return resp
        first_error = first_error or edits_error

    task["state"] = TASK_FAILED
    task["error"] = first_error or "图生图请求失败。"
    return None


def _chat_body(spec: dict[str, Any], references: list[str]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": spec["prompt"]}]
    content.extend(
        {"type": "image_url", "image_url": {"url": url}} for url in references
    )
    return {
        "model": spec["model"],
        "stream": False,
        "size": spec["size"],
        "quality": spec["quality"],
        "messages": [{"role": "user", "content": content}],
    }


def _safe_json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _make_sync_response(items: list[Any], spec: dict[str, Any]) -> requests.Response:
    """把上游同步结果包装成本地已完成的 payload。"""
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps(  # noqa: SLF001 - 内部构造，仅用于统一后续处理
        {"data": items, "model": spec.get("model")}, ensure_ascii=False
    ).encode("utf-8")
    resp.headers["Content-Type"] = "application/json"
    return resp


def _submit_edits(
    spec: dict[str, Any],
    headers: dict[str, str],
) -> tuple[requests.Response | None, str | None]:
    """multipart 回退通道：把 data URL 还原成文件重新上传。"""
    files_args = []
    for index, data_url in enumerate(spec.get("reference_urls") or []):
        raw = _decode_base64_image(data_url)
        extension = _image_extension(raw)
        mime = _IMAGE_MIME_BY_EXTENSION.get(extension, "image/png")
        files_args.append(
            ("image", (f"reference-{index + 1}{extension}", raw, mime))
        )
    data = {
        "model": spec["model"],
        "prompt": spec["prompt"],
        "n": "1",
        "size": spec["size"],
        "quality": spec["quality"],
    }
    resp = _post_with_retry(
        f"{spec['base_url']}/images/edits",
        headers=headers,
        files=files_args,
        data=data,
        timeout=SUBMIT_TIMEOUT,
    )
    if resp.status_code in SUBMIT_OK_STATUS:
        return resp, None
    return None, _summarize_http_error(resp)


# ---------------------------------------------------------------------------
# 本地接口：阻塞式生成与异步任务
# ---------------------------------------------------------------------------
def _reference_files() -> list[Any]:
    files = request.files.getlist("image") or request.files.getlist("images")
    return [item for item in files if item and item.filename]


def _payload_image_values(payload: Any) -> list[str]:
    raw = payload.get("image") if hasattr(payload, "get") else None
    if not raw:
        return []
    values = raw if isinstance(raw, list) else [raw]
    return [value.strip() for value in values if isinstance(value, str) and value.strip()]


def build_request_spec(
    payload: Any,
    *,
    files: list[Any] | None = None,
    enforce_key: bool = True,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """校验请求并整理出上游调用所需的全部参数。"""
    files = files or []
    json_images = _payload_image_values(payload)
    model = _clean_text(payload.get("model")) or DEFAULT_MODEL
    prompt = _clean_text(payload.get("prompt"))
    base_url = resolve_base_url(_clean_text(payload.get("base_url")))
    api_key = _resolve_api_key(_clean_text(payload.get("api_key")))

    if not is_gpt_image_model(model):
        return None, (
            {"ok": False, "error": f"模型 {model} 不是 GPT Image 生图模型。"},
            400,
        )
    if not prompt:
        return None, ({"ok": False, "error": "prompt 不能为空。"}, 400)
    if enforce_key and not api_key:
        return None, ({"ok": False, "error": MISSING_KEY_ERROR}, 400)
    if not base_url:
        return None, ({"ok": False, "error": MISSING_BASE_URL_ERROR}, 400)

    # 参考图通道由 /api/models 依据上游声明的 supported_endpoint_types 决定，
    # 客户端会把它随请求回传；未提供时默认首选 chat 通道。
    hint = _clean_text(payload.get("edit_transport"))
    identity = model_identity(model)
    capabilities = identity["capabilities"]
    if hint in GPT_REFERENCE_TRANSPORTS:
        capabilities["edit_transport"] = hint

    has_reference = bool(files or json_images)
    if has_reference:
        data_urls = _files_to_data_urls(files) if files else list(json_images)
        if len(data_urls) > GPT_MAX_REFERENCES:
            return None, (
                {
                    "ok": False,
                    "error": f"GPT 生图最多支持 {GPT_MAX_REFERENCES} 张参考图。",
                },
                400,
            )
        n = 1
    else:
        data_urls = []
        n, count_error = resolve_count(payload, capabilities, False)
        if count_error:
            return None, ({"ok": False, "error": count_error}, 400)

    size, size_error = resolve_size(payload, capabilities)
    if size_error:
        return None, ({"ok": False, "error": size_error}, 400)
    quality, quality_error = resolve_quality(payload, capabilities)
    if quality_error:
        return None, ({"ok": False, "error": quality_error}, 400)

    spec = {
        "model": model,
        "prompt": prompt,
        "base_url": base_url,
        "api_key": api_key,
        "n": n,
        "size": size,
        "quality": quality,
        "ratio": _clean_text(payload.get("ratio")) or GPT_RATIOS[0],
        "k": _clean_text(payload.get("k")) or capabilities["k_levels"][0],
        "references": data_urls,
        "reference_urls": data_urls,
        "edit_transport": capabilities["edit_transport"],
    }
    return spec, None


def _history_records(task: dict[str, Any], images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    return [
        {
            "id": uuid.uuid4().hex,
            "url": img["url"],
            "filename": img["filename"],
            "model": task["model"],
            "prompt": task["prompt"],
            "ratio": task.get("ratio", ""),
            "k": task.get("k", ""),
            "size": img["size"],
            "quality": task.get("quality"),
            "reference": bool(task.get("reference")),
            "created_at": created_at,
        }
        for img in images
    ]


def _generate_response(task: dict[str, Any]) -> tuple[Any, int]:
    if task["state"] != TASK_DONE:
        return (
            jsonify(
                {
                    "ok": False,
                    "status": task["state"],
                    "error": task.get("error") or "生成失败。",
                }
            ),
            502,
        )

    images = list(task.get("images") or [])
    _append_history(_history_records(task, images))
    return (
        jsonify(
            {
                "ok": True,
                "count": len(images),
                "images": images,
                "meta": {
                    "model": task["model"],
                    "size": images[0]["size"] if images else task.get("size"),
                    "ratio": task.get("ratio", ""),
                    "k": task.get("k", ""),
                    "quality": task.get("quality"),
                    "prompt": task["prompt"],
                    "reference": bool(task.get("reference")),
                },
            }
        ),
        200,
    )


def _start_task(spec: dict[str, Any]) -> dict[str, Any]:
    task = _new_task(
        model=spec["model"],
        prompt=spec["prompt"],
        n=spec["n"],
        size=spec["size"],
        quality=spec["quality"],
    )
    task["ratio"] = spec["ratio"]
    task["k"] = spec["k"]
    task["reference"] = bool(spec.get("references"))
    worker = threading.Thread(
        target=_run_generation_task,
        args=(task, spec),
        daemon=True,
        name=f"gpt-image-{task['id'][:8]}",
    )
    worker.start()
    return task


@app.post("/api/generate")
def generate():
    """阻塞式生成：内部提交 + 轮询到完成再返回，兼容既有脚本。"""
    files = _reference_files()
    if files:
        spec, error = build_request_spec(request.form, files=files)
    else:
        spec, error = build_request_spec(request.get_json(silent=True) or {})
    if error:
        payload_error, status = error
        return jsonify(payload_error), status

    task = _start_task(spec)
    while not task["finished"].wait(TASK_POLL_SLEEP_SECONDS):
        if task["cancelled"].is_set():
            break
    return _generate_response(task)


@app.post("/api/tasks")
def create_task():
    """异步生成：立刻返回 task_id，由客户端轮询进度。"""
    files = _reference_files()
    if files:
        spec, error = build_request_spec(request.form, files=files)
    else:
        spec, error = build_request_spec(request.get_json(silent=True) or {})
    if error:
        payload_error, status = error
        return jsonify(payload_error), status

    task = _start_task(spec)
    return jsonify({"ok": True, **_task_public(task)}), 202


@app.get("/api/tasks/<task_id>")
def task_status(task_id: str):
    task = _lookup_task(task_id)
    if task is None:
        return jsonify({"ok": False, "error": "任务不存在或已过期。"}), 404
    if task["state"] == TASK_DONE and task.get("images") and not task.get("recorded"):
        task["recorded"] = True
        _append_history(_history_records(task, task["images"]))
        task["reference"] = bool(task.get("reference"))
    task["elapsed"] = time.time() - float(task["created_at"])
    return jsonify({"ok": True, **_task_public(task)})


@app.delete("/api/tasks/<task_id>")
def cancel_task(task_id: str):
    task = _lookup_task(task_id)
    if task is None:
        return jsonify({"ok": False, "error": "任务不存在或已过期。"}), 404
    if task["state"] in TASK_TERMINAL:
        return jsonify({"ok": True, "state": task["state"]})
    task["cancelled"].set()
    task["state"] = TASK_CANCELLED
    task["error"] = "已取消等待；上游可能仍在生成，不会扣费的部分请联系中转站。"
    task["updated_at"] = time.time()
    task["finished"].set()
    return jsonify({"ok": True, "state": TASK_CANCELLED})


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _post_with_retry(
    url: str,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
    files: Any | None = None,
    data: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    attempts: int = 3,
    timeout: int = SUBMIT_TIMEOUT,
    retry_status: set[int] | None = None,
):
    retryable = retry_status if retry_status is not None else RETRYABLE_STATUS
    for attempt in range(attempts):
        try:
            resp = HTTP.post(
                url,
                headers=headers,
                json=json,
                files=files,
                data=data,
                params=params,
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise

        if resp.status_code in retryable and attempt < attempts - 1:
            time.sleep(2 * (attempt + 1))
            continue
        return resp
    raise RuntimeError("请求重试次数已用尽")  # pragma: no cover


def _extract_error_detail(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        for key in ("message", "code"):
            value = error.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return ""


def _summarize_http_error(resp: requests.Response) -> str:
    status = resp.status_code
    text = resp.text.strip().lower()
    if status == 401:
        return "认证失败（401）。请检查客户端填写的 API Key 是否正确，以及 Base URL 是否指向你的中转站。"
    if status == 403:
        if "just a moment" in text or "cloudflare" in text:
            return "中转站被 Cloudflare/WAF 拦截（403）。请确认 Base URL 正确，或中转站是否限制了脚本访问。"
        return "无权限（403）。请确认 key 已开通 GPT 生图权限。"
    if status == 404:
        return "接口不存在（404）。请确认 Base URL 填写完整（通常以 /v1 结尾），且该中转站已开通对应接口。"
    if status == 429:
        return "触发限流（429）。请稍后重试，或降低并发。"
    if status == 524:
        return "中转站源站响应超时（524）。请稍后重试，或改用 1K 档。"
    if status >= 500:
        return f"中转站服务异常（{status}）。请稍后重试。"
    if "invalid_api_key" in text:
        return "无效 API key。请检查客户端填写的 key 与 Base URL 是否属于同一个中转站。"
    detail = _extract_error_detail(resp)
    if status == 400:
        return f"中转站参数被拒绝（400）：{detail or resp.text[:240]}"
    return f"中转站返回错误（{status}）：{resp.text[:160]}"


def _extract_image_error(images: list[Any]) -> str | None:
    for item in images:
        if not isinstance(item, dict):
            continue
        error = item.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if message:
                return f"上游单张图片生成失败：{message}"
        if isinstance(error, str) and error.strip():
            return f"上游单张图片生成失败：{error.strip()}"
    return None


def _decode_base64_image(value: str) -> bytes:
    raw = value.strip()
    if raw.startswith("data:"):
        try:
            raw = raw.split(",", 1)[1]
        except IndexError as exc:
            raise ValueError("无效的 data URL") from exc
    if not raw:
        raise ValueError("图片 base64 为空")
    return base64.b64decode(raw, validate=True)


_IMAGE_MIME_BY_EXTENSION = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _image_extension(raw: bytes, content_type: str = "") -> str:
    ctype = (content_type or "").lower()
    if "image/png" in ctype or raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if "image/jpeg" in ctype or raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if "image/webp" in ctype or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
        return ".webp"
    if "image/gif" in ctype or raw.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return ".png"


def _save_image(
    item: dict[str, Any],
    size: str | None,
    index: int,
) -> tuple[str, str | None]:
    if not isinstance(item, dict):
        raise ValueError("响应图片项格式不正确")

    raw: bytes | None = None
    extension = ""
    b64 = item.get("b64_json") or item.get("b64")
    if isinstance(b64, str) and b64.strip():
        raw = _decode_base64_image(b64)
    else:
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("响应中缺少 b64_json 或 url")
        img_resp = HTTP.get(url, timeout=IMAGE_DOWNLOAD_TIMEOUT)
        img_resp.raise_for_status()
        raw = img_resp.content
        extension = _image_extension(raw, img_resp.headers.get("Content-Type", ""))

    if not raw:
        raise ValueError("图片内容为空")

    extension = extension or _image_extension(raw)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"gpt-image-{stamp}-{uuid.uuid4().hex[:8]}-{index + 1}{extension}"
    target = OUTPUT_DIR / filename
    target.write_bytes(raw)
    return filename, _actual_image_size(raw, size)


def _actual_image_size(raw: bytes, fallback: str | None) -> str | None:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            return f"{image.width}x{image.height}"
    except (OSError, ValueError):
        return fallback


def _resolve_port(default: int = 8787) -> int:
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg.startswith("--port="):
            value = arg.split("=", 1)[1]
        elif arg in ("-p", "--port") and index + 1 < len(argv):
            value = argv[index + 1]
        else:
            continue
        try:
            return int(value)
        except ValueError:
            break
    try:
        return int(os.environ.get("PORT") or default)
    except ValueError:
        return default


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=_resolve_port(), debug=False, threaded=True)
