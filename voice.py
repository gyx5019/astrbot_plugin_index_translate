"""语音 / 音频翻译：ASR → 翻译 → TTS。"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Record, Reply

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".amr", ".ogg", ".flac", ".silk", ".opus", ".aac"}


class AudioTooLong(Exception):
    """音频超过设定时长。"""


class VoiceMixin:
    # ---------------- 语音 ----------------
    async def _collect_voices(self, event: AstrMessageEvent) -> list[str]:
        out: list[str] = []
        for comp in event.message_obj.message:
            if isinstance(comp, Reply):
                got = False
                for sub in getattr(comp, "chain", None) or []:
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
                    ref = await self._fetch_quoted_voice(
                        event, getattr(comp, "id", None)
                    )
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

    async def _fetch_quoted_voice(
        self, event: AstrMessageEvent, message_id
    ) -> str | None:
        """引用消息里的语音经常不在 chain 里，调 get_msg 把它捞出来。"""
        if not message_id:
            return None
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if not callable(call_action):
            logger.warning(
                "[IndexTranslate] 这个平台拿不到 call_action，无法回捞引用语音"
            )
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
                    logger.info(
                        f"[IndexTranslate] 从 get_msg 捞到引用语音：{str(ref)[:80]}"
                    )
                    return str(ref)
        return None

    @staticmethod
    async def _audio_seconds(path: str) -> float | None:
        """用 ffprobe 读音频时长（秒），读不到就返回 None。"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
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
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".m4a": "audio/mp4",
                ".ogg": "audio/ogg",
                ".amr": "audio/amr",
                ".silk": "audio/silk",
                ".flac": "audio/flac",
            }.get(Path(audio_path).suffix.lower(), "audio/wav")
            data_uri = f"data:{mime};base64,{base64.b64encode(data).decode()}"
            async with httpx.AsyncClient(timeout=120, trust_env=True) as client:
                r = await client.post(
                    f"{base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_audio",
                                        "input_audio": {"data": data_uri},
                                    }
                                ],
                            }
                        ],
                        "stream": False,
                        "asr_options": {"enable_itn": False},
                    },
                )
            if r.status_code != 200:
                raise RuntimeError(f"ASR HTTP {r.status_code}：{r.text[:200]}")
            return ((r.json().get("choices") or [{}])[0].get("message") or {}).get(
                "content"
            ) or ""

        stt = None
        if event is not None:
            try:
                stt = await self.context.get_using_stt_provider_async(
                    event.unified_msg_origin
                )
            except Exception as e:
                logger.error(f"[IndexTranslate] 取 STT provider 失败: {e}")
                stt = None
        if stt:
            return (await stt.get_text(audio_url=audio_path)) or ""
        return ""

    async def _voice_flow(
        self, event: AstrMessageEvent, voices: list[str], target: str
    ):
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
        self._plog(f"ASR 命中 {len(heard)} 字")
        if not heard:
            yield event.plain_result("没听清，换一段试试")
            return

        try:
            translated = await self._translate(heard, target, None)
        except Exception as e:
            logger.error(f"[IndexTranslate] 语音翻译失败: {e}")
            yield event.plain_result(f"翻不动了：{e}")
            return
        self._plog(f"译文 {len(translated)} 字 target={target}")
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
                tts = await self.context.get_using_tts_provider_async(
                    event.unified_msg_origin
                )
                if not tts:
                    # 全局 TTS 开关关着时上面会直接返回 None，这里绕过开关，直接从已加载实例里挑
                    pm = getattr(self.context, "provider_manager", None)
                    insts = getattr(pm, "tts_provider_insts", None) or []
                    tts = insts[0] if insts else None
                if not tts:
                    note = "\n（暂时没有可用的 TTS 模型，先发文字）"
                else:
                    audio = await tts.get_audio(translated)
                    if not audio:
                        note = "\n（TTS 没返回音频，先发文字）"
                    elif not Path(str(audio)).exists():
                        note = "\n（TTS 生成的音频找不到，先发文字）"
            except Exception:
                note = "\n（TTS 出了点问题，先发文字）"

        # 有音频就只发语音；TTS 出岔子才退回文字，并把原因一起给出来
        if audio and Path(str(audio)).exists():
            self._plog("发送：仅语音")
            yield event.chain_result([Record(file=audio)])
        else:
            self._plog("发送：仅文字（附原因）")
            yield event.plain_result(translated + note)
