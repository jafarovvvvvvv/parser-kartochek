"""Разбор HTML раздела уценки Sulpak — на маленьких вручную составленных
фрагментах (структура разметки видна в adapters/sulpak.py и README.md), без
похода в сеть."""
from adapters.sulpak import _clean_defect, _parse_discounted_listing


def _card(name: str, code: str, price: str, href: str, old_price_html: str = "") -> str:
    return (
        f'<div class="product__item product__item-js tile-container" '
        f'data-name="{name}" data-code="{code}" data-price="{price}" data-brand="Haier">\n'
        f'  <a href="{href}">\n'
        f'  {old_price_html}\n'
        f'</div>\n'
    )


class TestParseDiscountedListing:
    def test_отбрасывает_плейсхолдерную_цену_нет_в_наличии(self):
        page_html = _card(
            "Стиральная машина Haier HW80-B14979", "AAA111", "124995.0",
            "/g/haier-hw80-b14979",
            '<div class="product__item-price-old">149 990</div>',
        ) + _card(
            "Товар без остатка", "BBB222", "1000000.0", "/g/no-stock-item",
        )
        items = _parse_discounted_listing(page_html)
        assert len(items) == 1
        assert items[0]["code"] == "AAA111"
        assert all(i["price"] != 1_000_000 for i in items)

    def test_берёт_зачёркнутую_цену_из_карточки(self):
        page_html = _card(
            "Стиральная машина Haier HW80-B14979", "AAA111", "124995.0",
            "/g/haier-hw80-b14979",
            '<div class="product__item-price-old">149 990</div>',
        )
        items = _parse_discounted_listing(page_html)
        assert items[0]["old_price"] == 149990
        assert items[0]["price"] == 124995

    def test_зачёркнутая_цена_не_перетекает_из_соседней_карточки(self):
        # Регрессия по смыслу: зачёркнутая цена лежит ниже по разметке, а не в
        # data-атрибутах, поэтому парсинг режет HTML по границам карточек —
        # если границы съехали, старая цена одной карточки может утечь в
        # соседнюю. Проверяем на двух карточках с разными старыми ценами.
        page_html = _card(
            "Товар А", "AAA111", "10000.0", "/g/tovar-a",
            '<div class="product__item-price-old">15 000</div>',
        ) + _card(
            "Товар Б", "BBB222", "20000.0", "/g/tovar-b",
            '<div class="product__item-price-old">25 000</div>',
        )
        items = _parse_discounted_listing(page_html)
        assert len(items) == 2
        by_code = {i["code"]: i for i in items}
        assert by_code["AAA111"]["old_price"] == 15000
        assert by_code["BBB222"]["old_price"] == 25000

    def test_карточка_без_старой_цены_даёт_none(self):
        page_html = _card("Товар без скидки на листинге", "CCC333", "5000.0", "/g/tovar-v")
        items = _parse_discounted_listing(page_html)
        assert items[0]["old_price"] is None


class TestCleanDefect:
    def test_убирает_хвост_undefined(self):
        assert _clean_defect("Замена помпы - undefined") == "Замена помпы"

    def test_убирает_разделитель_0_между_частями(self):
        assert _clean_defect("Потертости;0;Царапины") == "Потертости; Царапины"

    def test_схлопывает_переводы_строк_в_одну_строку(self):
        text = "Неисправность: нет\nРемонт: заменена помпа"
        result = _clean_defect(text)
        assert "\n" not in result
        assert result == "Неисправность: нет Ремонт: заменена помпа"

    def test_комбинация_undefined_и_переводов_строк(self):
        text = "Упаковка: не оригинальная\nКомплектация: - undefined"
        result = _clean_defect(text)
        assert "\n" not in result
        assert "undefined" not in result
