from abc import ABC, abstractmethod

from models import Product


class ShopAdapter(ABC):
    name: str

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[Product]:
        """Вернуть до limit товаров по текстовому запросу."""

    @abstractmethod
    async def fetch_price(self, external_id: str) -> Product | None:
        """Обновить цену/наличие одного товара по его id для отслеживания."""
