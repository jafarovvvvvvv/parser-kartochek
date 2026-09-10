import re

from models import Product

_STOPWORDS = {
    "для", "и", "с", "в", "на", "от", "по", "из", "к", "но", "или", "не", "а",
}


def _stem(word: str) -> str:
    # Грубый стемминг под русскую морфологию: обрезаем окончание, чтобы
    # "черные"/"чёрный"/"чёрного" совпадали друг с другом при сравнении.
    word = word.replace("ё", "е")
    return word[:4] if len(word) > 4 else word


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))
    return {_stem(w) for w in words if len(w) > 2 and w not in _STOPWORDS}


def rerank(query: str, products: list[Product], limit: int) -> list[Product]:
    """Отсекает товары, никак не пересекающиеся по словам с запросом
    (например, "черепашки для MMA" на запрос "черные перчатки для бокса"),
    и поднимает наверх те, что совпадают полнее."""
    q_tokens = _tokens(query)
    if not q_tokens:
        return products[:limit]

    scored = [(len(q_tokens & _tokens(p.name)), i, p) for i, p in enumerate(products)]
    relevant = [t for t in scored if t[0] > 0]
    if not relevant:
        return products[:limit]  # запрос совсем не совпал с названиями — лучше показать хоть что-то

    relevant.sort(key=lambda t: (-t[0], t[1]))
    return [p for _, _, p in relevant[:limit]]
