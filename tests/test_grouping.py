"""grouping._merge_same_model: ИИ иногда раскладывает одну и ту же модель на
две группы (например, "с микрофоном" и "просто проводные" из одной линейки).
Регрессия была в том, что "дешевле всего" считалось внутри каждой половины по
отдельности — например, для Sony MDR-ZX310AP отметка "дешевле всего" стояла на
18 670 при 5 990 в соседней карточке. После merge должна остаться одна группа
с полным списком предложений, отсортированным по цене, дешевле — первым."""
from grouping import _merge_same_model
from models import Product, ProductGroup


def _product(shop, price, name="Sony MDR-ZX310AP"):
    return Product(shop=shop, name=name, price=price, currency="KZT", url=f"http://{shop}")


def test_две_группы_одной_модели_схлопываются_в_одну():
    group_expensive = ProductGroup(
        model="Sony MDR-ZX310AP", specs="", products=[_product("kaspi", 18670)]
    )
    group_cheap = ProductGroup(
        model="Sony MDR-ZX310AP", specs="проводные",
        products=[_product("wildberries", 5990)],
    )
    merged = _merge_same_model([group_expensive, group_cheap])
    assert len(merged) == 1
    assert [p.price for p in merged[0].products] == [5990, 18670]


def test_дешевле_всего_после_merge_это_реальный_минимум_а_не_из_первой_половины():
    group_a = ProductGroup(model="Sony MDR-ZX310AP", specs="",
                            products=[_product("kaspi", 18670)])
    group_b = ProductGroup(model="Sony MDR-ZX310AP", specs="проводные",
                            products=[_product("wildberries", 5990)])
    merged = _merge_same_model([group_a, group_b])
    cheapest = merged[0].products[0]
    assert cheapest.price == 5990
    assert cheapest.shop == "wildberries"


def test_разные_модели_не_схлопываются():
    group_a = ProductGroup(model="Sony MDR-ZX310AP", specs="", products=[_product("kaspi", 18670)])
    group_b = ProductGroup(model="Sony MDR-ZX110", specs="", products=[_product("dns", 4000, "Sony MDR-ZX110")])
    merged = _merge_same_model([group_a, group_b])
    assert len(merged) == 2


def test_совпадение_модели_не_зависит_от_регистра_и_пунктуации():
    # _model_key нормализует название (убирает всё кроме букв/цифр в нижнем
    # регистре) — "Sony MDR-ZX310AP" и "sony mdr zx310ap" должны считаться
    # одной и той же моделью для склейки.
    group_a = ProductGroup(model="Sony MDR-ZX310AP", specs="", products=[_product("kaspi", 18670)])
    group_b = ProductGroup(model="sony mdr zx310ap", specs="", products=[_product("wildberries", 5990)])
    merged = _merge_same_model([group_a, group_b])
    assert len(merged) == 1
    assert [p.price for p in merged[0].products] == [5990, 18670]


def test_specs_берётся_из_первой_непустой_группы():
    group_a = ProductGroup(model="Sony MDR-ZX310AP", specs="", products=[_product("kaspi", 18670)])
    group_b = ProductGroup(model="Sony MDR-ZX310AP", specs="проводные",
                            products=[_product("wildberries", 5990)])
    merged = _merge_same_model([group_a, group_b])
    assert merged[0].specs == "проводные"
