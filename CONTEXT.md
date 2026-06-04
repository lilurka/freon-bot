# Freon Bot — Full Context

## Description
Telegram bot for auto AC service workers to track freon refueling, payments, apparatus refills, nipple replacements, and безнал (non-cash) transfers with person/bank attribution. Runs in multiple group chats simultaneously.

---

## Tech Stack
- Python 3.10, python-telegram-bot v21.3, SQLite, openpyxl, Moscow timezone
- Server: root@77.221.152.199, /root/tg_bot, runs in venv
- Bot token: stored in `config.py` (gitignored)
- Admin IDs: 575339694, 164039564, 142917029
- ALLOWED_CHAT_IDS currently empty (works in all chats)
- Private chat (DM) messages are **ignored** — reports only from group chats
- Deploy via sshpass + ssh with pipe (scp often times out), restart with pkill + nohup
- Systemd service: `freon-bot.service` (WorkingDirectory=/root/tg_bot)

---

## File Structure

| File | Purpose |
|------|---------|
| `bot.py` | Entry point — handlers, save functions, auto-close job, clarification flows |
| `parser.py` | Regex patterns, message parsing, validation (car#, freon, money, payment, nipple, beznal, final) |
| `database.py` | SQLite — shifts/records/refills tables, migrations, stats queries |
| `config.py` | BOT_TOKEN, ALLOWED_CHAT_IDS, ADMIN_IDS, TIMEZONE, AUTO_REPORT_TIME (gitignored) |
| `excel_export.py` | Multi-chat Excel with per-chat sheets + "Итого" + "Безнал" sheet + CSV fallback |
| `requirements.txt` | python-telegram-bot>=21.3, openpyxl>=3.1.2 |
| `freon-bot.service` | systemd unit for production |
| `CONTEXT.md` | This file — full project context |

---

## Message Flow

1. Message arrives → `handle_message()` in bot.py
2. `parser.parse(text)` returns a dict with `type`:
   - `"final"` — financial report (shift close)
   - `"partial_final"` — incomplete financial report, needs clarification
   - `"report"` — work report (car + freon + money + payment)
   - `"partial"` — incomplete work report, needs clarification
   - `"refill"` — apparatus refueling
   - `"nipple"` — standalone nipple report (no car)
   - `"ignore"` — nothing relevant
3. Bot saves to DB and replies with current day stats

---

## Parser Details (parser.py)

### Regexes
- `CAR_RE`: strict Russian plate (АВЕКМНОРСТУХ + 3 digits + 2 letters + optional region)
- `FLEX_CAR_RE`: flexible cyrillic/latin combination for non-standard plates
- `FREON_LINE_RE`: `залил N` (poured N grams)
- `MONEY_LINE_RE`: `взял N` (took N rubles)
- `REFILL_RE`: `заправка аппарата N` (apparatus refill, N in kg, can be float)
- `PAYMENT_RE`: `нал` or `безнал` (cash or non-cash)
- `NIPPLE_RE`: `N ниппель[яи]?` or `N нипель[яи]?` — digit before word
- `NIPPLE_WORD_RE`: standalone `ниппель[яи]?` / `нипель[яи]?` without digit — triggers clarification
- `BEZNAL_NAME_RE`: `ксения|алина|алексей|владимир|себе`
- `BEZNAL_BANK_RE`: `т-банк|сбер|альфа|озон`
- `FINAL_TOTAL_RE`: `общая N`
- `FINAL_MINE_RE`: `мои N`
- `FINAL_TRANSFER_RE`: `перевожу N`

### Validation Rules
- Money < 1000 AND != 0 → rejected (typo protection for amounts like "500")
- nipple_count > 5 → rejected, asks for correction
- nipple word without number → asks for count
- If `безнал` → name is required; if name != `себе` → bank is required
- Car number must exist; freon volume required; money required; payment type required

---

## Database (database.py)

### Tables
- **shifts**: id, chat_id, shift_date, status (open/closed), reason, fr_total, fr_mine, fr_transfer, created_at, closed_at
- **records**: id, shift_id, chat_id, user_id, username, car_number, freon_volume, money, payment_type, payment_name, payment_bank, nipple_count, raw_text, created_at
- **refills**: id, shift_id, chat_id, user_id, username, refill_kg, raw_text, created_at

### Key Queries
- `get_stats(chat_id, day)`: aggregated stats for a chat/day — count, total_freon, total_money, total_nal, total_beznal, total_nipples, total_refill, beznal_breakdown (per-person totals)
- `get_user_daily_totals(chat_id, user_id, day)`: nal_sum (cash) + sebe_sum (безнал where name='себе')
- `get_records(chat_id, day)`: all records for export
- `close_shift(chat_id, shift_date, reason, fr_total, fr_mine, fr_transfer)`
- `delete_records_by_car(chat_id, car_number, day)`: /delete command

---

## Report Formats

### Work Report (per car)
```
e133yy
залил 450
взял 3500
нал
```
OR
```
e133yy
залил 450
взял 3500
безнал алексей т-банк
```

Optional: append `2 ниппеля` at the end.

### Apparatus Refill
```
заправка аппарата 5.5
```

### Standalone Nipples (no car number)
```
2 ниппеля
```

### Financial Report (end of shift) — NEW auto-calc format
```
Общая 13500
```
Bot calculates: 25% of total = salary. Compares with нал + себе taken during shift. Shows "К переводу: X руб." if over 25%.

### Financial Report — Old manual format
```
Общая 13500
Мои 2900
Перевожу 10600
```
Bot also shows the same нал+себе vs 25% comparison for reference.

---

## Auto Salary Calculation (25%)
- `salary = total * 25 // 100`
- `already_taken = nal_sum + sebe_sum` (from today's records for this user)
- `to_transfer = already_taken - salary` (if positive → show transfer amount; if negative → "всё в норме")
- Shown in the final report reply AND in shift summary on close

---

## Безнал (Non-Cash) Tracking
- Payment name pool: Ксения, Алина, Алексей, Владимир, Себе
- Bank pool: Т-банк, Сбер, Альфа, Озон
- "Себе" = transfer to self → bank not required
- Breakdown shown in every stats response, in shift summary, and in Excel (dedicated "Безнал" sheet)

---

## Commands
| Command | Who | Description |
|---------|-----|-------------|
| `/stat` | All | Stats for current shift in this chat |
| `/statall` | Admins | Stats across all chats |
| `/export [date]` | Admins | Excel export (today or specific date) |
| `/delete car_number` | Admins | Delete records for a car today |
| `/help` | All | Show help message |

---

## Auto-Close
- Runs daily at 23:50 MSK
- Closes all open shifts silently (no message sent to chat)
- Sends daily Excel to all admins
- Fin=None (no financial data for auto-closed shifts)

---

## Deployment
```bash
# Copy files
sshpass -p 'PASS' ssh root@HOST 'cat > /root/tg_bot/bot.py' < bot.py

# Restart
sshpass -p 'PASS' ssh root@HOST 'pkill -f "python3 bot.py"; sleep 2; source /root/tg_bot/venv/bin/activate && nohup python3 /root/tg_bot/bot.py > /root/tg_bot/bot.log 2>&1 &'

# Check
sshpass -p 'PASS' ssh root@HOST 'pgrep -af "python3 bot.py"; tail -5 /root/tg_bot/bot.log'
```

---

## Key Design Decisions
1. ALLOWED_CHAT_IDS empty = works everywhere; populate to restrict
2. Payment_type is mandatory with clarification
3. Nipple count: optional but if word "ниппеля" appears without digit → ask for count; max=5
4. NIPPLE_RE uses `[ \t]*` not `\s*` to prevent matching across newlines
5. "Себе" exempts bank requirement (self-transfer)
6. `_try_nipple` checked after `_looks_like_report` to avoid stealing full reports
7. Auto-close at 23:50 is silent (no message to chat), just DB close + Excel to admins
8. Integer math for 25%: `total * 25 // 100`

---

## Edge Cases Handled
- Latin letters in car numbers → auto-converted to Cyrillic
- Money < 1000 and > 0 → rejected (likely typo)
- nipple_count > 5 → re-ask
- Безнал without name → ask name; without bank → ask bank (unless "Себе")
- Multiple clarifications follow `reply_to_message` chain
- Partial final report asks for missing fields one by one
