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
