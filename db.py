import sqlite3
import time

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracked_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    shop TEXT NOT NULL,
    external_id TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    currency TEXT NOT NULL,
    last_price INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(chat_id, shop, external_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracked_item_id INTEGER NOT NULL REFERENCES tracked_items(id),
    price INTEGER NOT NULL,
    checked_at INTEGER NOT NULL
);

-- Уценённые экземпляры, о которых уже отправлен алерт. Ключ — документ уценки:
-- он привязан к физическому экземпляру, поэтому два одинаковых товара с разными
-- дефектами считаются разными находками, а повторный обход того же экземпляра —
-- нет.
CREATE TABLE IF NOT EXISTS seen_deals (
    document TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    price INTEGER NOT NULL,
    discount INTEGER NOT NULL,
    notified_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deals_subscribers (
    chat_id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL
);

-- Отправленные сообщения о находках: чтобы потом дописать в них «продано» или
-- дослать фото, не заставляя пользователя искать сообщение самому.
CREATE TABLE IF NOT EXISTS deal_messages (
    document TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (document, chat_id)
);

-- Модели, про которые пользователь сказал «больше не показывай».
CREATE TABLE IF NOT EXISTS muted_models (
    chat_id INTEGER NOT NULL,
    shop TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, shop, code)
);

CREATE INDEX IF NOT EXISTS idx_price_history_item
    ON price_history(tracked_item_id, checked_at);
"""

# Схема дополняется по ходу жизни бота, а база на сервере живая — поэтому
# недостающие колонки досыпаются отдельно: CREATE TABLE IF NOT EXISTS их не
# добавит, а ронять базу из-за нового поля нельзя.
MIGRATIONS = [
    ("seen_deals", "shop", "TEXT NOT NULL DEFAULT 'sulpak'"),
    # Ссылки на фото дефектов — чтобы кнопка «показать фото» работала и после
    # перезапуска бота, когда сам объект находки давно потерян.
    ("seen_deals", "photos", "TEXT NOT NULL DEFAULT ''"),
    ("seen_deals", "url", "TEXT NOT NULL DEFAULT ''"),
]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def get_setting(key: str, default: str | None = None) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def add_tracked_item(chat_id: int, shop: str, external_id: str, name: str, url: str,
                      currency: str, price: int) -> int:
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "INSERT INTO tracked_items(chat_id, shop, external_id, name, url, currency, "
            "last_price, last_checked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, shop, external_id) DO UPDATE SET active = 1",
            (chat_id, shop, external_id, name, url, currency, price, now, now),
        )
        # Не lastrowid: после ветки DO UPDATE (повторный /track уже знакомого
        # товара) он не указывает на нужную строку, и запись истории падала по
        # внешнему ключу. Спрашиваем id явно.
        item_id = conn.execute(
            "SELECT id FROM tracked_items WHERE chat_id = ? AND shop = ? AND external_id = ?",
            (chat_id, shop, external_id),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO price_history(tracked_item_id, price, checked_at) VALUES (?, ?, ?)",
            (item_id, price, now),
        )
        return item_id


def list_active_tracked_items() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM tracked_items WHERE active = 1").fetchall()


def list_tracked_items_for_chat(chat_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM tracked_items WHERE chat_id = ? AND active = 1 ORDER BY created_at DESC",
            (chat_id,),
        ).fetchall()


def deactivate_tracked_item(item_id: int, chat_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE tracked_items SET active = 0 WHERE id = ? AND chat_id = ?",
            (item_id, chat_id),
        )
        return cur.rowcount > 0


def filter_new_deals(documents: list[str]) -> set[str]:
    """Из пришедших документов уценки оставляет те, о которых ещё не сообщали."""
    if not documents:
        return set()
    with connect() as conn:
        placeholders = ",".join("?" * len(documents))
        known = {
            row["document"]
            for row in conn.execute(
                f"SELECT document FROM seen_deals WHERE document IN ({placeholders})", documents
            )
        }
    return set(documents) - known


def mark_deal_notified(document: str, shop: str, code: str, name: str, price: int,
                        discount: int, url: str = "", photos: list[str] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO seen_deals(document, shop, code, name, price, discount, notified_at, "
            "url, photos) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(document) DO UPDATE SET "
            "price = excluded.price, discount = excluded.discount, "
            "notified_at = excluded.notified_at, photos = excluded.photos, url = excluded.url",
            (document, shop, code, name, price, discount, int(time.time()), url,
             "\n".join(photos or [])),
        )


def get_deal(document: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM seen_deals WHERE document = ?", (document,)).fetchone()


def deal_photos(document: str) -> list[str]:
    """Фото всех экземпляров модели, к которой относится находка: сообщение
    одно на модель, и кнопка под ним обещает фото всех её экземпляров, а не
    только первого."""
    row = get_deal(document)
    if not row:
        return []
    with connect() as conn:
        rows = conn.execute(
            "SELECT photos FROM seen_deals WHERE shop = ? AND code = ? ORDER BY document",
            (row["shop"], row["code"]),
        ).fetchall()
    photos: list[str] = []
    for r in rows:
        photos += [url for url in (r["photos"] or "").split("\n") if url and url not in photos]
    return photos


def forget_deals(shop: str | None = None) -> int:
    """Забыть показанные находки — чтобы прислать их заново (кнопка «показать
    всё сначала» и ручная перепроверка после смены порога)."""
    with connect() as conn:
        if shop:
            return conn.execute("DELETE FROM seen_deals WHERE shop = ?", (shop,)).rowcount
        return conn.execute("DELETE FROM seen_deals").rowcount


def record_deal_message(document: str, chat_id: int, message_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO deal_messages(document, chat_id, message_id) VALUES (?, ?, ?) "
            "ON CONFLICT(document, chat_id) DO UPDATE SET message_id = excluded.message_id",
            (document, chat_id, message_id),
        )


def list_deal_messages(document: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT chat_id, message_id FROM deal_messages WHERE document = ?", (document,)
        ).fetchall()


def mute_model(chat_id: int, shop: str, code: str, name: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO muted_models(chat_id, shop, code, name, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, shop, code) DO NOTHING",
            (chat_id, shop, code, name, int(time.time())),
        )


def unmute_model(chat_id: int, shop: str, code: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "DELETE FROM muted_models WHERE chat_id = ? AND shop = ? AND code = ?",
            (chat_id, shop, code),
        ).rowcount > 0


def muted_codes(chat_id: int) -> set[tuple[str, str]]:
    with connect() as conn:
        return {
            (row["shop"], row["code"])
            for row in conn.execute(
                "SELECT shop, code FROM muted_models WHERE chat_id = ?", (chat_id,)
            )
        }


def list_muted(chat_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT shop, code, name FROM muted_models WHERE chat_id = ? ORDER BY created_at DESC",
            (chat_id,),
        ).fetchall()


def add_deals_subscriber(chat_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO deals_subscribers(chat_id, created_at) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO NOTHING",
            (chat_id, int(time.time())),
        )


def remove_deals_subscriber(chat_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM deals_subscribers WHERE chat_id = ?", (chat_id,)).rowcount > 0


def list_deals_subscribers() -> list[int]:
    with connect() as conn:
        return [row["chat_id"] for row in conn.execute("SELECT chat_id FROM deals_subscribers")]


def record_price(item_id: int, price: int) -> None:
    now = int(time.time())
    with connect() as conn:
        conn.execute("UPDATE tracked_items SET last_price = ?, last_checked_at = ? WHERE id = ?",
                      (price, now, item_id))
        conn.execute("INSERT INTO price_history(tracked_item_id, price, checked_at) VALUES (?, ?, ?)",
                      (item_id, price, now))
