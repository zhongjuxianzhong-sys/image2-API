# Image2 生图工坊

一个本机运行的多模型生图 Web 小工具。程序会从你填写的中转站动态发现生图模型，支持 GPT Image、DALL-E 与 Doubao-Seedream，模型与参数在网页界面选择。程序**不内置任何中转站（提供商）与 API key**：两项只在网页界面填写，后端不读 `.env`、不读环境变量，也没有任何默认地址。未填写时前端直接拦下，后端返回 400，不会向任何服务器发请求。key 只随请求头使用，不写入日志与历史记录。

## 功能

- 输入提示词，分别选择“画幅”与“分辨率”、质量、数量后一键生图
- 画幅支持 `自动 / 1:1 / 4:3 / 3:4 / 16:9 / 9:16 / 自定义比例`；选择“自定义比例”后可填写如 `21:9`，并按当前分辨率档推导实际像素
- 分辨率最高 `2K`（1K/1.5K/2K），选“自动”画幅时分辨率自动忽略
- 可上传一张或多张参考图；支持编辑的模型按参考图生成，不支持编辑的模型自动禁用上传
- 提供商地址（Base URL）与 API Key 全部由界面输入，两项默认为空、必填，填一次即记在浏览器本地
- 配置完成后自动请求上游 `/models`，按“推荐 / 其他”分组列出可用生图模型；模型选择记在浏览器本地
- 不同模型自动切换参数能力：GPT Image / Seedream 使用画幅 + 分辨率，DALL-E 使用固定尺寸；不支持参考图的模型会禁用上传
- 两项任一缺失时，前端提示“待配置”并聚焦对应输入框，后端同样拒绝，不会向任何默认地址发请求
- 结果实时预览，支持点击放大、单张下载
- 最近生图历史持久化在服务端 `outputs/history.json`，跨重启保留，历史项可直接下载或清空
- 后端先读取中转站 `/models`，筛出可用生图模型并下发模型能力
- 对 401/429/5xx/524 等常见错误给出中文可读提示；对 429/5xx 做有限重试，不对 `524`（Cloudflare 代理超时）盲目重试

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
    Base URL 会保存在程序目录的 `client-settings.json`，API Key 只保存在当前运行期间。

## 配置项

生图配置（界面填写，无默认值）：

| 界面项 | 默认值 | 说明 |
| --- | --- | --- |
| API Key | 空（必填） | 你的中转站 API key |
| 提供商地址（Base URL） | 空（必填） | 中转站地址，通常以 `/v1` 结尾，勿填官方接口 |

服务配置（可选，环境变量 / 命令行）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HTTP_PROXY` / `HTTPS_PROXY` | 无 | 需要走本地代理时设置，例如 `http://127.0.0.1:7897` |
| `PORT` | 不使用 | 原生客户端使用内部随机端口，用户无需配置 |

## 说明

- 模型由上游模型列表动态提供；GPT Image 与 Seedream 的画幅和分辨率分别通过下拉框选择，并从各模型官方推荐像素表得到实际 `宽x高`；DALL-E 使用固定尺寸。
- GPT Image 2 / 2.5 使用 `1K / 2K / 4K` 档位与推荐标准像素表，质量自动映射为 `standard / hd / 4k`，例如 `16:9 + 2K → 2560x1440 + hd`。
- GPT Image 选“自动”时，普通模型下发 `size=auto`；Seedream 不发送该字段，以避免部分中转站返回 400。
- 自定义比例范围为 `1:16` 到 `16:1`，宽高必须为正整数；Seedream 自定义尺寸会自动落在对应版本的官方像素范围内。
- Seedream 5.0 pro 仅支持单图；5.0 lite、4.5、4.0 的数量大于 1 时会映射为组图请求。
- 如果上游 `/models` 没有返回可识别的生图模型，客户端会提示“未发现可用的生图模型”并禁用生成。
- 目前未暴露透明背景、水印、联网搜索等模型专有选项。
- 生成的图片保存在 `outputs/generated/`。
- GPT Image 参考图走中转站的 `/images/edits`；Seedream 参考图按上游协议走 `/images/generations` 的 `image` 字段；若你的中转站未开启对应接口，会返回相应错误提示。
- `524` 来自中转站前端 Cloudflare 的“源站读取超时”（约 120 秒），不是 key 或接口错误；出现时请稍后重试或改用 `auto`/低质量、更小尺寸。
- 如果返回 `401 invalid_api_key`，先确认界面里的 Base URL 指向你自己的中转站，而非官方地址。
- 连接超时通常是代理未设置或不通，检查 `HTTP_PROXY` / `HTTPS_PROXY`。

## 直接调用接口

后端也暴露了 API，供脚本或其它程序复用。因为不再有服务端配置，`base_url` 与 `api_key`
必须随请求一起传：

```powershell
$body = @{
  model = "gpt-image-2"
  prompt = "high-end portrait, minimalist indoor background, soft natural light, no text"
  base_url = "https://你的中转站地址/v1"
  api_key = "你的中转站key"
  ratio = "3:4"
  k = "2K"
  quality = "high"
  n = 1
} | ConvertTo-Json

curl.exe "http://127.0.0.1:8787/api/generate" `
  -H "Content-Type: application/json" `
  -d $body
```

带参考图（multipart）：

```powershell
curl.exe "http://127.0.0.1:8787/api/generate" `
  -F "model=gpt-image-2" `
  -F "prompt=keep the subject but make the background pastel, no text" `
  -F "base_url=https://你的中转站地址/v1" `
  -F "api_key=你的中转站key" `
  -F "ratio=auto" `
  -F "quality=auto" `
  -F "n=1" `
  -F "image=@D:\path\to\ref.png;type=image/png"
```

## 打包为程序

运行 `build.ps1` 即可用 PyInstaller 生成单文件 Windows 程序：

```powershell
.\build.ps1
```

产物为 `dist\Image2Studio.exe`，`build.ps1` 还会把 `使用说明.txt` 复制进 `dist\`。打包版与源码运行一致，但路径行为不同：

- 原生客户端和内部服务已内置进 exe，无需随程序分发 Python 或浏览器。
- **exe 内不含任何 key 与中转站地址**，也不读取 `.env`；使用者只需在客户端内填写两项。
- 生成的图片与历史写到 exe 同级 `outputs\generated\` 与 `outputs\history.json`，可持久化保存。

分发时把 `Image2Studio.exe` 与 `使用说明.txt` 发给使用者即可：双击 exe 后直接进入原生桌面客户端，
不打开浏览器，也无需保持终端窗口。客户端内填写提供商地址与 API Key 后即可生图，关闭客户端窗口即可退出程序。

## GitHub Actions

- 推送到 `main` 或提交 Pull Request 时，CI 会在 Windows 上使用 Python 3.10 与 3.13 运行编译、单元测试和 `/api/health` 冒烟检查。
- 推送 `v*` 标签（例如 `v1.0.0`）时，Release 工作流会打包 `Image2Studio.exe` 并创建 GitHub Release。
- 也可以在 Actions 页面手动运行 “Build Windows release”，只生成可下载的构建产物，不创建 Release。

## 项目规范

- [CHANGELOG.md](CHANGELOG.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [LICENSE](LICENSE)（MIT）
- [SECURITY.md](SECURITY.md)
