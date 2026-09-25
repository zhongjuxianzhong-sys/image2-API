# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project does not use semantic versioning yet.

## [Unreleased]

### Changed

- 项目收敛为只对接 GPT Image（`gpt-image-*`）生图模型，程序更名为 **GPT 生图工坊**，产物为 `dist\GptImageStudio.exe`。
- 生成接口改用文档推荐的异步模式：提交时带 `?async=1` 与 `Prefer: respond-async`，随后按 3 秒节奏轮询任务状态；上游同步返回图片时直接使用。
- 参数模型固定为官方「10 档画幅 × 1K / 2K / 4K」，每个组合下发官方推荐像素；移除画幅 `auto` 与自定义比例。
- 质量不再由界面选择，改为随分辨率档自动映射 `1K → standard`、`2K → hd`、`4K → 4k`。
- 图生图默认走 `/v1/chat/completions`（`image_url` 内联 data URL），失败时回退 `/v1/images/edits` multipart；传输顺序按上游 `supported_endpoint_types` 里的 `openai` 决定。
- 参考图上限由 16 张收紧到 4 张；带参考图时数量固定为 1 张。
- 桌面客户端默认分辨率由 2K 改为 1K，并记住上次选择的模型。
- 删除 `static/` 网页界面、首页路由与浏览器自动打开逻辑，`app.py` 只作为本地内部服务。

### Added

- `POST /api/tasks`、`GET /api/tasks/<id>`、`DELETE /api/tasks/<id>` 异步任务接口，支持进度查询与「取消等待」。
- 客户端进度条与“生成中 xx%（已用 xx 秒）”提示，生成期间可取消等待。
- 上游 SSE（`text/event-stream`）流式响应解析，兼容 chat 通道按流返回图片 URL 的情况。
- 任务表加锁与 TTL 清理：终态任务保留 30 分钟，进程退出时清空。

### Removed

- DALL-E、Doubao-Seedream、Gemini 等非 GPT 模型的能力表、尺寸表与分支代码。
- 自定义比例解析与 `auto` 画幅相关的尺寸推导逻辑。

### Fixed

- 异步提交接受 `200 / 201 / 202` 状态码，不再把 `202` 误判为失败。
- 需求中的“超时清理”得到保障：轮询在取消、超时或终态后立即停止，不再占用连接。

### Documentation

- README、使用说明与 CI/Release 工作流同步更名与接口说明。
