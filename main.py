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
import time
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Plain, Record, Reply
from astrbot.api.star import Context, Star

from .ocr import OcrMixin
from .voice import AUDIO_EXTS, AudioTooLong, VoiceMixin

from .helpers import (
    DEFAULT_API_BASE,
    DEFAULT_FONT,
    DEFAULT_MODEL,
    LANG_ALIAS,
    LANG_NAMES,
    TARGET_PREFIX,
    TRIGGER,
    build_image_prompt,
    build_json_prompt,
    build_prompt,
    lang_name,
    split_text,
    strip_think,
    to_data_url,
)


class IndexTranslatePlugin(OcrMixin, VoiceMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        logger.info(
            f"[IndexTranslate] 已加载，模型 {self.config.get('model') or DEFAULT_MODEL}，"
            f"默认目标语言 {self.config.get('target_lang') or 'en'}，"
            f"图片回填 {'开' if self.config.get('image_render') else '关'}"
        )

    # ---------------- 日志 ----------------
    def _log_path(self) -> Path:
        """按 AstrBot 规范落到 data/plugin_data/<插件名>/ 下，别写插件自身目录。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            base = Path(get_astrbot_plugin_data_path())
        except Exception:
            base = Path("/AstrBot/data/plugin_data")
        return base / self.name / "plugin.log"

    def _plog(self, msg: str) -> None:
        """写一行运行日志。只记长度/状态/结果，不记录用户内容原文。"""
        try:
            p = self._log_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > 2 * 1024 * 1024:
                p.replace(p.with_name("plugin.log.1"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass

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
        self._plog(f"收到指令 原文{len(raw)}字 参数{len(payload)}字")

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
        if not text and not images and not voices:
            event.stop_event()
            dbg = ""
            try:
                comps = [type(c).__name__ for c in event.message_obj.message]
                chain = []
                for c in event.message_obj.message:
                    if isinstance(c, Reply):
                        chain = [type(s).__name__ for s in (getattr(c, "chain", None) or [])]
                        break
                dbg = (
                    f"\n（调试：消息组件={comps}｜引用链={chain}"
                    f"｜取自消息的语音={len(voices)}）"
                )
            except Exception:
                dbg = ""
            yield event.plain_result(
                "要翻什么？发「翻译 内容」，或者引用一条消息/图片再发「翻译」。\n"
                "也能指定语言：「翻译成日语 xxx」" + dbg
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
            self._plog(f"翻译失败 HTTP {r.status_code}")
            raise RuntimeError(f"HTTP {r.status_code}：{r.text[:160]}")
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"返回里没有 choices：{str(data)[:160]}")
        return strip_think(choices[0].get("message", {}).get("content") or "")

