"""Поток находок уценки целиком (без сети): лимит за обход, очередь непоказанных,
скрытые модели, падение одного магазина, одновременные обходы."""
import asyncio

import pytest

import db
import deals
from models import DiscountedUnit

CHAT = 777


def _unit(shop, code, doc, discount):
    new = 100_000
    return DiscountedUnit(document=doc, code=code, shop=shop, name=f"Товар {code}",
                          price=int(new * (100 - discount) / 100), url=f"https://x/{code}",
                          new_price=new, defects={"Состояние": "царапина"})


INVENTORY = [
    _unit("sulpak", "A", "S1", 90), _unit("sulpak", "A", "S2", 90),  # два экземпляра одной модели
    _unit("sulpak", "B", "S3", 85),
    _unit("dns", "C", "dns:1", 80),
    _unit("dns", "D", "dns:2", 75),
    _unit("sulpak", "E", "S4", 72),
    _unit("sulpak", "F", "S5", 40),  # ниже порога
]


class FakeShop:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail

    async def scan_discounted(self):
        await asyncio.sleep(0.05)
        if self.fail:
            raise RuntimeError("магазин лёг")
        return [u for u in INVENTORY if u.shop == self.name]


@pytest.fixture()
def flow(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "flow.db"))
    db.init_db()
    sent = []

    async def fake_send(chat_id, text, **kwargs):
        sent.append(text)
        return {"message_id": len(sent)}

    async def no_analog(unit):
        return None

    monkeypatch.setattr(deals.tg, "send_message", fake_send)
    monkeypatch.setattr(deals, "find_cheapest_analog", no_analog)
    monkeypatch.setattr(deals, "PAUSE_BETWEEN_LOOKUPS_SEC", 0)
    monkeypatch.setattr(deals, "DEALS_MAX_PER_SCAN", 3)
    # Лок живёт на уровне модуля и привязывается к циклу событий при первой
    # конкуренции — между тестами (разные asyncio.run) нужен свежий.
    monkeypatch.setattr(deals, "_scan_lock", asyncio.Lock())
    monkeypatch.setattr(deals.registry, "discounted_adapters",
                        lambda: [FakeShop("sulpak"), FakeShop("dns")])
    deals.set_min_discount(70)
    return sent


def _scan():
    return asyncio.run(deals.scan_once(chats=[CHAT]))


def _deal_messages(sent):
    return [t for t in sent if t.startswith("🔥")]


def test_за_обход_не_больше_лимита_и_сначала_самые_большие_скидки(flow):
    shown = _scan()
    assert len(_deal_messages(flow)) == 3, "лимит находок за один обход не соблюдён"
    assert {u.code for u in shown} == {"A", "B", "C"}, "сначала должны идти самые большие скидки"


def test_экземпляры_одной_модели_идут_одним_сообщением(flow):
    _scan()
    assert sum("Товар A" in t for t in _deal_messages(flow)) == 1


def test_про_остальные_приходит_одна_свёртка_с_правильным_склонением(flow):
    _scan()
    digest = [t for t in flow if t.startswith("…и ещё")]
    assert len(digest) == 1
    assert "2 модели" in digest[0] and "2 экземпляра" in digest[0], digest[0]


def test_ниже_порога_не_показывается(flow):
    _scan()
    assert not any("Товар F" in t for t in flow)


def test_непоказанные_остаются_в_очереди_и_приходят_следующим_обходом(flow):
    _scan()
    assert db.filter_new_deals(["dns:2", "S4"]) == {"dns:2", "S4"}, \
        "не влезшие в лимит находки не должны помечаться показанными"
    flow.clear()
    second = _scan()
    assert {u.code for u in second} == {"D", "E"}
    assert not any(t.startswith("…и ещё") for t in flow), "очередь пуста — свёртка не нужна"


def test_показанное_повторно_не_присылается(flow):
    _scan()
    _scan()
    flow.clear()
    assert _scan() == [] and flow == []


def test_скрытая_модель_не_приходит_и_не_помечается_показанной(flow):
    db.mute_model(CHAT, "sulpak", "A", "Товар A")
    _scan()
    assert not any("Товар A" in t for t in flow)
    assert db.filter_new_deals(["S1"]) == {"S1"}, \
        "скрытая модель должна вернуться, если её снова открыть"


def test_падение_одного_магазина_не_лишает_находок_из_другого(flow, monkeypatch):
    monkeypatch.setattr(deals.registry, "discounted_adapters",
                        lambda: [FakeShop("sulpak", fail=True), FakeShop("dns")])
    shown = _scan()
    assert {u.shop for u in shown} == {"dns"}


def test_одновременные_обходы_не_задваивают_находки(flow):
    async def both():
        return await asyncio.gather(deals.scan_once(chats=[CHAT]), deals.scan_once(chats=[CHAT]))

    first, second = asyncio.run(both())
    documents = [u.document for u in first + second]
    assert len(documents) == len(set(documents)), "плановый и ручной обход прислали одно и то же"


def test_недоставленная_находка_не_помечается_показанной(flow, monkeypatch):
    async def failing_send(chat_id, text, **kwargs):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(deals.tg, "send_message", failing_send)
    _scan()
    assert db.filter_new_deals(["S1", "S3", "dns:1"]) == {"S1", "S3", "dns:1"}, \
        "сбой отправки навсегда прятал бы товар, о котором никто не узнал"
