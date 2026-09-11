"""ai.py — общий слой вызова OpenAI для query_planner/grouping/advice.

Ключевая регрессия (README, ai.py): OPENAI_MODEL — reasoning-модель, и без
"reasoning_effort": "none" в теле запроса она тратит весь max_completion_tokens
на скрытые размышления и отдаёт пустой ответ. Этот параметр должен уходить в
каждый запрос, независимо от ask_json/ask_text. Сеть не трогаем — httpx.AsyncClient
подменяется фейком."""
import asyncio

import ai


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Фейковый httpx.AsyncClient: запоминает отправленное тело запроса,
    отвечает заранее заданным content без похода в сеть."""

    last_json = None

    def __init__(self, content, *, raise_exc=None):
        self._content = content
        self._raise_exc = raise_exc

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.last_json = json
        if self._raise_exc:
            raise self._raise_exc
        return _FakeResponse({"choices": [{"message": {"content": self._content}}]})


def test_ask_json_без_ключа_возвращает_none_и_не_ходит_в_сеть(monkeypatch):
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "")
    result = asyncio.run(ai.ask_json("sys", "user", max_tokens=100))
    assert result is None


def test_ask_text_без_ключа_возвращает_пустую_строку(monkeypatch):
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "")
    result = asyncio.run(ai.ask_text("sys", "user", max_tokens=100))
    assert result == ""


def test_ask_json_отправляет_reasoning_effort_none_и_json_object(monkeypatch):
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "test-key")
    fake = _FakeAsyncClient('{"groups": []}')
    monkeypatch.setattr(ai.httpx, "AsyncClient", fake)

    result = asyncio.run(ai.ask_json("sys", "user", max_tokens=600))

    assert result == {"groups": []}
    assert _FakeAsyncClient.last_json["reasoning_effort"] == "none"
    assert _FakeAsyncClient.last_json["response_format"] == {"type": "json_object"}
    assert _FakeAsyncClient.last_json["max_completion_tokens"] == 600


def test_ask_text_отправляет_reasoning_effort_none_без_response_format(monkeypatch):
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "test-key")
    fake = _FakeAsyncClient("короткий совет")
    monkeypatch.setattr(ai.httpx, "AsyncClient", fake)

    result = asyncio.run(ai.ask_text("sys", "user", max_tokens=100))

    assert result == "короткий совет"
    assert _FakeAsyncClient.last_json["reasoning_effort"] == "none"
    assert "response_format" not in _FakeAsyncClient.last_json


def test_ask_json_пустой_ответ_ии_не_падает_а_возвращает_none(monkeypatch):
    # "Пустой ответ" — та самая живая поломка без reasoning_effort=none:
    # ИИ отвечает пустой строкой content. Вызывающий код (grouping.py)
    # рассчитывает на None, а не на исключение.
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "test-key")
    fake = _FakeAsyncClient("")
    monkeypatch.setattr(ai.httpx, "AsyncClient", fake)

    result = asyncio.run(ai.ask_json("sys", "user", max_tokens=600))

    assert result is None


def test_ask_json_сетевая_ошибка_гасится_и_не_поднимается_наружу(monkeypatch):
    monkeypatch.setattr(ai, "OPENAI_API_KEY", "test-key")
    fake = _FakeAsyncClient(None, raise_exc=RuntimeError("network is down"))
    monkeypatch.setattr(ai.httpx, "AsyncClient", fake)

    result = asyncio.run(ai.ask_json("sys", "user", max_tokens=600))

    assert result is None
