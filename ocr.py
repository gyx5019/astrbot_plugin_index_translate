"""OCR 与图片回填：识别文字坐标 → 擦原文 → 写译文 → 出图。

支持 cnocr（自建 FastAPI）和 glm（智谱 GLM-OCR）两种来源。
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

import httpx

from astrbot.api import logger

from .helpers import (
    DEFAULT_API_BASE,
    DEFAULT_MODEL,
    build_image_prompt,
    build_json_prompt,
    build_prompt,
    lang_name,
    split_text,
    to_data_url,
)


class OcrMixin:
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
