"""Единый список магазинов.

Раньше словарь ADAPTERS собирался отдельно в bot.py, scheduler.py и deals.py —
с разным составом: Ozon висел только в scheduler.py, хотя добавить ozon-товар в
отслеживание было неоткуда. Теперь состав задаётся здесь одним местом.
"""
from adapters.dns import DnsAdapter
from adapters.kaspi import KaspiAdapter
from adapters.sulpak import SulpakAdapter
from adapters.technodom import TechnodomAdapter
from adapters.wildberries import WildberriesAdapter

# Ozon не в строю: капча на каждом запросе, в выдаче от него была только строка
# "не отвечает" (см. README). Код адаптера цел, вернуть — добавить сюда.
SEARCH = [
    WildberriesAdapter(),
    KaspiAdapter(),
    SulpakAdapter(),
    TechnodomAdapter(),
    DnsAdapter(),
]

BY_NAME = {a.name: a for a in SEARCH}

# Магазины с настоящей уценкой: товары с дефектами, привязанные к складу.
# У Технодома, Kaspi, Mechta и Alser такого раздела в онлайне нет — только
# обычные акции без дефектов и адреса (проверено разведкой по сайтам).
DISCOUNTED = ["sulpak", "dns"]


def by_name(shop: str):
    return BY_NAME.get(shop)


def discounted_adapters():
    return [BY_NAME[name] for name in DISCOUNTED if name in BY_NAME]
