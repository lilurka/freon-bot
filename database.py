"""
База данных SQLite.

Таблицы:
  shifts  — смены (одна смена на чат в день)
  records — отчёты, привязаны к смене
"""

import sqlite3
from datetime import date, datetime
from typing import Optional, List, Dict
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")


def now_msk() -> datetime:
    return datetime.now(MSK)


def today_msk() -> date:
    return now_msk().date()


DB_PATH = "reports.db"


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._init_db()

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            # Смены
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shifts (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id       INTEGER NOT NULL,
                    shift_date    TEXT NOT NULL,          -- YYYY-MM-DD по МСК
                    opened_at     TEXT NOT NULL,          -- datetime МСК
                    closed_at     TEXT,                   -- datetime МСК, NULL = открыта
                    close_reason  TEXT,                   -- 'final_report' | 'auto_midnight'
                    -- финансовый отчёт сотрудника (из маркера конца смены)
                    fr_total      INTEGER,                -- Общая
                    fr_mine       INTEGER,                -- Мои
                    fr_transfer   INTEGER,                -- Перевожу
                    UNIQUE(chat_id, shift_date)
                )
            """)
            # Записи
            conn.execute("""
                CREATE TABLE IF NOT EXISTS records (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    shift_id      INTEGER NOT NULL REFERENCES shifts(id),
                    chat_id       INTEGER NOT NULL,
                    user_id       INTEGER NOT NULL,
                    username      TEXT,
                    car_number    TEXT NOT NULL,
                    freon_volume  INTEGER NOT NULL,
                    money         INTEGER NOT NULL,
                    raw_text      TEXT,
                    created_at    TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_records_shift ON records(shift_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_shifts_chat_date ON shifts(chat_id, shift_date)")
            conn.commit()

    # ------------------------------------------------------------------
    # Смены
    # ------------------------------------------------------------------

    def get_or_create_shift(self, chat_id: int) -> dict:
        """Возвращает открытую смену за сегодня (МСК), создаёт если нет."""
        today = today_msk().isoformat()
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shifts WHERE chat_id = ? AND shift_date = ?",
                (chat_id, today)
            ).fetchone()
            if row:
                return dict(row)
            # Создаём новую смену
            cursor = conn.execute(
                "INSERT INTO shifts (chat_id, shift_date, opened_at) VALUES (?, ?, ?)",
                (chat_id, today, now_msk().strftime("%Y-%m-%d %H:%M:%S"))
            )
            conn.commit()
            row = conn.execute("SELECT * FROM shifts WHERE id = ?", (cursor.lastrowid,)).fetchone()
            return dict(row)

    def close_shift(self, chat_id: int, shift_date: str, reason: str,
                    fr_total: Optional[int] = None,
                    fr_mine: Optional[int] = None,
                    fr_transfer: Optional[int] = None) -> bool:
        """Закрывает смену. Возвращает False если смена уже закрыта."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT id, closed_at FROM shifts WHERE chat_id = ? AND shift_date = ?",
                (chat_id, shift_date)
            ).fetchone()
            if not row or row["closed_at"]:
                return False
            conn.execute(
                """UPDATE shifts
                   SET closed_at = ?, close_reason = ?,
                       fr_total = ?, fr_mine = ?, fr_transfer = ?
                   WHERE id = ?""",
                (now_msk().strftime("%Y-%m-%d %H:%M:%S"), reason,
                 fr_total, fr_mine, fr_transfer, row["id"])
            )
            conn.commit()
            return True

    def get_open_shifts(self) -> List[Dict]:
        """Все открытые смены (для автозакрытия в 23:59)."""
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM shifts WHERE closed_at IS NULL"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_shift(self, chat_id: int, shift_date: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM shifts WHERE chat_id = ? AND shift_date = ?",
                (chat_id, shift_date)
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Записи
    # ------------------------------------------------------------------

    def add_record(self, chat_id: int, user_id: int, username: str,
                   car_number: str, freon_volume: int, money: int,
                   raw_text: str = "") -> int:
        shift = self.get_or_create_shift(chat_id)
        with self._get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO records
                   (shift_id, chat_id, user_id, username, car_number, freon_volume, money, raw_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (shift["id"], chat_id, user_id, username,
                 car_number, freon_volume, money, raw_text)
            )
            conn.commit()
            return cursor.lastrowid

    def get_stats(self, chat_id: Optional[int] = None,
                  day: Optional[date] = None) -> Dict:
        """Агрегированная статистика за смену (день МСК)."""
        day_str = (day or today_msk()).isoformat()

        if chat_id is not None:
            query = """
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(r.freon_volume), 0) as total_freon,
                       COALESCE(SUM(r.money), 0) as total_money
                FROM records r
                JOIN shifts s ON s.id = r.shift_id
                WHERE s.chat_id = ? AND s.shift_date = ?
            """
            params = (chat_id, day_str)
        else:
            query = """
                SELECT COUNT(*) as cnt,
                       COALESCE(SUM(r.freon_volume), 0) as total_freon,
                       COALESCE(SUM(r.money), 0) as total_money
                FROM records r
                JOIN shifts s ON s.id = r.shift_id
                WHERE s.shift_date = ?
            """
            params = (day_str,)

        with self._get_conn() as conn:
            row = conn.execute(query, params).fetchone()

        return {
            "count": row["cnt"],
            "total_freon": row["total_freon"],
            "total_money": row["total_money"],
        }

    def get_records(self, chat_id: Optional[int] = None,
                    day: Optional[date] = None) -> List[Dict]:
        """Все записи смены за день МСК."""
        day_str = (day or today_msk()).isoformat()

        if chat_id is not None:
            query = """
                SELECT r.* FROM records r
                JOIN shifts s ON s.id = r.shift_id
                WHERE s.chat_id = ? AND s.shift_date = ?
                ORDER BY r.created_at
            """
            params = (chat_id, day_str)
        else:
            query = """
                SELECT r.* FROM records r
                JOIN shifts s ON s.id = r.shift_id
                WHERE s.shift_date = ?
                ORDER BY r.created_at
            """
            params = (day_str,)

        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
