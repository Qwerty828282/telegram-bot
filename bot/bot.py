#!/usr/bin/env python3
"""Telegram bot with builds, tutorials (key system), support, and admin panel."""

import os
import re
import html
import sqlite3
import logging
import secrets
import string
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID  = int(os.environ["OWNER_ID"])

# In-memory cache: users already registered this process run — skip repeated DB writes
_registered_ids: set = set()
# In-memory ban cache — loaded from DB at startup, updated on ban/unban
_banned_ids: set = set()

# ─────────────────────────── Database backend ───────────────────────────
# На Railway ОБЯЗАТЕЛЬНО нужен PostgreSQL (данные не стираются при деплое).
# SQLite используется только для локальной разработки.

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_PG = bool(DATABASE_URL)

ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
if ON_RAILWAY and not USE_PG:
    logger.warning("⚠️  DATABASE_URL не задан — используется SQLite. Данные сбросятся при перезапуске!")

if not USE_PG:
    # SQLite — только локально
    DB_PATH = os.environ.get("DB_PATH", "bot/bot_data.db")
    _dir = os.path.dirname(DB_PATH)
    if _dir:
        os.makedirs(_dir, exist_ok=True)
    logger.warning("⚠️  Используется SQLite (%s). Данные НЕ сохранятся на Railway!", DB_PATH)
else:
    logger.info("✅ PostgreSQL подключён — данные сохраняются навсегда.")


def _pg_adapt(sql: str) -> str:
    """Convert SQLite-style SQL to PostgreSQL-compatible SQL."""
    sql = sql.replace("?", "%s")
    sql = re.sub(r"\bINSERT OR IGNORE INTO\b", "INSERT INTO", sql)
    return sql


class _PGConn:
    """Thin wrapper making psycopg2 connection behave like sqlite3 for our use-case."""

    def __init__(self, raw):
        import psycopg2.extras
        self._raw = raw
        self._cur = raw.cursor(cursor_factory=psycopg2.extras.DictCursor)

    def execute(self, sql: str, params=()):
        adapted = _pg_adapt(sql)
        if re.search(r"^\s*INSERT INTO\s", adapted, re.I) and \
                "ON CONFLICT" not in adapted.upper() and \
                re.search(r"\bINSERT OR IGNORE\b", sql, re.I):
            adapted = adapted.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        self._cur.execute(adapted, params or ())
        return self._cur

    def executemany(self, sql: str, seq):
        self._cur.executemany(_pg_adapt(sql), seq)
        return self._cur

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def get_db():
    if USE_PG:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        return _PGConn(conn)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


# ─────────────────────────── Init DB ───────────────────────────

def init_db():
    conn = get_db()

    if USE_PG:
        stmts = [
            """CREATE TABLE IF NOT EXISTS admins (
                user_id  BIGINT PRIMARY KEY,
                username TEXT,
                added_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS pending_admins (
                username   TEXT PRIMARY KEY,
                granted_by BIGINT,
                granted_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS users (
                user_id   BIGINT PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                joined_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS builds (
                id         SERIAL PRIMARY KEY,
                title      TEXT,
                text       TEXT,
                file_id    TEXT,
                file_type  TEXT,
                created_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS tutorials (
                id          SERIAL PRIMARY KEY,
                title       TEXT,
                text        TEXT,
                file_id     TEXT,
                file_type   TEXT,
                video_id    TEXT,
                document_id TEXT,
                created_at  TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS tutorial_keys (
                id         SERIAL PRIMARY KEY,
                key_value  TEXT UNIQUE NOT NULL,
                used_by    BIGINT,
                used_at    TEXT,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS tutorial_access (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                key_used    TEXT,
                accessed_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS build_keys (
                id         SERIAL PRIMARY KEY,
                key_value  TEXT UNIQUE NOT NULL,
                used_by    BIGINT,
                used_at    TEXT,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS build_access (
                id          SERIAL PRIMARY KEY,
                user_id     BIGINT NOT NULL,
                key_used    TEXT,
                accessed_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS support_messages (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                username   TEXT,
                full_name  TEXT,
                message    TEXT,
                file_id    TEXT,
                file_type  TEXT,
                sent_at    TEXT,
                replied    INTEGER DEFAULT 0,
                reply_text TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS support_cooldown (
                user_id   BIGINT PRIMARY KEY,
                last_sent TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS banned_users (
                user_id   BIGINT PRIMARY KEY,
                username  TEXT,
                banned_at TEXT,
                reason    TEXT
            )""",
        ]
        for stmt in stmts:
            conn._cur.execute(stmt)
        migrations = [
            "ALTER TABLE builds    ADD COLUMN IF NOT EXISTS title TEXT",
            "ALTER TABLE tutorials ADD COLUMN IF NOT EXISTS title       TEXT",
            "ALTER TABLE tutorials ADD COLUMN IF NOT EXISTS file_id     TEXT",
            "ALTER TABLE tutorials ADD COLUMN IF NOT EXISTS file_type   TEXT",
            "ALTER TABLE tutorials ADD COLUMN IF NOT EXISTS video_id    TEXT",
            "ALTER TABLE tutorials ADD COLUMN IF NOT EXISTS document_id TEXT",
        ]
        for m in migrations:
            try:
                conn._cur.execute(m)
            except Exception:
                conn._raw.rollback()
        try:
            conn._cur.execute(
                "SELECT constraint_name FROM information_schema.table_constraints "
                "WHERE table_name='tutorial_access' AND constraint_type='PRIMARY KEY'"
            )
            pk_col = None
            row = conn._cur.fetchone()
            if row:
                conn._cur.execute(
                    "SELECT column_name FROM information_schema.key_column_usage "
                    "WHERE table_name='tutorial_access' AND constraint_name=%s",
                    (row[0],),
                )
                pk_row = conn._cur.fetchone()
                pk_col = pk_row[0] if pk_row else None
            if pk_col == "user_id":
                conn._cur.execute("ALTER TABLE tutorial_access RENAME TO tutorial_access_old")
                conn._cur.execute("""
                    CREATE TABLE tutorial_access (
                        id          SERIAL PRIMARY KEY,
                        user_id     BIGINT NOT NULL,
                        key_used    TEXT,
                        accessed_at TEXT
                    )
                """)
                conn._cur.execute("""
                    INSERT INTO tutorial_access (user_id, key_used, accessed_at)
                    SELECT user_id, key_used, accessed_at FROM tutorial_access_old
                """)
                conn._cur.execute("DROP TABLE tutorial_access_old")
                logger.info("Migrated tutorial_access to new schema (id SERIAL PK)")
        except Exception as e:
            logger.warning("tutorial_access migration skipped: %s", e)
            conn._raw.rollback()
    else:
        raw = conn
        raw.executescript("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                added_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS pending_admins (
                username   TEXT PRIMARY KEY,
                granted_by INTEGER,
                granted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                joined_at TEXT
            );
            CREATE TABLE IF NOT EXISTS builds (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                title      TEXT,
                text       TEXT,
                file_id    TEXT,
                file_type  TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tutorials (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT,
                text        TEXT,
                file_id     TEXT,
                file_type   TEXT,
                video_id    TEXT,
                document_id TEXT,
                created_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS tutorial_keys (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key_value  TEXT UNIQUE NOT NULL,
                used_by    INTEGER,
                used_at    TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tutorial_access (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                key_used    TEXT,
                accessed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS build_keys (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key_value  TEXT UNIQUE NOT NULL,
                used_by    INTEGER,
                used_at    TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS build_access (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                key_used    TEXT,
                accessed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS support_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                username   TEXT,
                full_name  TEXT,
                message    TEXT,
                file_id    TEXT,
                file_type  TEXT,
                sent_at    TEXT,
                replied    INTEGER DEFAULT 0,
                reply_text TEXT
            );
            CREATE TABLE IF NOT EXISTS support_cooldown (
                user_id   INTEGER PRIMARY KEY,
                last_sent TEXT
            );
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                banned_at TEXT,
                reason    TEXT
            );
        """)
        for sql in [
            "ALTER TABLE builds    ADD COLUMN title       TEXT",
            "ALTER TABLE tutorials ADD COLUMN title       TEXT",
            "ALTER TABLE tutorials ADD COLUMN file_id     TEXT",
            "ALTER TABLE tutorials ADD COLUMN file_type   TEXT",
            "ALTER TABLE tutorials ADD COLUMN video_id    TEXT",
            "ALTER TABLE tutorials ADD COLUMN document_id TEXT",
        ]:
            try:
                raw.execute(sql)
            except Exception:
                pass

    conn.commit()
    # Загружаем список забаненных в кэш при старте
    rows = conn.execute("SELECT user_id FROM banned_users").fetchall()
    _banned_ids.update(r["user_id"] for r in rows)
    conn.close()
    logger.info("DB initialised (backend: %s), banned cache: %d ids",
                "PostgreSQL" if USE_PG else "SQLite", len(_banned_ids))


# ─────────────────────────── DB upsert helpers ───────────────────────────

def _db_insert_ignore_user(conn, user_id, username, full_name, joined_at):
    if USE_PG:
        conn._cur.execute(
            "INSERT INTO users (user_id, username, full_name, joined_at) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE "
            "SET username = EXCLUDED.username, full_name = EXCLUDED.full_name",
            (user_id, username, full_name, joined_at),
        )
    else:
        conn.execute(
            "INSERT INTO users (user_id, username, full_name, joined_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE "
            "SET username = excluded.username, full_name = excluded.full_name",
            (user_id, username, full_name, joined_at),
        )


def _db_insert_ignore_admin(conn, user_id, username, added_at):
    if USE_PG:
        conn._cur.execute(
            "INSERT INTO admins (user_id, username, added_at) "
            "VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING",
            (user_id, username, added_at),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO admins (user_id, username, added_at) "
            "VALUES (?, ?, ?)",
            (user_id, username, added_at),
        )


def _db_upsert_pending_admin(conn, username, granted_by, granted_at):
    if USE_PG:
        conn._cur.execute(
            "INSERT INTO pending_admins (username, granted_by, granted_at) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (username) DO UPDATE "
            "SET granted_by = EXCLUDED.granted_by, granted_at = EXCLUDED.granted_at",
            (username, granted_by, granted_at),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO pending_admins (username, granted_by, granted_at) "
            "VALUES (?, ?, ?)",
            (username, granted_by, granted_at),
        )


def _db_upsert_cooldown(conn, user_id, last_sent):
    if USE_PG:
        conn._cur.execute(
            "INSERT INTO support_cooldown (user_id, last_sent) VALUES (%s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET last_sent = EXCLUDED.last_sent",
            (user_id, last_sent),
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO support_cooldown (user_id, last_sent) VALUES (?, ?)",
            (user_id, last_sent),
        )


def _db_upsert_tutorial_access(conn, user_id, key_used, accessed_at):
    if USE_PG:
        conn._cur.execute(
            "INSERT INTO tutorial_access (user_id, key_used, accessed_at) "
            "VALUES (%s, %s, %s)",
            (user_id, key_used, accessed_at),
        )
    else:
        conn.execute(
            "INSERT INTO tutorial_access (user_id, key_used, accessed_at) "
            "VALUES (?, ?, ?)",
            (user_id, key_used, accessed_at),
        )


def _db_upsert_build_access(conn, user_id, key_used, accessed_at):
    if USE_PG:
        conn._cur.execute(
            "INSERT INTO build_access (user_id, key_used, accessed_at) "
            "VALUES (%s, %s, %s)",
            (user_id, key_used, accessed_at),
        )
    else:
        conn.execute(
            "INSERT INTO build_access (user_id, key_used, accessed_at) "
            "VALUES (?, ?, ?)",
            (user_id, key_used, accessed_at),
        )


def _db_insert_support_message(conn, user_id, username, full_name, body, file_id, file_type, sent_at) -> int:
    if USE_PG:
        conn._cur.execute(
            "INSERT INTO support_messages "
            "(user_id, username, full_name, message, file_id, file_type, sent_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, username, full_name, body, file_id, file_type, sent_at),
        )
        return conn._cur.fetchone()[0]
    else:
        conn.execute(
            "INSERT INTO support_messages "
            "(user_id, username, full_name, message, file_id, file_type, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, full_name, body, file_id, file_type, sent_at),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ─────────────────────────── Button texts ───────────────────────────

BTN_BUILDS    = "📦 Все сборки"
BTN_SUPPORT   = "🆘 Поддержка"
BTN_TUTORIALS = "📚 Туторы на сборки"
BTN_ADMIN     = "🔧 Админ-панель"

# ─────────────────────────── Helpers ───────────────────────────

def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    conn = get_db()
    row = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return row is not None


def generate_key() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


def register_and_promote(user):
    """Register user and auto-promote if pending.
    Uses in-memory cache: skip DB entirely if user was already seen this run.
    Promotion check still runs once per restart (cheap — only fires when in pending_admins).
    """
    if user.id in _registered_ids:
        return
    conn = get_db()
    _db_insert_ignore_user(conn, user.id, user.username, user.full_name, datetime.now().isoformat())
    if user.username:
        row = conn.execute(
            "SELECT * FROM pending_admins WHERE username = ?", (user.username.lower(),)
        ).fetchone()
        if row:
            _db_insert_ignore_admin(conn, user.id, user.username, datetime.now().isoformat())
            conn.execute("DELETE FROM pending_admins WHERE username = ?", (user.username.lower(),))
    conn.commit()
    conn.close()
    _registered_ids.add(user.id)


def _extract_media(msg):
    if msg.photo:        return msg.photo[-1].file_id, "photo"
    if msg.video:        return msg.video.file_id,     "video"
    if msg.animation:    return msg.animation.file_id, "animation"
    if msg.audio:        return msg.audio.file_id,     "audio"
    if msg.voice:        return msg.voice.file_id,     "voice"
    if msg.sticker:      return msg.sticker.file_id,   "sticker"
    if msg.document:     return msg.document.file_id,  "document"
    return None, None


# ─────────────────────────── Keyboards ───────────────────────────

def main_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_BUILDS), KeyboardButton(BTN_SUPPORT)],
        [KeyboardButton(BTN_TUTORIALS)],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def admin_inline_kb(user_id: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📦 Опубликовать сборку",          callback_data="admin_add_build")],
        [InlineKeyboardButton("📚 Опубликовать туториал",        callback_data="admin_add_tutorial")],
        [InlineKeyboardButton("🗑 Удалить сборку",               callback_data="admin_delete_builds")],
        [InlineKeyboardButton("🗑 Удалить туториал",             callback_data="admin_delete_tutorials")],
        [InlineKeyboardButton("📊 Статистика",                   callback_data="admin_stats")],
        [InlineKeyboardButton("🔑 Создать ключ для туториалов",  callback_data="admin_create_key")],
        [InlineKeyboardButton("📋 Все ключи туториалов",         callback_data="admin_keys_list")],
        [InlineKeyboardButton("💬 Обращения в поддержку",        callback_data="admin_support_list")],
        [InlineKeyboardButton("🔄 Обнулить доступ (туториал)",   callback_data="admin_reset_sub_list")],
    ]
    if user_id == OWNER_ID:
        rows.append([InlineKeyboardButton("👤 Выдать права админа",  callback_data="admin_grant")])
        rows.append([InlineKeyboardButton("🚫 Забрать права админа", callback_data="admin_revoke_list")])
        rows.append([InlineKeyboardButton("🔨 Забанить пользователя",  callback_data="owner_ban")])
        rows.append([InlineKeyboardButton("✅ Список банов / разбан",  callback_data="owner_ban_list")])
    return InlineKeyboardMarkup(rows)


def back_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад в панель", callback_data="admin_panel")]])


# ─────────────────────────── /start ───────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    register_and_promote(user)

    await update.message.reply_text(
        f"👋 Здравствуйте, {user.first_name}!\n\n"
        "Вы попали в нашего Telegram-бота.\n"
        "Здесь вы можете найти многое о сборках.\n\n"
        "Выберите раздел:",
        reply_markup=main_kb(user.id),
    )


# ─────────────────────────── /dbinfo ───────────────────────────

async def cmd_dbinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ Нет доступа.")
        return
    try:
        conn = get_db()
        tutorials = conn.execute("SELECT COUNT(*) FROM tutorials").fetchone()[0]
        keys_total = conn.execute("SELECT COUNT(*) FROM tutorial_keys").fetchone()[0]
        keys_used  = conn.execute("SELECT COUNT(*) FROM tutorial_keys WHERE used_by IS NOT NULL").fetchone()[0]
        users      = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        last_tut   = conn.execute("SELECT id, title, created_at FROM tutorials ORDER BY id DESC LIMIT 3").fetchall()
        last_keys  = conn.execute("SELECT key_value, used_by, created_at FROM tutorial_keys ORDER BY id DESC LIMIT 3").fetchall()
        conn.close()

        backend = f"PostgreSQL\n`{DATABASE_URL[:40]}...`" if USE_PG else f"SQLite\n`{DB_PATH}`"

        tut_lines = "\n".join(
            f"  #{r['id']} — {(r['title'] or '—')[:30]} ({r['created_at'][:10]})"
            for r in last_tut
        ) or "  нет"
        key_lines = "\n".join(
            f"  `{r['key_value']}` — {'✅ использован' if r['used_by'] else '🆕 свободен'}"
            for r in last_keys
        ) or "  нет"

        await update.message.reply_text(
            f"🔍 *Диагностика БД*\n\n"
            f"*Backend:* {backend}\n\n"
            f"*Пользователей:* {users}\n"
            f"*Туториалов:* {tutorials}\n"
            f"*Ключей:* {keys_used}/{keys_total} (использовано/всего)\n\n"
            f"*Последние туториалы:*\n{tut_lines}\n\n"
            f"*Последние ключи:*\n{key_lines}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка подключения к БД:\n<code>{e}</code>", parse_mode="HTML")


# ─────────────────────────── /resetdb ───────────────────────────

async def cmd_resetdb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != OWNER_ID:
        await update.message.reply_text("❌ Только для владельца.")
        return

    args = context.args or []
    if not args or args[0] != "CONFIRM":
        await update.message.reply_text(
            "⚠️ *Внимание!* Это удалит ВСЕ данные из базы данных.\n\n"
            "Для подтверждения напишите:\n`/resetdb CONFIRM`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("🔄 Очищаю базу данных...")

    TABLES = [
        "support_cooldown", "support_messages", "tutorial_access",
        "tutorial_keys", "build_access", "build_keys",
        "tutorials", "builds", "users",
        "pending_admins", "admins",
    ]
    try:
        conn = get_db()
        if USE_PG:
            for t in TABLES:
                conn._cur.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
        else:
            for t in TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
        conn.close()

        init_db()

        backend = "PostgreSQL ☁️" if USE_PG else "SQLite 💾"
        await update.message.reply_text(
            f"✅ *База данных успешно очищена и пересоздана!*\n\n"
            f"🗄 Backend: {backend}\n"
            "Все таблицы созданы заново. Бот готов к работе.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка при сбросе БД:\n<code>{e}</code>",
            parse_mode="HTML",
        )


# ─────────────────────────── /admin ───────────────────────────

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("❌ У вас нет доступа к админ-панели.")
        return
    context.user_data.clear()
    await update.message.reply_text(
        "🔧 *Админ-панель*\n\nВыберите действие:",
        parse_mode="Markdown",
        reply_markup=admin_inline_kb(user.id),
    )


# ─────────────────────────── Support list helper ───────────────────────────

async def cb_admin_support_list(q, context, page: int = 0):
    PAGE_SIZE = 5
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM support_messages").fetchone()[0]
    rows = conn.execute(
        "SELECT id, username, full_name, message, sent_at, replied "
        "FROM support_messages ORDER BY id DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE, page * PAGE_SIZE),
    ).fetchall()
    conn.close()

    if not rows:
        await q.edit_message_text(
            "💬 *Обращения в поддержку*\n\nОбращений нет.",
            parse_mode="Markdown",
            reply_markup=back_admin_kb(),
        )
        return

    lines = []
    buttons = []
    for r in rows:
        name = f"@{r['username']}" if r["username"] else (r["full_name"] or "Без имени")
        status = "✅" if r["replied"] else "🔴"
        preview = (r["message"] or "[медиа]")[:40]
        date = (r["sent_at"] or "")[:10]
        lines.append(f"{status} #{r['id']} {name} ({date})\n   {preview}")
        if not r["replied"]:
            buttons.append([InlineKeyboardButton(
                f"💬 Ответить #{r['id']}",
                callback_data=f"admin_reply_{r['id']}",
            )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"admin_support_page_{page - 1}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"admin_support_page_{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])

    text = f"💬 *Обращения* (стр. {page + 1}, всего {total})\n\n" + "\n\n".join(lines)
    if len(text) > 4096:
        text = text[:4090] + "\n…"
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


# ─────────────────────────── Save tutorial helper ───────────────────────────

async def _save_tutorial(q_or_msg, context):
    title      = context.user_data.get("tut_title", "")
    text       = context.user_data.get("tut_text") or ""
    file_id    = context.user_data.get("tut_file_id")
    file_type  = context.user_data.get("tut_file_type")
    video_id   = context.user_data.get("tut_video_id")
    doc_id     = context.user_data.get("tut_doc_id")

    conn = get_db()
    conn.execute(
        "INSERT INTO tutorials (title, text, file_id, file_type, video_id, document_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, text, file_id, file_type, video_id, doc_id, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    context.user_data.clear()

    edit_fn = q_or_msg.edit_message_text if hasattr(q_or_msg, "edit_message_text") else q_or_msg.reply_text
    await edit_fn(
        f"✅ *Туториал «{title}» опубликован!*",
        parse_mode="Markdown",
        reply_markup=back_admin_kb(),
    )


# ─────────────────────────── Callback router ───────────────────────────

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    user = q.from_user
    register_and_promote(user)

    if user.id in _banned_ids and user.id != OWNER_ID:
        await q.answer("🚫 Вы заблокированы.", show_alert=True)
        return

    if data == "admin_panel":
        if not is_admin(user.id):
            await q.edit_message_text("❌ Нет доступа.")
            return
        context.user_data.clear()
        await q.edit_message_text(
            "🔧 *Админ-панель*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=admin_inline_kb(user.id),
        )

    elif data == "admin_stats":
        if not is_admin(user.id):
            return
        conn = get_db()
        total_users     = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_builds    = conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0]
        total_tutorials = conn.execute("SELECT COUNT(*) FROM tutorials").fetchone()[0]
        total_keys      = conn.execute("SELECT COUNT(*) FROM tutorial_keys").fetchone()[0]
        used_keys       = conn.execute("SELECT COUNT(*) FROM tutorial_keys WHERE used_by IS NOT NULL").fetchone()[0]
        free_keys       = total_keys - used_keys
        key_users = conn.execute(
            "SELECT tk.key_value, tk.used_at, tk.created_at, "
            "u.username, u.full_name, tk.used_by "
            "FROM tutorial_keys tk "
            "LEFT JOIN users u ON u.user_id = tk.used_by "
            "WHERE tk.used_by IS NOT NULL "
            "ORDER BY tk.used_at DESC LIMIT 5"
        ).fetchall()
        conn.close()

        if key_users:
            klines = []
            for r in key_users:
                name = html.escape(f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["used_by"])))
                key  = html.escape(r["key_value"] or "—")
                date = (r["used_at"] or "")[:10]
                klines.append(f"  • {name} — <code>{key}</code> ({date})")
            key_lines = "\n" + "\n".join(klines)
        else:
            key_lines = "\n  <i>нет активных</i>"

        db_type = "PostgreSQL ☁️" if USE_PG else "SQLite 💾"
        text = (
            "📊 <b>Статистика бота</b>\n\n"
            f"👥 Пользователей: <b>{total_users}</b>\n"
            f"📦 Сборок: <b>{total_builds}</b>\n"
            f"📚 Туториалов: <b>{total_tutorials}</b>\n"
            f"🗄 База данных: <b>{html.escape(db_type)}</b>\n\n"
            f"🔑 <b>Ключи:</b>\n"
            f"  • Всего создано: <b>{total_keys}</b>\n"
            f"  • Использовано: <b>{used_keys}</b>\n"
            f"  • Свободных: <b>{free_keys}</b>\n\n"
            f"<b>Последние активации:</b>{key_lines}"
        )
        if len(text) > 4096:
            text = text[:4090] + "\n…"
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Все ключи", callback_data="admin_keys_list")],
                [InlineKeyboardButton("◀️ Назад в панель", callback_data="admin_panel")],
            ])
        )

    elif data == "admin_keys_list":
        if not is_admin(user.id):
            return
        conn = get_db()
        all_keys = conn.execute(
            "SELECT tk.key_value, tk.created_at, tk.used_at, tk.used_by, "
            "u.username, u.full_name "
            "FROM tutorial_keys tk "
            "LEFT JOIN users u ON u.user_id = tk.used_by "
            "ORDER BY tk.id DESC"
        ).fetchall()
        conn.close()

        if not all_keys:
            await q.edit_message_text(
                "🔑 *Ключи*\n\nКлючей ещё нет. Создайте первый ключ.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔑 Создать ключ", callback_data="admin_create_key")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
                ])
            )
            return

        lines = []
        free_count = 0
        used_count = 0
        for r in all_keys:
            key_esc = html.escape(r["key_value"] or "")
            if r["used_by"]:
                used_count += 1
                name = html.escape(f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["used_by"])))
                date = (r["used_at"] or "")[:10]
                lines.append(f"🔴 <code>{key_esc}</code>\n   └ {name} ({date})")
            else:
                free_count += 1
                created = (r["created_at"] or "")[:10]
                lines.append(f"🟢 <code>{key_esc}</code>\n   └ свободен (создан {created})")

        header = (
            f"📋 <b>Все ключи</b> — всего {len(all_keys)}\n"
            f"🟢 Свободных: <b>{free_count}</b>  |  🔴 Использованных: <b>{used_count}</b>\n\n"
        )
        body = "\n".join(lines)
        text = header + body
        if len(text) > 4096:
            text = text[:4080] + "\n\n…и ещё больше"
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Создать ключ", callback_data="admin_create_key")],
                [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
            ])
        )

    elif data == "admin_add_build":
        if not is_admin(user.id):
            return
        context.user_data["state"] = "admin_build_msg"
        await q.edit_message_text(
            "📦 *Публикация сборки*\n\n"
            "Отправьте сообщение со сборкой — как будто пишете в обычный чат.\n\n"
            "Можно отправить *текст*, *фото*, *видео*, *файл*, *аудио*, *GIF*, *стикер* — всё что угодно.\n"
            "Первая строка текста/подписи станет названием в списке.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]),
        )

    elif data == "admin_add_tutorial":
        if not is_admin(user.id):
            return
        context.user_data["state"]        = "admin_tutorial_title"
        context.user_data["tut_text"]     = None
        context.user_data["tut_video_id"] = None
        context.user_data["tut_doc_id"]   = None
        await q.edit_message_text(
            "📚 *Публикация туториала — шаг 1/4*\n\n"
            "Введите *название* туториала (будет отображаться в списке):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]),
        )

    elif data == "tutorial_skip_text":
        if not is_admin(user.id):
            return
        context.user_data["state"] = "admin_tutorial_video"
        await q.edit_message_text(
            "📚 *Публикация туториала — шаг 3/4*\n\n"
            "Отправьте *видео* для туториала (или пропустите):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить видео", callback_data="tutorial_skip_video")],
                [InlineKeyboardButton("◀️ Отмена",          callback_data="admin_panel")],
            ]),
        )

    elif data == "tutorial_skip_video":
        if not is_admin(user.id):
            return
        context.user_data["state"] = "admin_tutorial_file"
        await q.edit_message_text(
            "📚 *Публикация туториала — шаг 4/4*\n\n"
            "Отправьте *файл* для туториала (или сохраните без файла):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Сохранить без файла", callback_data="tutorial_skip_file")],
                [InlineKeyboardButton("◀️ Отмена",              callback_data="admin_panel")],
            ]),
        )

    elif data == "tutorial_skip_file":
        if not is_admin(user.id):
            return
        await _save_tutorial(q, context)

    elif data == "admin_delete_builds":
        if not is_admin(user.id):
            return
        conn = get_db()
        rows = conn.execute("SELECT id, title, text FROM builds ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            await q.edit_message_text(
                "🗑 *Удалить сборку*\n\nСборок нет.",
                parse_mode="Markdown",
                reply_markup=back_admin_kb(),
            )
            return
        buttons = []
        for r in rows:
            label = (r["title"] or r["text"] or "—")[:40]
            buttons.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"del_build_{r['id']}")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
        await q.edit_message_text(
            "🗑 *Удалить сборку*\n\nВыберите публикацию для удаления:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data == "admin_delete_tutorials":
        if not is_admin(user.id):
            return
        conn = get_db()
        rows = conn.execute("SELECT id, title, text FROM tutorials ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            await q.edit_message_text(
                "🗑 *Удалить туториал*\n\nТуториалов нет.",
                parse_mode="Markdown",
                reply_markup=back_admin_kb(),
            )
            return
        buttons = []
        for r in rows:
            label = (r["title"] or r["text"] or "—")[:40]
            buttons.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"del_tutorial_{r['id']}")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
        await q.edit_message_text(
            "🗑 *Удалить туториал*\n\nВыберите публикацию для удаления:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("del_build_confirm_"):
        if not is_admin(user.id):
            return
        item_id = int(data.split("_")[3])
        conn = get_db()
        conn.execute("DELETE FROM builds WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        await q.edit_message_text("✅ Сборка удалена.", reply_markup=back_admin_kb())

    elif data.startswith("del_build_"):
        if not is_admin(user.id):
            return
        item_id = int(data.split("_")[2])
        conn = get_db()
        row = conn.execute("SELECT title, text FROM builds WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        if not row:
            await q.edit_message_text("❌ Сборка не найдена.", reply_markup=back_admin_kb())
            return
        label = row["title"] or (row["text"] or "—")[:40]
        await q.edit_message_text(
            f"❓ Удалить сборку *{label}*?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, удалить",  callback_data=f"del_build_confirm_{item_id}")],
                [InlineKeyboardButton("❌ Нет",           callback_data="admin_delete_builds")],
            ]),
        )

    elif data.startswith("del_tutorial_confirm_"):
        if not is_admin(user.id):
            return
        item_id = int(data.split("_")[3])
        conn = get_db()
        conn.execute("DELETE FROM tutorials WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        await q.edit_message_text("✅ Туториал удалён.", reply_markup=back_admin_kb())

    elif data.startswith("del_tutorial_"):
        if not is_admin(user.id):
            return
        item_id = int(data.split("_")[2])
        conn = get_db()
        row = conn.execute("SELECT title, text FROM tutorials WHERE id = ?", (item_id,)).fetchone()
        conn.close()
        if not row:
            await q.edit_message_text("❌ Туториал не найден.", reply_markup=back_admin_kb())
            return
        label = row["title"] or (row["text"] or "—")[:40]
        await q.edit_message_text(
            f"❓ Удалить туториал *{label}*?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, удалить",  callback_data=f"del_tutorial_confirm_{item_id}")],
                [InlineKeyboardButton("❌ Нет",           callback_data="admin_delete_tutorials")],
            ]),
        )

    elif data == "admin_create_key":
        if not is_admin(user.id):
            return
        key = generate_key()
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO tutorial_keys (key_value, created_at) VALUES (?, ?)",
                (key, datetime.now().isoformat()),
            )
            conn.commit()
        except Exception as e:
            logger.error("Failed to create key: %s", e)
            try:
                conn._raw.rollback()
            except Exception:
                pass
            await q.edit_message_text(
                f"❌ Ошибка создания ключа:\n<code>{e}</code>",
                parse_mode="HTML",
                reply_markup=back_admin_kb(),
            )
            return
        finally:
            conn.close()
        backend = "PostgreSQL" if USE_PG else f"SQLite"
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🔑 *Ключ создан!*\n\n`{key}`\n\n"
                 "Нажмите на ключ чтобы скопировать. "
                 "Ключ одноразовый — привязывается к одному аккаунту навсегда.",
            parse_mode="Markdown",
        )
        await q.edit_message_text(
            f"✅ *Ключ создан и отправлен в чат!*\n\n"
            f"`{key}`\n\n"
            f"_БД: {backend}_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Ещё один ключ", callback_data="admin_create_key")],
                [InlineKeyboardButton("◀️ Назад",          callback_data="admin_panel")],
            ]),
        )

    elif data == "admin_grant":
        if user.id != OWNER_ID:
            return
        context.user_data["state"] = "admin_grant"
        await q.edit_message_text(
            "👤 Введите *username* пользователя Telegram (без @),\n"
            "которому хотите выдать права администратора:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]),
        )

    elif data.startswith("confirm_grant_"):
        if user.id != OWNER_ID:
            return
        username = data[len("confirm_grant_"):]
        conn = get_db()
        already = conn.execute(
            "SELECT 1 FROM admins WHERE LOWER(username) = ?", (username.lower(),)
        ).fetchone()
        if already:
            conn.close()
            context.user_data.clear()
            await q.edit_message_text(
                f"ℹ️ @{username} уже является администратором.",
                reply_markup=back_admin_kb(),
            )
            return
        known = conn.execute(
            "SELECT user_id FROM users WHERE LOWER(username) = ?", (username.lower(),)
        ).fetchone()
        if known:
            target_id = known["user_id"]
            _db_insert_ignore_admin(conn, target_id, username, datetime.now().isoformat())
            conn.execute("DELETE FROM pending_admins WHERE LOWER(username) = ?", (username.lower(),))
            conn.commit()
            conn.close()
            context.user_data.clear()
            try:
                await context.bot.send_message(
                    target_id,
                    "✅ Вам выданы права администратора! Нажмите /start чтобы увидеть кнопку админ-панели.",
                )
            except Exception:
                pass
            await q.edit_message_text(
                f"✅ Права администратора выданы @{username} немедленно.",
                reply_markup=back_admin_kb(),
            )
        else:
            _db_upsert_pending_admin(conn, username.lower(), user.id, datetime.now().isoformat())
            conn.commit()
            conn.close()
            context.user_data.clear()
            await q.edit_message_text(
                f"⏳ @{username} ещё не запускал бота.\n\n"
                "Права будут выданы автоматически, как только они напишут /start.",
                reply_markup=back_admin_kb(),
            )

    elif data == "admin_support_list":
        if not is_admin(user.id):
            return
        await cb_admin_support_list(q, context, page=0)

    elif data.startswith("admin_support_page_"):
        if not is_admin(user.id):
            return
        page = int(data.split("_")[3])
        await cb_admin_support_list(q, context, page=page)

    elif data.startswith("admin_reply_"):
        if not is_admin(user.id):
            return
        msg_id = int(data.split("_")[2])
        context.user_data["state"]        = "admin_reply"
        context.user_data["reply_msg_id"] = msg_id
        conn = get_db()
        row  = conn.execute("SELECT * FROM support_messages WHERE id = ?", (msg_id,)).fetchone()
        conn.close()
        preview = (row["message"] or "[медиафайл]") if row else "?"
        await q.edit_message_text(
            f"💬 *Ответ на обращение #{msg_id}*\n\n"
            f"Сообщение:\n_{preview}_\n\n"
            "Напишите ответ:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_support_list")]]),
        )

    elif data == "admin_revoke_list":
        if user.id != OWNER_ID:
            return
        conn = get_db()
        admins = conn.execute(
            "SELECT user_id, username FROM admins WHERE user_id != ?", (OWNER_ID,)
        ).fetchall()
        conn.close()
        if not admins:
            await q.edit_message_text(
                "🚫 *Забрать права*\n\nДополнительных администраторов нет.",
                parse_mode="Markdown",
                reply_markup=back_admin_kb(),
            )
            return
        buttons = [
            [InlineKeyboardButton(
                f"🚫 @{r['username'] or r['user_id']}",
                callback_data=f"revoke_admin_{r['user_id']}",
            )]
            for r in admins
        ]
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
        await q.edit_message_text(
            "🚫 *Выберите администратора для снятия прав:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("revoke_admin_"):
        if user.id != OWNER_ID:
            return
        target_id = int(data.split("_")[2])
        conn = get_db()
        conn.execute("DELETE FROM admins WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        try:
            await context.bot.send_message(target_id, "❌ Ваши права администратора были отозваны.")
        except Exception:
            pass
        await q.edit_message_text("✅ Права администратора отозваны.", reply_markup=back_admin_kb())

    elif data == "owner_ban":
        if user.id != OWNER_ID:
            return
        context.user_data["state"] = "owner_ban_input"
        await q.edit_message_text(
            "🔨 *Бан пользователя*\n\n"
            "Введите *username* (без @) или *числовой ID* пользователя:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]),
        )

    elif data == "owner_ban_list":
        if user.id != OWNER_ID:
            return
        conn = get_db()
        bans = conn.execute(
            "SELECT user_id, username, banned_at FROM banned_users ORDER BY banned_at DESC"
        ).fetchall()
        conn.close()
        if not bans:
            await q.edit_message_text(
                "✅ *Список банов*\n\nЗабаненных пользователей нет.",
                parse_mode="Markdown",
                reply_markup=back_admin_kb(),
            )
            return
        buttons = []
        for r in bans:
            label = f"@{r['username']}" if r["username"] else str(r["user_id"])
            date  = (r["banned_at"] or "")[:10]
            buttons.append([InlineKeyboardButton(
                f"✅ Разбанить {label} ({date})",
                callback_data=f"owner_unban_{r['user_id']}",
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
        await q.edit_message_text(
            f"🔨 *Забаненные пользователи* ({len(bans)}):\n\nНажмите чтобы разбанить:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("confirm_ban_"):
        if user.id != OWNER_ID:
            return
        target_id = int(data.split("_")[2])
        if target_id == OWNER_ID:
            await q.edit_message_text("❌ Нельзя забанить создателя.", reply_markup=back_admin_kb())
            return
        conn = get_db()
        target = conn.execute("SELECT username, full_name FROM users WHERE user_id = ?", (target_id,)).fetchone()
        uname  = target["username"] if target and target["username"] else None
        now    = datetime.now().isoformat()
        if USE_PG:
            conn._cur.execute(
                "INSERT INTO banned_users (user_id, username, banned_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET banned_at = EXCLUDED.banned_at",
                (target_id, uname, now),
            )
        else:
            conn.execute(
                "INSERT OR REPLACE INTO banned_users (user_id, username, banned_at) VALUES (?, ?, ?)",
                (target_id, uname, now),
            )
        # Убираем из кэша регистрации — следующее сообщение от него опять упрётся в бан
        _registered_ids.discard(target_id)
        conn.commit()
        conn.close()
        _banned_ids.add(target_id)
        context.user_data.clear()
        label = f"@{uname}" if uname else str(target_id)
        try:
            await context.bot.send_message(target_id, "🚫 Вы заблокированы создателем бота.")
        except Exception:
            pass
        await q.edit_message_text(
            f"🔨 Пользователь {label} заблокирован.",
            reply_markup=back_admin_kb(),
        )

    elif data.startswith("owner_unban_"):
        if user.id != OWNER_ID:
            return
        target_id = int(data.split("_")[2])
        conn = get_db()
        target = conn.execute("SELECT username FROM banned_users WHERE user_id = ?", (target_id,)).fetchone()
        conn.execute("DELETE FROM banned_users WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        _banned_ids.discard(target_id)
        label = f"@{target['username']}" if target and target["username"] else str(target_id)
        try:
            await context.bot.send_message(target_id, "✅ Вы разблокированы. Напишите /start.")
        except Exception:
            pass
        await q.edit_message_text(
            f"✅ Пользователь {label} разблокирован.",
            reply_markup=back_admin_kb(),
        )

    elif data == "admin_reset_sub_list":
        if not is_admin(user.id):
            return
        conn = get_db()
        rows = conn.execute(
            "SELECT ta.user_id, u.username, u.full_name, COUNT(ta.id) as cnt "
            "FROM tutorial_access ta "
            "LEFT JOIN users u ON u.user_id = ta.user_id "
            "GROUP BY ta.user_id, u.username, u.full_name "
            "ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
        conn.close()
        if not rows:
            await q.edit_message_text(
                "🔄 *Обнулить подписку*\n\nНет пользователей с доступом.",
                parse_mode="Markdown",
                reply_markup=back_admin_kb(),
            )
            return
        buttons = []
        for r in rows:
            name = f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["user_id"]))
            buttons.append([InlineKeyboardButton(
                f"🔄 {name} ({r['cnt']} ключей)",
                callback_data=f"reset_sub_{r['user_id']}",
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
        await q.edit_message_text(
            "🔄 *Выберите пользователя для обнуления подписки:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("reset_sub_"):
        if not is_admin(user.id):
            return
        target_id = int(data.split("_")[2])
        conn = get_db()
        # Удаляем ключ полностью — повторно ввести его нельзя
        conn.execute("DELETE FROM tutorial_keys WHERE used_by = ?", (target_id,))
        conn.execute("DELETE FROM tutorial_access WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        await q.edit_message_text(
            f"✅ Подписка (туториалы) пользователя {target_id} обнулена.\n"
            "Ключ удалён — повторно ввести его невозможно.",
            reply_markup=back_admin_kb(),
        )

    # ── Build keys admin handlers ──

    elif data == "admin_create_build_key":
        if not is_admin(user.id):
            return
        key = generate_key()
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO build_keys (key_value, created_at) VALUES (?, ?)",
                (key, datetime.now().isoformat()),
            )
            conn.commit()
        except Exception as e:
            logger.error("Failed to create build key: %s", e)
            try:
                conn._raw.rollback()
            except Exception:
                pass
            await q.edit_message_text(
                f"❌ Ошибка создания ключа:\n<code>{e}</code>",
                parse_mode="HTML",
                reply_markup=back_admin_kb(),
            )
            return
        finally:
            conn.close()
        await context.bot.send_message(
            chat_id=user.id,
            text=f"🔑 *Ключ для сборок создан!*\n\n`{key}`\n\n"
                 "Нажмите на ключ чтобы скопировать. "
                 "Ключ одноразовый — привязывается к одному аккаунту навсегда.",
            parse_mode="Markdown",
        )
        await q.edit_message_text(
            f"✅ *Ключ для сборок создан!*\n\n`{key}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Ещё один ключ", callback_data="admin_create_build_key")],
                [InlineKeyboardButton("◀️ Назад",          callback_data="admin_panel")],
            ]),
        )

    elif data == "admin_build_keys_list":
        if not is_admin(user.id):
            return
        conn = get_db()
        all_keys = conn.execute(
            "SELECT bk.key_value, bk.created_at, bk.used_at, bk.used_by, "
            "u.username, u.full_name "
            "FROM build_keys bk "
            "LEFT JOIN users u ON u.user_id = bk.used_by "
            "ORDER BY bk.id DESC"
        ).fetchall()
        conn.close()
        if not all_keys:
            await q.edit_message_text(
                "🔑 *Ключи сборок*\n\nКлючей ещё нет. Создайте первый ключ.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔑 Создать ключ", callback_data="admin_create_build_key")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
                ])
            )
            return
        lines = []
        free_count = 0
        used_count = 0
        for r in all_keys:
            if r["used_by"]:
                used_count += 1
                name = f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["used_by"]))
                date = (r["used_at"] or "")[:10]
                lines.append(f"🔴 `{r['key_value']}`\n   └ {name} ({date})")
            else:
                free_count += 1
                created = (r["created_at"] or "")[:10]
                lines.append(f"🟢 `{r['key_value']}`\n   └ свободен (создан {created})")
        header = (
            f"📋 *Ключи сборок* — всего {len(all_keys)}\n"
            f"🟢 Свободных: *{free_count}*  |  🔴 Использованных: *{used_count}*\n\n"
        )
        text = header + "\n".join(lines)
        if len(text) > 4096:
            text = text[:4080] + "\n\n…и ещё больше"
        await q.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Создать ключ", callback_data="admin_create_build_key")],
                [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
            ])
        )

    elif data == "admin_reset_build_sub_list":
        if not is_admin(user.id):
            return
        conn = get_db()
        rows = conn.execute(
            "SELECT ba.user_id, u.username, u.full_name, COUNT(ba.id) as cnt "
            "FROM build_access ba "
            "LEFT JOIN users u ON u.user_id = ba.user_id "
            "GROUP BY ba.user_id, u.username, u.full_name "
            "ORDER BY cnt DESC LIMIT 20"
        ).fetchall()
        conn.close()
        if not rows:
            await q.edit_message_text(
                "🔄 *Обнулить подписку (сборки)*\n\nНет пользователей с доступом.",
                parse_mode="Markdown",
                reply_markup=back_admin_kb(),
            )
            return
        buttons = []
        for r in rows:
            name = f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["user_id"]))
            buttons.append([InlineKeyboardButton(
                f"🔄 {name} ({r['cnt']} ключей)",
                callback_data=f"reset_build_sub_{r['user_id']}",
            )])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")])
        await q.edit_message_text(
            "🔄 *Выберите пользователя для обнуления доступа к сборкам:*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    elif data.startswith("reset_build_sub_"):
        if not is_admin(user.id):
            return
        target_id = int(data.split("_")[3])
        conn = get_db()
        conn.execute("UPDATE build_keys SET used_by = NULL, used_at = NULL WHERE used_by = ?", (target_id,))
        conn.execute("DELETE FROM build_access WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        await q.edit_message_text(
            f"✅ Доступ к сборкам пользователя {target_id} обнулён.",
            reply_markup=back_admin_kb(),
        )

    elif data.startswith("view_build_"):
        build_id = int(data.split("_")[2])
        conn = get_db()
        row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
        conn.close()
        if not row:
            await q.edit_message_text("❌ Сборка не найдена.")
            return
        caption = row["text"] or row["title"] or ""
        fid  = row["file_id"]
        ftype = row["file_type"]
        chat  = q.message.chat_id
        if fid:
            if ftype == "photo":
                await context.bot.send_photo(chat_id=chat, photo=fid, caption=caption or None)
            elif ftype == "video":
                await context.bot.send_video(chat_id=chat, video=fid, caption=caption or None)
            elif ftype == "animation":
                await context.bot.send_animation(chat_id=chat, animation=fid, caption=caption or None)
            elif ftype == "audio":
                await context.bot.send_audio(chat_id=chat, audio=fid, caption=caption or None)
            elif ftype == "voice":
                await context.bot.send_voice(chat_id=chat, voice=fid, caption=caption or None)
            elif ftype == "sticker":
                if caption:
                    await context.bot.send_message(chat_id=chat, text=caption)
                await context.bot.send_sticker(chat_id=chat, sticker=fid)
            else:  # document и всё остальное
                await context.bot.send_document(chat_id=chat, document=fid, caption=caption or None)
        else:
            if caption:
                await context.bot.send_message(chat_id=chat, text=caption)

    elif data.startswith("view_tutorial_"):
        tut_id = int(data.split("_")[2])
        user_id = user.id
        conn = get_db()
        # Check access
        access = conn.execute(
            "SELECT id FROM tutorial_access WHERE user_id = ? LIMIT 1", (user_id,)
        ).fetchone()
        if not access and user_id != OWNER_ID and not is_admin(user_id):
            conn.close()
            await q.answer("🔑 Для просмотра туториала нужен ключ доступа.", show_alert=True)
            return
        row = conn.execute("SELECT * FROM tutorials WHERE id = ?", (tut_id,)).fetchone()
        conn.close()
        if not row:
            await q.edit_message_text("❌ Туториал не найден.")
            return
        text = row["text"] or row["title"] or "Туториал"
        sent = False
        if row["video_id"]:
            await context.bot.send_video(chat_id=q.message.chat_id, video=row["video_id"], caption=text if not sent else None)
            sent = True
        if row["file_id"]:
            ft = row["file_type"] or "document"
            if ft == "photo":
                await context.bot.send_photo(chat_id=q.message.chat_id, photo=row["file_id"], caption=text if not sent else None)
            elif ft == "video":
                await context.bot.send_video(chat_id=q.message.chat_id, video=row["file_id"], caption=text if not sent else None)
            else:
                await context.bot.send_document(chat_id=q.message.chat_id, document=row["file_id"], caption=text if not sent else None)
            sent = True
        if not sent:
            await context.bot.send_message(chat_id=q.message.chat_id, text=text)


# ─────────────────────────── Message handler ───────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg  = update.message
    user = update.effective_user
    text = msg.text or ""
    register_and_promote(user)

    if user.id in _banned_ids and user.id != OWNER_ID:
        await msg.reply_text("🚫 Вы заблокированы и не можете использовать бота.")
        return

    state = context.user_data.get("state")

    # ── Main menu buttons ──

    if text == BTN_BUILDS:
        conn = get_db()
        rows = conn.execute("SELECT id, title, text FROM builds ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            await msg.reply_text("📦 Сборок пока нет.")
            return
        buttons = [[InlineKeyboardButton(
            (r["title"] or r["text"] or f"Сборка #{r['id']}")[:40],
            callback_data=f"view_build_{r['id']}"
        )] for r in rows]
        await msg.reply_text("📦 *Все сборки:*", parse_mode="Markdown",
                             reply_markup=InlineKeyboardMarkup(buttons))
        return

    if text == BTN_TUTORIALS:
        conn = get_db()
        has_access = conn.execute(
            "SELECT id FROM tutorial_access WHERE user_id = ? LIMIT 1", (user.id,)
        ).fetchone()
        admin = is_admin(user.id)
        if not has_access and not admin:
            conn.close()
            context.user_data["state"] = "awaiting_key"
            await msg.reply_text(
                "📚 *Туториалы на сборки*\n\n"
                "🔑 Для доступа нужен ключ.\n\n"
                "Введите ваш ключ:",
                parse_mode="Markdown",
            )
            return
        rows = conn.execute("SELECT id, title, text FROM tutorials ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            await msg.reply_text("📚 Туториалов пока нет.")
            return
        buttons = [[InlineKeyboardButton(
            (r["title"] or r["text"] or f"Туториал #{r['id']}")[:40],
            callback_data=f"view_tutorial_{r['id']}"
        )] for r in rows]
        await msg.reply_text("📚 *Туториалы на сборки:*", parse_mode="Markdown",
                             reply_markup=InlineKeyboardMarkup(buttons))
        return

    if text == BTN_SUPPORT:
        await msg.reply_text(
            "🆘 *Поддержка*\n\n"
            "Опишите вашу проблему или вопрос — отправьте сообщение (текст, фото, видео или файл).\n\n"
            "⏱ Между сообщениями — 5 минут.",
            parse_mode="Markdown",
        )
        context.user_data["state"] = "support_waiting"
        return

    if text == BTN_ADMIN:
        if not is_admin(user.id):
            await msg.reply_text("❌ Нет доступа.")
            return
        context.user_data.clear()
        await msg.reply_text(
            "🔧 *Админ-панель*\n\nВыберите действие:",
            parse_mode="Markdown",
            reply_markup=admin_inline_kb(user.id),
        )
        return

    # ── State machine ──

    if state == "owner_ban_input":
        if user.id != OWNER_ID:
            return
        context.user_data["state"] = None
        raw = text.strip().lstrip("@")
        conn = get_db()
        # Ищем по username или по числовому ID
        if raw.isdigit():
            target = conn.execute("SELECT user_id, username FROM users WHERE user_id = ?", (int(raw),)).fetchone()
            target_id = int(raw) if not target else target["user_id"]
            uname = target["username"] if target else None
        else:
            target = conn.execute(
                "SELECT user_id, username FROM users WHERE LOWER(username) = ?", (raw.lower(),)
            ).fetchone()
            target_id = target["user_id"] if target else None
            uname     = raw if not target else target["username"]
        conn.close()
        if not target_id:
            await msg.reply_text(
                f"❌ Пользователь @{raw} не найден в базе.\n\n"
                "Он должен хотя бы раз написать боту. Введите другой username или ID:",
            )
            context.user_data["state"] = "owner_ban_input"
            return
        if target_id == OWNER_ID:
            await msg.reply_text("❌ Нельзя забанить создателя.")
            return
        label = f"@{uname}" if uname else str(target_id)
        await msg.reply_text(
            f"🔨 Забанить {label} (ID: {target_id})?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да, забанить",  callback_data=f"confirm_ban_{target_id}")],
                [InlineKeyboardButton("❌ Отмена",        callback_data="admin_panel")],
            ]),
        )
        return

    if state == "awaiting_key":
        key_value = text.strip().upper()
        context.user_data["state"] = None
        conn = get_db()
        # 1 пользователь — 1 ключ: проверяем, нет ли уже доступа
        already = conn.execute(
            "SELECT id FROM tutorial_access WHERE user_id = ? LIMIT 1", (user.id,)
        ).fetchone()
        if already:
            conn.close()
            await msg.reply_text(
                "ℹ️ У вас уже активирован ключ — доступ к туториалам открыт.",
                reply_markup=main_kb(user.id),
            )
            return
        row = conn.execute(
            "SELECT * FROM tutorial_keys WHERE key_value = ?", (key_value,)
        ).fetchone()
        if not row:
            conn.close()
            await msg.reply_text(
                "❌ Ключ не найден. Проверьте правильность ввода и попробуйте ещё раз.\n\n"
                "Введите ваш ключ:",
            )
            context.user_data["state"] = "awaiting_key"
            return
        if row["used_by"] is not None:
            conn.close()
            if row["used_by"] == user.id:
                await msg.reply_text(
                    "ℹ️ Этот ключ уже привязан к вашему аккаунту.",
                    reply_markup=main_kb(user.id),
                )
            else:
                await msg.reply_text(
                    "❌ Этот ключ уже использован другим пользователем.\n\n"
                    "Введите другой ключ:",
                )
                context.user_data["state"] = "awaiting_key"
            return
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE tutorial_keys SET used_by = ?, used_at = ? WHERE key_value = ?",
            (user.id, now, key_value),
        )
        _db_upsert_tutorial_access(conn, user.id, key_value, now)
        conn.commit()
        conn.close()
        await msg.reply_text(
            "✅ *Ключ активирован!*\n\n"
            "Доступ к туториалам открыт. Нажмите «📚 Туторы на сборки» в меню.",
            parse_mode="Markdown",
            reply_markup=main_kb(user.id),
        )
        return

    if state == "support_waiting":
        # Cooldown check
        conn = get_db()
        cd = conn.execute("SELECT last_sent FROM support_cooldown WHERE user_id = ?", (user.id,)).fetchone()
        if cd:
            last = datetime.fromisoformat(cd["last_sent"])
            if datetime.now() - last < timedelta(minutes=5):
                remaining = 5 - int((datetime.now() - last).total_seconds() // 60)
                conn.close()
                await msg.reply_text(f"⏱ Подождите ещё {remaining} мин. перед следующим сообщением.")
                return
        body    = msg.text or msg.caption or ""
        file_id, file_type = _extract_media(msg)
        msg_id = _db_insert_support_message(
            conn, user.id, user.username, user.full_name,
            body, file_id, file_type, datetime.now().isoformat(),
        )
        _db_upsert_cooldown(conn, user.id, datetime.now().isoformat())
        conn.commit()
        conn.close()
        context.user_data["state"] = None
        await msg.reply_text(
            f"✅ Обращение #{msg_id} принято! Мы ответим вам в ближайшее время.",
            reply_markup=main_kb(user.id),
        )
        return

    if state == "admin_reply":
        if not is_admin(user.id):
            return
        reply_text = msg.text or ""
        msg_id     = context.user_data.get("reply_msg_id")
        conn = get_db()
        row  = conn.execute("SELECT user_id FROM support_messages WHERE id = ?", (msg_id,)).fetchone()
        conn.execute(
            "UPDATE support_messages SET replied = 1, reply_text = ? WHERE id = ?",
            (reply_text, msg_id),
        )
        conn.commit()
        conn.close()
        context.user_data.clear()
        if row:
            try:
                await context.bot.send_message(
                    row["user_id"],
                    f"📩 *Ответ от поддержки на ваше обращение #{msg_id}:*\n\n{reply_text}",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
        await msg.reply_text(
            f"✅ Ответ отправлен пользователю (обращение #{msg_id}).",
            reply_markup=main_kb(user.id),
        )
        return

    if state == "admin_grant":
        if user.id != OWNER_ID:
            return
        username = text.lstrip("@").strip()
        if not username:
            await msg.reply_text("❌ Неверный username.")
            return
        context.user_data["state"] = None
        await msg.reply_text(
            f"❓ Выдать права администратора @{username}?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Да", callback_data=f"confirm_grant_{username}")],
                [InlineKeyboardButton("❌ Нет", callback_data="admin_panel")],
            ]),
        )
        return

    # ── Admin build flow ──

    if state == "admin_build_msg":
        if not is_admin(user.id):
            return
        body = msg.text or msg.caption or ""
        # Первая строка — название; если пусто — дата
        first_line = body.split("\n")[0].strip()
        title = first_line[:60] if first_line else datetime.now().strftime("Сборка %d.%m.%Y %H:%M")
        file_id, file_type = _extract_media(msg)
        conn = get_db()
        conn.execute(
            "INSERT INTO builds (title, text, file_id, file_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (title, body, file_id, file_type, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
        context.user_data.clear()
        await msg.reply_text(
            f"✅ *Сборка опубликована!*\n\nНазвание в списке: _{title}_",
            parse_mode="Markdown",
            reply_markup=main_kb(user.id),
        )
        return

    # ── Admin tutorial flow ──

    if state == "admin_tutorial_title":
        if not is_admin(user.id):
            return
        context.user_data["tut_title"] = text
        context.user_data["state"]     = "admin_tutorial_text"
        await msg.reply_text(
            "📚 *Публикация туториала — шаг 2/4*\n\n"
            "Отправьте *текст* туториала (или пропустите):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить текст", callback_data="tutorial_skip_text")],
                [InlineKeyboardButton("◀️ Отмена",          callback_data="admin_panel")],
            ]),
        )
        return

    if state == "admin_tutorial_text":
        if not is_admin(user.id):
            return
        context.user_data["tut_text"] = msg.text or msg.caption or ""
        context.user_data["state"]    = "admin_tutorial_video"
        await msg.reply_text(
            "📚 *Публикация туториала — шаг 3/4*\n\n"
            "Отправьте *видео* для туториала (или пропустите):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏭ Пропустить видео", callback_data="tutorial_skip_video")],
                [InlineKeyboardButton("◀️ Отмена",          callback_data="admin_panel")],
            ]),
        )
        return

    if state == "admin_tutorial_video":
        if not is_admin(user.id):
            return
        if msg.video:
            context.user_data["tut_video_id"] = msg.video.file_id
        context.user_data["state"] = "admin_tutorial_file"
        await msg.reply_text(
            "📚 *Публикация туториала — шаг 4/4*\n\n"
            "Отправьте *файл* для туториала (или сохраните без файла):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💾 Сохранить без файла", callback_data="tutorial_skip_file")],
                [InlineKeyboardButton("◀️ Отмена",              callback_data="admin_panel")],
            ]),
        )
        return

    if state == "admin_tutorial_file":
        if not is_admin(user.id):
            return
        file_id, file_type = _extract_media(msg)
        context.user_data["tut_file_id"]   = file_id
        context.user_data["tut_file_type"] = file_type
        await _save_tutorial(msg, context)
        return


# ─────────────────────────── /key command ───────────────────────────

async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias: /key — просто запускает диалоговый ввод ключа."""
    user = update.effective_user
    register_and_promote(user)
    conn = get_db()
    already = conn.execute(
        "SELECT id FROM tutorial_access WHERE user_id = ? LIMIT 1", (user.id,)
    ).fetchone()
    conn.close()
    if already:
        await update.message.reply_text(
            "ℹ️ У вас уже активирован ключ — доступ к туториалам открыт.",
            reply_markup=main_kb(user.id),
        )
        return
    context.user_data["state"] = "awaiting_key"
    await update.message.reply_text("🔑 Введите ваш ключ:")


# ─────────────────────────── main ───────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("admin",   cmd_admin))
    app.add_handler(CommandHandler("key",     cmd_key))
    app.add_handler(CommandHandler("dbinfo",  cmd_dbinfo))
    app.add_handler(CommandHandler("resetdb", cmd_resetdb))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
