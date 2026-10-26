# astrbot_plugin_index_translate

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-blue)](https://github.com/AstrBotDevs/AstrBot)
[![version](https://img.shields.io/badge/version-0.1.0-green)]()

调用 **[bilibili/Index-Translate](https://github.com/bilibili/Index-Translate)** 免费公开 API 的 AstrBot 翻译插件。

免鉴权、无需 API Key、不需要本地部署模型，直接打官方公开接口。

---

## 功能

| 场景 | 用法 |
| --- | --- |
| 文本翻译 | `翻译 这是一句话` |
| 临时指定语言 | `翻译成日语 这是一句话` |
| 引用消息翻译 | 引用任意消息后只发 `翻译` |
| 引用图片翻译 | 引用一张图后只发 `翻译` |
| 直接发图 | 图片配一句 `翻译`（或引用带图消息再发 `翻译`） |
| 图片回填成图 | 开启 `image_render` + 配好 OCR，直接发回一张「原文被替换成译文」的图 |
| 英文触发词 | `tr hello world` / `译 hello world` |

- 长文本按段落自动切段（默认 900 字）逐段翻译后拼接，避免被 `max_tokens` 截断
- 三档模型可选：`Index-Translate-35B-A3B`（旗舰）/ `9B`（均衡）/ `2B`（极速）
- 支持 30+ 目标语言，配置里改默认值，指令里可临时覆盖

## 安装

1. 在 AstrBot 的插件管理里从本仓库安装，或直接把整个目录丢进 `data/plugins/`
2. 重载插件（新装插件需要走安装流程，热重载拉不起未注册的插件）
3. 在插件配置里按需改 `target_lang` / `model`

依赖：`httpx`（AstrBot 自带）。

## 配置

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `target_lang` | string | `en` | 默认目标语言代码（`zh` / `en` / `ja` / `ko` …） |
| `model` | string | `Index-Translate-35B-A3B` | 模型档位 |
| `max_tokens` | int | `1024` | 单次生成上限 |
| `chunk_chars` | int | `900` | 长文分段长度，`0` 表示不分段 |
| `timeout` | int | `90` | 单次请求超时（秒） |
| `api_base` | string | `https://index-translate.bilibili.com/v1` | API 地址，自建可改 |
| `show_usage` | bool | `false` | 是否在结果后附耗时/用量 |
| `image_render` | bool | `false` | 图片翻译是否输出「回填译文的图」 |
| `ocr_provider` | string | `cnocr` | OCR 用哪家：`cnocr` / `glm` / `deepseek` |
| `ocr_model` | string | `deepseek-ai/DeepSeek-OCR` | `deepseek` 模式的模型名 |
| `ocr_prompt` | string | `<image>\n<\|grounding\|>OCR this image.` | `deepseek` 模式的提示词，带 `<|grounding|>` 才有坐标 |
| `ocr_api_base` | string | 空 | cnocr 填服务地址；glm 留空则用官方地址 |
| `ocr_api_key` | string | 空 | 选 `glm` 时必填 |
| `ocr_api_path` | string | `/ocr` | cnocr 的接口路径 |
| `ocr_timeout` | int | `60` | OCR 超时（秒） |
| `render_font` | string | `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | 回填文字用的字体 |

## 图片回填（可选）

默认关闭，图片翻译只输出文字。开启 `image_render` 并配好 OCR 后，会走这条路：

1. OCR 逐块拿到文字和坐标
2. 各块拼成一个 JSON 一次性翻译（省调用；漏翻的逐块补翻）
3. 按框擦掉原文（背景色从框外一圈采样，取中位色）
4. 写入译文（字色按亮度自动选黑/白，自动折行、缩字号塞进框）
5. 发回一张 `xxx_translated.png`

任何一步失败都会自动退回文字输出，不会丢图。

三种 OCR 选一种：

**CnOCR（自己起服务）**

```bash
pip install "cnocr[serve]"
cnocr serve -p 8501
```

然后 `ocr_provider = cnocr`，`ocr_api_base = http://127.0.0.1:8501`。

**智谱 GLM-OCR**

```http
POST https://open.bigmodel.cn/api/paas/v4/layout_parsing
Authorization: <你的 API Key>
Content-Type: application/json

{"model": "glm-ocr", "file": "<图片 URL 或 base64>"}
```

然后 `ocr_provider = glm`，`ocr_api_key = 你的 key`，`ocr_api_base` 留空即可。

**DeepSeek-OCR（官方接口，或自己起 vLLM）**

默认走 `https://api.deepseek.com`，key 填在 `ocr_api_key`：

```python
base_url = "https://api.deepseek.com"
```

自己用 vLLM 起也支持（OpenAI 兼容）：`vllm serve deepseek-ai/DeepSeek-OCR --trust-remote-code`，把 `ocr_api_base` 填 `http://127.0.0.1:8000/v1`，模型填 `deepseek-ai/DeepSeek-OCR`。

提示词带 `<|grounding|>` 才会返回坐标：

```
<|grounding|>OCR this image.
```

返回是一串标记，插件按 `<|ref|>` / `<|det|>` 配对拆：

```
<|ref|>BaseURL:<|/ref|><|det|>[[34, 105, 227, 209]]<|/det|>
```

坐标是**原图像素**，不是归一化，插件会自动识别。

> 三家返回结构差很多，插件统一做了兼容：递归找「带文本 + 带坐标」的节点（`position` / `bbox_2d` / `bbox` / `<|ref|><|det|>` 标记都认），坐标是 0~1 归一化还是像素会自动判断。

## 原理

请求格式与官方 `call_api.py` 一致：

```http
POST https://index-translate.bilibili.com/v1/chat/completions
Content-Type: application/json
User-Agent: Index-Translate-Client/1.0

{
  "model": "Index-Translate-35B-A3B",
  "messages": [{"role": "user", "content": "请将以下文本翻译为英语，直接输出翻译结果，不要进行任何解释。\n\n<原文>"}],
  "temperature": 0.0,
  "max_tokens": 1024,
  "stream": false,
  "chat_template_kwargs": {"enable_thinking": false}
}
```

两个关键点：

- **必须关掉 thinking**（`chat_template_kwargs.enable_thinking = false`），否则模型会输出思维链，把译文顶掉
- 返回可能带 `<think>` 包裹，插件里会二次剥离

图片翻译走多模态：`content` 传数组，`image_url.url` 用 base64 data URL。

## 注意

- 这是**公开免费接口**，稳定性、限流、可用性都由上游决定，不保证 SLA，别拿去跑重要业务
- 插件本身不保存任何译文和图片，请求直接转发给上游
- 请遵守上游项目的使用条款

## 致谢

- [bilibili/Index-Translate](https://github.com/bilibili/Index-Translate) —— 模型与公开 API

## License

MIT
