import httpx

from config import BOT_TOKEN

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def call(method: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{API_URL}/{method}", json=params)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error on {method}: {data}")
        return data["result"]


async def send_message(chat_id: int, text: str, disable_preview: bool = True) -> dict:
    try:
        return await call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            disable_web_page_preview=disable_preview,
        )
    except RuntimeError:
        # Текст (в т.ч. сгенерированный ИИ) мог сломать разметку Markdown —
        # лучше отправить как есть без форматирования, чем не отправить вовсе.
        return await call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            disable_web_page_preview=disable_preview,
        )


async def get_updates(offset: int | None = None, timeout: int = 25) -> list[dict]:
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    return await call("getUpdates", **params)


async def get_file_bytes(file_id: str) -> bytes:
    info = await call("getFile", file_id=file_id)
    path = info["file_path"]
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}")
        resp.raise_for_status()
        return resp.content
