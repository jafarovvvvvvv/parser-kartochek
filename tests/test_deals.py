"""Регрессионные тесты для чистых функций deals.py: сопоставление модели по
артикулу (same_model), отсев запчастей/аксессуаров (looks_like_part) и
нормализация названия для короткого поискового запроса (clean_name,
short_query). Все кейсы — из README.md, где описаны конкретные живые поломки,
которые эти проверки должны ловить при повторении."""
from deals import clean_name, format_deal, looks_like_part, same_model, short_query
from models import DiscountedUnit


class TestSameModel:
    def test_разные_модели_одной_линейки_haier_не_совпадают(self):
        # HW60-BP12929ASE и HW60-BP12919AS отличаются в середине артикула —
        # это разные модели стиральных машин, не должны считаться одной.
        assert not same_model(
            "Стиральная машина Haier HW60-BP12929ASE", "HW60-BP12919AS"
        )

    def test_candy_разные_артикулы_разной_длины_не_совпадают(self):
        assert not same_model("Candy CSO4 276TWMB-07", "CSO44 286TWMB-07")

    def test_candy_разные_артикулы_одинаковой_длины_не_совпадают(self):
        assert not same_model("Candy CSO4 276TWMB-07", "CSO4 107TB1/2-07")

    def test_sony_цветовой_суффикс_считается_той_же_моделью(self):
        # Хвост до 3 символов — код расцветки (V), а не другая модель.
        assert same_model("Sony MDR-EX15LP", "MDR-EX15LPV")

    def test_sony_другая_модификация_не_совпадает(self):
        assert not same_model("Sony MDR-EX15LP", "MDR-EX15AP")

    def test_артикулы_одинаковой_длины_сравниваются_на_точное_равенство(self):
        # Регрессия: раньше при равной длине артикул сравнивался как бы "сам
        # с собой" через startswith, и любые два артикула одной длины считались
        # совпадением. HW60-BP12919 и HW60-BP12929 — 11 символов у обоих,
        # отличаются одной цифрой в середине — должны остаться разными моделями.
        assert not same_model("HW60-BP12919", "HW60-BP12929")

    def test_одинаковый_артикул_одинаковой_длины_совпадает(self):
        # Контрольный положительный случай к предыдущему тесту: если артикулы
        # равной длины и правда совпадают буква в букву — это та же модель.
        assert same_model("HW60-BP12919", "HW60-BP12919")

    def test_нет_артикула_в_названии_не_считается_совпадением(self):
        assert not same_model("Стиральная машина без индекса", "Тоже без индекса")


class TestLooksLikePart:
    def test_подшипники_это_запчасть(self):
        assert looks_like_part(
            "Стиральная машина Haier HW80-B14979",
            "Подшипники для стиральной машины Haier HW80-B14979",
        )

    def test_полное_название_товара_с_объёмом_и_цветом_не_запчасть(self):
        assert not looks_like_part(
            "Стиральная машина Haier HW80-B14979",
            "Стиральная машина Haier HW80-B14979 8 кг белый",
        )

    def test_предлог_для_в_самом_названии_не_считается_признаком_запчасти(self):
        # У "Средство ДЛЯ очистки..." предлог "для" — часть названия самого
        # товара, а не связки "аксессуар для чего-то". Аналог с таким же
        # названием не должен отбрасываться как запчасть только из-за предлога.
        name = "Средство для очистки молочной системы кофемашины Philips CA6705"
        assert not looks_like_part(name, name)

    def test_предлог_для_у_чужого_товара_без_предлога_у_нашего_это_запчасть(self):
        assert looks_like_part(
            "Стиральная машина Haier HW80-B14979",
            "Шланг для стиральной машины Haier HW80-B14979",
        )


class TestCleanNameAndShortQuery:
    NAME = (
        "Наушники Huawei FreeBuds 5i T0014 Isle Blue FreeBuds 5i Isle Blue "
        "(Orange-T020)"
    )

    def test_clean_name_убирает_повторы_модели_и_цвета(self):
        assert clean_name(self.NAME) == (
            "Наушники Huawei FreeBuds 5i T0014 Isle Blue (Orange-T020)"
        )

    def test_short_query_даёт_пять_уникальных_слов_без_скобок(self):
        # В чужие магазины уходит короткий запрос без повторов и без
        # содержимого скобок — длинное имя из Sulpak магазины не находят.
        result = short_query(self.NAME)
        assert result == "Наушники Huawei FreeBuds 5i T0014"
        assert "(" not in result
        assert len(result.split()) == 5


class TestFormatDealEscapesHtml:
    def test_дефекты_и_название_экранируются_для_html_parse_mode(self):
        # format_deal собирает сообщение с parse_mode=HTML: необработанные
        # "<", ">", "&" в названии или в тексте дефекта (частые в описаниях
        # уценки) сломают разметку и уронят отправку. tg.escape должен
        # прогонять весь текст из магазина, а не только фиксированные части.
        unit = DiscountedUnit(
            document="DOC1",
            code="C1",
            shop="sulpak",
            name="Холодильник <Test> & Co",
            price=100000,
            url="http://example.com/item",
            old_price=150000,
            new_price=140000,
            defects={"Комментарий продавца": "Скол <на дверце> & вмятина"},
            address="ул. Тестовая 1",
            city="Костанай",
        )
        text = format_deal([unit], None)
        assert "<Test>" not in text
        assert "&lt;Test&gt;" in text
        assert "<на дверце>" not in text
        assert "&lt;на дверце&gt;" in text
