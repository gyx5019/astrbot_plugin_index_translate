"""Index-Translate 翻译插件。

基于 bilibili/Index-Translate 的免费公开 API：
    POST https://index-translate.bilibili.com/v1/chat/completions
OpenAI 兼容格式、免鉴权、无需 API Key。

触发：翻译 xxx / 翻译成日语 xxx / 译 xxx / tr xxx
引用消息后只发「翻译」→ 翻译被引用的文字或图片。

图片有两种输出：
  - 默认：把图丢给多模态模型，输出译文文字
  - 开启 image_render 且配置了 ocr_api_base：先 OCR 拿到文字坐标，
    擦掉原文把译文写回去，输出一张译好的图（CnOCR FastAPI，cnocr serve -p 8501）
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import re
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Record, Reply
from astrbot.api.star import Context, Star

DEFAULT_API_BASE = "https://index-translate.bilibili.com/v1"
DEFAULT_MODEL = "Index-Translate-35B-A3B"
DEFAULT_FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

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


def build_json_prompt(payload: dict, target: str) -> str:
    return (
        f"请把下面 JSON 里每个 value 翻译为{lang_name(target)}，key 保持不变，"
        f"只输出 JSON，不要有任何解释。\n\n"
        + json.dumps(payload, ensure_ascii=False)
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
            f"默认目标语言 {self.config.get('target_lang') or 'en'}，"
            f"图片回填 {'开' if self.config.get('image_render') else '关'}"
        )

    # ---------------- 消息入口 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        m = TRIGGER.match(raw)
        voice_on = bool(self.config.get("voice_translate", True))

        # 没带「翻译」关键词：纯语音 + voice_auto 才自动走语音翻译
        if not m:
            if voice_on and not raw and self.config.get("voice_auto"):
                voices = await self._collect_voices(event)
                if voices:
                    event.stop_event()
                    async for r in self._voice_flow(
                        event, voices, str(self.config.get("target_lang") or "en")
                    ):
                        yield r
            return
        payload = (m.group(1) or "").strip()

        quoted_text = ""
        images: list[str] = []
        voices: list[str] = []
        for comp in event.message_obj.message:
            if isinstance(comp, Reply):
                quoted_text = (getattr(comp, "message_str", "") or "").strip()
                for sub in (getattr(comp, "chain", None) or []):
                    if isinstance(sub, Image):
                        try:
                            images.append(await sub.convert_to_file_path())
                        except Exception as e:
                            logger.warning(f"[IndexTranslate] 引用图片取不到: {e}")
                    elif isinstance(sub, Record):
                        try:
                            voices.append(await sub.convert_to_file_path())
                        except Exception as e:
                            logger.warning(f"[IndexTranslate] 引用语音取不到: {e}")
            elif isinstance(comp, Image):
                try:
                    images.append(await comp.convert_to_file_path())
                except Exception as e:
                    logger.warning(f"[IndexTranslate] 图片取不到: {e}")
            elif isinstance(comp, Record):
                try:
                    voices.append(await comp.convert_to_file_path())
                except Exception as e:
                    logger.warning(f"[IndexTranslate] 语音取不到: {e}")

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

        # 语音：转文字 → 翻译（可选 TTS 念回来）
        if voices and voice_on:
            async for r in self._voice_flow(event, voices, target):
                yield r
            return

        # 图片 + 回填开关 + OCR 已就绪 → 输出译好的图
        if images and self.config.get("image_render") and self._ocr_ready():
            try:
                translated_text, out_image = await self._translate_image_render(images[0], target, text)
            except Exception as e:
                logger.error(f"[IndexTranslate] 图片回填失败，退回文字输出: {e}")
                translated_text, out_image = None, None
            if out_image:
                if self.config.get("show_usage") and translated_text:
                    yield event.plain_result(translated_text)
                yield event.image_result(out_image)
                return
            if translated_text:
                yield event.plain_result(translated_text)
                return

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

    # ---------------- 语音 ----------------
    async def _collect_voices(self, event: AstrMessageEvent) -> list[str]:
        out: list[str] = []
        for comp in event.message_obj.message:
            if isinstance(comp, Reply):
                for sub in (getattr(comp, "chain", None) or []):
                    if isinstance(sub, Record):
                        try:
                            out.append(await sub.convert_to_file_path())
                        except Exception as e:
                            logger.warning(f"[IndexTranslate] 引用语音取不到: {e}")
            elif isinstance(comp, Record):
                try:
                    out.append(await comp.convert_to_file_path())
                except Exception as e:
                    logger.warning(f"[IndexTranslate] 语音取不到: {e}")
        return out

    async def _voice_flow(self, event: AstrMessageEvent, voices: list[str], target: str):
        try:
            stt = await self.context.get_using_stt_provider_async(event.unified_msg_origin)
        except Exception as e:
            logger.error(f"[IndexTranslate] 取 STT provider 失败: {e}")
            stt = None
        if not stt:
            yield event.plain_result("没配语音转文字（STT）模型，这段语音我听不出来")
            return

        try:
            heard = (await stt.get_text(audio_url=voices[0])) or ""
        except Exception as e:
            logger.error(f"[IndexTranslate] 语音转文字失败: {e}")
            yield event.plain_result(f"语音转文字失败了：{e}")
            return
        heard = heard.strip()
        if not heard:
            yield event.plain_result("没听清，换一段试试")
            return

        try:
            translated = await self._translate(heard, target, None)
        except Exception as e:
            logger.error(f"[IndexTranslate] 语音翻译失败: {e}")
            yield event.plain_result(f"翻不动了：{e}")
            return
        if not translated.strip():
            yield event.plain_result("模型没吐译文出来，换个短点的试试")
            return

        yield event.plain_result(translated)

        if not self.config.get("voice_reply", True):
            return
        try:
            tts = await self.context.get_using_tts_provider_async(event.unified_msg_origin)
        except Exception as e:
            logger.warning(f"[IndexTranslate] 取 TTS provider 失败: {e}")
            return
        if not tts:
            return
        try:
            audio = await tts.get_audio(translated)
        except Exception as e:
            logger.warning(f"[IndexTranslate] TTS 合成失败: {e}")
            return
        if audio:
            yield event.chain_result([Record(file=audio)])

    # ---------------- 纯文本 / 多模态 ----------------
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

    # ---------------- 图片回填 ----------------
    async def _translate_image_render(
        self, image_path: str, target: str, hint: str
    ) -> tuple[str | None, str | None]:
        blocks, plain = await self._ocr(image_path)
        if not blocks:
            # 没坐标就只剩文字了：直接拿 OCR 文本走文本翻译，别硬塞回图里
            if plain:
                return await self._translate(plain, target, None), None
            return None, None
        texts = await self._translate_batch([b["text"] for b in blocks], target)
        out_image = await asyncio.to_thread(self._render_image, image_path, blocks, texts)
        return "\n".join(t for t in texts if t), out_image

    def _ocr_ready(self) -> bool:
        """判一下当前配置下的 OCR 是不是可用。"""
        provider = str(self.config.get("ocr_provider") or "cnocr").lower().strip()
        if provider in ("glm", "bigmodel", "zhipu"):
            return bool(str(self.config.get("ocr_api_key") or "").strip())
        return bool(str(self.config.get("ocr_api_base") or "").strip())

    async def _ocr(self, image_path: str) -> list[dict]:
        provider = str(self.config.get("ocr_provider") or "cnocr").lower().strip()
        timeout = int(self.config.get("ocr_timeout") or 60)
        raw = Path(image_path).read_bytes()
        filename = Path(image_path).name or "image.png"

        if provider in ("glm", "bigmodel", "zhipu"):
            # 智谱 GLM-OCR：POST /api/paas/v4/layout_parsing，Authorization 直接塞 key
            base = str(
                self.config.get("ocr_api_base") or "https://open.bigmodel.cn/api/paas/v4"
            ).strip().rstrip("/")
            key = str(self.config.get("ocr_api_key") or "").strip()
            if not key:
                raise RuntimeError("选了 GLM-OCR，但没填 ocr_api_key")
            async with httpx.AsyncClient(timeout=timeout, trust_env=True) as client:
                r = await client.post(
                    f"{base}/layout_parsing",
                    headers={"Authorization": key, "Content-Type": "application/json"},
                    json={"model": "glm-ocr", "file": to_data_url(image_path)},
                )
            if r.status_code != 200:
                raise RuntimeError(f"GLM-OCR HTTP {r.status_code}：{r.text[:200]}")
        else:
            base = str(self.config.get("ocr_api_base") or "").strip().rstrip("/")
            path = str(self.config.get("ocr_api_path") or "/ocr").strip()
            if not path.startswith("/"):
                path = "/" + path
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                r = await client.post(
                    f"{base}{path}",
                    files={"image": (filename, raw, "application/octet-stream")},
                )
            if r.status_code != 200:
                raise RuntimeError(f"OCR HTTP {r.status_code}：{r.text[:200]}")

        blocks = self._collect_blocks(r.json())
        plain = self._extract_plain(r.json())
        if not blocks:
            logger.warning(
                f"[IndexTranslate] OCR 没解析出坐标，原始返回片段：{r.text[:300]}"
            )
            return [], plain

        # GLM 的 bbox_2d 是 0~1 的相对坐标，得按图片尺寸换算成像素
        try:
            from PIL import Image as _Image

            with _Image.open(image_path) as im:
                img_w, img_h = im.size
        except Exception:
            img_w = img_h = 0
        if img_w and img_h:
            for b in blocks:
                x0, y0, x1, y1 = b["box"]
                if max(abs(x0), abs(y0), abs(x1), abs(y1)) <= 1.5:
                    b["box"] = (x0 * img_w, y0 * img_h, x1 * img_w, y1 * img_h)

        blocks.sort(key=lambda b: (round(b["box"][1] / 10), b["box"][0]))
        return blocks, plain

    @staticmethod
    def _extract_plain(payload) -> str:
        """有些 OCR（比如 GLM 的文档解析）会额外给一份整体文本，留着兜底用。"""
        if not isinstance(payload, dict):
            return ""
        for key in ("md_results", "text", "content", "markdown"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    @staticmethod
    def _normalize_boxes(blocks: list[dict], image_path: str) -> list[dict]:
        """坐标可能是 0~1 归一化，也可能是像素，按最大值判断后换算。"""
        try:
            from PIL import Image as _Image

            with _Image.open(image_path) as im:
                img_w, img_h = im.size
        except Exception:
            return blocks
        if not blocks or not img_w or not img_h:
            return blocks
        for b in blocks:
            x0, y0, x1, y1 = b["box"]
            if max(abs(x0), abs(y0), abs(x1), abs(y1)) <= 1.5:
                b["box"] = (x0 * img_w, y0 * img_h, x1 * img_w, y1 * img_h)
        return blocks


    @classmethod
    def _collect_blocks(cls, obj, out: list | None = None) -> list[dict]:
        """两种 OCR 返回结构差挺多，索性递归找「有文本 + 有框」的节点。"""
        if out is None:
            out = []
        if isinstance(obj, dict):
            text = obj.get("text") or obj.get("content")
            box = (
                obj.get("position")
                or obj.get("bbox_2d")
                or obj.get("bbox")
                or obj.get("box")
            )
            if isinstance(text, str) and text.strip() and box:
                rect = cls._to_box(box)
                if rect:
                    out.append({"text": text.strip(), "box": rect})
            for v in obj.values():
                cls._collect_blocks(v, out)
        elif isinstance(obj, list):
            for v in obj:
                cls._collect_blocks(v, out)
        return out

    @staticmethod
    def _to_box(box) -> tuple | None:
        nums: list[float] = []
        for item in box if isinstance(box, (list, tuple)) else []:
            if isinstance(item, (list, tuple)):
                if len(item) >= 2:
                    nums.extend([float(item[0]), float(item[1])])
            elif isinstance(item, (int, float)):
                nums.append(float(item))
        if len(nums) < 4:
            return None
        xs, ys = nums[0::2], nums[1::2]
        return (min(xs), min(ys), max(xs), max(ys))

    async def _translate_batch(self, texts: list[str], target: str) -> list[str]:
        base = str(self.config.get("api_base") or DEFAULT_API_BASE).rstrip("/")
        model = str(self.config.get("model") or DEFAULT_MODEL)
        max_tokens = max(int(self.config.get("max_tokens") or 1024), 1024)
        timeout = int(self.config.get("timeout") or 90)

        if len(texts) == 1:
            return [await self._post(base, model, build_prompt(texts[0], target), max_tokens, timeout)]

        payload_in = {str(i + 1): t for i, t in enumerate(texts)}
        raw = await self._post(base, model, build_json_prompt(payload_in, target), max_tokens, timeout)
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[-1] if cleaned.count("```") >= 2 else cleaned.strip("`")
        start, end = cleaned.find("{"), cleaned.rfind("}")
        parsed: dict = {}
        if start >= 0 and end > start:
            try:
                parsed = json.loads(cleaned[start:end + 1])
            except Exception:
                parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}

        out: list[str] = []
        for i, original in enumerate(texts):
            val = parsed.get(str(i + 1))
            out.append(str(val).strip() if val else "")
        # 有漏的直接逐块补翻，宁可慢也不能漏
        for i, val in enumerate(out):
            if not val:
                out[i] = await self._post(base, model, build_prompt(texts[i], target), max_tokens, timeout)
        return out

    def _render_image(self, image_path: str, blocks: list[dict], texts: list[str]) -> str:
        from PIL import Image, ImageDraw

        font_path = str(self.config.get("render_font") or DEFAULT_FONT)
        img = Image.open(image_path).convert("RGB")
        draw = ImageDraw.Draw(img)
        width, height = img.size

        for block, new_text in zip(blocks, texts):
            if not new_text.strip():
                continue
            x0, y0, x1, y1 = [int(v) for v in block["box"]]
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(width, x1), min(height, y1)
            if x1 - x0 < 4 or y1 - y0 < 4:
                continue
            bg = self._sample_bg(img, (x0, y0, x1, y1))
            draw.rectangle([x0, y0, x1, y1], fill=bg)
            lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
            color = (17, 17, 17) if lum > 140 else (245, 245, 245)
            self._draw_fit(draw, new_text, (x0, y0, x1, y1), font_path, color)

        out_path = str(Path(image_path).with_name(Path(image_path).stem + "_translated.png"))
        img.save(out_path, "PNG")
        return out_path

    @staticmethod
    def _sample_bg(img, box):
        """在框外一圈采样，取中位亮度那个颜色当背景，擦除后不至于突兀。"""
        x0, y0, x1, y1 = box
        pts = []
        step_x = max(1, (x1 - x0) // 12)
        step_y = max(1, (y1 - y0) // 12)
        for x in range(x0, x1, step_x):
            for y in (max(0, y0 - 2), min(img.size[1] - 1, y1 + 2)):
                pts.append(img.getpixel((x, y)))
        for y in range(y0, y1, step_y):
            for x in (max(0, x0 - 2), min(img.size[0] - 1, x1 + 2)):
                pts.append(img.getpixel((x, y)))
        if not pts:
            return (255, 255, 255)
        pts.sort(key=lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2])
        return pts[len(pts) // 2]

    @staticmethod
    def _draw_fit(draw, text: str, box, font_path: str, color):
        """在框内自动折行 + 缩字号，塞不进去就继续缩。"""
        from PIL import ImageFont

        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        # textlength 量不了含换行的串，先按原文换行拆段，段内再按宽度折
        paragraphs = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if not paragraphs:
            paragraphs = [text or ""]

        size = max(8, int(bh * 0.92))
        while size >= 8:
            try:
                font = ImageFont.truetype(font_path, size)
            except Exception:
                return
            lines: list[str] = []
            for para in paragraphs:
                cur = ""
                for ch in para:
                    try:
                        fits = draw.textlength(cur + ch, font=font) <= bw - 2
                    except Exception:
                        fits = True
                    if fits:
                        cur += ch
                    else:
                        if cur:
                            lines.append(cur)
                        cur = ch
                lines.append(cur)
            line_h = size + 3
            if len(lines) * line_h <= bh + 2:
                y = y0 + max(0, (bh - len(lines) * line_h) // 2)
                for line in lines:
                    if line:
                        draw.text((x0 + 1, y), line, font=font, fill=color)
                    y += line_h
                return
            size = int(size * 0.85)
