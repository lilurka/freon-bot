"""
Телеграм-бот учёта заправки фреона.

Смены:
  - Открываются автоматически с первым отчётом за день (МСК)
  - Закрываются финальным отчётом «Общая N / Мои N / Перевожу N»
  - Автозакрытие в 23:50 МСК
  - При закрытии — итоговое сообщение в чат + Excel администраторам
"""

import logging
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

from telegram import Update, Message, Bot
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)
from telegram.constants import ParseMode

from database import Database, today_msk, now_msk
from parser import ReportParser
from excel_export import ExcelExporter
from config import BOT_TOKEN, ALLOWED_CHAT_IDS, ADMIN_IDS

MSK = ZoneInfo("Europe/Moscow")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

db = Database()
parser = ReportParser()
exporter = ExcelExporter()


# ======================================================================
# Вспомогательные функции
# ======================================================================

def _fmt_beznal_breakdown(stats: dict) -> str:
    """Возвращает строки с разбивкой безнала по именам/банкам."""
    lines = []
    for item in stats.get("beznal_breakdown", []):
        name = item["name"].capitalize() if item["name"] else "?"
        bank = f" → {item['bank']}" if item.get("bank") else ""
        lines.append(f"    👤 {name}{bank}: *{item['total']} руб.*")
    return "\n".join(lines) + "\n" if lines else ""


def _shift_summary_text(stats: dict, shift_date: date,
                         fin: dict = None, label: str = "смену") -> str:
    """Формирует итоговый текст статистики смены."""
    breakdown = _fmt_beznal_breakdown(stats)
    beznal_total = f"  💳 Безнал: *{stats['total_beznal']} руб.*"
    if breakdown:
        beznal_total += "\n" + breakdown.rstrip("\n")
    lines = [
        f"📊 *Итого за {label} ({shift_date.strftime('%d.%m.%Y')}):*",
        f"🚗 Машин: *{stats['count']}*",
        f"🧊 Фреона: *{stats['total_freon']} г*",
        f"💰 Выручка: *{stats['total_money']} руб.*",
        f"  💵 Нал: *{stats['total_nal']} руб.*",
        beznal_total,
    ]
    lines += [
        f"⛽ Заправка аппарата: *{stats['total_refill']} кг*",
        f"🔩 Ниппелей заменено: *{stats['total_nipples']}*",
    ]
    if fin:
        lines += [
            "",
            "💼 *Финансовый отчёт сотрудника:*",
            f"  Общая касса:  *{fin['fr_total']} руб.*",
            f"  25%:           *{fin.get('fr_mine', 0)} руб.*",
            f"  К переводу:   *{fin.get('fr_transfer', 0)} руб.*",
        ]
    return "\n".join(lines)


async def _close_shift_and_notify(bot: Bot, chat_id: int,
                                   shift_date_str: str, reason: str,
                                   fin: dict = None, silent: bool = False):
    """Закрывает смену, шлёт итог в чат (если not silent)."""
    shift_date = date.fromisoformat(shift_date_str)

    closed = db.close_shift(
        chat_id=chat_id,
        shift_date=shift_date_str,
        reason=reason,
        fr_total=fin.get("fr_total") if fin else None,
        fr_mine=fin.get("fr_mine") if fin else None,
        fr_transfer=fin.get("fr_transfer") if fin else None,
    )
    if not closed:
        return  # уже закрыта

    if silent:
        return

    stats = db.get_stats(chat_id=chat_id, day=shift_date)

    reason_label = "конец смены" if reason == "final_report" else "автозакрытие (23:50)"
    header = f"🔔 *Смена закрыта* — {reason_label}\n\n"
    summary = _shift_summary_text(stats, shift_date, fin=fin, label="смену")

    # Итог в чат
    await bot.send_message(chat_id=chat_id,
                           text=header + summary,
                           parse_mode=ParseMode.MARKDOWN)


# ======================================================================
# Обработчики сообщений
# ======================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик всех текстовых сообщений и фото с подписью."""
    message = update.message
    if not message:
        return

    chat_id = message.chat_id
    if message.chat.type == "private":
        return
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        return

    user_id   = message.from_user.id
    username  = message.from_user.username or message.from_user.first_name

    # Текст из сообщения или подпись к фото
    text = (message.text or message.caption or "").strip()
    if not text:
        return

    # Проверяем — ждёт ли бот уточнения от этого пользователя
    if context.user_data.get("waiting_for") and message.reply_to_message:
        if message.reply_to_message.from_user.id == context.bot.id:
            await handle_clarification_reply(update, context)
            return
        # Reply не на бота — сбрасываем ожидание
        context.user_data.pop("waiting_for", None)
        context.user_data.pop("partial", None)
        context.user_data.pop("partial_final", None)

    result = parser.parse(text)
    logger.info(f"Парсинг: chat_id={chat_id}, text={text!r}, result={result}")

    if result["type"] == "final":
        await _handle_final_report(message, context, result, user_id=user_id, raw_text=text)

    elif result["type"] == "partial_final":
        context.user_data["partial_final"] = result
        context.user_data["chat_id"] = chat_id
        await _ask_final_clarification(message, result, context)

    elif result["type"] == "report":
        await _save_work_report(message, context, result, chat_id, user_id, username, text)

    elif result["type"] == "partial":
        context.user_data["partial"] = result
        context.user_data["chat_id"] = chat_id
        context.user_data["username"] = username
        context.user_data["raw_text"] = text
        await ask_clarification(message, result, context)

    elif result["type"] == "refill":
        await _save_refill_report(message, context, result, chat_id, user_id, username)

    elif result["type"] == "nipple":
        await _save_nipple_report(message, context, result, chat_id, user_id, username)

    # "ignore" — молчим


async def _handle_final_report(message: Message, context: ContextTypes.DEFAULT_TYPE, fin: dict,
                                user_id: int = 0, raw_text: str = ""):
    """Финальный отчёт: закрываем смену."""
    chat_id = message.chat_id
    total   = fin["fr_total"]

    # Берём дату из открытой смены, а не today_msk() — чтобы работало через полночь
    open_shift = db.get_latest_open_shift(chat_id)
    if open_shift:
        shift_date = open_shift["shift_date"]
        day = date.fromisoformat(shift_date)
    else:
        shift_date = today_msk().isoformat()
        day = today_msk()

    # Считаем нал + себе ПО ВСЕЙ СМЕНЕ (все пользователи, не только закрывающий)
    stats   = db.get_stats(chat_id=chat_id, day=day)
    nal_sum = stats["total_nal"]
    sebe_sum = db.get_shift_sebe(chat_id=chat_id, day=day)
    kept     = nal_sum + sebe_sum
    salary25 = total * 25 // 100

    if fin.get("auto_calc"):
        salary   = salary25
        transfer = max(kept - salary25, 0)
        fin["fr_mine"]     = salary
        fin["fr_transfer"] = transfer
    else:
        salary   = fin.get("fr_mine", 0)
        transfer = fin.get("fr_transfer", 0)

    reply_parts = [
        f"✅ *Финансовый отчёт принят!*",
        f"💼 Общая: *{total} руб.*  |  Мои: *{salary} руб.*  |  Перевожу: *{transfer} руб.*",
        "",
        f"📊 *Сверка:*",
        f"💰 Нал: *{nal_sum} руб.*",
        f"🔄 Себе: *{sebe_sum} руб.*",
        f"✖️ Итого взято: *{kept} руб.*",
        f"📐 25% от общей: *{salary25} руб.*",
    ]
    diff = kept - salary25
    if diff > 0:
        reply_parts.append(f"\n🔁 *К переводу: {diff} руб.*")
    elif diff < 0:
        reply_parts.append(f"\n🔸 *К получению: {-diff} руб.*")
    else:
        reply_parts.append(f"\n✅ *Всё в норме, зарплата покрыта*")

    await message.reply_text("\n".join(reply_parts), parse_mode=ParseMode.MARKDOWN)

    await _close_shift_and_notify(
        bot=message.get_bot(),
        chat_id=chat_id,
        shift_date_str=shift_date,
        reason="final_report",
        fin=fin,
    )


async def _save_work_report(message, context, result, chat_id, user_id, username, raw_text):
    """Сохраняет рабочий отчёт и отвечает текущей статистикой смены."""
    logger.info(f"Сохраняю отчёт: chat_id={chat_id}, car={result['car_number']}, freon={result['freon_volume']}, money={result['money']}, payment={result['payment_type']}")
    payment_label = result['payment_type']
    if result['payment_type'] == 'безнал' and result.get('payment_name') and result.get('payment_bank'):
        payment_label += f" ({result['payment_name']}, {result['payment_bank']})"
    db.add_record(
        chat_id=chat_id,
        user_id=user_id,
        username=username,
        car_number=result["car_number"],
        freon_volume=result["freon_volume"],
        money=result["money"],
        payment_type=result["payment_type"],
        payment_name=result.get("payment_name"),
        payment_bank=result.get("payment_bank"),
        nipple_count=result.get("nipple_count", 0),
        raw_text=raw_text,
    )
    stats = db.get_stats(chat_id=chat_id, day=today_msk())
    freon_label = f"{result['freon_volume']} г" if result['freon_volume'] > 0 else "0 (диагностика)"
    await message.reply_text(
        f"✅ *Отчёт принят!*\n"
        f"🚗 `{result['car_number']}` | 🧊 {freon_label} | 💰 {result['money']} руб. | 💳 {payment_label}\n\n"
        f"📊 *Итого за смену:*\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*\n"
        f"  💵 Нал: *{stats['total_nal']} руб.*\n"
        f"  💳 Безнал: *{stats['total_beznal']} руб.*\n"
        f"{_fmt_beznal_breakdown(stats)}"
        f"⛽ Заправка аппарата: *{stats['total_refill']} кг*\n"
        f"🔩 Ниппелей: *{stats['total_nipples']}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ======================================================================
# Уточняющие вопросы — рабочий отчёт
# ======================================================================

async def ask_clarification(message: Message, partial: dict, context: ContextTypes.DEFAULT_TYPE):
    missing  = partial.get("missing", [])
    problems = partial.get("problems", {})
    car      = partial.get("car_number") or "?"
    example  = f"{car}\nзалил 450\nвзял 3500\nнал" if car != "?" else "е133уу\nзалил 450\nвзял 3500\nнал"

    # Если несколько полей с bad_format — показываем полный формат
    bad_fields = [f for f in missing if problems.get(f) == "bad_format"]
    if len(bad_fields) >= 2:
        await message.reply_text(
            f"⚠️ Машина *{car}* — неверный формат отчёта.\n\n"
            f"Правильный формат:\n"
            f"```\n{example}\n```\n"
            "_(каждая строка отдельно, без двоеточий)_",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["waiting_for"] = bad_fields[0]
        return

    if "car_number" in missing:
        if problems.get("car_number") == "latin_not_allowed":
            await message.reply_text(
                f"⚠️ Номер машины написан *латиницей*.\n"
                "Используй только *кириллицу* (русские буквы).\n\n"
                "Правильный формат:\n"
                f"```\n{example}\n```\n"
                "_(1 буква + 3 цифры + 2 буквы)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text(
                f"❓ Не могу распознать *номер машины*.\n\n"
                "Правильный формат:\n"
                f"```\n{example}\n```\n"
                "_(1 буква + 3 цифры + 2 буквы, только кириллица)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data["waiting_for"] = "car_number"

    elif "freon_volume" in missing:
        if problems.get("freon_volume") == "ambiguous_kg":
            val = partial.get("freon_volume", "?")
            await message.reply_text(
                f"❓ Машина *{car}* — ты написал «залил {val}».\n"
                f"Это *{val} кг* или *{val} граммов*?\n\n"
                "Если граммов — напиши точное число (≥11).\n"
                "Если кг — переведи в граммы (1 кг = 1000 г).\n"
                "Если 0 — напиши *0* (только диагностика).\n\n"
                f"Правильный формат:\n"
                f"```\n{example}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
        elif problems.get("freon_volume") == "bad_format":
            await message.reply_text(
                f"⚠️ Машина *{car}* — неверный формат.\n\n"
                "Правильный формат:\n"
                f"```\n{example}\n```\n"
                "_(без двоеточий и лишних знаков)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text(
                f"❓ Машина *{car}* — не вижу строки «залил N».\n"
                "Сколько грамм фреона залили? (0 = только диагностика)\n\n"
                f"Правильный формат:\n"
                f"```\n{example}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data["waiting_for"] = "freon_volume"

    elif "money" in missing:
        if problems.get("money") == "too_small":
            await message.reply_text(
                f"⚠️ Машина *{car}* — сумма меньше 500 руб.\n"
                "Минимальная сумма для записи: *500 руб.* (0 = гарантия/дозаправка).\n\n"
                f"Правильный формат:\n"
                f"```\n{example}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
        elif problems.get("money") == "bad_format":
            await message.reply_text(
                f"⚠️ Машина *{car}* — неверный формат.\n\n"
                "Правильный формат:\n"
                f"```\n{example}\n```\n"
                "_(без двоеточий и лишних знаков)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text(
                f"❓ Машина *{car}* — не вижу строки «взял N».\n"
                "Сколько рублей взял с клиента?\n\n"
                f"Правильный формат:\n"
                f"```\n{example}\n```",
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data["waiting_for"] = "money"

    elif "payment_type" in missing:
        await message.reply_text(
            f"❓ Машина *{car}* — укажи способ оплаты: *нал* или *безнал*.\n\n"
            f"Правильный формат:\n"
            f"```\n{example}\n```",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data["waiting_for"] = "payment_type"

    elif "payment_name" in missing:
        prob = problems.get("payment_name")
        if prob == "invalid":
            await message.reply_text(
                f"⚠️ Машина *{car}* — имя *{partial.get('payment_name', '?')}* не найдено.\n\n"
                "Доступные имена: *Ксения*, *Алина*, *Алексей*, *Владимир*, *Себе*.\n"
                "Напиши правильное имя.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text(
                f"❓ Машина *{car}* — для *безнал* укажи, кому перевод:\n"
                "Доступные: *Ксения*, *Алина*, *Алексей*, *Владимир*, *Себе*.\n"
                "_(если *Себе* — банк можно не указывать)_",
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data["waiting_for"] = "payment_name"

    elif "payment_bank" in missing:
        prob = problems.get("payment_bank")
        if prob == "invalid":
            await message.reply_text(
                f"⚠️ Машина *{car}* — банк *{partial.get('payment_bank', '?')}* не найден.\n\n"
                "Доступные банки: *т-банк*, *сбер*, *альфа*.\n"
                "Напиши правильный банк.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text(
                f"❓ Машина *{car}* — для *безнал* укажи банк:\n"
                "Доступные: *т-банк*, *сбер*, *альфа*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data["waiting_for"] = "payment_bank"

    elif "nipple_count" in missing:
        prob = problems.get("nipple_count")
        if prob == "too_many":
            await message.reply_text(
                f"⚠️ Машина *{car}* — ниппелей не может быть больше *5*.\n"
                "Напиши правильное количество (0-5).",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.reply_text(
                f"❓ Машина *{car}* — ты написал «ниппеля», но не указал *количество*.\n"
                "Напиши число (например: *2 ниппеля*).",
                parse_mode=ParseMode.MARKDOWN,
            )
        context.user_data["waiting_for"] = "nipple_count"


async def handle_clarification_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message     = update.message
    waiting_for = context.user_data.get("waiting_for")
    text        = (message.text or message.caption or "").strip()
    chat_id     = message.chat_id
    username    = context.user_data.get("username", message.from_user.username)

    # --- уточнение финального отчёта ---
    if waiting_for in ("fr_total", "fr_mine", "fr_transfer"):
        partial = context.user_data.get("partial_final", {})
        val = parser.extract_number(text)
        if val is None:
            await message.reply_text(
                "⚠️ Напиши просто число.\n\n"
                "Новый формат (бот сам посчитает):\n"
                "```\nОбщая 13500\n```\n"
                "Старый формат (ручной ввод):\n"
                "```\nОбщая 13500\nМои 2900\nПеревожу 10600\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        partial[waiting_for] = val
        partial["missing"] = [m for m in partial.get("missing", []) if m != waiting_for]
        context.user_data["partial_final"] = partial

        if partial.get("missing"):
            await _ask_final_clarification(message, partial, context)
        else:
            fin = {"fr_total": partial["fr_total"],
                   "fr_mine": partial["fr_mine"],
                   "fr_transfer": partial["fr_transfer"]}
            context.user_data.clear()
            uid = message.from_user.id
            txt = message.text or ""
            await _handle_final_report(message, context, fin, user_id=uid, raw_text=txt)
        return

    # --- уточнение рабочего отчёта ---
    partial = context.user_data.get("partial", {})
    if not waiting_for or not partial:
        return

    if waiting_for == "car_number":
        if parser.has_latin_car(text):
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Номер написан *латиницей*.\n"
                "Перепиши *кириллицей*.\n\n"
                f"Правильный формат:\n"
                f"```\n{car}\nзалил 450\nвзял 3500\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
        car = parser.extract_car_number(text)
        if car:
            partial["car_number"] = car
            partial["missing"] = [m for m in partial.get("missing", []) if m != "car_number"]
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        else:
            car_example = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Не могу распознать номер.\n\n"
                f"Правильный формат:\n"
                f"```\n{car_example}\nзалил 450\nвзял 3500\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif waiting_for == "freon_volume":
        vol = parser.extract_number(text)
        if vol is not None and (vol == 0 or vol >= 11):
            partial["freon_volume"] = vol
            partial["missing"] = [m for m in partial.get("missing", []) if m != "freon_volume"]
            partial.get("problems", {}).pop("freon_volume", None)
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        elif vol is not None and 1 <= vol <= 10:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ *{vol}* — это кг или граммы?\n"
                "Напиши в граммах (≥11) или *0* для диагностики.\n\n"
                f"Правильный формат:\n"
                f"```\n{car}\nзалил 450\nвзял 3500\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Напиши число.\n\n"
                f"Правильный формат:\n"
                f"```\n{car}\nзалил 450\nвзял 3500\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif waiting_for == "money":
        money = parser.extract_number(text)
        if money is not None and (money == 0 or money >= 500):
            partial["money"] = money
            partial["missing"] = [m for m in partial.get("missing", []) if m != "money"]
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        elif money is not None and 0 < money < 500:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Сумма меньше 500 руб.\n"
                "Минимум: *500 руб.* (0 = гарантия/дозаправка).\n\n"
                f"Правильный формат:\n"
                f"```\n{car}\nзалил 450\nвзял 3500\nнал\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Введи сумму числом.\n\n"
                f"Правильный формат:\n"
                f"```\n{car}\nзалил 450\nвзял 3500\nнал\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif waiting_for == "payment_type":
        payment = parser.extract_payment_type(text)
        if payment:
            partial["payment_type"] = payment
            partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_type"]
            partial.get("problems", {}).pop("payment_type", None)
            if payment == "безнал":
                name = parser.extract_beznal_name(text)
                bank = parser.extract_beznal_bank(text)
                if name:
                    partial["payment_name"] = name
                    partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_name"]
                    partial.get("problems", {}).pop("payment_name", None)
                else:
                    if "payment_name" not in partial.get("missing", []):
                        partial.setdefault("missing", []).append("payment_name")
                        partial.setdefault("problems", {})["payment_name"] = "not_found"
                if name == "себе":
                    partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_bank"]
                    partial.get("problems", {}).pop("payment_bank", None)
                elif bank:
                    partial["payment_bank"] = bank
                    partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_bank"]
                    partial.get("problems", {}).pop("payment_bank", None)
                else:
                    if "payment_bank" not in partial.get("missing", []):
                        partial.setdefault("missing", []).append("payment_bank")
                        partial.setdefault("problems", {})["payment_bank"] = "not_found"
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        else:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Напиши *нал* или *безнал*.\n\n"
                f"Правильный формат:\n"
                f"```\n{car}\nзалил 450\nвзял 3500\nнал\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif waiting_for == "payment_name":
        name = parser.extract_beznal_name(text)
        bank = parser.extract_beznal_bank(text)
        if name:
            partial["payment_name"] = name
            partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_name"]
            partial.get("problems", {}).pop("payment_name", None)
            if name == "себе":
                partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_bank"]
                partial.get("problems", {}).pop("payment_bank", None)
            elif bank:
                partial["payment_bank"] = bank
                partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_bank"]
                partial.get("problems", {}).pop("payment_bank", None)
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        else:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Имя не распознано.\n"
                "Доступные: *Ксения*, *Алина*, *Алексей*, *Владимир*, *Себе*.",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif waiting_for == "payment_bank":
        bank = parser.extract_beznal_bank(text)
        name = parser.extract_beznal_name(text)
        if bank:
            partial["payment_bank"] = bank
            partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_bank"]
            partial.get("problems", {}).pop("payment_bank", None)
            if name:
                partial["payment_name"] = name
                partial["missing"] = [m for m in partial.get("missing", []) if m != "payment_name"]
                partial.get("problems", {}).pop("payment_name", None)
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        else:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Банк не распознан.\n"
                "Доступные: *т-банк*, *сбер*, *альфа*.",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif waiting_for == "nipple_count":
        vol = parser.extract_number(text)
        if vol is not None and 0 <= vol <= 5:
            partial["nipple_count"] = vol
            partial["missing"] = [m for m in partial.get("missing", []) if m != "nipple_count"]
            partial.get("problems", {}).pop("nipple_count", None)
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        else:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Напиши число от 0 до 5.\n"
                "Пример: *2 ниппеля*",
                parse_mode=ParseMode.MARKDOWN,
            )


async def _finish_work_report(message, context, data, chat_id, username):
    payment_label = data['payment_type']
    if data['payment_type'] == 'безнал' and data.get('payment_name') and data.get('payment_bank'):
        payment_label += f" ({data['payment_name']}, {data['payment_bank']})"
    db.add_record(
        chat_id=chat_id,
        user_id=message.from_user.id,
        username=username,
        car_number=data["car_number"],
        freon_volume=data["freon_volume"],
        money=data["money"],
        payment_type=data["payment_type"],
        payment_name=data.get("payment_name"),
        payment_bank=data.get("payment_bank"),
        nipple_count=data.get("nipple_count", 0),
        raw_text=context.user_data.get("raw_text", ""),
    )
    context.user_data.clear()
    stats = db.get_stats(chat_id=chat_id, day=today_msk())
    freon_label = f"{data['freon_volume']} г" if data['freon_volume'] > 0 else "0 (диагностика)"
    await message.reply_text(
        f"✅ *Отчёт сохранён!*\n"
        f"🚗 `{data['car_number']}` | 🧊 {freon_label} | 💰 {data['money']} руб. | 💳 {payment_label}\n\n"
        f"📊 *Итого за смену:*\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*\n"
        f"  💵 Нал: *{stats['total_nal']} руб.*\n"
        f"  💳 Безнал: *{stats['total_beznal']} руб.*\n"
        f"{_fmt_beznal_breakdown(stats)}"
        f"⛽ Заправка аппарата: *{stats['total_refill']} кг*\n"
        f"🔩 Ниппелей: *{stats['total_nipples']}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _save_refill_report(message, context, result, chat_id, user_id, username):
    """Сохраняет отчёт о заправке аппарата."""
    kg = result["refill_kg"]
    db.add_refill(
        chat_id=chat_id,
        user_id=user_id,
        username=username,
        refill_kg=kg,
        raw_text=(message.text or message.caption or ""),
    )
    stats = db.get_stats(chat_id=chat_id, day=today_msk())
    await message.reply_text(
        f"⛽ *Заправка аппарата принята!*\n"
        f"🛢️ Заправлено: *{kg} кг*\n\n"
        f"📊 *Итого за смену:*\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*\n"
        f"  💵 Нал: *{stats['total_nal']} руб.*\n"
        f"  💳 Безнал: *{stats['total_beznal']} руб.*\n"
        f"{_fmt_beznal_breakdown(stats)}"
        f"⛽ Заправка аппарата: *{stats['total_refill']} кг*\n"
        f"🔩 Ниппелей: *{stats['total_nipples']}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _save_nipple_report(message, context, result, chat_id, user_id, username):
    """Сохраняет отчёт о ниппелях (без привязки к машине)."""
    count = result["nipple_count"]
    db.add_record(
        chat_id=chat_id,
        user_id=user_id,
        username=username,
        car_number="—",
        freon_volume=0,
        money=0,
        payment_type="",
        nipple_count=count,
        raw_text=message.text or "",
    )
    stats = db.get_stats(chat_id=chat_id, day=today_msk())
    await message.reply_text(
        f"🔩 *Ниппели приняты!*\n"
        f"Заменено: *{count} шт.*\n\n"
        f"📊 *Итого за смену:*\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*\n"
        f"  💵 Нал: *{stats['total_nal']} руб.*\n"
        f"  💳 Безнал: *{stats['total_beznal']} руб.*\n"
        f"{_fmt_beznal_breakdown(stats)}"
        f"⛽ Заправка аппарата: *{stats['total_refill']} кг*\n"
        f"🔩 Ниппелей: *{stats['total_nipples']}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ======================================================================
# Уточняющие вопросы — финальный отчёт
# ======================================================================

async def _ask_final_clarification(message: Message, partial: dict,
                                    context: ContextTypes.DEFAULT_TYPE):
    missing = partial.get("missing", [])
    labels  = {"fr_total": "Общая (полная касса)",
               "fr_mine": "Мои (сколько оставляешь себе)",
               "fr_transfer": "Перевожу (сколько переводишь)"}

    field = missing[0]
    await message.reply_text(
        f"❓ В финансовом отчёте не вижу *{labels[field]}*.\n\n"
        "Новый формат (бот сам посчитает):\n"
        "```\nОбщая 13500\n```\n"
        "Старый формат (ручной ввод):\n"
        "```\nОбщая 13500\nМои 2900\nПеревожу 10600\n```\n",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["waiting_for"] = field


# ======================================================================
# Команды
# ======================================================================

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    logger.info(f"/stat вызван: chat_id={chat_id}")
    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        return

    today = today_msk()
    stats = db.get_stats(chat_id=chat_id, day=today)
    shift = db.get_shift(chat_id, today.isoformat())
    status = "🟢 открыта" if shift and not shift.get("closed_at") else "🔴 закрыта"

    if stats["count"] == 0 and not shift:
        await update.message.reply_text("📭 Сегодня смена ещё не открывалась.")
        return

    await update.message.reply_text(
        f"📊 *Статистика смены {today.strftime('%d.%m.%Y')}* ({status})\n\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*\n"
        f"  💵 Нал: *{stats['total_nal']} руб.*\n"
        f"  💳 Безнал: *{stats['total_beznal']} руб.*\n"
        f"{_fmt_beznal_breakdown(stats)}"
        f"⛽ Заправка аппарата: *{stats['total_refill']} кг*\n"
        f"🔩 Ниппелей: *{stats['total_nipples']}*\n\n"
        f"_Обновлено: {now_msk().strftime('%H:%M')} МСК_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stats_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_IDS and update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Только для администраторов.")
        return
    today = today_msk()
    stats = db.get_stats(day=today)
    await update.message.reply_text(
        f"📊 *Все чаты — {today.strftime('%d.%m.%Y')}*\n\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*\n"
        f"  💵 Нал: *{stats['total_nal']} руб.*\n"
        f"  💳 Безнал: *{stats['total_beznal']} руб.*\n"
        f"{_fmt_beznal_breakdown(stats)}"
        f"⛽ Заправка аппарата: *{stats['total_refill']} кг*\n"
        f"🔩 Ниппелей: *{stats['total_nipples']}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def send_daily_excel(bot: Bot, export_date: date,
                            target_chat_id: int = None):
    """Собирает данные по всем чатам за день и шлёт Excel.
    Если target_chat_id указан — шлёт только ему, иначе всем админам."""
    records = db.get_records(day=export_date)
    refills = db.get_refills(day=export_date)
    if not records and not refills:
        logger.info("Нет записей за сегодня, Excel не отправляю.")
        return

    # Группируем записи по chat_id
    chat_data = {}
    for r in records:
        cid = r["chat_id"]
        if cid not in chat_data:
            chat_data[cid] = {"records": [], "refills": [], "stats": None, "fin": None, "name": None}
        chat_data[cid]["records"].append(r)
    for r in refills:
        cid = r["chat_id"]
        if cid not in chat_data:
            chat_data[cid] = {"records": [], "refills": [], "stats": None, "fin": None, "name": None}
        chat_data[cid]["refills"].append(r)

    # Получаем имена чатов и статистику
    for cid in chat_data:
        try:
            chat = await bot.get_chat(cid)
            chat_data[cid]["name"] = chat.title or f"Чат {cid}"
        except Exception:
            chat_data[cid]["name"] = f"Чат {cid}"
        
        chat_data[cid]["stats"] = db.get_stats(chat_id=cid, day=export_date)
        
        shift = db.get_shift(cid, export_date.isoformat())
        if shift:
            chat_data[cid]["fin"] = {
                "fr_total": shift.get("fr_total"),
                "fr_mine": shift.get("fr_mine"),
                "fr_transfer": shift.get("fr_transfer"),
            }

    # Экспортируем мульти-чат файл
    filepath = exporter.export_multi_chat(chat_data, export_date)
    
    # Кому отправляем
    targets = [target_chat_id] if target_chat_id else ADMIN_IDS
    for cid in targets:
        try:
            with open(filepath, "rb") as f:
                await bot.send_document(
                    chat_id=cid,
                    document=f,
                    filename=os.path.basename(filepath),
                    caption=f"📋 Отчёт за {export_date.strftime('%d.%m.%Y')} ({len(chat_data)} точек)"
                )
        except Exception as e:
            logger.warning(f"Не смог отправить Excel {cid}: {e}")
    
    os.remove(filepath)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_IDS and update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Только для администраторов.")
        return

    args = context.args
    if args:
        try:
            export_date = datetime.strptime(args[0], "%d.%m.%Y").date()
        except ValueError:
            await update.message.reply_text("⚠️ Формат: /выгрузка 18.05.2025")
            return
    else:
        export_date = today_msk()

    await update.message.reply_text("⏳ Формирую общий отчёт по всем точкам...")
    await send_daily_excel(context.bot, export_date,
                            target_chat_id=update.effective_user.id)


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удалить все отчёты с указанным номером машины за сегодня."""
    args = context.args
    if not args:
        await update.message.reply_text("⚠️ Формат: /удалить <номер машины>\nПример: /удалить е133уу")
        return

    car_number = " ".join(args)  # In case the plate has spaces
    chat_id = update.effective_chat.id
    
    # Normalize the car number for comparison
    normalized_car = None
    try:
        from parser import ReportParser
        parser = ReportParser()
        normalized_car = parser.extract_car_number(car_number)
        if not normalized_car:
            # Try without normalization for flexible plates
            normalized_car = car_number.replace(' ', '').upper()
    except Exception:
        normalized_car = car_number.replace(' ', '').upper()

    deleted_count = db.delete_records_by_car(chat_id, normalized_car)
    
    if deleted_count > 0:
        await update.message.reply_text(
            f"🗑️ Удалено {deleted_count} отчёт(ов) с номером *{car_number}* за сегодня.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ Отчётов с номером *{car_number}* за сегодня не найдено.",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот учёта заправки фреона*\n\n"
        "*Формат рабочего отчёта:*\n"
        "```\ne133уу\nзалил 450\nвзял 3500\nнал\n```\n"
        "_(нал или безнал — обязательно)_\n\n"
        "*Заправка аппарата:*\n"
        "```\nЗаправка аппарата 5\n```\n"
        "_(число в кг, можно с дробью: 5.5)_\n\n"
        "*Финансовый отчёт (конец смены):*\n"
        "```\nОбщая 13500\n```\n"
        "👆 *Новый формат* — бот сам посчитает:\n"
        "   • 25% от общей — твоя зарплата\n"
        "   • Вычтет нал и безнал «Себе»\n"
        "   • Скажет сумму к переводу\n\n"
        "```\nОбщая 13500\nМои 2900\nПеревожу 10600\n```\n"
        "👆 *Старый формат* — ручной ввод (если считаешь сам)\n\n"
        "*Команды:*\n"
        "/stat — статистика текущей смены\n"
        "/statall — все чаты (адм.)\n"
        "/export — Excel сегодня (адм.)\n"
        "/export 18.05.2025 — Excel за дату (адм.)",
        parse_mode=ParseMode.MARKDOWN,
    )


# ======================================================================
# Автозакрытие смен в 23:50 МСК
# ======================================================================

async def auto_close_shifts(context: ContextTypes.DEFAULT_TYPE):
    """Джоб: закрывает все незакрытые смены за сегодня и шлёт общий Excel."""
    today_str = today_msk().isoformat()
    open_shifts = db.get_open_shifts()

    for shift in open_shifts:
        if shift["shift_date"] != today_str:
            continue  # чужой день — не трогаем
        logger.info(f"Автозакрытие смены chat_id={shift['chat_id']}")
        await _close_shift_and_notify(
            bot=context.bot,
            chat_id=shift["chat_id"],
            shift_date_str=shift["shift_date"],
            reason="auto_midnight",
            fin=None,
            silent=True,
        )
    
    # Ждём пару секунд, чтобы база успела обновиться
    import asyncio
    await asyncio.sleep(2)
    
    # Отправляем общий Excel админам
    await send_daily_excel(context.bot, today_msk())


# ======================================================================
# Запуск
# ======================================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("stat", cmd_stats))
    app.add_handler(CommandHandler("statall", cmd_stats_all))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("delete", cmd_delete))
    app.add_handler(CommandHandler(["help", "start"], cmd_help))

    # Все текстовые сообщения — через один обработчик
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO, handle_message))

    # Джоб автозакрытия в 23:50 МСК каждый день
    from datetime import time as dtime
    app.job_queue.run_daily(
        auto_close_shifts,
        time=dtime(hour=23, minute=50, second=0, tzinfo=MSK),
        name="auto_close",
    )

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
