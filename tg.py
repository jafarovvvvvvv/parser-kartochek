import httpx

from config import BOT_TOKEN

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


class TelegramError(RuntimeError):
    """Ошибка со стороны Telegram (в т.ч. сломанная разметка)."""


async def call(method: str, **params) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{API_URL}/{method}", json=params)
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if not data.get("ok"):
        # Раньше здесь стоял raise_for_status(), и на любой отказ Telegram
        # (400 — это и «сломана разметка») летел httpx.HTTPStatusError, а
        # send_message ловил только RuntimeError — фолбэк на текст без разметки
        # не срабатывал никогда, сообщение просто терялось. Плюс httpx кладёт в
        # текст ошибки URL, а в нём токен бота — в логи ему не надо.
        raise TelegramError(
            f"{method}: {resp.status_code} {data.get('description') or 'нераспознанный ответ'}"
        )
    return data["result"]


async def send_message(chat_id: int, text: str, disable_preview: bool = True,
                        parse_mode: str | None = "Markdown") -> dict:
    try:
        return await call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_preview,
        )
    except TelegramError:
        if parse_mode is None:
            raise
        # Текст (в т.ч. сгенерированный ИИ или взятый из описания уценки) мог
        # сломать разметку — лучше отправить как есть, чем не отправить вовсе.
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
    if resp.status_code != 200:
        # Не raise_for_status: httpx положит в текст ошибки URL, а в нём токен.
        raise TelegramError(f"скачивание файла: {resp.status_code}")
    return resp.content
