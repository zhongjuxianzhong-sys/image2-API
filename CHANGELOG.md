# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project does not use semantic versioning yet.

## [Unreleased]

### Added

- 动态生图模型发现与用户模型选择。
- GPT Image、DALL-E 与 Doubao-Seedream 的多模型参数适配。
- 模型能力驱动的尺寸、质量、数量和参考图控件联动。
- 后端接口与模型分类单元测试。
- 客户端可选“记住 Base URL 和 API Key”，凭据使用 Windows DPAPI 加密保存。

### Changed

- 生成接口从固定 `gpt-image-2` 改为按请求中的 `model` 调用。
- GPT Image 参考图继续使用 `/images/edits`，Seedream 参考图使用 `/images/generations` 的 `image` 字段。
- GPT Image 2 / 2.5 使用 1K / 2K / 4K 标准像素表，质量按档位映射为 `standard / hd / 4k`。
- Seedream 优先发送推荐精确像素，失败时回退到档位；客户端默认优先选择 2K。

### Fixed

- 修正 GPT Image 2.5 / 1.5 的家族识别：模型 ID 归一化后不再被误判为 GPT Image 2 / 1。
- 修正 GPT Image 2 / 2.5 的质量档位映射：质量选 `auto` 时按 1K / 2K / 4K 映射为 `standard / hd / 4k`。
- 修正桌面客户端参考图上传的 MIME 类型，Seedream 不再收到 `application/octet-stream` 的 data URL。
- 删除调用未定义函数的 `chat_completions` 死分支；历史写入改为单次原子替换。
- 数量输入框接受非法文本时按 1 处理，不再抛 `TclError` 中断生成。
- 上游返回 `data: null` 或空图片项时不再产生 500 或 0 字节图片。
- 历史记录写入增加进程内锁与原子替换，降低并发覆盖风险。
- 结果与历史记录改为显示生成图片的真实像素尺寸。
- Seedream 5.0 lite 的分辨率选项修正为官方支持的 2K / 3K。

### Documentation

- Project convention files: `.editorconfig`, `.gitattributes`, `CONTRIBUTING.md`, and `SECURITY.md`.
- MIT License file (`LICENSE`).
