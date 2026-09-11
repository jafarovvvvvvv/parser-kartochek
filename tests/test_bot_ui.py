"""Интерфейс бота на кнопках (без сети): сообщение о находке, нажатия,
настройки, отслеживание. Подменяется только сетевой слой httpx — настоящий
tg.call() работает, включая фильтрацию None-параметров."""
import asyncio

import pytest

import bot
import db
import deals
import tg
from models import DiscountedUnit, Product

CHAT = 4242


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _Client.calls.append((url.rsplit("/", 1)[-1], json or {}))
        return _Response({"ok": True, "result": {"message_id": len(_Client.calls)}})


@pytest.fixture()
def ui(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "ui.db"))
    db.init_db()
    _Client.calls = []
    monkeypatch.setattr(tg.httpx, "AsyncClient", _Client)
    bot._last_search.clear()
    bot._pending_clarification.clear()
    return _Client.calls


def _last(calls, method):
    return next((p for m, p in reversed(calls) if m == method), None)


def _press(data, message_id=1):
    asyncio.run(bot.handle_callback({
        "id": "cb", "data": data,
        "message": {"message_id": message_id, "chat": {"id": CHAT}},
    }))


def _say(text):
    asyncio.run(bot.handle_text(CHAT, text))


UNITS = [
    DiscountedUnit(
        document="ЦО111", code="298206", name="Наушники <AirPods> *2*", price=19995,
        url="https://www.sulpak.kz/g/airpods", old_price=69990, new_price=39990,
        address="1-й микрорайон, 52", city="Костанай",
        defects={"Комментарий продавца": "Замена наушника [левый] & кейса"},
        photos=["https://discounted.object.pscloud.io/1_1.jpg",
                "https://discounted.object.pscloud.io/1_2.jpg"],
        rating=4.8, reviews_count=179,
    ),
    DiscountedUnit(
        document="ЦО222", code="298206", name="Наушники <AirPods> *2*", price=19995,
        url="https://www.sulpak.kz/g/airpods", old_price=69990, new_price=39990,
        address="1-й микрорайон, 52", city="Костанай",
        defects={"Комментарий продавца": "Плохое звучание"},
        photos=["https://discounted.object.pscloud.io/2_1.jpg"],
    ),
]


def _remember(units):
    for u in units:
        db.mark_deal_notified(u.document, u.shop, u.code, u.name, u.price,
                              u.best_discount, u.url, u.photos)


def test_находка_экранирует_текст_магазина_и_собирает_экземпляры(ui):
    text = deals.format_deal(UNITS, None)
    assert "<AirPods>" not in text and "&lt;AirPods&gt;" in text, "сырые < > ломают HTML-разметку"
    assert "&amp; кейса" in text
    assert "Экземпляров: 2" in text
    assert text.count("1-й микрорайон") == 1, "общий адрес должен быть один раз"
    assert "4.8" in text and "179" in text


def test_под_находкой_кнопки_фото_скрыть_сравнить(ui):
    labels = [b["text"] for row in deals.deal_buttons(UNITS)["inline_keyboard"] for b in row]
    assert "📷 Фото дефектов (3)" in labels
    assert any("Скрыть" in x for x in labels) and any("Сравнить" in x for x in labels)


def test_без_фото_кнопки_фото_нет(ui):
    bare = [DiscountedUnit(document="X", code="1", name="Товар", price=1, url="u")]
    labels = [b["text"] for row in deals.deal_buttons(bare)["inline_keyboard"] for b in row]
    assert not any("Фото" in x for x in labels)


def test_кнопка_фото_шлёт_альбом_и_работает_после_перезапуска(ui):
    _remember(UNITS)  # фото лежат в базе — объект находки после рестарта уже потерян
    _press("ph:ЦО111")
    assert _last(ui, "sendMediaGroup") is not None
    assert _last(ui, "answerCallbackQuery") is not None, "без ответа кнопка крутит часики"


def test_альбом_содержит_фото_всех_экземпляров_модели(ui):
    _remember(UNITS)
    _press("ph:ЦО111")
    media = _last(ui, "sendMediaGroup")["media"]
    assert len(media) == 3, "кнопка обещает фото всех экземпляров, а не только первого"


def test_фото_dns_не_называются_фото_дефектов(ui):
    dns = [DiscountedUnit(document="dns:1", code="5", shop="dns", name="Пылесос", price=1,
                          url="u", photos=["https://c.dns-shop.kz/1.jpg"])]
    labels = [b["text"] for row in deals.deal_buttons(dns)["inline_keyboard"] for b in row]
    assert "📷 Фото (1)" in labels, "у DNS картинка модели, а не снимок дефекта"


def test_скрыть_и_вернуть_модель(ui):
    _remember(UNITS)
    _press("mute:ЦО111")
    assert ("sulpak", "298206") in db.muted_codes(CHAT)
    assert "Вернуть" in str(_last(ui, "editMessageReplyMarkup"))
    _press("unmute:sulpak:298206")
    assert ("sulpak", "298206") not in db.muted_codes(CHAT)


def test_порог_меняется_кнопками_и_не_уходит_за_пределы(ui):
    deals.set_min_discount(60)
    _press("set:disc:+5")
    assert deals.min_discount() == 65
    _press("set:disc:-5")
    assert deals.min_discount() == 60
    _say("/deals 150")
    assert deals.min_discount() == 60 and "от 1 до 99" in _last(ui, "sendMessage")["text"]


def test_подписка_кнопками(ui):
    _press("deals:on")
    assert db.list_deals_subscribers() == [CHAT]
    _press("deals:off")
    assert db.list_deals_subscribers() == []


def test_отслеживание_и_снятие_кнопками(ui):
    bot._last_search[CHAT] = [Product(shop="kaspi", name="Товар", price=100, currency="KZT",
                                      url="https://kaspi.kz/p/2", external_id="p2")]
    _press("track:1")
    items = db.list_tracked_items_for_chat(CHAT)
    assert len(items) == 1
    _say("/list")
    assert f"untrack:{items[0]['id']}" in str(_last(ui, "sendMessage"))
    _press(f"untrack:{items[0]['id']}")
    assert db.list_tracked_items_for_chat(CHAT) == []


def test_устаревшая_кнопка_после_перезапуска_объясняет_что_делать(ui):
    _press("track:1")
    assert "поищи заново" in _last(ui, "sendMessage")["text"]


def test_cancel_сбрасывает_уточнение(ui):
    bot._pending_clarification[CHAT] = ("что-то", 1)
    _say("/cancel")
    assert CHAT not in bot._pending_clarification


def test_настройки_показывают_порог(ui):
    deals.set_min_discount(55)
    _say("/settings")
    assert "от 55%" in _last(ui, "sendMessage")["text"]


def test_в_запросы_к_telegram_не_попадает_none(ui):
    _remember(UNITS)
    deals.set_min_discount(60)
    for data in ("ph:ЦО111", "mute:ЦО111", "set:disc:+5", "deals:on"):
        _press(data)
    _say("/settings")
    bad = [(m, k) for m, p in ui for k, v in p.items() if v is None]
    assert not bad, f"None в параметрах Telegram API даёт 400: {bad}"
