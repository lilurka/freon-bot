"""
Парсер сообщений.

Распознаёт два типа:
  1. Рабочий отчёт:   номер машины / залил N / взял N
  2. Финальный отчёт: Общая N / Мои N / Перевожу N  → маркер конца смены
"""

import re
from typing import Optional

LAT_TO_CYR = {
    'A': 'А', 'B': 'В', 'E': 'Е', 'K': 'К', 'M': 'М',
    'H': 'Н', 'O': 'О', 'P': 'Р', 'C': 'С', 'T': 'Т',
    'Y': 'У', 'X': 'Х',
}

CAR_RE = re.compile(
    r'[АВЕКМНОРСТУХABEHKMOPTYCX][0-9]{3}[АВЕКМНОРСТУХABEHKMOPTYCX]{2}(?:\s*\d{2,3})?',
    re.IGNORECASE | re.UNICODE
)
LAT_CAR_RE = re.compile(
    r'[ABEKMHOPCTYX][0-9]{3}[ABEKMHOPCTYX]{2}(?:\s*\d{2,3})?',
    re.IGNORECASE | re.UNICODE
)
FREON_LINE_RE = re.compile(r'залил\s*(\d+)', re.IGNORECASE)
MONEY_LINE_RE = re.compile(r'взял\s*(\d+)', re.IGNORECASE)
REPORT_KEYWORDS = ('залил', 'взял', 'фреон', 'заправил', 'диагностик')

# Финальный отчёт
FINAL_TOTAL_RE    = re.compile(r'общая[^\d]{0,5}(\d+)',   re.IGNORECASE)
FINAL_MINE_RE     = re.compile(r'мои[^\d]{0,5}(\d+)',     re.IGNORECASE)
FINAL_TRANSFER_RE = re.compile(r'перевожу[^\d]{0,5}(\d+)', re.IGNORECASE)


def _to_cyr(text: str) -> str:
    return ''.join(LAT_TO_CYR.get(ch, ch) for ch in text.upper())


def normalize_car(raw: str) -> str:
    return _to_cyr(raw.replace(' ', '').strip())


class ReportParser:

    def parse(self, text: str) -> dict:
        """
        type = "final" | "partial_final" | "report" | "partial" | "ignore"
        """
        final = self._try_final(text)
        if final:
            return final
        if not self._looks_like_report(text):
            return {"type": "ignore"}
        return self._parse_work(text)

    # --- финальный отчёт ---

    def _try_final(self, text: str) -> Optional[dict]:
        m_total    = FINAL_TOTAL_RE.search(text)
        m_mine     = FINAL_MINE_RE.search(text)
        m_transfer = FINAL_TRANSFER_RE.search(text)

        found = sum(1 for m in (m_total, m_mine, m_transfer) if m)
        if found == 0:
            return None

        total    = int(m_total.group(1))    if m_total    else None
        mine     = int(m_mine.group(1))     if m_mine     else None
        transfer = int(m_transfer.group(1)) if m_transfer else None

        missing = []
        if total    is None: missing.append("fr_total")
        if mine     is None: missing.append("fr_mine")
        if transfer is None: missing.append("fr_transfer")

        if missing:
            return {"type": "partial_final",
                    "fr_total": total, "fr_mine": mine, "fr_transfer": transfer,
                    "missing": missing}

        return {"type": "final",
                "fr_total": total, "fr_mine": mine, "fr_transfer": transfer}

    # --- рабочий отчёт ---

    def _looks_like_report(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in REPORT_KEYWORDS) or \
               self.extract_car_number(text) is not None or \
               self.has_latin_car(text)

    def _parse_work(self, text: str) -> dict:
        freon, ambiguous = self.extract_freon_volume(text)
        money = self.extract_money(text)

        missing, problems = [], {}

        # Сначала проверяем на латиницу
        if self.has_latin_car(text):
            missing.append("car_number")
            problems["car_number"] = "latin_not_allowed"
            car = None
        else:
            car = self.extract_car_number(text)
            if not car:
                missing.append("car_number")
                problems["car_number"] = "not_found"

        if freon is None:
            if re.search(r'залил', text, re.IGNORECASE):
                problems["freon_volume"] = "bad_format"
            else:
                problems["freon_volume"] = "not_found"
            missing.append("freon_volume")
        elif ambiguous:
            missing.append("freon_volume"); problems["freon_volume"] = "ambiguous_kg"
        if money is None:
            if re.search(r'взял', text, re.IGNORECASE):
                problems["money"] = "bad_format"
            else:
                problems["money"] = "not_found"
            missing.append("money")

        if not missing:
            return {"type": "report",
                    "car_number": car, "freon_volume": freon, "money": money}
        return {"type": "partial",
                "car_number": car, "freon_volume": freon, "money": money,
                "missing": missing, "problems": problems}

    # --- извлечение полей ---

    def extract_car_number(self, text: str) -> Optional[str]:
        m = CAR_RE.search(_to_cyr(text))
        return normalize_car(m.group(0)) if m else None

    def has_latin_car(self, text: str) -> bool:
        return LAT_CAR_RE.search(text) is not None

    def extract_freon_volume(self, text: str) -> tuple:
        m = FREON_LINE_RE.search(text)
        if not m:
            return None, False
        val = int(m.group(1))
        if val == 0:     return 0,   False
        if val <= 10:    return val, True
        if val <= 50000: return val, False
        return None, False

    def extract_money(self, text: str) -> Optional[int]:
        m = MONEY_LINE_RE.search(text)
        if not m:
            return None
        val = int(m.group(1))
        return val if 0 <= val <= 1_000_000 else None

    def extract_number(self, text: str) -> Optional[int]:
        nums = re.findall(r'\d+', text)
        return int(nums[0]) if nums else None
