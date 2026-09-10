"""Расшифровка голосовых сообщений Telegram через OpenAI (gpt-transcribe).

Telegram отдаёт голос в OGG/Opus, который API не всегда корректно принимает
по имени/расширению — конвертируем в mp3 через ffmpeg, чтобы не гадать.
"""
import asyncio

import httpx

import tg
from config import OPENAI_API_KEY, TRANSCRIBE_MODEL

API_URL = "https://api.openai.com/v1/audio/transcriptions"


async def _to_mp3(data: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-vn", "-ac", "1", "-ar", "16000", "-f", "mp3", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(data)
    if proc.returncode != 0 or not out:
        raise RuntimeError(f"ffmpeg: {err.decode('utf-8', 'replace')[-300:]}")
    return out


async def transcribe(file_id: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан — распознавание голоса выключено")
    raw = await tg.get_file_bytes(file_id)
    mp3 = await _to_mp3(raw)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            API_URL,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            data={"model": TRANSCRIBE_MODEL},
            files={"file": ("audio.mp3", mp3, "audio/mpeg")},
        )
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()
