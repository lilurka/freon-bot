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

FLEX_CAR_RE = re.compile(
    r'[A-Za-z\u0410-\u044f\u0401\u0451][A-Za-z\u0410-\u044f\u0401\u04510-9]{2,10}[0-9][A-Za-z\u0410-\u044f\u0401\u04510-9]*',
    re.IGNORECASE | re.UNICODE
)

FREON_LINE_RE = re.compile(r'залил\s*(\d+)', re.IGNORECASE)
MONEY_LINE_RE = re.compile(r'взял\s*(\d+)', re.IGNORECASE)
REFILL_RE = re.compile(r'заправк[аиу]\s+аппарат[ауы]?\s*(\d+(?:[.,]\d+)?)', re.IGNORECASE)
PAYMENT_RE = re.compile(r'\b(нал|безнал)\b', re.IGNORECASE)
NIPPLE_RE = re.compile(r'(\d+)[ \t]*(?:ниппель?[яи]?|нипель?[яи]?)\b', re.IGNORECASE)
NIPPLE_WORD_RE = re.compile(r'\b(?:ниппель?[яи]?|нипель?[яи]?)\b', re.IGNORECASE)

BEZNAL_NAMES = {"ксения", "алина", "алексей", "владимир", "себе"}
BEZNAL_BANKS = {"т-банк", "сбер", "альфа", "озон"}
BEZNAL_NAME_RE = re.compile(r'\b(ксения|алина|алексей|владимир|себе)\b', re.IGNORECASE)
BEZNAL_BANK_RE = re.compile(r'\b(т-банк|сбер|альфа|озон)\b', re.IGNORECASE)

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
        type = "final" | "partial_final" | "report" | "partial" | "refill" | "nipple" | "ignore"
        """
        final = self._try_final(text)
        if final:
            return final
        refill = self._try_refill(text)
        if refill:
            return refill
        if not self._looks_like_report(text):
            nipple = self._try_nipple(text)
            if nipple:
                return nipple
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

        if total is not None and mine is None and transfer is None:
            return {"type": "final",
                    "fr_total": total, "fr_mine": 0, "fr_transfer": 0,
                    "auto_calc": True}

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
        return self.extract_car_number(text) is not None

    def _is_refill_command(self, text: str) -> bool:
        return REFILL_RE.search(text) is not None

    def _try_refill(self, text: str) -> Optional[dict]:
        m = REFILL_RE.search(text)
        if not m:
            return None
        raw_val = m.group(1).replace(',', '.')
        try:
            kg = float(raw_val)
        except ValueError:
            return None
        if kg < 0:
            return None
        return {"type": "refill", "refill_kg": kg}

    def _try_nipple(self, text: str) -> Optional[dict]:
        m = NIPPLE_RE.search(text)
        if m:
            try:
                count = int(m.group(1))
            except ValueError:
                return None
            if count <= 0:
                return None
            if count > 5:
                return {"type": "partial_nipple", "nipple_count": count,
                        "missing": ["nipple_count"], "problems": {"nipple_count": "too_many"}}
            return {"type": "nipple", "nipple_count": count}
        if NIPPLE_WORD_RE.search(text):
            return {"type": "partial_nipple", "nipple_count": None,
                    "missing": ["nipple_count"], "problems": {"nipple_count": "not_found"}}
        return None

    def _parse_work(self, text: str) -> dict:
        freon, ambiguous = self.extract_freon_volume(text)
        money = self.extract_money(text)
        payment = self.extract_payment_type(text)
        payment_name = self.extract_beznal_name(text) if payment == "безнал" else None
        payment_bank = self.extract_beznal_bank(text) if payment == "безнал" else None
        nipple = self.extract_nipple_count(text)

        missing, problems = [], {}

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
                m_bad = MONEY_LINE_RE.search(text)
                if m_bad:
                    bad_val = int(m_bad.group(1))
                    if 0 < bad_val < 500:
                        problems["money"] = "too_small"
                    else:
                        problems["money"] = "bad_format"
                else:
                    problems["money"] = "bad_format"
            else:
                problems["money"] = "not_found"
            missing.append("money")
        if payment is None:
            problems["payment_type"] = "not_found"
            missing.append("payment_type")
        elif payment == "безнал":
            if payment_name is None:
                problems["payment_name"] = "not_found"
                missing.append("payment_name")
            elif payment_name not in BEZNAL_NAMES:
                problems["payment_name"] = "invalid"
                missing.append("payment_name")
            if payment_name == "себе":
                if payment_bank is not None and payment_bank not in BEZNAL_BANKS:
                    problems["payment_bank"] = "invalid"
                    missing.append("payment_bank")
            else:
                if payment_bank is None:
                    problems["payment_bank"] = "not_found"
                    missing.append("payment_bank")
                elif payment_bank not in BEZNAL_BANKS:
                    problems["payment_bank"] = "invalid"
                    missing.append("payment_bank")
        if nipple is None and NIPPLE_WORD_RE.search(text):
            problems["nipple_count"] = "not_found"
            missing.append("nipple_count")
        elif nipple is not None and nipple > 5:
            problems["nipple_count"] = "too_many"
            missing.append("nipple_count")

        if not missing:
            return {"type": "report",
                    "car_number": car, "freon_volume": freon, "money": money,
                    "payment_type": payment, "payment_name": payment_name,
                    "payment_bank": payment_bank, "nipple_count": nipple}
        return {"type": "partial",
                "car_number": car, "freon_volume": freon, "money": money,
                "payment_type": payment, "payment_name": payment_name,
                "payment_bank": payment_bank, "nipple_count": nipple,
                "missing": missing, "problems": problems}

    # --- извлечение полей ---

    def extract_car_number(self, text: str) -> Optional[str]:
        m = CAR_RE.search(_to_cyr(text))
        if m:
            return normalize_car(m.group(0))

        m = FLEX_CAR_RE.search(_to_cyr(text))
        if m:
            raw = m.group(0)
            if any(c.isdigit() for c in raw) and any(c.isalpha() for c in raw):
                return normalize_car(raw)

        return None

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
        if val == 0:
            return 0
        return val if val >= 500 and val <= 1_000_000 else None

    def extract_number(self, text: str) -> Optional[int]:
        nums = re.findall(r'\d+', text)
        return int(nums[0]) if nums else None

    def extract_payment_type(self, text: str) -> Optional[str]:
        m = PAYMENT_RE.search(text)
        if not m:
            return None
        val = m.group(1).lower()
        return "нал" if val == "нал" else "безнал"

    def extract_nipple_count(self, text: str) -> Optional[int]:
        m = NIPPLE_RE.search(text)
        if not m:
            return None
        try:
            return int(m.group(1))
        except ValueError:
            return None

    def extract_beznal_name(self, text: str) -> Optional[str]:
        m = BEZNAL_NAME_RE.search(text)
        if not m:
            return None
        return m.group(1).lower()

    def extract_beznal_bank(self, text: str) -> Optional[str]:
        m = BEZNAL_BANK_RE.search(text)
        if not m:
            return None
        return m.group(1).lower()
