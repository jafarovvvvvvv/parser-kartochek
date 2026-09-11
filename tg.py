import asyncio

import httpx

from config import BOT_TOKEN

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Лимит Telegram на подпись к фото (sendPhoto/sendMediaGroup) — длиннее не
# примет вовсе, а не обрежет сам.
CAPTION_LIMIT = 1024


class TelegramError(RuntimeError):
    """Ошибка со стороны Telegram (в т.ч. сломанная разметка)."""


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    """Собрать inline-клавиатуру из строк [("Текст", "callback:data")]."""
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def escape(text: str) -> str:
    # & — первым, иначе он же экранирует только что вставленные &lt;/&gt;.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate_caption(caption: str) -> str:
    if len(caption) <= CAPTION_LIMIT:
        return caption
    # Обрезаем по границе строки, чтобы не резать цену/дефект посередине слова.
    cut = caption[:CAPTION_LIMIT - 1]
    boundary = cut.rfind("\n")
    if boundary > 0:
        cut = cut[:boundary]
    return cut.rstrip() + "…"


async def call(method: str, **params) -> dict:
    # Параметр со значением None нужно не передавать вовсе, а не слать null:
    # на {"parse_mode": null} Telegram отвечает 400 "unsupported parse_mode"
    # (поймано на живой отправке находок уценки).
    params = {key: value for key, value in params.items() if value is not None}
    for attempt in range(2):  # не больше одного повтора на 429
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{API_URL}/{method}", json=params)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if data.get("ok"):
            return data["result"]
        if data.get("error_code") == 429 and attempt == 0:
            # Превышен лимит частоты — Telegram сам говорит, сколько ждать.
            retry_after = (data.get("parameters") or {}).get("retry_after", 1)
            await asyncio.sleep(retry_after)
            continue
        # Раньше здесь стоял raise_for_status(), и на любой отказ Telegram
        # (400 — это и «сломана разметка») летел httpx.HTTPStatusError, а
        # send_message ловил только RuntimeError — фолбэк на текст без разметки
        # не срабатывал никогда, сообщение просто терялось. Плюс httpx кладёт в
        # текст ошибки URL, а в нём токен бота — в логи ему не надо.
        raise TelegramError(
            f"{method}: {resp.status_code} {data.get('description') or 'нераспознанный ответ'}"
        )


async def send_message(chat_id: int, text: str, *, buttons: dict | None = None,
                        parse_mode: str | None = "HTML", disable_preview: bool = True,
                        reply_to: int | None = None) -> dict:
    try:
        return await call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=buttons,
            disable_web_page_preview=disable_preview,
            reply_to_message_id=reply_to,
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
            reply_markup=buttons,
            disable_web_page_preview=disable_preview,
            reply_to_message_id=reply_to,
        )


async def send_photo(chat_id: int, photo: str, *, caption: str | None = None,
                      buttons: dict | None = None, parse_mode: str | None = "HTML") -> dict:
    if caption is not None:
        caption = _truncate_caption(caption)
    # Если Telegram не смог скачать фото по URL, call() поднимет TelegramError
    # с описанием ("failed to get HTTP URL content" / "wrong file identifier") —
    # даём этой ошибке дойти до вызывающего кода как есть, без падения процесса.
    return await call(
        "sendPhoto",
        chat_id=chat_id,
        photo=photo,
        caption=caption,
        parse_mode=parse_mode,
        reply_markup=buttons,
    )


async def send_photo_group(chat_id: int, photos: list[str], *,
                            caption: str | None = None) -> list[dict]:
    if len(photos) == 1:
        return [await send_photo(chat_id, photos[0], caption=caption)]
    # У альбома (sendMediaGroup) нет reply_markup — Telegram его не поддерживает
    # для медиагрупп, поэтому кнопок здесь не бывает.
    media = [{"type": "photo", "media": url} for url in photos]
    if caption is not None:
        # Подпись Telegram показывает одну на всю группу — кладём её только в
        # первый элемент, у остальных это поле игнорируется.
        media[0]["caption"] = _truncate_caption(caption)
    return await call("sendMediaGroup", chat_id=chat_id, media=media)


async def edit_message_text(chat_id: int, message_id: int, text: str, *,
                             buttons: dict | None = None,
                             parse_mode: str | None = "HTML") -> dict:
    return await call(
        "editMessageText",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        parse_mode=parse_mode,
        reply_markup=buttons,
    )


async def edit_message_buttons(chat_id: int, message_id: int, buttons: dict | None) -> dict:
    # buttons=None — это просьба убрать клавиатуру, а не "не менять": call()
    # выкидывает параметры со значением None, поэтому None здесь превращаем в
    # пустую клавиатуру явно, иначе кнопки останутся висеть как были.
    return await call(
        "editMessageReplyMarkup",
        chat_id=chat_id,
        message_id=message_id,
        reply_markup=buttons if buttons is not None else {"inline_keyboard": []},
    )


async def answer_callback(callback_query_id: str, text: str | None = None, *,
                           alert: bool = False) -> dict:
    return await call(
        "answerCallbackQuery",
        callback_query_id=callback_query_id,
        text=text,
        show_alert=alert,
    )


async def set_my_commands(commands: list[tuple[str, str]]) -> dict:
    return await call(
        "setMyCommands",
        commands=[{"command": cmd, "description": desc} for cmd, desc in commands],
    )


async def send_chat_action(chat_id: int, action: str = "typing") -> dict:
    return await call("sendChatAction", chat_id=chat_id, action=action)


async def delete_message(chat_id: int, message_id: int) -> bool:
    try:
        return bool(await call("deleteMessage", chat_id=chat_id, message_id=message_id))
    except TelegramError:
        # Сообщение уже удалено или старше ~48ч (Telegram такое не даёт удалить) —
        # вызывающему коду это не повод падать.
        return False


async def get_updates(offset: int | None = None, timeout: int = 25) -> list[dict]:
    params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
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
