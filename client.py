"""Image2 生图工坊原生 Windows 客户端。

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
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import requests
from PIL import Image, ImageTk
from werkzeug.serving import make_server

import app as backend


CREDENTIALS_FILENAME = "client-credentials.dat"
DPAPI_DESCRIPTION = "Image2 Studio credentials"


class CredentialError(RuntimeError):
    pass


def preferred_k_level(k_levels: list[str]) -> str:
    if "2K" in k_levels:
        return "2K"
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
    }


def save_encrypted_credentials(path: Path, base_url: str, api_key: str) -> None:
    payload = json.dumps(
        {"base_url": base_url, "api_key": api_key},
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


class Image2Client:
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

        self.root.title("Image2 生图工坊")
        self.root.geometry("1080x780")
        self.root.minsize(900, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build_ui()
        self._load_settings()
        self.root.after(100, self._drain_tasks)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 14, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Image2 生图工坊", font=("Segoe UI", 18, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.status_var = tk.StringVar(value="请填写 API Key 与 Base URL")
        ttk.Label(header, textvariable=self.status_var, foreground="#667085").grid(
            row=0, column=1, padx=(20, 0), sticky="w"
        )
        self.generate_button = ttk.Button(header, text="开始生成", command=self.generate, state="disabled")
        self.generate_button.grid(row=0, column=2, padx=(14, 0), ipadx=12, ipady=3)

        body = ttk.Frame(self.root, padding=(18, 0, 18, 18))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=0, minsize=395)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        controls = ttk.Frame(body)
        controls.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        controls.columnconfigure(0, weight=1)

        config = ttk.LabelFrame(controls, text="连接配置", padding=10)
        config.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        config.columnconfigure(0, weight=1)
        ttk.Label(config, text="提供商地址（Base URL）").grid(row=0, column=0, sticky="w")
        self.base_url_var = tk.StringVar()
        ttk.Entry(config, textvariable=self.base_url_var).grid(row=1, column=0, sticky="ew", pady=(3, 8))
        ttk.Label(config, text="API Key").grid(row=2, column=0, sticky="w")
        key_row = ttk.Frame(config)
        key_row.grid(row=3, column=0, sticky="ew", pady=(3, 8))
        key_row.columnconfigure(0, weight=1)
        self.api_key_var = tk.StringVar()
        self.key_entry = ttk.Entry(key_row, textvariable=self.api_key_var, show="*")
        self.key_entry.grid(row=0, column=0, sticky="ew")
        self.show_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(key_row, text="显示", variable=self.show_key_var, command=self._toggle_key).grid(
            row=0, column=1, padx=(7, 0)
        )
        config_buttons = ttk.Frame(config)
        config_buttons.grid(row=4, column=0, sticky="ew")
        ttk.Button(config_buttons, text="刷新模型", command=self.refresh_models).pack(side="left")
        self.remember_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            config,
            text="记住 Base URL 和 API Key",
            variable=self.remember_var,
            command=self._toggle_remember,
        ).grid(row=5, column=0, sticky="w", pady=(8, 0))
        self.remember_hint_var = tk.StringVar(value="未启用：关闭程序后不会保存凭据。")
        ttk.Label(config, textvariable=self.remember_hint_var, foreground="#667085", wraplength=350).grid(
            row=6, column=0, sticky="w", pady=(3, 0)
        )

        prompt_frame = ttk.LabelFrame(controls, text="提示词", padding=10)
        prompt_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        prompt_frame.columnconfigure(0, weight=1)
        prompt_frame.rowconfigure(0, weight=1)
        self.prompt = tk.Text(prompt_frame, height=7, wrap="word", undo=True)
        self.prompt.grid(row=0, column=0, sticky="ew")
        ttk.Button(prompt_frame, text="清空", command=lambda: self.prompt.delete("1.0", "end")).grid(
            row=1, column=0, sticky="e", pady=(7, 0)
        )

        model_frame = ttk.LabelFrame(controls, text="模型与参数", padding=10)
        model_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        model_frame.columnconfigure(1, weight=1)
        ttk.Label(model_frame, text="模型").grid(row=0, column=0, sticky="w", pady=3)
        self.model_var = tk.StringVar()
        self.model_combo = ttk.Combobox(model_frame, textvariable=self.model_var, state="disabled")
        self.model_combo.grid(row=0, column=1, sticky="ew", pady=3)
        self.model_combo.bind("<<ComboboxSelected>>", lambda _event: self._apply_model())

        ttk.Label(model_frame, text="画幅").grid(row=1, column=0, sticky="w", pady=3)
        self.ratio_var = tk.StringVar(value="auto")
        self.ratio_combo = ttk.Combobox(model_frame, textvariable=self.ratio_var, state="disabled", values=["auto"])
        self.ratio_combo.grid(row=1, column=1, sticky="ew", pady=3)
        self.ratio_combo.bind("<<ComboboxSelected>>", lambda _event: self._sync_ratio())

        self.custom_label = ttk.Label(model_frame, text="自定义比例")
        self.custom_ratio_var = tk.StringVar()
        self.custom_ratio_entry = ttk.Entry(model_frame, textvariable=self.custom_ratio_var)

        ttk.Label(model_frame, text="分辨率").grid(row=3, column=0, sticky="w", pady=3)
        self.k_var = tk.StringVar(value="1K")
        self.k_combo = ttk.Combobox(model_frame, textvariable=self.k_var, state="disabled", values=["1K"])
        self.k_combo.grid(row=3, column=1, sticky="ew", pady=3)

        self.size_label = ttk.Label(model_frame, text="尺寸")
        self.size_var = tk.StringVar()
        self.size_combo = ttk.Combobox(model_frame, textvariable=self.size_var, state="disabled")
        self.quality_label = ttk.Label(model_frame, text="质量")
        self.quality_var = tk.StringVar()
        self.quality_combo = ttk.Combobox(model_frame, textvariable=self.quality_var, state="disabled")
        self._grid_optional_controls()

        ttk.Label(model_frame, text="数量").grid(row=6, column=0, sticky="w", pady=3)
        self.count_var = tk.IntVar(value=1)
        self.count_spinbox = ttk.Spinbox(
            model_frame,
            from_=1,
            to=4,
            textvariable=self.count_var,
            width=8,
        )
        self.count_spinbox.grid(row=6, column=1, sticky="w", pady=3)

        refs = ttk.LabelFrame(controls, text="参考图（可选）", padding=10)
        refs.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        refs.columnconfigure(0, weight=1)
        self.reference_var = tk.StringVar(value="未选择参考图")
        ttk.Label(refs, textvariable=self.reference_var, foreground="#667085").grid(row=0, column=0, sticky="w")
        ref_buttons = ttk.Frame(refs)
        ref_buttons.grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Button(ref_buttons, text="选择图片", command=self.choose_references).pack(side="left")
        ttk.Button(ref_buttons, text="清除", command=self.clear_references).pack(side="left", padx=(7, 0))

        result = ttk.LabelFrame(body, text="生成结果", padding=10)
        result.grid(row=0, column=1, sticky="nsew")
        result.columnconfigure(0, weight=1)
        result.rowconfigure(0, weight=1)
        self.result_label = ttk.Label(result, text="生成的图片将在这里显示", anchor="center")
        self.result_label.grid(row=0, column=0, sticky="nsew")
        self.result_name_var = tk.StringVar(value="")
        ttk.Label(result, textvariable=self.result_name_var, foreground="#667085").grid(row=1, column=0, pady=(8, 0))
        result_buttons = ttk.Frame(result)
        result_buttons.grid(row=2, column=0, pady=(10, 0))
        self.previous_button = ttk.Button(result_buttons, text="上一张", command=lambda: self._show_result(-1), state="disabled")
        self.previous_button.pack(side="left", padx=3)
        self.next_button = ttk.Button(result_buttons, text="下一张", command=lambda: self._show_result(1), state="disabled")
        self.next_button.pack(side="left", padx=3)
        ttk.Button(result_buttons, text="打开输出目录", command=self.open_output_dir).pack(side="left", padx=3)

        self.history_var = tk.StringVar(value="")
        ttk.Label(result, textvariable=self.history_var, foreground="#667085", wraplength=560).grid(
            row=3, column=0, pady=(16, 0)
        )

    def _grid_optional_controls(self) -> None:
        self.size_label.grid(row=2, column=0, sticky="w", pady=3)
        self.size_combo.grid(row=2, column=1, sticky="ew", pady=3)
        self.quality_label.grid(row=4, column=0, sticky="w", pady=3)
        self.quality_combo.grid(row=4, column=1, sticky="ew", pady=3)

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
        base_url = self.base_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if not base_url and not api_key:
            return
        try:
            save_encrypted_credentials(self.credentials_path, base_url, api_key)
        except CredentialError as exc:
            self.remember_hint_var.set(str(exc))

    def _delete_saved_credentials(self) -> None:
        try:
            self.credentials_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.remember_hint_var.set(f"删除凭据文件失败：{exc}")

    def _url(self, path: str) -> str:
        return f"{self.base_api}{path}"

    def _run_task(self, kind: str, func: Any) -> None:
        def worker() -> None:
            try:
                self.tasks.put((kind, (True, func())))
            except Exception as exc:  # network errors are shown in the UI
                self.tasks.put((kind, (False, str(exc))))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_tasks(self) -> None:
        try:
            while True:
                kind, result = self.tasks.get_nowait()
                if kind == "models":
                    self._finish_models(*result)
                elif kind == "generate":
                    self._finish_generate(*result)
        except queue.Empty:
            pass
        if not self.closing:
            self.root.after(100, self._drain_tasks)

    def refresh_models(self) -> None:
        base_url = self.base_url_var.get().strip()
        api_key = self.api_key_var.get().strip()
        if not base_url or not api_key:
            self.status_var.set("请先填写 API Key 与 Base URL")
            return
        self._save_settings()
        self.status_var.set("正在获取模型列表…")
        self.generate_button.configure(state="disabled")
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
            self.status_var.set(result)
            return
        self.model_items = result.get("items") or []
        ids = [item.get("id", "") for item in self.model_items]
        self.model_combo.configure(values=ids, state="readonly" if ids else "disabled")
        if ids:
            preferred = result.get("default_model") if result.get("default_model") in ids else ids[0]
            self.model_var.set(preferred)
            self._apply_model()
            self.status_var.set(f"已发现 {len(ids)} 个生图模型")
        else:
            self.model_var.set("")
            self.status_var.set("未发现可用的生图模型")

    def _apply_model(self) -> None:
        model_id = self.model_var.get()
        self.active_model = next((item for item in self.model_items if item.get("id") == model_id), None)
        caps = (self.active_model or {}).get("capabilities") or {}
        mode = caps.get("size_mode")
        is_preset = mode == "preset"
        self.ratio_combo.configure(values=caps.get("ratios") or ["auto"], state="disabled" if is_preset or mode == "none" else "readonly")
        self.k_combo.configure(values=caps.get("k_levels") or ["1K"], state="disabled" if is_preset or mode == "none" else "readonly")
        self.size_combo.configure(values=[item.get("value", "") for item in caps.get("preset_sizes") or []], state="readonly" if is_preset else "disabled")
        self.quality_combo.configure(values=caps.get("qualities") or [], state="readonly" if caps.get("qualities") else "disabled")
        if caps.get("ratios"):
            self.ratio_var.set(caps["ratios"][0])
        preferred_k = preferred_k_level(caps.get("k_levels") or [])
        if preferred_k:
            self.k_var.set(preferred_k)
        if caps.get("preset_sizes"):
            self.size_var.set(caps["preset_sizes"][0].get("value", ""))
        if caps.get("qualities"):
            self.quality_var.set(caps["qualities"][0])
        max_n = int(caps.get("max_n") or 1)
        self.count_var.set(min(self._read_count(), max_n))
        self.count_spinbox.configure(to=max_n)
        self._sync_ratio()
        max_refs = int(caps.get("max_references") or 0)
        if caps.get("edits") is False:
            self.clear_references()
            self.reference_var.set("当前模型不支持参考图")
        elif max_refs == 1 and len(self.reference_paths) > 1:
            self.reference_paths = self.reference_paths[:1]
            self.reference_var.set(Path(self.reference_paths[0]).name)
        else:
            self.reference_var.set(self._reference_text())
        self.generate_button.configure(state="normal" if model_id else "disabled")

    def _sync_ratio(self) -> None:
        enabled = self.ratio_var.get() == "custom" and self.ratio_combo.cget("state") != "disabled"
        if enabled:
            self.custom_label.grid(row=2, column=0, sticky="w", pady=3)
            self.custom_ratio_entry.grid(row=2, column=1, sticky="ew", pady=3)
            self.size_label.grid_configure(row=3)
            self.size_combo.grid_configure(row=3)
            self.quality_label.grid_configure(row=5)
            self.quality_combo.grid_configure(row=5)
        else:
            self.custom_label.grid_remove()
            self.custom_ratio_entry.grid_remove()
            self.size_label.grid_configure(row=2)
            self.size_combo.grid_configure(row=2)
            self.quality_label.grid_configure(row=4)
            self.quality_combo.grid_configure(row=4)
        caps = (self.active_model or {}).get("capabilities") or {}
        k_enabled = caps.get("size_mode") == "ratio_and_k" and bool(caps.get("k_levels"))
        self.k_combo.configure(state="disabled" if not k_enabled or self.ratio_var.get() == "auto" else "readonly")

    def choose_references(self) -> None:
        if not self.active_model or (self.active_model.get("capabilities") or {}).get("edits") is False:
            messagebox.showinfo("参考图", "当前模型不支持参考图。", parent=self.root)
            return
        max_refs = int((self.active_model.get("capabilities") or {}).get("max_references") or 0)
        paths = filedialog.askopenfilenames(
            title="选择参考图",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.gif"), ("所有文件", "*.*")],
            parent=self.root,
        )
        if max_refs == 1:
            paths = paths[:1]
        elif max_refs:
            paths = paths[:max_refs]
        self.reference_paths = list(paths)
        self.reference_var.set(self._reference_text())

    def _reference_text(self) -> str:
        if not self.reference_paths:
            return "未选择参考图"
        return "、".join(Path(path).name for path in self.reference_paths)

    def clear_references(self) -> None:
        self.reference_paths = []
        self.reference_var.set("未选择参考图")

    def _read_count(self) -> int:
        """Spinbox 允许手输，非法内容按 1 处理，避免 TclError 打断回调。"""
        try:
            return max(1, int(self.count_var.get()))
        except (tk.TclError, ValueError):
            return 1

    def generate(self) -> None:
        prompt = self.prompt.get("1.0", "end").strip()
        if not prompt:
            messagebox.showwarning("提示词", "请先输入提示词。", parent=self.root)
            return
        if not self.active_model:
            messagebox.showwarning("模型", "请先刷新并选择模型。", parent=self.root)
            return
        self.status_var.set("正在生成，请耐心等待…")
        self.generate_button.configure(state="disabled")
        args = {
            "prompt": prompt,
            "model": self.model_var.get(),
            "base_url": self.base_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "references": list(self.reference_paths),
            "caps": (self.active_model.get("capabilities") or {}),
            "ratio": self.ratio_var.get(),
            "custom_ratio": self.custom_ratio_var.get().strip(),
            "k": self.k_var.get(),
            "size": self.size_var.get(),
            "quality": self.quality_var.get(),
            "n": self._read_count(),
        }
        self._run_task("generate", lambda: self._request_generate(args))

    def _request_generate(self, args: dict[str, Any]) -> dict[str, Any]:
        caps = args["caps"]
        payload: dict[str, Any] = {"model": args["model"], "prompt": args["prompt"], "n": args["n"]}
        if caps.get("size_mode") == "preset":
            payload["size"] = args["size"]
        elif caps.get("size_mode") != "none":
            payload.update({"ratio": args["ratio"], "k": args["k"]})
            if args["ratio"] == "custom":
                payload["custom_ratio"] = args["custom_ratio"]
        if caps.get("qualities"):
            payload["quality"] = args["quality"]

        files = []
        handles = []
        try:
            if args["references"]:
                payload["base_url"] = args["base_url"]
                payload["api_key"] = args["api_key"]
                for path in args["references"]:
                    handle = open(path, "rb")
                    handles.append(handle)
                    files.append(("image", (Path(path).name, handle, reference_mime_type(path))))
                response = requests.post(self._url("/api/generate"), data=payload, files=files, timeout=360)
            else:
                payload["base_url"] = args["base_url"]
                payload["api_key"] = args["api_key"]
                response = requests.post(self._url("/api/generate"), json=payload, timeout=360)
            data = response.json()
            if not response.ok or not data.get("ok"):
                raise RuntimeError(data.get("error") or f"生成失败（HTTP {response.status_code}）")
            return data
        finally:
            for handle in handles:
                handle.close()

    def _finish_generate(self, ok: bool, result: Any) -> None:
        self.generate_button.configure(state="normal" if self.active_model else "disabled")
        if not ok:
            self.status_var.set("生成失败")
            messagebox.showerror("生成失败", result, parent=self.root)
            return
        self.result_paths = [self._output_path(item["filename"]) for item in result.get("images", [])]
        self.result_index = 0
        self._show_result(0, absolute=True)
        self.clear_references()
        self.status_var.set(f"生成完成，共 {len(self.result_paths)} 张")
        self.history_var.set(f"模型：{result.get('meta', {}).get('model', '')}    尺寸：{result.get('meta', {}).get('size', '')}")

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
        self._save_settings()
        self.server.stop()
        self.root.destroy()


def main() -> None:
    server = BackendThread()
    server.start()
    root = tk.Tk()
    try:
        Image2Client(root, server)
        root.mainloop()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
