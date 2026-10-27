"""公共常量与纯函数：语言表、prompt 构造、图片转 data URL、文本分段。"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path

DEFAULT_API_BASE = "https://index-translate.bilibili.com/v1"
DEFAULT_MODEL = "Index-Translate-35B-A3B"
DEFAULT_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

# 与官方 call_api.py 保持一致的语言代码 → 中文名
LANG_NAMES = {
    "en": "英语",
    "zh": "中文",
    "de": "德语",
    "fr": "法语",
    "es": "西班牙语",
    "ja": "日语",
    "ko": "韩语",
    "pt": "葡萄牙语",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "it": "意大利语",
    "nl": "荷兰语",
    "pl": "波兰语",
    "ro": "罗马尼亚语",
    "sv": "瑞典语",
    "tr": "土耳其语",
    "hi": "印地语",
    "vi": "越南语",
    "th": "泰语",
    "id": "印尼语",
    "ms": "马来语",
    "fil": "菲律宾语",
    "ukr_cyrl": "乌克兰语",
    "fas_arab": "波斯语",
    "ces_latn": "捷克语",
    "ell_grek": "希腊语",
    "dan_latn": "丹麦语",
    "hun_latn": "匈牙利语",
    "fin_latn": "芬兰语",
    "nob_latn": "书面挪威语",
    "slk_latn": "斯洛伐克语",
    "bul_cyrl": "保加利亚语",
}

# 指令里能直接写的语言别名
LANG_ALIAS = {
    "英语": "en",
    "英文": "en",
    "中文": "zh",
    "汉语": "zh",
    "日文": "ja",
    "日语": "ja",
    "韩语": "ko",
    "韩文": "ko",
    "法语": "fr",
    "德语": "de",
    "俄语": "ru",
    "西班牙语": "es",
    "葡萄牙语": "pt",
    "意大利语": "it",
    "阿拉伯语": "ar",
    "印地语": "hi",
    "越南语": "vi",
    "泰语": "th",
    "印尼语": "id",
    "马来语": "ms",
    "菲律宾语": "fil",
    "土耳其语": "tr",
    "荷兰语": "nl",
    "波兰语": "pl",
    "瑞典语": "sv",
    "罗马尼亚语": "ro",
    "乌克兰语": "ukr_cyrl",
    "波斯语": "fas_arab",
    "捷克语": "ces_latn",
    "希腊语": "ell_grek",
    "丹麦语": "dan_latn",
    "匈牙利语": "hun_latn",
    "芬兰语": "fin_latn",
    "挪威语": "nob_latn",
    "斯洛伐克语": "slk_latn",
    "保加利亚语": "bul_cyrl",
}

TRIGGER = re.compile(
    r"^\s*(?:翻译成|翻译到|翻译为|翻译|译成|译|tr)\s*(.*)$", re.I | re.S
)
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


def build_json_prompt(payload: dict, target: str) -> str:
    return (
        f"请把下面 JSON 里每个 value 翻译为{lang_name(target)}，key 保持不变，"
        f"只输出 JSON，不要有任何解释。\n\n" + json.dumps(payload, ensure_ascii=False)
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
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
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
