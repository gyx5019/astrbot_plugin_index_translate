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
    LOG_PATH = Path(__file__).resolve().parent / "data" / "plugin.log"

    def _plog(self, msg: str) -> None:
        """写一行插件自己的日志，AstrBot 的日志文件没开，靠这个事后查。"""
        try:
            p = self.LOG_PATH
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
        self._plog(f"收到指令 raw={raw[:40]!r} 参数={payload[:40]!r}")

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

    # ---------------- 语音 ----------------
    async def _collect_voices(self, event: AstrMessageEvent) -> list[str]:
        out: list[str] = []
        for comp in event.message_obj.message:
            if isinstance(comp, Reply):
                got = False
                for sub in (getattr(comp, "chain", None) or []):
                    if isinstance(sub, Record):
                        try:
                            out.append(await sub.convert_to_file_path())
                            got = True
                        except Exception as e:
                            logger.warning(f"[IndexTranslate] 引用语音取不到: {e}")
                    elif isinstance(sub, File):
                        name = str(getattr(sub, "name", "") or "")
                        if Path(name).suffix.lower() in AUDIO_EXTS:
                            try:
                                out.append(await sub.get_file())
                                got = True
                            except Exception as e:
                                logger.warning(f"[IndexTranslate] 引用音频取不到: {e}")
                if not got:
                    # 引用链里没有语音段，按消息 id 回捞一次
                    ref = await self._fetch_quoted_voice(event, getattr(comp, "id", None))
                    if ref:
                        out.append(ref)
            elif isinstance(comp, Record):
                try:
                    out.append(await comp.convert_to_file_path())
                except Exception as e:
                    logger.warning(f"[IndexTranslate] 语音取不到: {e}")
            elif isinstance(comp, File):
                # 音频文件也算，后缀对得上才收
                name = str(getattr(comp, "name", "") or "")
                if Path(name).suffix.lower() in AUDIO_EXTS:
                    try:
                        out.append(await comp.get_file())
                    except Exception as e:
                        logger.warning(f"[IndexTranslate] 音频文件取不到: {e}")
        return out

    async def _fetch_quoted_voice(self, event: AstrMessageEvent, message_id) -> str | None:
        """引用消息里的语音经常不在 chain 里，调 get_msg 把它捞出来。"""
        if not message_id:
            return None
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            logger.warning("[IndexTranslate] 这个平台拿不到 call_action，无法回捞引用语音")
            return None
        try:
            ret = await call_action("get_msg", message_id=message_id)
        except Exception as e:
            logger.warning(f"[IndexTranslate] get_msg 失败: {e}")
            return None
        data = ret.get("data") if isinstance(ret, dict) else None
        if not isinstance(data, dict):
            data = ret if isinstance(ret, dict) else {}
        segments = data.get("message") or data.get("messages") or []
        if isinstance(segments, str):
            return None
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if str(seg.get("type")) in ("record", "voice", "audio"):
                payload = seg.get("data") or {}
                ref = payload.get("url") or payload.get("file") or payload.get("path")
                if ref:
                    logger.info(f"[IndexTranslate] 从 get_msg 捞到引用语音：{str(ref)[:80]}")
                    return str(ref)
        return None

    @staticmethod
    async def _audio_seconds(path: str) -> float | None:
        """用 ffprobe 读音频时长（秒），读不到就返回 None。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            val = (out or b"").decode(errors="ignore").strip()
            return float(val) if val else None
        except Exception as e:
            logger.warning(f"[IndexTranslate] 读音频时长失败: {e}")
            return None

    async def _asr(self, audio_path: str, event=None) -> str:
        """先走插件自己配的 ASR 网关；没配就退回 AstrBot 的 STT provider。"""
        # 引用语音回捞到的是 URL，先落到本地
        if str(audio_path).startswith(("http://", "https://")):
            async with httpx.AsyncClient(timeout=60, trust_env=True) as c:
                r = await c.get(audio_path)
            if r.status_code != 200:
                raise RuntimeError(f"下载语音失败 HTTP {r.status_code}")
            ct = (r.headers.get("content-type") or "").lower()
            suffix = ".wav"
            if "mpeg" in ct or "mp3" in ct:
                suffix = ".mp3"
            elif "amr" in ct:
                suffix = ".amr"
            elif "ogg" in ct:
                suffix = ".ogg"
            local = Path("/tmp") / f"it_voice_{int(time.time() * 1000)}{suffix}"
            local.write_bytes(r.content)
            audio_path = str(local)
        limit = int(self.config.get("voice_max_seconds") or 0)
        if limit > 0:
            secs = await self._audio_seconds(audio_path)
            if secs and secs > limit:
                self._plog(f"音频过长：{secs:.0f}s > {limit}s")
                raise AudioTooLong(
                    f"音频文件过长（{int(secs // 60)} 分 {int(secs % 60)} 秒），"
                    f"上限 {limit // 60} 分钟"
                )

        base = str(self.config.get("asr_api_base") or "").strip().rstrip("/")
        key = str(self.config.get("asr_api_key") or "").strip()
        model = str(self.config.get("asr_model") or "qwen3-asr-flash")
        if base and key:
            data = Path(audio_path).read_bytes()
            mime = {
                ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                ".ogg": "audio/ogg", ".amr": "audio/amr", ".silk": "audio/silk",
                ".flac": "audio/flac",
            }.get(Path(audio_path).suffix.lower(), "audio/wav")
            data_uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            async with httpx.AsyncClient(timeout=120, trust_env=True) as client:
                r = await client.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
                            }
                        ],
                        "stream": False,
                        "asr_options": {"enable_itn": False},
                    },
                )
            if r.status_code != 200:
                raise RuntimeError(f"ASR HTTP {r.status_code}：{r.text[:200]}")
            return ((r.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""

        stt = None
        if event is not None:
            try:
                stt = await self.context.get_using_stt_provider_async(event.unified_msg_origin)
            except Exception as e:
                logger.error(f"[IndexTranslate] 取 STT provider 失败: {e}")
                stt = None
        if stt:
            return (await stt.get_text(audio_url=audio_path)) or ""
        return ""

    async def _voice_flow(self, event: AstrMessageEvent, voices: list[str], target: str):
        try:
            heard = await self._asr(voices[0], event)
        except AudioTooLong as e:
            yield event.plain_result(str(e))
            return
        except Exception as e:
            logger.error(f"[IndexTranslate] 语音转文字失败: {e}")
            yield event.plain_result(f"语音转文字失败了：{e}")
            return
        heard = heard.strip()
        self._plog(f"ASR 结果={heard[:80]!r}")
        if not heard:
            yield event.plain_result("没听清，换一段试试")
            return

        try:
            translated = await self._translate(heard, target, None)
        except Exception as e:
            logger.error(f"[IndexTranslate] 语音翻译失败: {e}")
            yield event.plain_result(f"翻不动了：{e}")
            return
        self._plog(f"语音译文={translated[:80]!r} target={target}")
        if not translated.strip():
            yield event.plain_result("模型没吐译文出来，换个短点的试试")
            return

        # voice_reply 关着 → 只发文字；开着 → 只发语音（TTS 失败才退回文字）
        if not self.config.get("voice_reply", True):
            self._plog("voice_reply 关闭，只发文字")
            yield event.plain_result(translated)
            return

        note, audio = "", None
        if True:
            try:
                tts = await self.context.get_using_tts_provider_async(event.unified_msg_origin)
                if not tts:
                    # 全局 TTS 开关关着时上面会直接返回 None，这里绕过开关，直接从已加载实例里挑
                    pm = getattr(self.context, "provider_manager", None)
                    insts = getattr(pm, "tts_provider_insts", None) or []
                    tts = insts[0] if insts else None
                if not tts:
                    note = "\n[调试] 没取到 TTS provider（已加载的 TTS 实例也是空的）"
                else:
                    audio = await tts.get_audio(translated)
                    if not audio:
                        note = "\n[调试] TTS 返回了空"
                    elif not Path(str(audio)).exists():
                        note = f"\n[调试] TTS 给的路径不存在：{audio}"
            except Exception as e:
                note = f"\n[调试] TTS 出错：{type(e).__name__}: {e}"

        # 有音频就只发语音；TTS 出岔子才退回文字，并把原因一起给出来
        if audio and Path(str(audio)).exists():
            self._plog(f"发送：仅语音 {audio}")
            yield event.chain_result([Record(file=audio)])
        else:
            self._plog(f"发送：文字（无语音）note={note!r}")
            yield event.plain_result(translated + note)

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
            self._plog(f"翻译失败 HTTP {r.status_code}：{r.text[:160]}")
            raise RuntimeError(f"HTTP {r.status_code}：{r.text[:160]}")
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"返回里没有 choices：{str(data)[:160]}")
        return strip_think(choices[0].get("message", {}).get("content") or "")

