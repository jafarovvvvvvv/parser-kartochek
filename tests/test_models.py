"""Тесты на models.DiscountedUnit: скидка считается от двух независимых баз
(зачёркнутая цена на листинге и цена нового такого же товара), и при цене
уценённого экземпляра выше базы скидка не должна получаться отрицательной —
в этом случае просто нет процента для этой базы (None), а не "минус N%"."""
from models import DiscountedUnit


def _unit(price, old_price=None, new_price=None):
    return DiscountedUnit(
        document="D1", code="C1", name="Тестовый товар", price=price, url="u",
        old_price=old_price, new_price=new_price,
    )


def test_discount_vs_old_считается_от_зачёркнутой_цены():
    unit = _unit(price=15000, old_price=20000)
    assert unit.discount_vs_old == 25


def test_discount_vs_new_считается_от_цены_нового():
    unit = _unit(price=15000, new_price=18000)
    assert unit.discount_vs_new == 17


def test_best_discount_берёт_максимум_из_двух_баз():
    # Sulpak сам считает скидку от третьей величины, поэтому обе базы дают
    # разный процент — берём большую (README: "максимум из двух", а не
    # "одна правильная").
    unit = _unit(price=15000, old_price=20000, new_price=18000)
    assert unit.best_discount == max(unit.discount_vs_old, unit.discount_vs_new)
    assert unit.best_discount == 25


def test_цена_выше_базы_не_даёт_отрицательную_скидку():
    # Бывает, что data-full-price/зачёркнутая цена оказывается ниже текущей
    # цены уценённого — тогда это не "скидка минус", а отсутствие скидки
    # от этой базы (None), а best_discount не должен уходить в минус.
    unit = _unit(price=30000, old_price=20000, new_price=18000)
    assert unit.discount_vs_old is None
    assert unit.discount_vs_new is None
    assert unit.best_discount == 0
    assert unit.best_discount >= 0


def test_цена_равная_базе_тоже_не_считается_скидкой():
    unit = _unit(price=20000, old_price=20000)
    assert unit.discount_vs_old is None


def test_нет_базы_вообще_best_discount_ноль():
    unit = _unit(price=15000)
    assert unit.discount_vs_old is None
    assert unit.discount_vs_new is None
    assert unit.best_discount == 0
