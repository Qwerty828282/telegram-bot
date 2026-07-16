#!/usr/bin/env python3
"""Telegram bot with builds, tutorials (key system), support, and admin panel."""

import os
import re
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
    conn.close()
    logger.info("DB initialised (backend: %s)", "PostgreSQL" if USE_PG else "SQLite")


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


def register_user(user):
    conn = get_db()
    _db_insert_ignore_user(conn, user.id, user.username, user.full_name, datetime.now().isoformat())
    conn.commit()
    conn.close()


def check_and_promote(user_id: int, username: str | None):
    if not username:
        return
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM pending_admins WHERE username = ?", (username.lower(),)
    ).fetchone()
    if row:
        _db_insert_ignore_admin(conn, user_id, username, datetime.now().isoformat())
        conn.execute("DELETE FROM pending_admins WHERE username = ?", (username.lower(),))
        conn.commit()
    conn.close()


def _extract_media(msg):
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.document:
        return msg.document.file_id, "document"
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
        [InlineKeyboardButton("🔑 Создать ключ для сборок",      callback_data="admin_create_build_key")],
        [InlineKeyboardButton("📋 Все ключи сборок",             callback_data="admin_build_keys_list")],
        [InlineKeyboardButton("💬 Обращения в поддержку",        callback_data="admin_support_list")],
        [InlineKeyboardButton("🔄 Обнулить подписку (туториал)", callback_data="admin_reset_sub_list")],
        [InlineKeyboardButton("🔄 Обнулить подписку (сборки)",   callback_data="admin_reset_build_sub_list")],
    ]
    if user_id == OWNER_ID:
        rows.append([InlineKeyboardButton("👤 Выдать права админа",  callback_data="admin_grant")])
        rows.append([InlineKeyboardButton("🚫 Забрать права админа", callback_data="admin_revoke_list")])
    return InlineKeyboardMarkup(rows)


def back_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад в панель", callback_data="admin_panel")]])


# ─────────────────────────── /start ───────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    register_user(user)
    check_and_promote(user.id, user.username)

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
    check_and_promote(user.id, user.username)

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

        key_lines = ""
        if key_users:
            lines = []
            for r in key_users:
                name = f"@{r['username']}" if r["username"] else (r["full_name"] or str(r["used_by"]))
                key  = r["key_value"] or "—"
                date = (r["used_at"] or "")[:10]
                lines.append(f"  • {name} — `{key}` ({date})")
            key_lines = "\n" + "\n".join(lines)
        else:
            key_lines = "\n  _нет активных_"

        db_type = "PostgreSQL ☁️" if USE_PG else "SQLite 💾"
        text = (
            "📊 *Статистика бота*\n\n"
            f"👥 Пользователей: *{total_users}*\n"
            f"📦 Сборок: *{total_builds}*\n"
            f"📚 Туториалов: *{total_tutorials}*\n"
            f"🗄 База данных: *{db_type}*\n\n"
            f"🔑 *Ключи:*\n"
            f"  • Всего создано: *{total_keys}*\n"
            f"  • Использовано: *{used_keys}*\n"
            f"  • Свободных: *{free_keys}*\n\n"
            f"*Последние активации:*{key_lines}"
        )
        if len(text) > 4096:
            text = text[:4090] + "\n…"
        await q.edit_message_text(
            text,
            parse_mode="Markdown",
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
            f"📋 *Все ключи* — всего {len(all_keys)}\n"
            f"🟢 Свободных: *{free_count}*  |  🔴 Использованных: *{used_count}*\n\n"
        )
        body = "\n".join(lines)
        text = header + body
        if len(text) > 4096:
            text = text[:4080] + "\n\n…и ещё больше"
        await q.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔑 Создать ключ", callback_data="admin_create_key")],
                [InlineKeyboardButton("◀️ Назад", callback_data="admin_panel")],
            ])
        )

    elif data == "admin_add_build":
        if not is_admin(user.id):
            return
        context.user_data["state"] = "admin_build_title"
        await q.edit_message_text(
            "📦 *Публикация сборки — шаг 1/2*\n\n"
            "Введите *название* сборки (будет отображаться в списке):",
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
        conn.execute("UPDATE tutorial_keys SET used_by = NULL, used_at = NULL WHERE used_by = ?", (target_id,))
        conn.execute("DELETE FROM tutorial_access WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        await q.edit_message_text(
            f"✅ Подписка (туториалы) пользователя {target_id} обнулена.",
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
        access = conn.execute(
            "SELECT id FROM build_access WHERE user_id = ? LIMIT 1", (user.id,)
        ).fetchone()
        if not access and user.id != OWNER_ID and not is_admin(user.id):
            conn.close()
            await q.answer("🔑 Для просмотра сборки нужен ключ доступа.", show_alert=True)
            return
        row = conn.execute("SELECT * FROM builds WHERE id = ?", (build_id,)).fetchone()
        conn.close()
        if not row:
            await q.edit_message_text("❌ Сборка не найдена.")
            return
        caption = row["text"] or row["title"] or "Сборка"
        if row["file_id"] and row["file_type"] == "photo":
            await context.bot.send_photo(
                chat_id=q.message.chat_id,
                photo=row["file_id"],
                caption=caption,
            )
        elif row["file_id"] and row["file_type"] == "video":
            await context.bot.send_video(
                chat_id=q.message.chat_id,
                video=row["file_id"],
                caption=caption,
            )
        elif row["file_id"] and row["file_type"] == "document":
            await context.bot.send_document(
                chat_id=q.message.chat_id,
                document=row["file_id"],
                caption=caption,
            )
        else:
            await context.bot.send_message(chat_id=q.message.chat_id, text=caption)

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
    register_user(user)
    check_and_promote(user.id, user.username)

    state = context.user_data.get("state")

    # ── Main menu buttons ──

    if text == BTN_BUILDS:
        conn = get_db()
        rows = conn.execute("SELECT id, title, text FROM builds ORDER BY id DESC LIMIT 30").fetchall()
        has_build_access = conn.execute(
            "SELECT id FROM build_access WHERE user_id = ? LIMIT 1", (user.id,)
        ).fetchone()
        conn.close()
        if not rows:
            await msg.reply_text("📦 Сборок пока нет.")
            return
        buttons = []
        for r in rows:
            label = (r["title"] or r["text"] or f"Сборка #{r['id']}")[:40]
            buttons.append([InlineKeyboardButton(label, callback_data=f"view_build_{r['id']}")])
        if not has_build_access and not is_admin(user.id):
            await msg.reply_text(
                "📦 *Сборки*\n\n"
                "🔑 Для доступа к сборкам нужен ключ.\n"
                "Введите ключ командой: `/key XXXX-XXXX-XXXX-XXXX`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await msg.reply_text(
                "📦 *Все сборки:*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        return

    if text == BTN_TUTORIALS:
        conn = get_db()
        rows = conn.execute("SELECT id, title, text FROM tutorials ORDER BY id DESC LIMIT 30").fetchall()
        conn.close()
        if not rows:
            await msg.reply_text("📚 Туториалов пока нет.")
            return
        # Check if user has access
        user_access = conn.execute if False else None  # re-open below
        conn2 = get_db()
        has_access = conn2.execute(
            "SELECT id FROM tutorial_access WHERE user_id = ? LIMIT 1", (user.id,)
        ).fetchone()
        conn2.close()

        buttons = []
        for r in rows:
            label = (r["title"] or r["text"] or f"Туториал #{r['id']}")[:40]
            buttons.append([InlineKeyboardButton(label, callback_data=f"view_tutorial_{r['id']}")])

        if not has_access and not is_admin(user.id):
            await msg.reply_text(
                "📚 *Туториалы на сборки*\n\n"
                "🔑 Для доступа к туториалам нужен ключ.\n"
                "Введите ключ командой: /key XXXX-XXXX-XXXX-XXXX",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await msg.reply_text(
                "📚 *Туториалы на сборки:*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
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

    if state == "admin_build_title":
        if not is_admin(user.id):
            return
        context.user_data["build_title"] = text
        context.user_data["state"]       = "admin_build_content"
        await msg.reply_text(
            "📦 *Публикация сборки — шаг 2/2*\n\n"
            "Отправьте *текст* сборки (и/или прикрепите фото, видео, файл):",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="admin_panel")]]),
        )
        return

    if state == "admin_build_content":
        if not is_admin(user.id):
            return
        title     = context.user_data.get("build_title", "")
        body      = msg.text or msg.caption or ""
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
            f"✅ *Сборка «{title}» опубликована!*",
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
    user = update.effective_user
    register_user(user)
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🔑 *Активация ключа*\n\n"
            "Использование: `/key XXXX-XXXX-XXXX-XXXX`\n\n"
            "Ключ даёт доступ к разделу *Сборки* или *Туториалы* — в зависимости от типа ключа.",
            parse_mode="Markdown",
        )
        return
    key_value = args[0].strip().upper()
    conn = get_db()

    # Проверяем ключ туториалов
    tut_row = conn.execute(
        "SELECT * FROM tutorial_keys WHERE key_value = ?", (key_value,)
    ).fetchone()

    # Проверяем ключ сборок
    build_row = conn.execute(
        "SELECT * FROM build_keys WHERE key_value = ?", (key_value,)
    ).fetchone()

    if not tut_row and not build_row:
        conn.close()
        await update.message.reply_text("❌ Ключ не найден. Проверьте правильность ввода.")
        return

    now = datetime.now().isoformat()

    if tut_row:
        if tut_row["used_by"] is not None:
            conn.close()
            if tut_row["used_by"] == user.id:
                await update.message.reply_text("ℹ️ Этот ключ туториалов уже привязан к вашему аккаунту.")
            else:
                await update.message.reply_text("❌ Этот ключ уже использован другим пользователем.")
            return
        conn.execute(
            "UPDATE tutorial_keys SET used_by = ?, used_at = ? WHERE key_value = ?",
            (user.id, now, key_value),
        )
        _db_upsert_tutorial_access(conn, user.id, key_value, now)
        conn.commit()
        conn.close()
        await update.message.reply_text(
            "✅ *Ключ активирован!*\n\n"
            "Теперь у вас есть доступ к *туториалам*.\n"
            "Нажмите «📚 Туторы на сборки» в меню.",
            parse_mode="Markdown",
            reply_markup=main_kb(user.id),
        )
        return

    if build_row:
        if build_row["used_by"] is not None:
            conn.close()
            if build_row["used_by"] == user.id:
                await update.message.reply_text("ℹ️ Этот ключ сборок уже привязан к вашему аккаунту.")
            else:
                await update.message.reply_text("❌ Этот ключ уже использован другим пользователем.")
            return
        conn.execute(
            "UPDATE build_keys SET used_by = ?, used_at = ? WHERE key_value = ?",
            (user.id, now, key_value),
        )
        _db_upsert_build_access(conn, user.id, key_value, now)
        conn.commit()
        conn.close()
        await update.message.reply_text(
            "✅ *Ключ активирован!*\n\n"
            "Теперь у вас есть доступ к *сборкам*.\n"
            "Нажмите «📦 Все сборки» в меню.",
            parse_mode="Markdown",
            reply_markup=main_kb(user.id),
        )


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
