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

-- Уценённые экземпляры, о которых уже отправлен алерт. Ключ — документ уценки
-- Sulpak: он привязан к физическому экземпляру, поэтому два одинаковых товара
-- с разными дефектами считаются разными находками, а повторный обход того же
-- экземпляра — нет.
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
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


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
        cur = conn.execute(
            "INSERT INTO tracked_items(chat_id, shop, external_id, name, url, currency, "
            "last_price, last_checked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id, shop, external_id) DO UPDATE SET active = 1",
            (chat_id, shop, external_id, name, url, currency, price, now, now),
        )
        item_id = cur.lastrowid
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


def mark_deal_notified(document: str, code: str, name: str, price: int, discount: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO seen_deals(document, code, name, price, discount, notified_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(document) DO UPDATE SET "
            "price = excluded.price, discount = excluded.discount, notified_at = excluded.notified_at",
            (document, code, name, price, discount, int(time.time())),
        )


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
