"""Image2 生图工坊 - Flask 后端。

职责：
  * 从中转站 /models 发现生图模型，并按模型能力组织请求参数。
  * 中转站地址与 API key 只从前端输入，后端不读 .env、不读环境变量。
  * 调用 Images API 的 generations / edits 端点，支持 GPT Image、DALL-E 与 Seedream。
  * 保存返回图片到 outputs/generated/，历史记录写入 outputs/history.json。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory


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

STATIC_DIR = RESOURCE_DIR / "static"

OUTPUT_DIR = APP_DIR / "outputs" / "generated"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MODEL = "gpt-image-2"

MISSING_BASE_URL_ERROR = "未填写提供商地址。请在页面下方输入中转站 Base URL（通常以 /v1 结尾）。"
MISSING_KEY_ERROR = "未填写 API key。请在页面下方输入中转站 API key。"

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
HTTP = requests.Session()
HTTP.headers.update(_BROWSER_HEADERS)

RATIOS = [
    "auto",
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
    "custom",
]
SEEDREAM_RATIOS = ["auto", "1:1", "4:3", "3:4", "16:9", "9:16", "custom"]
CUSTOM_RATIO = "custom"
CUSTOM_RATIO_MIN = 1 / 16
CUSTOM_RATIO_MAX = 16
K_LEVELS = ["1K", "1.5K", "2K"]
GPT_K_LEVELS = ["1K", "2K", "4K"]
GPT_QUALITY_ALIASES = {
    "1K": "standard",
    "2K": "hd",
    "4K": "4k",
}
# 示例中转站使用的标准像素表。auto/custom 另行处理。
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
K_LONG_EDGE = {
    "1K": 1024,
    "1.5K": 1536,
    "2K": 2048,
    "3K": 3072,
    "4K": 4096,
}

OUTPUTS_ROOT = APP_DIR / "outputs"
HISTORY_FILE = OUTPUTS_ROOT / "history.json"
HISTORY_LIMIT = 100
HISTORY_LOCK = threading.RLock()

GENERATION_TIMEOUT = 300
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

GROUP_RECOMMENDED = "recommended"
GROUP_OTHER = "other"
MODEL_GROUPS = [
    {"key": GROUP_RECOMMENDED, "label": "推荐"},
    {"key": GROUP_OTHER, "label": "其他"},
]

_FAMILY_ORDER = {
    "gpt-image-2.5": 10,
    "gpt-image-2": 20,
    "gpt-image-1.5": 30,
    "gpt-image-1": 40,
    "gpt-image-1-mini": 50,
    "chatgpt-image-latest": 60,
    "doubao-seedream": 70,
    "dall-e": 80,
}


def size_for(ratio: str, k: str) -> str | None:
    """由画幅与档位派生宽x高；auto 画幅直接交给模型决定。"""
    if ratio == "auto":
        return "auto"
    if ratio == CUSTOM_RATIO:
        return None
    try:
        rw_str, rh_str = ratio.split(":")
        rw, rh = int(rw_str), int(rh_str)
    except (ValueError, AttributeError):
        return None
    long_edge = K_LONG_EDGE.get(k)
    if long_edge is None or rw <= 0 or rh <= 0:
        return None
    if rw >= rh:
        w, h = long_edge, round(long_edge * rh / rw)
    else:
        h, w = long_edge, round(long_edge * rw / rh)
    return f"{w}x{h}"


def parse_custom_ratio(value: Any) -> tuple[int, int] | None:
    """解析自定义宽高比，限制在 1:16 到 16:1。"""
    text = _clean_text(value).replace("：", ":")
    if not text or text.count(":") != 1:
        return None
    left, right = (part.strip() for part in text.split(":", 1))
    if not left.isdigit() or not right.isdigit():
        return None
    rw, rh = int(left), int(right)
    if rw <= 0 or rh <= 0:
        return None
    ratio = rw / rh
    if ratio < CUSTOM_RATIO_MIN or ratio > CUSTOM_RATIO_MAX:
        return None
    return rw, rh


def size_for_custom_ratio(value: Any, k: str) -> str | None:
    parsed = parse_custom_ratio(value)
    long_edge = K_LONG_EDGE.get(k)
    if not parsed or long_edge is None:
        return None
    rw, rh = parsed
    if rw >= rh:
        w, h = long_edge, max(1, round(long_edge * rh / rw))
    else:
        h, w = long_edge, max(1, round(long_edge * rw / rh))
    return f"{w}x{h}"


def size_for_gpt(ratio: str, k: str) -> str | None:
    """GPT Image 使用上游示例中的具体像素档位。"""
    if ratio == "auto":
        return "auto"
    return GPT_TIER_SIZES.get(k, {}).get(ratio)


def size_for_gpt_custom(value: Any, k: str) -> str | None:
    parsed = parse_custom_ratio(value)
    if not parsed or k not in GPT_TIER_SIZES:
        return None
    long_edge = {
        "1K": 1280,
        "2K": 2560,
        "4K": 3840,
    }[k]
    rw, rh = parsed
    if rw >= rh:
        w, h = long_edge, max(1, round(long_edge * rh / rw))
    else:
        h, w = long_edge, max(1, round(long_edge * rw / rh))
    return f"{w}x{h}"


def _ratio_k_sizes(ratios: list[str], k_levels: list[str]) -> list[str]:
    result = []
    for ratio in ratios:
        if ratio == CUSTOM_RATIO:
            continue
        for k in k_levels:
            size = size_for(ratio, k)
            if size and size not in result:
                result.append(size)
    return result


def _ratio_capabilities(
    k_levels: list[str],
    qualities: list[str],
    *,
    ratios: list[str] | None = None,
    size_profile: str = "long_edge",
    edits: bool | str = True,
    edit_transport: str = "multipart",
    max_n: int = 4,
    max_references: int = 16,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_ratios = list(ratios or RATIOS)
    return {
        "edits": edits,
        "edit_transport": edit_transport,
        "size_mode": "ratio_and_k",
        "size_profile": size_profile,
        "ratios": active_ratios,
        "k_levels": list(k_levels),
        "derived_sizes": _ratio_k_sizes(active_ratios, list(k_levels)),
        "preset_sizes": [],
        "qualities": list(qualities),
        "max_n": max_n,
        "max_references": max_references,
        "extra": extra or {},
    }


def _preset_capabilities(
    preset_sizes: list[dict[str, str]],
    qualities: list[str],
    *,
    edits: bool | str = True,
    max_n: int = 4,
    max_references: int = 16,
) -> dict[str, Any]:
    return {
        "edits": edits,
        "edit_transport": "multipart" if edits is not False else "none",
        "size_mode": "preset",
        "size_profile": "preset",
        "ratios": [],
        "k_levels": [],
        "preset_sizes": preset_sizes,
        "qualities": list(qualities),
        "max_n": max_n,
        "max_references": max_references,
        "extra": {},
    }


def _unknown_image_capabilities() -> dict[str, Any]:
    return {
        "edits": "unknown",
        "edit_transport": "multipart",
        "size_mode": "none",
        "size_profile": "none",
        "ratios": [],
        "k_levels": [],
        "preset_sizes": [],
        "qualities": [],
        "max_n": 4,
        "max_references": 10,
        "extra": {},
    }


def _normalized_model_id(model_id: str) -> str:
    # 仅用于能力匹配；保留原始模型 ID 作为上游请求参数。
    return model_id.strip().lower().replace("_", "-").replace(".", "-")


def model_identity(model_id: str) -> dict[str, Any]:
    """返回模型家族、标签、分组、排序值和能力。"""
    original = (model_id or "").strip()
    normalized = _normalized_model_id(original)

    family = ""
    label = original
    group = GROUP_OTHER
    order = 1000
    capabilities: dict[str, Any]

    if normalized.startswith("gpt-image-2.5-"):
        family = "gpt-image-2.5"
        label = "GPT Image 2.5"
        capabilities = _ratio_capabilities(
            GPT_K_LEVELS,
            ["standard", "hd", "4k", "high", "ultra"],
            size_profile="gpt",
        )
    elif normalized.startswith("gpt-image-2"):
        family = "gpt-image-2"
        label = "GPT Image 2"
        capabilities = _ratio_capabilities(
            GPT_K_LEVELS,
            ["standard", "hd", "4k", "high", "ultra"],
            size_profile="gpt",
        )
    elif normalized.startswith("gpt-image-1.5"):
        family = "gpt-image-1.5"
        label = "GPT Image 1.5"
        capabilities = _ratio_capabilities(
            K_LEVELS,
            ["auto", "low", "medium", "high"],
        )
    elif normalized.startswith("gpt-image-1-mini"):
        family = "gpt-image-1-mini"
        label = "GPT Image 1 mini"
        capabilities = _ratio_capabilities(
            K_LEVELS,
            ["auto", "low", "medium", "high"],
        )
    elif normalized.startswith("gpt-image-1"):
        family = "gpt-image-1"
        label = "GPT Image 1"
        capabilities = _ratio_capabilities(
            K_LEVELS,
            ["auto", "low", "medium", "high"],
        )
    elif normalized.startswith("chatgpt-image"):
        family = "chatgpt-image-latest"
        label = "ChatGPT Image"
        capabilities = _ratio_capabilities(
            K_LEVELS,
            ["auto", "low", "medium", "high"],
        )
    elif "seedream" in normalized:
        family = "doubao-seedream"
        label = "Doubao-Seedream"
        seedream_ratios = list(SEEDREAM_RATIOS)
        seedream_extra = {
            "watermark": False,
            "sequential_image_generation": True,
        }
        if "5-0-pro" in normalized:
            label = "Doubao-Seedream 5.0 pro"
            order = 70
            capabilities = _ratio_capabilities(
                ["1K", "2K"],
                [],
                ratios=seedream_ratios,
                edit_transport="generation_image_field",
                max_references=10,
                extra=seedream_extra,
            )
        elif "5-0-lite" in normalized or (
            "5-0" in normalized and "5-0-pro" not in normalized
        ):
            label = "Doubao-Seedream 5.0 lite"
            order = 71
            capabilities = _ratio_capabilities(
                ["2K", "3K", "4K"],
                [],
                ratios=seedream_ratios,
                edit_transport="generation_image_field",
                max_references=14,
                extra=seedream_extra,
            )
        elif "4-5" in normalized or "4.5" in normalized:
            label = "Doubao-Seedream 4.5"
            order = 72
            capabilities = _ratio_capabilities(
                ["2K", "4K"],
                [],
                ratios=seedream_ratios,
                edit_transport="generation_image_field",
                max_references=14,
                extra=seedream_extra,
            )
        elif "4-0" in normalized or "4.0" in normalized:
            label = "Doubao-Seedream 4.0"
            order = 73
            capabilities = _ratio_capabilities(
                ["1K", "2K", "4K"],
                [],
                ratios=seedream_ratios,
                edit_transport="generation_image_field",
                max_references=14,
                extra=seedream_extra,
            )
        elif "3-0-t2i" in normalized or "3.0-t2i" in normalized:
            label = "Doubao-Seedream 3.0 t2i"
            order = 74
            capabilities = _ratio_capabilities(
                ["1K"],
                [],
                ratios=seedream_ratios,
                edits=False,
                edit_transport="none",
                max_references=0,
                extra=seedream_extra,
            )
        else:
            order = 75
            capabilities = _ratio_capabilities(
                ["1K", "2K", "4K"],
                [],
                ratios=seedream_ratios,
                edit_transport="generation_image_field",
                max_references=14,
                extra=seedream_extra,
            )
    elif normalized in {"dall-e-3", "dalle-3"} or normalized.startswith("dall-e-3-"):
        family = "dall-e"
        label = "DALL-E 3"
        order = 80
        capabilities = _preset_capabilities(
            [
                {"value": "1024x1024", "label": "1024x1024 · 方形"},
                {"value": "1792x1024", "label": "1792x1024 · 横向"},
                {"value": "1024x1792", "label": "1024x1792 · 纵向"},
            ],
            ["standard", "hd"],
            edits=False,
            max_n=1,
            max_references=0,
        )
    elif normalized in {"dall-e-2", "dalle-2"} or normalized.startswith("dall-e-2-"):
        family = "dall-e"
        label = "DALL-E 2"
        order = 81
        capabilities = _preset_capabilities(
            [
                {"value": "256x256", "label": "256x256 · 小图"},
                {"value": "512x512", "label": "512x512 · 中图"},
                {"value": "1024x1024", "label": "1024x1024 · 方形"},
            ],
            ["standard"],
            max_n=4,
            max_references=1,
        )
    elif "image" in normalized:
        family = "unknown-image"
        label = original
        capabilities = _unknown_image_capabilities()
    else:
        return {}

    if family != "unknown-image":
        group = GROUP_RECOMMENDED
        order = _FAMILY_ORDER.get(family, order) * 100 + (order % 100)

    return {
        "id": original,
        "label": label,
        "family": family,
        "group": group,
        "order": order,
        "capabilities": capabilities,
    }


def _model_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    return (
        0 if item["group"] == GROUP_RECOMMENDED else 1,
        int(item["order"]),
        item["id"].lower(),
    )


def list_image_models(model_ids: list[str]) -> list[dict[str, Any]]:
    items = []
    seen = set()
    for model_id in model_ids:
        if not isinstance(model_id, str):
            continue
        cleaned = model_id.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        identity = model_identity(cleaned)
        if identity:
            items.append(identity)
    return sorted(items, key=_model_sort_key)


def _default_model_for_items(items: list[dict[str, Any]]) -> str | None:
    ids = [item["id"] for item in items]
    if DEFAULT_MODEL in ids:
        return DEFAULT_MODEL
    for item in items:
        if item["family"] == "gpt-image-2.5":
            return item["id"]
    for item in items:
        if item["family"] == "gpt-image-2":
            return item["id"]
    for item in items:
        if item["group"] == GROUP_RECOMMENDED:
            return item["id"]
    return items[0]["id"] if items else None


def _resolve_api_key(override: str | None) -> str:
    return (override or "").strip()


def resolve_base_url(override: str | None) -> str:
    """去尾斜杠；漏写协议头时按 https 补全。"""
    candidate = (override or "").strip().rstrip("/")
    if not candidate:
        return ""
    if candidate.startswith(("http://", "https://")):
        return candidate
    return f"https://{candidate}"


def _clean_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="")


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/outputs/<path:filename>")
def serve_output(filename: str):
    return send_from_directory(OUTPUT_DIR, filename)


@app.get("/api/download/<path:filename>")
def download_output(filename: str):
    name = Path(filename).name
    target = OUTPUT_DIR / name
    if not target.is_file():
        return jsonify({"ok": False, "error": "文件不存在或已被删除。"}), 404
    return send_from_directory(OUTPUT_DIR, name, as_attachment=True, download_name=name)


# ---------------------------------------------------------------------------
# 历史记录
# ---------------------------------------------------------------------------
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
        _save_history(records + current)
        # _save_history writes the full list; trim while still under the lock.
        if len(records) + len(current) > HISTORY_LIMIT:
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
            "model_mode": "dynamic",
            "default_model": DEFAULT_MODEL,
            "ratios": RATIOS,
            "k_levels": K_LEVELS,
            "qualities": ["auto", "low", "medium", "high"],
            "seedream": {
                "k_levels": ["1K", "2K", "3K", "4K"],
                "watermark": False,
                "sequential_image_generation": True,
            },
        }
    )


@app.get("/api/models")
def models():
    """发现上游生图模型，并返回前端所需的完整能力信息。"""
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
    items = list_image_models(ids)
    default_model = _default_model_for_items(items)

    return jsonify(
        {
            "ok": True,
            "default_model": default_model,
            "available": DEFAULT_MODEL in ids,
            "count": len(ids),
            "image_count": len(items),
            "models": ids,
            "items": items,
            "groups": MODEL_GROUPS,
            "base_url": target_base,
        }
    )


# ---------------------------------------------------------------------------
# 生图
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


def _files_to_data_urls(files: list[Any]) -> list[str]:
    result = []
    for item in files:
        raw = item.read()
        if not raw:
            continue
        content_type = item.mimetype or "image/png"
        encoded = base64.b64encode(raw).decode("ascii")
        result.append(f"data:{content_type};base64,{encoded}")
    return result


def _allowed_sizes(capabilities: dict[str, Any]) -> set[str]:
    return set(capabilities.get("derived_sizes") or [])


def _resolve_size(payload: Any, capabilities: dict[str, Any]) -> tuple[str | None, str | None]:
    """返回 (size, error)。size=None 表示不向上游发送尺寸。"""
    requested = _clean_text(payload.get("size"))
    size_mode = capabilities["size_mode"]

    if size_mode == "none":
        return None, None

    if size_mode == "preset":
        allowed = {item["value"] for item in capabilities["preset_sizes"]}
        if requested:
            if requested not in allowed:
                return None, f"尺寸 {requested!r} 不受当前模型支持。"
            return requested, None
        if capabilities["preset_sizes"]:
            return capabilities["preset_sizes"][0]["value"], None
        return None, "当前模型没有可用的尺寸选项。"

    ratio = _clean_text(payload.get("ratio")) or (
        capabilities["ratios"][0] if capabilities["ratios"] else "auto"
    )
    if ratio not in capabilities["ratios"]:
        return None, (
            f"画幅 {ratio!r} 不受支持。可选："
            f"{'、'.join(capabilities['ratios'])}"
        )
    if ratio == "auto":
        if capabilities.get("edit_transport") == "generation_image_field":
            return None, None
        return "auto", None

    k = _clean_text(payload.get("k")) or (
        capabilities["k_levels"][0] if capabilities["k_levels"] else ""
    )
    if k not in capabilities["k_levels"]:
        return None, (
            f"分辨率 {k!r} 不受支持。可选："
            f"{'、'.join(capabilities['k_levels'])}"
        )

    if ratio == CUSTOM_RATIO:
        if capabilities.get("size_profile") == "gpt":
            derived = size_for_gpt_custom(payload.get("custom_ratio"), k)
        else:
            derived = size_for_custom_ratio(payload.get("custom_ratio"), k)
        if not derived:
            return None, (
                "自定义比例需填写“宽:高”，例如 21:9；宽高都必须是正整数，"
                "且比例范围在 1:16 到 16:1 之间。"
            )
        if requested and requested != derived:
            return None, "自定义比例与 size 参数不能同时指定不同的像素尺寸。"
        return derived, None

    if capabilities.get("size_profile") == "gpt":
        derived = size_for_gpt(ratio, k)
    else:
        derived = size_for(ratio, k)
    if not derived:
        return None, "无法从画幅与分辨率派生尺寸。"
    if requested and requested != derived:
        allowed = _allowed_sizes(capabilities)
        if requested not in allowed:
            return None, f"尺寸 {requested!r} 不受当前模型支持。"
        return requested, None
    return derived, None


def _resolve_quality(payload: Any, capabilities: dict[str, Any]) -> tuple[str | None, str | None]:
    qualities = capabilities["qualities"]
    if not qualities:
        return None, None
    quality = _clean_text(payload.get("quality"))
    if capabilities.get("size_profile") == "gpt" and quality in (None, "", "auto"):
        k = _clean_text(payload.get("k"))
        quality = GPT_QUALITY_ALIASES.get(k)
    quality = quality or qualities[0]
    if quality not in qualities:
        return None, f"质量 {quality!r} 不受支持。可选：{'、'.join(qualities)}"
    return quality, None


def _resolve_count(payload: Any, capabilities: dict[str, Any]) -> tuple[int, str | None]:
    try:
        n = int(payload.get("n") or 1)
    except (TypeError, ValueError):
        n = 1
    max_n = int(capabilities.get("max_n") or 1)
    if n < 1 or n > max_n:
        return 1, f"数量只能在 1 到 {max_n} 之间。"
    return n, None


def _seedream_body(
    model: str,
    prompt: str,
    size: str | None,
    n: int,
    images: list[str],
) -> dict[str, Any]:
    # 对齐中转站常见的 OpenAI Images API 形态：只发通用字段。
    # 部分中转站会拒绝 watermark / sequential_image_generation 等扩展字段。
    body: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "n": n,
    }
    if size:
        body["size"] = size
    if len(images) == 1:
        body["image"] = images[0]
    elif images:
        body["image"] = images
    if n > 1:
        body["sequential_image_generation"] = "auto"
        body["sequential_image_generation_options"] = {"max_images": n}
    return body


@app.post("/api/generate")
def generate():
    files = _reference_files()
    payload = request.form if files else (request.get_json(silent=True) or {})
    if not hasattr(payload, "get"):
        return jsonify({"ok": False, "error": "请求参数格式不正确。"}), 400

    model = _clean_text(payload.get("model")) or DEFAULT_MODEL
    prompt = _clean_text(payload.get("prompt"))
    override_base = resolve_base_url(_clean_text(payload.get("base_url")))
    key = _resolve_api_key(_clean_text(payload.get("api_key")))

    identity = model_identity(model)
    if not identity:
        identity = model_identity(f"unknown-image-{model}")
    capabilities = identity["capabilities"]
    edit_transport = capabilities["edit_transport"]

    json_images = _payload_image_values(payload)
    has_reference = bool(files or json_images)
    if has_reference and capabilities["edits"] is False:
        return jsonify({"ok": False, "error": f"模型 {model} 不支持参考图。"}), 400
    if has_reference and edit_transport == "multipart" and not files:
        return jsonify(
            {"ok": False, "error": "该模型的参考图需要使用 multipart 文件上传。"}
        ), 400

    reference_count = len(files) or len(json_images)
    max_references = int(capabilities.get("max_references") or 0)
    if has_reference and max_references and reference_count > max_references:
        return jsonify(
            {"ok": False, "error": f"模型 {model} 最多支持 {max_references} 张参考图。"}
        ), 400

    if not prompt:
        return jsonify({"ok": False, "error": "prompt 不能为空。"}), 400
    if not key:
        return jsonify({"ok": False, "error": MISSING_KEY_ERROR}), 400
    if not override_base:
        return jsonify({"ok": False, "error": MISSING_BASE_URL_ERROR}), 400

    size, size_error = _resolve_size(payload, capabilities)
    if size_error:
        return jsonify({"ok": False, "error": size_error}), 400

    quality, quality_error = _resolve_quality(payload, capabilities)
    if quality_error:
        return jsonify({"ok": False, "error": quality_error}), 400

    n, count_error = _resolve_count(payload, capabilities)
    if count_error:
        return jsonify({"ok": False, "error": count_error}), 400

    try:
        if edit_transport == "generation_image_field":
            images = _files_to_data_urls(files) if files else list(json_images)
            body = _seedream_body(model, prompt, size, n, images)
            resp = _post_with_retry(
                f"{override_base}/images/generations",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=GENERATION_TIMEOUT,
            )
        elif has_reference:
            files_args = []
            for idx, item in enumerate(files):
                filename = item.filename or f"reference-{idx + 1}.png"
                ctype = item.mimetype or "image/png"
                files_args.append(("image", (filename, item.stream, ctype)))
            data = {
                "model": model,
                "prompt": prompt,
                "n": str(n),
            }
            if size:
                data["size"] = size
            if quality:
                data["quality"] = quality
            resp = _post_with_retry(
                f"{override_base}/images/edits",
                headers={"Authorization": f"Bearer {key}"},
                files=files_args,
                data=data,
                timeout=GENERATION_TIMEOUT,
            )
        else:
            body = {
                "model": model,
                "prompt": prompt,
                "n": n,
            }
            if size:
                body["size"] = size
            if quality:
                body["quality"] = quality
            resp = _post_with_retry(
                f"{override_base}/images/generations",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=GENERATION_TIMEOUT,
            )
    except requests.RequestException as exc:
        return jsonify(
            {"ok": False, "error": f"请求中转站失败：{exc.__class__.__name__}. 请检查代理或网络。"}
        ), 502

    if resp.status_code != 200:
        return jsonify(
            {"ok": False, "status": resp.status_code, "error": _summarize_http_error(resp)}
        ), 502

    try:
        data = resp.json()
    except ValueError:
        return jsonify({"ok": False, "error": "中转站返回了非 JSON 响应。"}), 502

    if edit_transport == "chat_completions" and has_reference:
        images = _extract_chat_images(data)
    else:
        images = data.get("data") if isinstance(data, dict) else None
        if images is None:
            images = []
    if not isinstance(images, list):
        return jsonify({"ok": False, "error": "中转站图片列表格式不正确。"}), 502
    if not images:
        return jsonify({"ok": False, "error": "中转站返回的 data 为空，没有生成图片。"}), 502

    item_error = _extract_image_error(images)
    if item_error:
        return jsonify({"ok": False, "error": item_error}), 502

    saved = []
    try:
        for index, item in enumerate(images):
            filename = _save_image(item, size, index)
            saved.append(
                {
                    "url": f"/outputs/{filename}",
                    "filename": filename,
                    "size": size,
                    "quality": quality,
                    "model": model,
                }
            )
    except (requests.RequestException, OSError, ValueError, binascii.Error) as exc:
        return jsonify(
            {"ok": False, "error": f"保存生成图片失败：{exc.__class__.__name__}。请稍后重试。"}
        ), 502

    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    history_records = [
        {
            "id": uuid.uuid4().hex,
            "url": img["url"],
            "filename": img["filename"],
            "model": model,
            "prompt": prompt,
            "ratio": _clean_text(payload.get("ratio")),
            "k": _clean_text(payload.get("k")),
            "size": size,
            "quality": quality,
            "reference": bool(has_reference),
            "created_at": created_at,
        }
        for img in saved
    ]
    _append_history(history_records)

    return jsonify(
        {
            "ok": True,
            "count": len(saved),
            "images": saved,
            "meta": {
                "model": model,
                "size": size,
                "ratio": _clean_text(payload.get("ratio")),
                "k": _clean_text(payload.get("k")),
                "quality": quality,
                "prompt": prompt,
                "reference": bool(has_reference),
            },
        }
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _post_with_retry(
    url: str,
    headers: dict[str, str],
    json: dict[str, Any] | None = None,
    files: Any | None = None,
    data: dict[str, str] | None = None,
    attempts: int = 3,
    timeout: int = GENERATION_TIMEOUT,
):
    for attempt in range(attempts):
        try:
            resp = HTTP.post(
                url,
                headers=headers,
                json=json,
                files=files,
                data=data,
                timeout=timeout,
            )
        except requests.RequestException:
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise

        if resp.status_code in _RETRYABLE_STATUS and attempt < attempts - 1:
            time.sleep(2 * (attempt + 1))
            continue
        return resp
    raise RuntimeError("请求重试次数已用尽")  # pragma: no cover


def _summarize_http_error(resp: requests.Response) -> str:
    status = resp.status_code
    text = resp.text.strip().lower()
    if status == 401:
        return "认证失败（401）。请检查界面里填写的 API Key 是否正确，以及 Base URL 是否指向你的中转站。"
    if status == 403:
        if "just a moment" in text or "cloudflare" in text:
            return "中转站被 Cloudflare/WAF 拦截（403）。请确认 Base URL 正确，或中转站是否限制了脚本访问。"
        return "无权限（403）。请确认 key 已开通图片生成权限。"
    if status == 404:
        return "接口不存在（404）。请确认 Base URL 填写完整，且该中转站已开通对应图片接口。"
    if status == 429:
        return "触发限流（429）。请稍后重试，或降低并发/数量。"
    if status == 524:
        return "中转站源站响应超时（524）。请稍后重试，或降低质量/尺寸。"
    if status >= 500:
        return f"中转站服务异常（{status}）。请稍后重试。"
    if "invalid_api_key" in text:
        return "无效 API key。请检查界面里填写的 key 与 Base URL 是否属于同一个中转站。"
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


def _save_image(item: dict[str, Any], size: str | None, index: int) -> str:
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
        img_resp = HTTP.get(url, timeout=120)
        img_resp.raise_for_status()
        raw = img_resp.content
        extension = _image_extension(raw, img_resp.headers.get("Content-Type", ""))

    if not raw:
        raise ValueError("图片内容为空")

    extension = extension or _image_extension(raw)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"image2-{stamp}-{uuid.uuid4().hex[:8]}-{index + 1}{extension}"
    target = OUTPUT_DIR / filename
    target.write_bytes(raw)
    return filename


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
