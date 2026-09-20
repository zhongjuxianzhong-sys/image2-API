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

2. 启动：

   ```powershell
   .\start.ps1
   ```

   浏览器打开 `http://127.0.0.1:8787`（端口可用 `PORT` 环境变量或 `--port` 参数修改）。

3. 在页面下方填写 **API Key** 与 **提供商地址（Base URL）**，两项都填好后会自动获取模型列表。
   配置只保存在本机浏览器 localStorage，程序目录里不需要任何配置文件。

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
| `PORT` | `8787` | 本服务监听端口，也可用 `python app.py --port 8123` |

## 说明

- 模型由上游模型列表动态提供；GPT Image 与 Seedream 的画幅和分辨率分别通过下拉框选择，两两组合派生实际 `宽x高`；DALL-E 使用固定尺寸。
- GPT Image 2 / 2.5 使用 `1K / 2K / 4K` 档位与推荐标准像素表，质量自动映射为 `standard / hd / 4k`，例如 `16:9 + 2K → 2560x1440 + hd`。
- GPT Image 选“自动”时，普通模型下发 `size=auto`；Seedream 不发送该字段，以避免部分中转站返回 400。
- 自定义比例范围为 `1:16` 到 `16:1`，宽高必须为正整数；例如 `21:9` + `1K` 得到 `1024x439`。
- Seedream 的数量大于 1 时会映射为组图请求；默认关闭水印，不暴露联网搜索等模型专有选项。
- 如果上游 `/models` 没有返回可识别的生图模型，页面会提示“未发现可用的生图模型”并禁用生成。
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

- 前端静态资源（`static\`）已内置进 exe，无需随程序分发。
- **exe 内不含任何 key 与中转站地址**，也不读取 `.env`；使用者只需在网页界面填写两项。
- 生成的图片与历史写到 exe 同级 `outputs\generated\` 与 `outputs\history.json`，可持久化保存。
- 换端口：`Image2Studio.exe --port 8123`（默认 8787）。

分发时把 `Image2Studio.exe` 与 `使用说明.txt` 发给使用者即可：双击 exe，浏览器打开
`http://127.0.0.1:8787`，在页面下方填好自己的提供商地址与 API Key 就能生图。

## 项目规范

- [CHANGELOG.md](CHANGELOG.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [LICENSE](LICENSE)（MIT）
- [SECURITY.md](SECURITY.md)
