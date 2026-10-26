"""Index-Translate 翻译插件。

基于 bilibili/Index-Translate 的免费公开 API：
    POST https://index-translate.bilibili.com/v1/chat/completions
OpenAI 兼容格式、免鉴权、无需 API Key。

触发：翻译 xxx / 翻译成日语 xxx / 译 xxx / tr xxx
引用消息后只发「翻译」→ 翻译被引用的文字或图片。
"""

from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Reply
from astrbot.api.star import Context, Star

DEFAULT_API_BASE = "https://index-translate.bilibili.com/v1"
DEFAULT_MODEL = "Index-Translate-35B-A3B"

# 与官方 call_api.py 保持一致的语言代码 → 中文名
LANG_NAMES = {
    "en": "英语", "zh": "中文", "de": "德语", "fr": "法语", "es": "西班牙语",
    "ja": "日语", "ko": "韩语", "pt": "葡萄牙语", "ru": "俄语", "ar": "阿拉伯语",
    "it": "意大利语", "nl": "荷兰语", "pl": "波兰语", "ro": "罗马尼亚语",
    "sv": "瑞典语", "tr": "土耳其语", "hi": "印地语", "vi": "越南语",
    "th": "泰语", "id": "印尼语", "ms": "马来语", "fil": "菲律宾语",
    "ukr_cyrl": "乌克兰语", "fas_arab": "波斯语", "ces_latn": "捷克语",
    "ell_grek": "希腊语", "dan_latn": "丹麦语", "hun_latn": "匈牙利语",
    "fin_latn": "芬兰语", "nob_latn": "书面挪威语", "slk_latn": "斯洛伐克语",
    "bul_cyrl": "保加利亚语",
}

# 指令里能直接写的语言别名
LANG_ALIAS = {
    "英语": "en", "英文": "en", "中文": "zh", "汉语": "zh", "日文": "ja", "日语": "ja",
    "韩语": "ko", "韩文": "ko", "法语": "fr", "德语": "de", "俄语": "ru",
    "西班牙语": "es", "葡萄牙语": "pt", "意大利语": "it", "阿拉伯语": "ar",
    "印地语": "hi", "越南语": "vi", "泰语": "th", "印尼语": "id", "马来语": "ms",
    "菲律宾语": "fil", "土耳其语": "tr", "荷兰语": "nl", "波兰语": "pl",
    "瑞典语": "sv", "罗马尼亚语": "ro", "乌克兰语": "ukr_cyrl", "波斯语": "fas_arab",
    "捷克语": "ces_latn", "希腊语": "ell_grek", "丹麦语": "dan_latn",
    "匈牙利语": "hun_latn", "芬兰语": "fin_latn", "挪威语": "nob_latn",
    "斯洛伐克语": "slk_latn", "保加利亚语": "bul_cyrl",
}

TRIGGER = re.compile(r"^\s*(?:翻译成|翻译到|翻译为|翻译|译成|译|tr)\s*(.*)$", re.I | re.S)
TARGET_PREFIX = re.compile(
    r"^(?:成|到|为)?\s*([\u4e00-\u9fa5]{1,6}|[A-Za-z_]{2,10})\s*[:：,，]?\s*(.*)$", re.S
)


def strip_think(text: str) -> str:
    """去掉可能的思维链包裹。"""
    text = text or ""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip().removeprefix("<think>").strip()


def lang_name(code: str) -> str:
    return LANG_NAMES.get((code or "").lower(), code)


def build_prompt(text: str, target: str) -> str:
    return (
        f"请将以下文本翻译为{lang_name(target)}，直接输出翻译结果，不要进行任何解释。\n\n"
        f"{text.strip()}"
    )


def build_image_prompt(target: str, hint: str = "") -> str:
    base = f"请将图片中的文本翻译为{lang_name(target)}，直接输出翻译结果，不要进行任何解释。"
    if hint.strip():
        base += f"\n\n【参考文本】\n{hint.strip()}"
    return base


def to_data_url(image_path: str) -> str:
    p = Path(image_path)
    mime = mimetypes.guess_type(str(p))[0]
    if not mime:
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
        }.get(p.suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def split_text(text: str, limit: int) -> list[str]:
    """按段落切成不超过 limit 的片段。"""
    if not limit or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for para in text.split("\n"):
        while len(para) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(para[:limit])
            para = para[limit:]
        if cur and len(cur) + len(para) + 1 > limit:
            chunks.append(cur)
            cur = para
        else:
            cur = f"{cur}\n{para}" if cur else para
    if cur.strip():
        chunks.append(cur)
    return chunks or [text]


class IndexTranslatePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        logger.info(
            f"[IndexTranslate] 已加载，模型 {self.config.get('model') or DEFAULT_MODEL}，"
            f"默认目标语言 {self.config.get('target_lang') or 'en'}"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        m = TRIGGER.match(raw)
        if not m:
            return
        payload = (m.group(1) or "").strip()

        quoted_text = ""
        images: list[str] = []
        for comp in event.message_obj.message:
            if isinstance(comp, Reply):
                quoted_text = (getattr(comp, "message_str", "") or "").strip()
                for sub in (getattr(comp, "chain", None) or []):
                    if isinstance(sub, Image):
                        try:
                            images.append(await sub.convert_to_file_path())
                        except Exception as e:
                            logger.warning(f"[IndexTranslate] 引用图片取不到: {e}")
            elif isinstance(comp, Image):
                try:
                    images.append(await comp.convert_to_file_path())
                except Exception as e:
                    logger.warning(f"[IndexTranslate] 图片取不到: {e}")

        target = str(self.config.get("target_lang") or "en")
        if payload:
            tm = TARGET_PREFIX.match(payload)
            if tm:
                alias = LANG_ALIAS.get(tm.group(1))
                if alias:
                    target = alias
                    payload = (tm.group(2) or "").strip()

        text = payload or quoted_text
        if not text and not images:
            event.stop_event()
            yield event.plain_result(
                "要翻什么？发「翻译 内容」，或者引用一条消息/图片再发「翻译」。\n"
                "也能指定语言：「翻译成日语 xxx」"
            )
            return

        event.stop_event()
        try:
            result = await self._translate(text, target, images[0] if images else None)
        except Exception as e:
            logger.error(f"[IndexTranslate] 翻译失败: {e}")
            yield event.plain_result(f"翻不动了：{e}")
            return

        if not result.strip():
            yield event.plain_result("模型没吐译文出来，可能内容太长或被拦了，换个短点的试试")
            return
        yield event.plain_result(result)

    async def _translate(self, text: str, target: str, image_path: str | None) -> str:
        base = str(self.config.get("api_base") or DEFAULT_API_BASE).rstrip("/")
        model = str(self.config.get("model") or DEFAULT_MODEL)
        max_tokens = int(self.config.get("max_tokens") or 1024)
        timeout = int(self.config.get("timeout") or 90)
        chunk = int(self.config.get("chunk_chars") or 0)

        if image_path:
            content = [
                {"type": "text", "text": build_image_prompt(target, text)},
                {"type": "image_url", "image_url": {"url": to_data_url(image_path)}},
            ]
            return await self._post(base, model, content, max_tokens, timeout)

        pieces = split_text(text, chunk)
        outs = []
        for piece in pieces:
            outs.append(await self._post(base, model, build_prompt(piece, target), max_tokens, timeout))
        return "\n".join(x for x in outs if x.strip())

    async def _post(self, base: str, model: str, content, max_tokens: int, timeout: int) -> str:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "stream": False,
            # 关掉思维链：开着会把推理过程顶掉译文
            "chat_template_kwargs": {"enable_thinking": False},
        }
        async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
            r = await client.post(
                f"{base}/chat/completions",
                json=payload,
                headers={"User-Agent": "Index-Translate-Client/1.0"},
            )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}：{r.text[:160]}")
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"返回里没有 choices：{str(data)[:160]}")
        return strip_think(choices[0].get("message", {}).get("content") or "")
