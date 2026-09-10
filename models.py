from dataclasses import dataclass, field


@dataclass
class Product:
    shop: str            # "wildberries" | "ozon" | "kaspi"
    name: str
    price: int           # текущая цена в тенге/рублях (валюта see currency)
    currency: str
    url: str
    image: str | None = None
    old_price: int | None = None
    rating: float | None = None
    reviews_count: int | None = None
    specs: dict[str, str] = field(default_factory=dict)
    external_id: str | None = None  # id товара в магазине, для истории цен
    variant: str | None = None  # атрибут конкретного предложения (напр. цвет) —
    # не входит в ключ группировки, только в строку предложения в карточке


@dataclass
class ProductGroup:
    """Один и тот же товар (модель), найденный в разных магазинах — всё
    сравнение цен идёт внутри группы, а не по всей плоской выдаче."""

    model: str            # каноническое название модели, напр. "Xiaomi Pad 7 8/256GB"
    specs: str            # короткая строка характеристик для карточки
    products: list[Product]  # отсортированы по цене, дешевле — первый
