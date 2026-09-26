"""GPT 生图工坊原生 Windows 客户端。

Flask 后端在同一进程的后台线程中运行，Tkinter 提供桌面界面。
客户端不打开浏览器，关闭窗口时会同时停止内部服务。
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import secrets
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import requests
from PIL import Image, ImageTk
from werkzeug.serving import make_server

import app as backend


CREDENTIALS_FILENAME = "client-credentials.dat"
DPAPI_DESCRIPTION = "GPT Image Studio credentials"
APP_TITLE = "GPT 生图工坊"
POLL_INTERVAL_MS = 1000
REQUEST_TIMEOUT = 360


class CredentialError(RuntimeError):
    pass


class TaskCancelled(RuntimeError):
    """用户主动取消等待。"""


def default_k_level(k_levels: list[str]) -> str:
    """默认 1K：GPT IMAGE 2 在 1K 档单价最低、耗时最短。"""
    if "1K" in k_levels:
        return "1K"
    return k_levels[0] if k_levels else ""


def reference_mime_type(path: str | Path) -> str:
    """按扩展名推断参考图 MIME；识别不出时交给后端按文件内容判断。"""
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _dpapi_blob(data: bytes) -> Any:
    if os.name != "nt":
        raise CredentialError("当前系统不支持 Windows DPAPI 加密。")
    import ctypes
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    return DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _read_dpapi_output(blob: Any) -> bytes:
    import ctypes

    if not blob.cbData or not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect_credentials(data: bytes) -> bytes:
    """使用当前 Windows 用户的 DPAPI 凭据加密数据。"""
    if os.name != "nt":
        raise CredentialError("当前系统不支持 Windows DPAPI 加密。")
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob_in, _buffer = _dpapi_blob(data)
    blob_out = DataBlob()

    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        DPAPI_DESCRIPTION,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise CredentialError("Windows DPAPI 加密失败。")
    try:
        return _read_dpapi_output(blob_out)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


def unprotect_credentials(data: bytes) -> bytes:
    """使用当前 Windows 用户的 DPAPI 凭据解密数据。"""
    if os.name != "nt":
        raise CredentialError("当前系统不支持 Windows DPAPI 解密。")
    import ctypes
    from ctypes import wintypes

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    blob_in, _buffer = _dpapi_blob(data)
    blob_out = DataBlob()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise CredentialError("Windows DPAPI 解密失败，凭据可能不属于当前 Windows 用户。")
    try:
        return _read_dpapi_output(blob_out)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(blob_out.pbData)


def load_encrypted_credentials(path: Path) -> dict[str, str] | None:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CredentialError(f"读取凭据文件失败：{exc}") from exc

    try:
        payload = json.loads(unprotect_credentials(raw).decode("utf-8"))
    except (CredentialError, UnicodeDecodeError, ValueError) as exc:
        raise CredentialError(f"凭据文件无法解密：{exc}") from exc
    if not isinstance(payload, dict):
        raise CredentialError("凭据文件格式不正确。")
    return {
        "base_url": str(payload.get("base_url") or ""),
        "api_key": str(payload.get("api_key") or ""),
        "model": str(payload.get("model") or ""),
    }


def save_encrypted_credentials(
    path: Path,
    base_url: str,
    api_key: str,
    model: str = "",
) -> None:
    payload = json.dumps(
        {"base_url": base_url, "api_key": api_key, "model": model},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encrypted = protect_credentials(payload)
    temp_path = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        temp_path.write_bytes(encrypted)
        os.replace(temp_path, path)
    except OSError as exc:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise CredentialError(f"保存凭据文件失败：{exc}") from exc


class BackendThread(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.server = make_server("127.0.0.1", 0, backend.app, threaded=True)
        self.port = self.server.server_port

    def run(self) -> None:
        self.server.serve_forever()

    def stop(self) -> None:
        self.server.shutdown()

class GptImageClient:
    def __init__(self, root: tk.Tk, server: BackendThread) -> None:
        self.root = root
        self.server = server
        self.base_api = f"http://127.0.0.1:{server.port}"
        self.model_items: list[dict[str, Any]] = []
        self.active_model: dict[str, Any] | None = None
        self.reference_paths: list[str] = []
        self.result_paths: list[str] = []
        self.result_index = 0
        self.preview_image: ImageTk.PhotoImage | None = None
        self.tasks: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.closing = False
        self.credentials_path = backend.APP_DIR / CREDENTIALS_FILENAME
        self.current_task_id: str | None = None
        self.task_started_at = 0.0
        self.waited_seconds = 0.0

        self.root.title(APP_TITLE)
        self.root.geometry("1080x780")
        self.root.minsize(900, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()
        self._load_settings()
        self.root.after(100, self._drain_tasks)

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text=APP_TITLE, font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.status_var = tk.StringVar(value="请填写 API Key 与 Base URL")
        ttk.Label(header, textvariable=self.status_var, foreground="#667085").grid(
            row=0, column=1, padx=(20, 0), sticky="w"
        )
        self.generate_button = ttk.Button(
            header, text="开始生成", command=self.generate, state="disabled"
        )
        self.generate_button.grid(row=0, column=2, padx=(14, 0), ipadx=12, ipady=3)
        self.cancel_button = ttk.Button(
            header, text="取消等待", command=self.cancel_generation, state="disabled"
        )
        self.cancel_button.grid(row=0, column=3, padx=(8, 0), ipadx=8, ipady=3)

        body = ttk.Frame(self.root, padding=(18, 0, 18, 18))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0, minsize=412)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # 左侧控制区放进 Canvas：窗口较矮或系统缩放较大时可以上下滚动，不再被裁切。
        self.controls_pane = ttk.Frame(body)
        self.controls_pane.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        self.controls_pane.columnconfigure(0, weight=1)
        self.controls_pane.rowconfigure(0, weight=1)

        canvas_bg = ttk.Style(self.root).lookup("TFrame", "background") or self.root.cget(
            "background"
        )
        self.controls_canvas = tk.Canvas(
            self.controls_pane,
            highlightthickness=0,
            borderwidth=0,
            background=canvas_bg,
        )
        self.controls_canvas.grid(row=0, column=0, sticky="nsew")
        self.controls_scrollbar = ttk.Scrollbar(
            self.controls_pane, orient="vertical", command=self.controls_canvas.yview
        )
        self.controls_scrollbar.grid(row=0, column=1, sticky="ns")
        self.controls_canvas.configure(yscrollcommand=self.controls_scrollbar.set)

        controls = ttk.Frame(self.controls_canvas)
        controls.columnconfigure(0, weight=1)
        self.controls_window = self.controls_canvas.create_window(
            (0, 0), window=controls, anchor="nw"
        )
        controls.bind("<Configure>", self._refresh_controls_scrollregion)
        self.controls_canvas.bind("<Configure>", self._resize_controls_window)
        # 滚轮只在左侧控制区生效；提示词输入框保留自身滚动。
        self.controls_canvas.bind_all("<MouseWheel>", self._on_controls_wheel)
        self.controls_canvas.bind_all("<Button-4>", self._on_controls_wheel)
        self.controls_canvas.bind_all("<Button-5>", self._on_controls_wheel)

        config = ttk.LabelFrame(controls, text="连接配置", padding=10)
        config.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        config.columnconfigure(0, weight=1)
        ttk.Label(config, text="提供商地址（Base URL）").grid(row=0, column=0, sticky="w")
        self.base_url_var = tk.StringVar()
        ttk.Entry(config, textvariable=self.base_url_var).grid(
            row=1, column=0, sticky="ew", pady=(3, 8)
        )
        ttk.Label(config, text="API Key").grid(row=2, column=0, sticky="w")
        key_row = ttk.Frame(config)
        key_row.grid(row=3, column=0, sticky="ew", pady=(3, 8))
        key_row.columnconfigure(0, weight=1)
        self.api_key_var = tk.StringVar()
        self.key_entry = ttk.Entry(key_row, textvariable=self.api_key_var, show="*")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            key_row, text="显示", variable=self.show_key_var, command=self._toggle_key
        ).grid(row=0, column=1, padx=(7, 0))
        config_buttons = ttk.Frame(config)
        config_buttons.grid(row=4, column=0, sticky="ew")
        ttk.Button(
            config_buttons, text="刷新模型", command=self.refresh_models
        ).pack(side="left")
        ttk.Label(
            config_buttons,
            text="只列出上游的 gpt-image-* 模型",
            foreground="#667085",
        ).pack(side="left", padx=(8, 0))
        self.remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            config,
            text="记住 Base URL、API Key 与模型",
            variable=self.remember_var,
            command=self._toggle_remember,
        ).grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.remember_hint_var = tk.StringVar(value="未启用：关闭程序后不会保存凭据。")
        ttk.Label(
            config,
            textvariable=self.remember_hint_var,
            foreground="#667085",
            wraplength=350,
        ).grid(row=6, column=0, sticky="w", pady=(3, 0))

        prompt_frame = ttk.LabelFrame(controls, text="提示词", padding=10)
        prompt_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        prompt_frame.columnconfigure(0, weight=1)
        self.prompt = tk.Text(prompt_frame, height=7, wrap="word", undo=True)
        self.prompt.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            prompt_frame, text="清空", command=lambda: self.prompt.delete("1.0", "end")
        ).grid(row=1, column=0, sticky="e", pady=(7, 0))

        model_frame = ttk.LabelFrame(controls, text="模型与参数", padding=10)
        model_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        model_frame.columnconfigure(1, weight=1)
        ttk.Label(model_frame, text="模型").grid(row=0, column=0, sticky="w", pady=3)
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(
            model_frame, textvariable=self.model_var, state="disabled"
        )
        self.model_combo.grid(row=0, column=1, sticky="ew", pady=3)
        self.model_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_model())

        ttk.Label(model_frame, text="画幅").grid(row=1, column=0, sticky="w", pady=3)
        self.ratio_var = tk.StringVar(value=backend.GPT_RATIOS[0])
        self.ratio_combo = ttk.Combobox(
            model_frame,
            textvariable=self.ratio_var,
            state="readonly",
            values=backend.GPT_RATIOS,
        )
        self.ratio_combo.grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(model_frame, text="分辨率").grid(row=2, column=0, sticky="w", pady=3)
        self.k_var = tk.StringVar(value=default_k_level(backend.GPT_K_LEVELS))
        self.k_combo = ttk.Combobox(
            model_frame,
            textvariable=self.k_var,
            state="readonly",
            values=backend.GPT_K_LEVELS,
        )
        self.k_combo.grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Label(model_frame, text="数量").grid(row=3, column=0, sticky="w", pady=3)
        self.count_var = tk.IntVar(value=1)
        self.count_spinbox = ttk.Spinbox(
            model_frame, from_=1, to=backend.GPT_MAX_N, textvariable=self.count_var, width=8
        )
        self.count_spinbox.grid(row=3, column=1, sticky="w", pady=3)

        self.param_hint_var = tk.StringVar(value="")
        ttk.Label(
            model_frame,
            textvariable=self.param_hint_var,
            foreground="#667085",
            wraplength=350,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))

        refs = ttk.LabelFrame(controls, text="参考图（可选，最多 4 张）", padding=10)
        refs.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        refs.columnconfigure(0, weight=1)
        self.reference_var = tk.StringVar(value="未选择参考图")
        ttk.Label(
            refs, textvariable=self.reference_var, foreground="#667085", wraplength=350
        ).grid(row=0, column=0, sticky="w")
        ref_buttons = ttk.Frame(refs)
        ref_buttons.grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Button(ref_buttons, text="选择图片", command=self.choose_references).pack(
            side="left"
        )
        ttk.Button(ref_buttons, text="清除", command=self.clear_references).pack(
            side="left", padx=(7, 0)
        )
        ttk.Label(
            refs,
            text="有参考图时按图生图调用（数量固定为 1 张）。",
            foreground="#667085",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

        result = ttk.LabelFrame(body, text="生成结果", padding=10)
        result.grid(row=0, column=1, sticky="nsew")
        result.columnconfigure(0, weight=1)
        result.rowconfigure(0, weight=1)
        self.result_label = ttk.Label(result, text="生成的图片将在这里显示", anchor="center")
        self.result_label.grid(row=0, column=0, sticky="nsew")
        self.result_name_var = tk.StringVar(value="")
        ttk.Label(result, textvariable=self.result_name_var, foreground="#667085").grid(
            row=1, column=0, pady=(8, 0)
        )
        self.progress = ttk.Progressbar(result, mode="determinate", maximum=100)
        self.progress.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.progress.grid_remove()
        self.progress_var = tk.StringVar(value="")
        ttk.Label(result, textvariable=self.progress_var, foreground="#667085").grid(
            row=3, column=0, pady=(4, 0)
        )
        result_buttons = ttk.Frame(result)
        result_buttons.grid(row=4, column=0, pady=(10, 0))
        self.previous_button = ttk.Button(
            result_buttons, text="上一张", command=lambda: self._show_result(-1), state="disabled"
        )
        self.previous_button.pack(side="left", padx=3)
        self.next_button = ttk.Button(
            result_buttons, text="下一张", command=lambda: self._show_result(1), state="disabled"
        )
        self.next_button.pack(side="left", padx=3)
        ttk.Button(result_buttons, text="打开输出目录", command=self.open_output_dir).pack(
            side="left", padx=3
        )

        self.history_var = tk.StringVar(value="")
        ttk.Label(
            result, textvariable=self.history_var, foreground="#667085", wraplength=560
        ).grid(row=5, column=0, pady=(16, 0))

    # ------------------------------------------------------------------
    # 左侧控制区滚动
    # ------------------------------------------------------------------
    def _resize_controls_window(self, event: Any) -> None:
        """让滚动内容宽度跟随 Canvas，避免出现横向滚动。"""
        self.controls_canvas.itemconfigure(self.controls_window, width=event.width)
        self._refresh_controls_scrollregion()

    def _refresh_controls_scrollregion(self, _event: Any = None) -> None:
        """内容高度变化时刷新滚动范围；不需要滚动时隐藏滚动条。"""
        bbox = self.controls_canvas.bbox("all")
        if not bbox:
            return
        self.controls_canvas.configure(scrollregion=bbox)
        viewport_height = self.controls_canvas.winfo_height()
        if viewport_height <= 1:
            return
        if (bbox[3] - bbox[1]) > viewport_height:
            if not self.controls_scrollbar.winfo_ismapped():
                self.controls_scrollbar.grid()
        elif self.controls_scrollbar.winfo_ismapped():
            self.controls_scrollbar.grid_remove()
            self.controls_canvas.yview_moveto(0)

    def _on_controls_wheel(self, event: Any) -> str | None:
        """鼠标位于左侧控制区时滚动它；提示词输入框交给输入框自己处理。"""
        pointer = self.root.winfo_containing(
            self.root.winfo_pointerx(), self.root.winfo_pointery()
        )
        if pointer is None or isinstance(pointer, tk.Text):
            return None
        if not self._is_inside_controls(pointer):
            return None
        step = getattr(event, "delta", 0)
        if getattr(event, "num", None) == 4:
            step = 1
        elif getattr(event, "num", None) == 5:
            step = -1
        if not step:
            return None
        self.controls_canvas.yview_scroll(-1 if step > 0 else 1, "units")
        return "break"

    def _is_inside_controls(self, widget: Any) -> bool:
        while widget is not None:
            if widget is self.controls_pane:
                return True
            widget = getattr(widget, "master", None)
        return False

    # ------------------------------------------------------------------
    # 凭据与模型
    # ------------------------------------------------------------------
    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.show_key_var.get() else "*")

    def _toggle_remember(self) -> None:
        if self.remember_var.get():
            self.remember_hint_var.set(
                "将使用 Windows DPAPI 加密保存到程序目录的 client-credentials.dat，仅当前 Windows 用户可解密。"
            )
            self._save_settings()
        else:
            self._delete_saved_credentials()
            self.remember_hint_var.set("未启用：关闭程序后不会保存凭据。")

    def _load_settings(self) -> None:
        try:
            data = load_encrypted_credentials(self.credentials_path)
        except CredentialError as exc:
            self.remember_hint_var.set(str(exc))
            return
        if not data:
            return
        self.base_url_var.set(data.get("base_url", ""))
        self.api_key_var.set(data.get("api_key", ""))
        self.remember_var.set(True)
        self.remember_hint_var.set(
            "已启用：凭据使用 Windows DPAPI 加密保存在 client-credentials.dat。"
        )

    def _save_settings(self) -> None:
        if not self.remember_var.get():
            return
        try:
            save_encrypted_credentials(
                self.credentials_path,
                self.base_url_var.get().strip(),
                self.api_key_var.get().strip(),
                self.model_var.get().strip(),
            )
        except CredentialError as exc:
            self.remember_hint_var.set(str(exc))

    def _delete_saved_credentials(self) -> None:
        try:
            self.credentials_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            self.remember_hint_var.set(f"删除凭据文件失败：{exc}")

    def _url(self, path: str) -> str:
        return f"{self.base_api}{path}"

    def _run_task(self, kind: str, func: Any) -> None:
        def worker() -> None:
            try:
                self.tasks.put((kind, (True, func())))
            except TaskCancelled as exc:
                self.tasks.put((kind, (False, exc)))
            except Exception as exc:  # noqa: BLE001 - 线程边界统一回传错误
                self.tasks.put((kind, (False, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_tasks(self) -> None:
        try:
            while True:
                kind, result = self.tasks.get_nowait()
                ok, value = result
                if kind == "models":
                    self._finish_models(ok, value)
                elif kind == "progress":
                    self._update_progress(value)
                elif kind == "generate":
                    self._finish_generate(ok, value)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(100, self._drain_tasks)

    def refresh_models(self) -> None:
        base_url = self.base_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if not base_url or not api_key:
            messagebox.showinfo("连接配置", "请先填写 Base URL 与 API Key。", parent=self.root)
            return
        self.status_var.set("正在获取模型列表…")
        self._run_task("models", lambda: self._request_models(base_url, api_key))

    def _request_models(self, base_url: str, api_key: str) -> dict[str, Any]:
        response = requests.get(
            self._url("/api/models"),
            params={"base_url": base_url},
            headers={"X-Api-Key": api_key},
            timeout=35,
        )
        data = response.json()
        if not response.ok or not data.get("ok"):
            raise RuntimeError(data.get("error") or f"获取模型失败（HTTP {response.status_code}）")
        return data

    def _finish_models(self, ok: bool, result: Any) -> None:
        if not ok:
            self.status_var.set(str(result))
            return
        self.model_items = result.get("items") or []
        ids = [item.get("id", "") for item in self.model_items]
        self.model_combo.configure(values=ids, state="readonly" if ids else "disabled")
        if ids:
            preferred = result.get("default_model")
            if preferred not in ids:
                preferred = ids[0]
            self.model_var.set(preferred)
            self._apply_model()
            self.status_var.set(f"已发现 {len(ids)} 个 GPT 生图模型")
            self._save_settings()
        else:
            self.model_var.set("")
            self.active_model = None
            self.generate_button.configure(state="disabled")
            self.status_var.set("未发现可用的 GPT 生图模型（上游需提供 gpt-image-*）")

    def _apply_model(self) -> None:
        model_id = self.model_var.get()
        self.active_model = next(
            (item for item in self.model_items if item.get("id") == model_id), None
        )
        caps = (self.active_model or {}).get("capabilities") or {}

        ratios = caps.get("ratios") or list(backend.GPT_RATIOS)
        k_levels = caps.get("k_levels") or list(backend.GPT_K_LEVELS)
        self.ratio_combo.configure(values=ratios)
        self.k_combo.configure(values=k_levels)
        if self.ratio_var.get() not in ratios:
            self.ratio_var.set(ratios[0])
        if self.k_var.get() not in k_levels:
            self.k_var.set(default_k_level(k_levels))

        max_n = int(caps.get("max_n") or backend.GPT_MAX_N)
        self.count_spinbox.configure(to=max_n)
        self.count_var.set(min(self._read_count(), max_n))
        self._sync_reference_state()
        self.generate_button.configure(state="normal" if model_id else "disabled")
        self.progress_var.set("")

    def _sync_reference_state(self) -> None:
        """带参考图时数量锁 1（上游 chat 通道不支持 n）。"""
        has_refs = bool(self.reference_paths)
        self.count_spinbox.configure(state="disabled" if has_refs else "normal")
        if has_refs:
            self.count_var.set(1)

        caps = (self.active_model or {}).get("capabilities") or {}
        transport = caps.get("edit_transport")
        if transport == "edits":
            self.param_hint_var.set(
                "该模型上游只声明了 image-generation：参考图走 /images/edits 上传。"
            )
        else:
            self.param_hint_var.set(
                "参数按官方推荐：10 档画幅 × 1K/2K/4K；质量随档位映射 standard / hd / 4k。"
            )

    def choose_references(self) -> None:
        paths = filedialog.askopenfilenames(
            title="选择参考图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.gif"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if not paths:
            return
        max_refs = int(
            (self.active_model or {}).get("capabilities", {}).get("max_references")
            or backend.GPT_MAX_REFERENCES
        )
        selected = list(paths)[:max_refs]
        if len(paths) > max_refs:
            messagebox.showinfo(
                "参考图", f"GPT 生图最多支持 {max_refs} 张参考图，已保留前 {max_refs} 张。", parent=self.root
            )
        self.reference_paths = selected
        self.reference_var.set(self._reference_text())
        self._sync_reference_state()

    def _reference_text(self) -> str:
        if not self.reference_paths:
            return "未选择参考图"
        return "、".join(Path(path).name for path in self.reference_paths)

    def clear_references(self) -> None:
        self.reference_paths = []
        self.reference_var.set("未选择参考图")
        self._sync_reference_state()

    def _read_count(self) -> int:
        """Spinbox 允许手输，非法内容按 1 处理，避免 TclError 打断回调。"""
        try:
            return max(1, int(self.count_var.get()))
        except (tk.TclError, ValueError):
            return 1

    # ------------------------------------------------------------------
    # 生成与任务轮询
    # ------------------------------------------------------------------
    def generate(self) -> None:
        prompt = self.prompt.get("1.0", "end").strip()
        if not self.active_model:
            messagebox.showinfo("提示词", "请先刷新模型并选择一个 GPT 生图模型。", parent=self.root)
            return
        if not prompt:
            messagebox.showinfo("提示词", "请先填写提示词。", parent=self.root)
            return
        base_url = self.base_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if not base_url or not api_key:
            messagebox.showinfo("连接配置", "请先填写 Base URL 与 API Key。", parent=self.root)
            return

        caps = (self.active_model.get("capabilities") or {})
        payload: dict[str, Any] = {
            "model": self.model_var.get(),
            "prompt": prompt,
            "base_url": base_url,
            "api_key": api_key,
            "ratio": self.ratio_var.get(),
            "k": self.k_var.get(),
            "n": self._read_count(),
            "edit_transport": caps.get("edit_transport", "chat"),
        }
        references = list(self.reference_paths)

        self._set_busy(True)
        self.status_var.set("正在提交生成请求…")
        self.progress.configure(value=0)
        self.progress_var.set("生成中 0%（已用 0 秒）")
        self._run_task(
            "generate", lambda: self._request_generate(payload, references)
        )

    def _request_generate(
        self, payload: dict[str, Any], references: list[str]
    ) -> dict[str, Any]:
        files = []
        handles = []
        try:
            if references:
                for path in references:
                    handle = open(path, "rb")
                    handles.append(handle)
                    files.append(("image", (Path(path).name, handle, reference_mime_type(path))))
                response = requests.post(
                    self._url("/api/tasks"),
                    data=payload,
                    files=files,
                    timeout=REQUEST_TIMEOUT,
                )
            else:
                response = requests.post(
                    self._url("/api/tasks"), json=payload, timeout=REQUEST_TIMEOUT
                )
            data = response.json()
            if not response.ok or not data.get("ok"):
                raise RuntimeError(
                    data.get("error") or f"提交失败（HTTP {response.status_code}）"
                )
        finally:
            for handle in handles:
                handle.close()

        task_id = data.get("task_id")
        if not task_id:
            raise RuntimeError("服务端没有返回任务 ID。")
        return self._poll_task(task_id)

    def _poll_task(self, task_id: str) -> dict[str, Any]:
        """在后台线程按 1 秒节奏轮询本地任务状态。"""
        self.current_task_id = task_id
        self.task_started_at = time.time()
        while True:
            if self.closing:
                raise RuntimeError("程序正在关闭。")
            try:
                response = requests.get(
                    self._url(f"/api/tasks/{task_id}"), timeout=30
                )
                data = response.json()
            except (requests.RequestException, ValueError):
                time.sleep(POLL_INTERVAL_MS / 1000)
                continue

            if not response.ok or not data.get("ok"):
                raise RuntimeError(
                    data.get("error") or f"查询任务状态失败（HTTP {response.status_code}）"
                )

            state = data.get("state")
            self.tasks.put(
                (
                    "progress",
                    (
                        True,
                        {
                            "state": state,
                            "progress": data.get("progress") or 0,
                            "elapsed": data.get("elapsed") or 0,
                        },
                    ),
                )
            )
            if state == backend.TASK_DONE:
                return data
            if state == backend.TASK_CANCELLED:
                raise TaskCancelled("已取消等待。")
            if state == backend.TASK_FAILED:
                raise RuntimeError(data.get("error") or "生成失败。")
            time.sleep(POLL_INTERVAL_MS / 1000)

    def cancel_generation(self) -> None:
        task_id = self.current_task_id
        if not task_id:
            return
        self.status_var.set("正在取消等待…")
        self.cancel_button.configure(state="disabled")

        def worker() -> None:
            try:
                requests.delete(self._url(f"/api/tasks/{task_id}"), timeout=15)
            except requests.RequestException:
                pass
            self.current_task_id = None
            self.tasks.put(("generate", (False, TaskCancelled("已取消等待。"))))

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, info: Any) -> None:
        if not isinstance(info, dict) or self.current_task_id is None:
            return
        progress = int(info.get("progress") or 0)
        elapsed = int(round(float(info.get("elapsed") or 0)))
        self.progress.configure(value=progress)
        state = info.get("state")
        if state == backend.TASK_RUNNING:
            self.status_var.set("正在生成…")
        self.progress_var.set(f"生成中 {progress}%（已用 {elapsed} 秒）")

    def _set_busy(self, busy: bool) -> None:
        self.generate_button.configure(
            state="disabled"
            if busy
            else ("normal" if self.active_model else "disabled")
        )
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.model_combo.configure(
            state="disabled" if busy or not self.model_items else "readonly"
        )
        self.ratio_combo.configure(state="disabled" if busy else "readonly")
        self.k_combo.configure(state="disabled" if busy else "readonly")
        if not busy:
            self._sync_reference_state()
        if busy:
            self.progress.grid()
        else:
            self.progress.grid_remove()

    def _finish_generate(self, ok: bool, result: Any) -> None:
        self.current_task_id = None
        self._set_busy(False)
        if not ok:
            if isinstance(result, TaskCancelled):
                self.status_var.set("已取消等待")
                self.progress_var.set("已取消等待。")
                return
            self.status_var.set("生成失败")
            messagebox.showerror("生成失败", result, parent=self.root)
            return

        self.result_paths = [
            self._output_path(item["filename"]) for item in result.get("images", [])
        ]
        self.result_index = 0
        self._show_result(0, absolute=True)
        self.status_var.set(f"生成完成，共 {len(self.result_paths)} 张")
        self.progress_var.set("")
        meta = result.get("meta") or {}
        self.history_var.set(
            f"模型：{meta.get('model', '')}    尺寸：{meta.get('size', '')}    "
            f"档位：{meta.get('k', '')}"
        )

    def _output_path(self, filename: str) -> Path:
        return backend.OUTPUT_DIR / Path(filename).name

    def _show_result(self, delta: int, absolute: bool = False) -> None:
        if not self.result_paths:
            return
        if absolute:
            index = delta
        else:
            index = (self.result_index + delta) % len(self.result_paths)
        self.result_index = index
        path = self.result_paths[index]
        try:
            image = Image.open(path)
            image.thumbnail((600, 520), Image.Resampling.LANCZOS)
            self.preview_image = ImageTk.PhotoImage(image.copy())
            self.result_label.configure(image=self.preview_image, text="")
            self.result_name_var.set(f"{index + 1}/{len(self.result_paths)}  {path.name}")
            state = "normal" if len(self.result_paths) > 1 else "disabled"
            self.previous_button.configure(state=state)
            self.next_button.configure(state=state)
        except (OSError, ValueError) as exc:
            self.result_label.configure(image="", text=f"无法预览图片：{exc}")

    def open_output_dir(self) -> None:
        path = str(backend.OUTPUT_DIR)
        os.startfile(path)  # type: ignore[attr-defined]

    def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        task_id = self.current_task_id
        if task_id:
            try:
                requests.delete(self._url(f"/api/tasks/{task_id}"), timeout=5)
            except requests.RequestException:
                pass
        self._save_settings()
        self.server.stop()
        self.root.destroy()


def main() -> None:
    server = BackendThread()
    server.start()
    root = tk.Tk()
    try:
        GptImageClient(root, server)
        root.mainloop()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
