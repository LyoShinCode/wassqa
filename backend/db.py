import sqlite3
from contextlib import contextmanager

DB_PATH = "db.sqlite"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uin TEXT UNIQUE NOT NULL,
                nickname TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                status TEXT DEFAULT 'offline',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS contacts (
                user_id INTEGER NOT NULL,
                contact_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                PRIMARY KEY (user_id, contact_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (contact_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,
                user1_id INTEGER,
                user2_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                is_deleted INTEGER DEFAULT 0,
                is_read INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(id),
                FOREIGN KEY (author_id) REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS read_receipts (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                last_read_msg_id INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            );
        """)