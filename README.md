# GPT 生图工坊

一个本机运行的原生 Windows 生图客户端，只对接 GPT Image 系列（`gpt-image-*`）生图模型。程序从你填写的上游动态发现模型，按官方「10 档画幅 × 1K / 2K / 4K」参数模型发请求，并用异步任务 + 轮询出图。程序**不内置任何中转站（提供商）与 API key**：两项只在客户端界面填写，后端不读 `.env`、不读环境变量，也没有任何默认地址。未填写时前端直接拦下，后端返回 400，不会向任何服务器发请求。key 只随请求头使用，不写入日志与历史记录。

## 功能

- 输入提示词，选择画幅、分辨率与数量后一键生图
- 画幅固定为官方 10 档：`1:1 / 16:9 / 9:16 / 3:2 / 2:3 / 4:3 / 3:4 / 5:4 / 4:5 / 21:9`；不支持 `auto` 与自定义像素
- 分辨率 `1K / 2K / 4K`，默认 1K；每个组合直接下发官方推荐像素（如 `16:9 + 2K → 2560x1440`、`21:9 + 4K → 3808x1632`）
- 质量不需要手选：`1K → standard`、`2K → hd`、`4K → 4k` 随档位自动下发
- 可上传最多 4 张参考图；带参考图时按图生图调用，数量固定为 1 张
- 提供商地址（Base URL）与 API Key 全部由界面输入，两项默认为空、必填；可选择加密记住在本机
- 配置完成后自动请求上游 `/models`，只列出 `gpt-image-*` 模型，并优先选中 `gpt-image-2`
- 异步任务：文生图提交后立刻返回 `task_id`，客户端按秒轮询显示百分比与已用时间，可随时「取消等待」
- 兼顾两种中转站：上游支持异步（返回 `task_id` / `status_url`）时轮询其真实进度；只支持同步的中转站（如未实现任务查询接口）会继续等待一次请求完成，客户端按已用时间显示估算进度
- 上游声明 `supported_endpoint_types` 含 `openai` 时，参考图优先走 `/v1/chat/completions`；不声明或失败时回退 `/v1/images/edits`
- 结果实时预览，支持上一张 / 下一张与打开输出目录
- 最近生图历史持久化在服务端 `outputs/history.json`，跨重启保留，可一键清空
- 对 401/404/429/5xx/524 等常见错误给出中文可读提示；对 429/5xx 做有限重试，不对 `524`（Cloudflare 代理超时）盲目重试

## 快速开始

1. 安装依赖（Python 3.10+）：

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. 启动原生桌面客户端：

   ```powershell
   .\start.ps1
   ```

   程序会打开原生窗口，不启动浏览器；在窗口内填写 **API Key** 与 **提供商地址（Base URL）**，点击“刷新模型”即可。
   勾选“记住 Base URL、API Key 与模型”后，三项会使用 Windows DPAPI 加密保存到程序目录的 `client-credentials.dat`，
   只有当前 Windows 用户可以解密；取消勾选会删除已保存的凭据。

## 配置项

生图配置（界面填写，无默认值）：

| 界面项 | 默认值 | 说明 |
| --- | --- | --- |
| API Key | 空（必填） | 你的中转站 API key |
| 提供商地址（Base URL） | 空（必填） | 中转站地址，通常以 `/v1` 结尾，勿填官方接口 |

凭据保存是可选的：不勾选“记住 Base URL、API Key 与模型”时，关闭程序不会留下凭据文件。

服务配置（可选，环境变量 / 命令行）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HTTP_PROXY` / `HTTPS_PROXY` | 无 | 需要走本地代理时设置，例如 `http://127.0.0.1:7897` |
| `PORT` | 不使用 | 原生客户端使用内部随机端口，用户无需配置 |

## 说明

- 模型由上游模型列表动态提供，但只保留 `gpt-image-*`：Doc 里常见的 `gpt-image-2`、`gpt-image-2.5-flare`、`gpt-image-2.5-sunburst` 都会列出；`DALL-E`、`Doubao-Seedream`、`Gemini` 等模型不会出现，也不会被调用。
- 生成完成后显示的是图片文件的真实像素尺寸，而不是请求档位。
- 参考图最多 4 张；超过会被前端截断并在后端校验拒绝。
- `1K` 档最省积分也最快（`gpt-image-2` 约 15–30 秒），`2K / 4K` 明显更慢也更贵，建议先用 1K 试提示词。
- 生成的图片保存在 `outputs/generated/`，历史记录写入 `outputs/history.json`。
- 任务在客户端本地排队轮询：单次生成预算 15 分钟，完成或失败的任务保留 30 分钟后自动清理；「取消等待」会立刻停止轮询，但上游可能仍在生成。
- 同步型中转站不会返回百分比，进度条按已用时间估算（约 120 秒视为接近完成），到 95% 后保持不变直到真正出图。
- 如果上游 `/models` 没有返回任何 `gpt-image-*` 模型，客户端会提示“未发现可用的 GPT 生图模型”并禁用生成。
- 如果返回 `401 invalid_api_key`，先确认界面里的 Base URL 指向你自己的中转站，而非官方地址。
- 连接超时通常是代理未设置或不通，检查 `HTTP_PROXY` / `HTTPS_PROXY`。
- `524` 来自中转站前端 Cloudflare 的“源站读取超时”（约 120 秒），不是 key 或接口错误；出现时请稍后重试或改用 1K 档。

## 直接调用接口

后端也暴露了本地 API，供脚本或其它程序复用。因为不再有服务端配置，`base_url` 与 `api_key`
必须随请求一起传。阻塞式接口（内部提交 + 轮询到完成再返回）：

```powershell
$body = @{
  model = "gpt-image-2"
  prompt = "high-end portrait, minimalist indoor background, soft natural light, no text"
  base_url = "https://你的中转站地址/v1"
  api_key = "你的中转站key"
  ratio = "3:4"
  k = "2K"
  n = 1
} | ConvertTo-Json

curl.exe "http://127.0.0.1:8787/api/generate" `
  -H "Content-Type: application/json" `
  -d $body
```

异步接口（推荐，可查询进度与取消）：

```powershell
curl.exe -X POST "http://127.0.0.1:8787/api/tasks" -H "Content-Type: application/json" -d $body
curl.exe "http://127.0.0.1:8787/api/tasks/<task_id>"
curl.exe -X DELETE "http://127.0.0.1:8787/api/tasks/<task_id>"
```

带参考图（multipart，走图生图，数量固定 1 张）：

```powershell
curl.exe "http://127.0.0.1:8787/api/generate" `
  -F "model=gpt-image-2" `
  -F "prompt=keep the subject but make the background pastel, no text" `
  -F "base_url=https://你的中转站地址/v1" `
  -F "api_key=你的中转站key" `
  -F "ratio=1:1" `
  -F "k=1K" `
  -F "image=@D:\path\to\ref.png;type=image/png"
```

## 打包为程序

运行 `build.ps1` 即可用 PyInstaller 生成单文件 Windows 程序：

```powershell
.\build.ps1
```

产物为 `dist\GptImageStudio.exe`，`build.ps1` 还会把 `使用说明.txt` 复制进 `dist\`。打包版与源码运行一致，但路径行为不同：

- 原生客户端和内部服务已内置进 exe，无需随程序分发 Python。
- **exe 内不含任何 key 与中转站地址**，也不读取 `.env`；使用者只需在客户端内填写两项。
- 生成的图片与历史写到 exe 同级 `outputs\generated\` 与 `outputs\history.json`，可持久化保存。

分发时把 `GptImageStudio.exe` 与 `使用说明.txt` 发给使用者即可：双击 exe 后直接进入原生桌面客户端，
不打开浏览器，也无需保持终端窗口。客户端内填写提供商地址与 API Key 后即可生图，关闭客户端窗口即可退出程序。

## GitHub Actions

- 推送到 `main` 或提交 Pull Request 时，CI 会在 Windows 上使用 Python 3.10 与 3.13 运行编译、单元测试和 `/api/health` 冒烟检查。
- 推送 `v*` 标签（例如 `v1.0.0`）时，Release 工作流会打包 `GptImageStudio.exe` 并创建 GitHub Release。
- 也可以在 Actions 页面手动运行 “Build Windows release”，只生成可下载的构建产物，不创建 Release。

## 项目规范

- [CHANGELOG.md](CHANGELOG.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [LICENSE](LICENSE)（MIT）
- [SECURITY.md](SECURITY.md)
