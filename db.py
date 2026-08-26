"""
مسموع — طبقة قاعدة البيانات
تعمل مع SQLite (تجربة محلية) و PostgreSQL (تشغيل فعلي) بنفس الشيفرة.

الاختيار يتم عبر متغير البيئة DATABASE_URL:
    غير موجود                        → SQLite في ملف محلي
    postgresql://user:pass@host/db   → PostgreSQL
"""
import os, re, sqlite3
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.getenv("MASMOO_DB",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "masmoo.db"))

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row
    # Render يعطي postgres:// بينما psycopg يريد postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------------------- المخطط
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS reports (
    num TEXT PRIMARY KEY, type TEXT NOT NULL, cat TEXT, pri TEXT, dept TEXT,
    when_ TEXT, text TEXT NOT NULL, photo TEXT, name TEXT, phone TEXT, file TEXT,
    anon INTEGER DEFAULT 1, created_at INTEGER NOT NULL, status TEXT DEFAULT 'جديد',
    assignee TEXT, rating INTEGER DEFAULT 0, timeline TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_status  ON reports(status);
CREATE INDEX IF NOT EXISTS ix_created ON reports(created_at);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, name TEXT, role TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS subscriptions (
    endpoint TEXT PRIMARY KEY, data TEXT NOT NULL, device TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings (
    skey TEXT PRIMARY KEY, sval TEXT NOT NULL
);
"""

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS reports (
    num TEXT PRIMARY KEY, type TEXT NOT NULL, cat TEXT, pri TEXT, dept TEXT,
    when_ TEXT, text TEXT NOT NULL, photo TEXT, name TEXT, phone TEXT, file TEXT,
    anon INTEGER DEFAULT 1, created_at BIGINT NOT NULL, status TEXT DEFAULT 'جديد',
    assignee TEXT, rating INTEGER DEFAULT 0, timeline TEXT NOT NULL,
    updated_at BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_status  ON reports(status);
CREATE INDEX IF NOT EXISTS ix_created ON reports(created_at);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, name TEXT, role TEXT, created_at BIGINT
);
CREATE TABLE IF NOT EXISTS subscriptions (
    endpoint TEXT PRIMARY KEY, data TEXT NOT NULL, device TEXT, created_at BIGINT
);
CREATE TABLE IF NOT EXISTS settings (
    skey TEXT PRIMARY KEY, sval TEXT NOT NULL
);
"""


def to_pg(sql: str) -> str:
    """يحوّل علامات الاستفهام إلى صيغة PostgreSQL دون المساس بما داخل النصوص."""
    out, in_str = [], False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
        if ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class Cur:
    """غلاف موحّد يجعل نتائج المحرّكين تُقرأ بنفس الطريقة: row['column']."""

    def __init__(self, cur, is_pg):
        self._c, self._pg = cur, is_pg

    def execute(self, sql, params=()):
        self._c.execute(to_pg(sql) if self._pg else sql, params)
        return self

    def fetchone(self):
        return self._c.fetchone()

    def fetchall(self):
        return self._c.fetchall()

    def __iter__(self):
        return iter(self._c.fetchall())


class Conn:
    def __init__(self, raw, is_pg):
        self._r, self._pg = raw, is_pg

    def execute(self, sql, params=()):
        cur = self._r.cursor()
        return Cur(cur, self._pg).execute(sql, params)

    def executescript(self, sql):
        if self._pg:
            with self._r.cursor() as c:
                c.execute(sql)
        else:
            self._r.executescript(sql)

    def commit(self):
        self._r.commit()

    def close(self):
        self._r.close()


@contextmanager
def db():
    if IS_PG:
        raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        raw = sqlite3.connect(SQLITE_PATH)
        raw.row_factory = sqlite3.Row
    con = Conn(raw, IS_PG)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with db() as con:
        con.executescript(SCHEMA_PG if IS_PG else SCHEMA_SQLITE)


def engine_name() -> str:
    return "postgresql" if IS_PG else "sqlite"


# عبارة الإدراج مع التعامل مع التكرار — متطابقة في المحرّكين
UPSERT_SUB = """
INSERT INTO subscriptions (endpoint, data, device, created_at)
VALUES (?, ?, ?, ?)
ON CONFLICT (endpoint) DO UPDATE SET data = EXCLUDED.data, device = EXCLUDED.device
"""


# ---------------------------------------------------------------- الإعدادات
def get_setting(key: str):
    with db() as con:
        r = con.execute("SELECT sval FROM settings WHERE skey=?", (key,)).fetchone()
    return r["sval"] if r else None


def set_setting(key: str, val: str):
    with db() as con:
        con.execute("""INSERT INTO settings (skey, sval) VALUES (?, ?)
                       ON CONFLICT (skey) DO UPDATE SET sval = EXCLUDED.sval""", (key, val))


def ensure_vapid():
    """يولّد مفاتيح الإشعارات تلقائياً عند أول تشغيل ويحفظها في قاعدة البيانات."""
    pub, priv = get_setting("vapid_public"), get_setting("vapid_private")
    if pub and priv:
        return pub, priv
    import base64
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    k = ec.generate_private_key(ec.SECP256R1())
    b64 = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    priv = b64(k.private_numbers().private_value.to_bytes(32, "big"))
    pub = b64(k.public_key().public_bytes(serialization.Encoding.X962,
                                          serialization.PublicFormat.UncompressedPoint))
    set_setting("vapid_public", pub)
    set_setting("vapid_private", priv)
    return pub, priv
