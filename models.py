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
class DiscountedUnit:
    """Конкретный уценённый экземпляр товара в Sulpak. Важно: это не модель, а
    именно физический экземпляр — у одной модели их бывает несколько, у каждого
    свой документ уценки, своя цена и свои дефекты."""

    document: str        # id экземпляра ("ЦО2313010415" у Sulpak), он же ключ дедупа
    code: str            # код модели в магазине
    name: str
    price: int           # цена этого уценённого экземпляра
    url: str
    shop: str = "sulpak"
    currency: str = "KZT"
    old_price: int | None = None   # зачёркнутая цена на карточке листинга
    new_price: int | None = None   # цена нового такого же (data-full-price)
    defects: dict[str, str] = field(default_factory=dict)  # Упаковка / Комплектация / ...
    address: str | None = None     # адрес магазина, где лежит экземпляр
    city: str | None = None
    image: str | None = None
    # Фото именно этого экземпляра — царапины и вмятины, а не стоковая картинка
    # модели. По ним видно, стоит ли ехать смотреть.
    photos: list[str] = field(default_factory=list)
    rating: float | None = None
    reviews_count: int | None = None
    shop_discount: str | None = None  # "-50%" — как это считает сам магазин

    def _pct(self, base: int | None) -> int | None:
        if not base or base <= 0 or self.price <= 0 or self.price >= base:
            return None
        return round((1 - self.price / base) * 100)

    @property
    def discount_vs_old(self) -> int | None:
        """Скидка от зачёркнутой цены — тот самый процент, который видно на сайте."""
        return self._pct(self.old_price)

    @property
    def discount_vs_new(self) -> int | None:
        """Честная скидка: сколько экономишь против нового такого же прямо сейчас."""
        return self._pct(self.new_price)

    @property
    def best_discount(self) -> int:
        return max([p for p in (self.discount_vs_old, self.discount_vs_new) if p] or [0])


@dataclass
class ProductGroup:
    """Один и тот же товар (модель), найденный в разных магазинах — всё
    сравнение цен идёт внутри группы, а не по всей плоской выдаче."""

    model: str            # каноническое название модели, напр. "Xiaomi Pad 7 8/256GB"
    specs: str            # короткая строка характеристик для карточки
    products: list[Product]  # отсортированы по цене, дешевле — первый
