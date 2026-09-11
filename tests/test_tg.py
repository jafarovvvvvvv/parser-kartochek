"""tg.call: параметры со значением None не должны попадать в payload (Telegram
отвечает 400 unsupported parse_mode на null), и на 429 должен быть ровно один
повтор с паузой retry_after, не больше. Сеть не трогаем — httpx.AsyncClient
подменяется фейком, реальных запросов к Telegram не происходит."""
import asyncio

import pytest

import tg


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _RecordingClient:
    """Отвечает одной и той же полезной нагрузкой на каждый вызов post()."""

    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.calls.append(json)
        return _FakeResponse(self.payload, self.status_code)


class _SequenceClient:
    """Отвечает по очереди разными полезными нагрузками — для теста повтора."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.calls.append(json)
        payload, status = self.responses[len(self.calls) - 1]
        return _FakeResponse(payload, status)


def test_call_не_отправляет_параметры_со_значением_none(monkeypatch):
    client = _RecordingClient({"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr(tg.httpx, "AsyncClient", client)

    result = asyncio.run(tg.call(
        "sendMessage", chat_id=123, text="hi", parse_mode=None,
        reply_markup=None, reply_to_message_id=None, disable_web_page_preview=True,
    ))

    assert result == {"message_id": 1}
    sent = client.calls[0]
    assert "parse_mode" not in sent
    assert "reply_markup" not in sent
    assert "reply_to_message_id" not in sent
    assert sent == {"chat_id": 123, "text": "hi", "disable_web_page_preview": True}


def test_send_message_с_parse_mode_none_тоже_не_шлёт_null(monkeypatch):
    client = _RecordingClient({"ok": True, "result": {"message_id": 2}})
    monkeypatch.setattr(tg.httpx, "AsyncClient", client)

    asyncio.run(tg.send_message(1, "текст без разметки", parse_mode=None))

    sent = client.calls[0]
    assert "parse_mode" not in sent
    assert sent["text"] == "текст без разметки"


def test_call_повторяет_ровно_один_раз_на_429_и_потом_успевает(monkeypatch):
    client = _SequenceClient([
        ({"ok": False, "error_code": 429, "parameters": {"retry_after": 0}}, 429),
        ({"ok": True, "result": {"message_id": 3}}, 200),
    ])
    monkeypatch.setattr(tg.httpx, "AsyncClient", client)

    result = asyncio.run(tg.call("sendMessage", chat_id=1, text="hi"))

    assert result == {"message_id": 3}
    assert len(client.calls) == 2


def test_call_после_второго_429_бросает_telegramerror_без_третьей_попытки(monkeypatch):
    client = _RecordingClient(
        {"ok": False, "error_code": 429, "parameters": {"retry_after": 0},
         "description": "Too Many Requests"},
        status_code=429,
    )
    monkeypatch.setattr(tg.httpx, "AsyncClient", client)

    with pytest.raises(tg.TelegramError):
        asyncio.run(tg.call("sendMessage", chat_id=1, text="hi"))

    # Ровно две попытки (первая + один повтор), а не бесконечный ретрай.
    assert len(client.calls) == 2


def test_send_message_фолбэк_на_текст_без_разметки_при_ошибке_html(monkeypatch):
    # Сломанная HTML-разметка -> первый вызов падает TelegramError, второй
    # (без parse_mode) должен уйти без разметки и вернуть успех.
    client = _SequenceClient([
        ({"ok": False, "error_code": 400, "description": "can't parse entities"}, 400),
        ({"ok": True, "result": {"message_id": 4}}, 200),
    ])
    monkeypatch.setattr(tg.httpx, "AsyncClient", client)

    result = asyncio.run(tg.send_message(1, "<b>битая", parse_mode="HTML"))

    assert result == {"message_id": 4}
    assert "parse_mode" in client.calls[0]
    assert "parse_mode" not in client.calls[1]
