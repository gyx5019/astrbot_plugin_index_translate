# astrbot_plugin_index_translate

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16-blue)](https://github.com/AstrBotDevs/AstrBot)
[![version](https://img.shields.io/badge/version-0.1.1-green)]()

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
| 语音翻译 | 引用一条语音后发 `翻译`，转文字 → 翻译 → 可选 TTS 念回来 |
| 英文触发词 | `tr hello world` / `译 hello world` |

- 长文本按段落自动切段（默认 900 字）逐段翻译后拼接，避免被 `max_tokens` 截断
- 三档模型可选：`Index-Translate-35B-A3B`（旗舰）/ `9B`（均衡）/ `2B`（极速）
- 支持 30+ 目标语言，配置里改默认值，指令里可临时覆盖

## 安装

1. 在 AstrBot 的插件管理里从本仓库安装，或直接把整个目录丢进 `data/plugins/`
2. 装依赖：`pip install -r requirements.txt`（其实 AstrBot 已自带 `httpx` 和 `Pillow`，这步可选）
3. 系统装 `ffmpeg`（语音翻译要用 `ffprobe` 读时长）：`apt install -y ffmpeg` / `brew install ffmpeg`
4. 重载插件（新装插件需要走安装流程，热重载拉不起未注册的插件）
5. 在插件配置里按需改 `target_lang` / `model`

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
| `ocr_provider` | string | `cnocr` | OCR 用哪家：`cnocr` / `glm` |
| `ocr_api_base` | string | 空 | cnocr 填服务地址；glm 留空则用官方地址 |
| `ocr_api_key` | string | 空 | 选 `glm` 时必填 |
| `ocr_api_path` | string | `/ocr` | cnocr 的接口路径 |
| `ocr_timeout` | int | `60` | OCR 超时（秒） |
| `render_font` | string | `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc` | 回填文字用的字体 |
| `voice_translate` | bool | `true` | 语音翻译总开关 |
| `asr_api_base` | string | 空 | 语音转文字的接口地址（OpenAI 兼容） |
| `asr_api_key` | string | 空 | 语音转文字的 API Key |
| `asr_model` | string | `qwen3-asr-flash` | 语音转文字的模型名 |
| `voice_auto` | bool | `false` | 直接发语音就自动翻（不用配「翻译」） |
| `voice_reply` | bool | `true` | 译文用 TTS 语音回一遍 |

## 图片回填（可选）

默认关闭，图片翻译只输出文字。开启 `image_render` 并配好 OCR 后，会走这条路：

1. OCR 逐块拿到文字和坐标
2. 各块拼成一个 JSON 一次性翻译（省调用；漏翻的逐块补翻）
3. 按框擦掉原文（背景色从框外一圈采样，取中位色）
4. 写入译文（字色按亮度自动选黑/白，自动折行、缩字号塞进框）
5. 发回一张 `xxx_translated.png`

任何一步失败都会自动退回文字输出，不会丢图。

两种 OCR 二选一：

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

> 注意：`glm` 和 `cnocr` 都会返回坐标、能回填。两家的返回结构差很多，插件统一做了兼容：递归找「带文本 + 带坐标」的节点（`position` / `bbox_2d` / `bbox` 都认），坐标是 0~1 归一化还是像素会自动判断。

## 语音翻译

> **注意：语音翻译目前只实测过 `qwen3-asr-flash`（阿里云 MaaS 网关），其它 ASR 模型 / 服务未经验证，需自行测试。**

语音转文字有两条路（见下方说明），TTS 走 AstrBot 里已加载的 TTS provider。

1. 引用一条语音消息，发 `翻译`
2. 用会话绑定的 STT provider 把语音转成文字
3. 交给 Index-Translate 翻译
4. 先发文字译文，再用 TTS provider 合成一段语音发出来（`voice_reply` 可关）

想让它听到语音就自动翻，打开 `voice_auto`（默认关，免得群里聊着聊着被打断）。

语音转文字有两种走法：

1. 插件自己配 `asr_api_base` + `asr_api_key` + `asr_model`，直接打 OpenAI 兼容的 ASR 接口（推荐，实测阿里云 MaaS 网关用 `qwen3-asr-flash` 可通）
2. 留空则会退回用 AstrBot 里绑定的 STT provider

> 注意：AstrBot 自带的 `openai_whisper_api` 走 `/audio/transcriptions`，在 Qwen 这类只提供 `chat/completions` 的网关上会 404，那种情况请用第 1 种。

TTS 走 AstrBot 里绑定的那个（如 `gemini_tts`）。

## 目录结构

```
astrbot_plugin_index_translate/
├── main.py             入口：Star 类、消息路由、翻译主体
├── ocr.py              OCR 与图片回填（cnocr / glm），mixin
├── voice.py            语音、音频（ASR → 翻译 → TTS），mixin
├── data/               运行数据
│   ├── plugin.log      插件日志（超 2MB 自动轮转为 plugin.log.1）
│   └── *.bak.*         改动前的备份
├── _conf_schema.json   配置定义
├── metadata.yaml       插件元信息
├── requirements.txt    依赖
└── README.md

```
OCR 和语音用 mixin 拆出去，方法仍挂在同一个 `self` 上，所以 `self._ocr()`、`self._voice_flow()` 这类调用不用改。

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
