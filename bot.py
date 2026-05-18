"""
Телеграм-бот учёта заправки фреона.

Смены:
  - Открываются автоматически с первым отчётом за день (МСК)
  - Закрываются финальным отчётом «Общая N / Мои N / Перевожу N»
  - Автозакрытие в 23:59 МСК если финальный отчёт не пришёл
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

def _shift_summary_text(stats: dict, shift_date: date,
                         fin: dict = None, label: str = "смену") -> str:
    """Формирует итоговый текст статистики смены."""
    lines = [
        f"📊 *Итого за {label} ({shift_date.strftime('%d.%m.%Y')}):*",
        f"🚗 Машин: *{stats['count']}*",
        f"🧊 Фреона: *{stats['total_freon']} г*",
        f"💰 Выручка: *{stats['total_money']} руб.*",
    ]
    if fin:
        lines += [
            "",
            "💼 *Финансовый отчёт сотрудника:*",
            f"  Общая касса:  *{fin['fr_total']} руб.*",
            f"  Мои:          *{fin['fr_mine']} руб.*",
            f"  Перевожу:     *{fin['fr_transfer']} руб.*",
        ]
    return "\n".join(lines)


async def _close_shift_and_notify(bot: Bot, chat_id: int,
                                   shift_date_str: str, reason: str,
                                   fin: dict = None):
    """Закрывает смену, шлёт итог в чат."""
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

    stats = db.get_stats(chat_id=chat_id, day=shift_date)

    reason_label = "конец смены" if reason == "final_report" else "автозакрытие (23:59)"
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
        await handle_clarification_reply(update, context)
        return

    result = parser.parse(text)
    logger.info(f"Парсинг: chat_id={chat_id}, text={text!r}, result={result}")

    if result["type"] == "final":
        await _handle_final_report(message, context, result)

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

    # "ignore" — молчим


async def _handle_final_report(message: Message, context: ContextTypes.DEFAULT_TYPE, fin: dict):
    """Финальный отчёт: закрываем смену."""
    chat_id    = message.chat_id
    shift_date = today_msk().isoformat()

    # Подтверждение в чат немедленно
    await message.reply_text(
        f"✅ *Финансовый отчёт принят!*\n"
        f"💼 Общая: *{fin['fr_total']} руб.*  |  "
        f"Мои: *{fin['fr_mine']} руб.*  |  "
        f"Перевожу: *{fin['fr_transfer']} руб.*\n\n"
        f"⏳ Закрываю смену...",
        parse_mode=ParseMode.MARKDOWN
    )

    await _close_shift_and_notify(
        bot=message.get_bot(),
        chat_id=chat_id,
        shift_date_str=shift_date,
        reason="final_report",
        fin=fin,
    )


async def _save_work_report(message, context, result, chat_id, user_id, username, raw_text):
    """Сохраняет рабочий отчёт и отвечает текущей статистикой смены."""
    logger.info(f"Сохраняю отчёт: chat_id={chat_id}, car={result['car_number']}, freon={result['freon_volume']}, money={result['money']}")
    db.add_record(
        chat_id=chat_id,
        user_id=user_id,
        username=username,
        car_number=result["car_number"],
        freon_volume=result["freon_volume"],
        money=result["money"],
        raw_text=raw_text,
    )
    stats = db.get_stats(chat_id=chat_id, day=today_msk())
    freon_label = f"{result['freon_volume']} г" if result['freon_volume'] > 0 else "0 (диагностика)"
    await message.reply_text(
        f"✅ *Отчёт принят!*\n"
        f"🚗 `{result['car_number']}` | 🧊 {freon_label} | 💰 {result['money']} руб.\n\n"
        f"📊 *Итого за смену:*\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ======================================================================
# Уточняющие вопросы — рабочий отчёт
# ======================================================================

async def ask_clarification(message: Message, partial: dict, context: ContextTypes.DEFAULT_TYPE):
    missing  = partial.get("missing", [])
    problems = partial.get("problems", {})
    car      = partial.get("car_number") or "?"
    example  = f"{car}\nзалил 450\nвзял 3500" if car != "?" else "е133уу\nзалил 450\nвзял 3500"

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
        if problems.get("money") == "bad_format":
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
                "Правильный формат:\n"
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
            await _handle_final_report(message, context, fin)
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
        if money is not None and money >= 0:
            partial["money"] = money
            partial["missing"] = [m for m in partial.get("missing", []) if m != "money"]
            context.user_data["partial"] = partial
            if partial.get("missing"):
                await ask_clarification(message, partial, context)
            else:
                await _finish_work_report(message, context, partial, chat_id, username)
        else:
            car = partial.get("car_number") or "е133уу"
            await message.reply_text(
                f"⚠️ Введи сумму числом.\n\n"
                f"Правильный формат:\n"
                f"```\n{car}\nзалил 450\nвзял 3500\n```\n",
                parse_mode=ParseMode.MARKDOWN,
            )


async def _finish_work_report(message, context, data, chat_id, username):
    db.add_record(
        chat_id=chat_id,
        user_id=message.from_user.id,
        username=username,
        car_number=data["car_number"],
        freon_volume=data["freon_volume"],
        money=data["money"],
        raw_text=context.user_data.get("raw_text", ""),
    )
    context.user_data.clear()
    stats = db.get_stats(chat_id=chat_id, day=today_msk())
    freon_label = f"{data['freon_volume']} г" if data['freon_volume'] > 0 else "0 (диагностика)"
    await message.reply_text(
        f"✅ *Отчёт сохранён!*\n"
        f"🚗 `{data['car_number']}` | 🧊 {freon_label} | 💰 {data['money']} руб.\n\n"
        f"📊 *Итого за смену:*\n"
        f"🚗 Машин: *{stats['count']}*\n"
        f"🧊 Фреона: *{stats['total_freon']} г*\n"
        f"💰 Выручка: *{stats['total_money']} руб.*",
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
        "Правильный формат:\n"
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
        f"💰 Выручка: *{stats['total_money']} руб.*\n\n"
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
        f"💰 Выручка: *{stats['total_money']} руб.*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def send_daily_excel(bot: Bot, export_date: date):
    """Собирает данные по всем чатам за день и шлёт один Excel админам."""
    records = db.get_records(day=export_date)
    if not records:
        logger.info("Нет записей за сегодня, Excel не отправляю.")
        return

    # Группируем записи по chat_id
    chat_data = {}
    for r in records:
        cid = r["chat_id"]
        if cid not in chat_data:
            chat_data[cid] = {"records": [], "stats": None, "fin": None, "name": None}
        chat_data[cid]["records"].append(r)

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
    
    # Отправляем админам
    for admin_id in ADMIN_IDS:
        try:
            with open(filepath, "rb") as f:
                await bot.send_document(
                    chat_id=admin_id,
                    document=f,
                    filename=os.path.basename(filepath),
                    caption=f"📋 Отчёт за {export_date.strftime('%d.%m.%Y')} ({len(chat_data)} точек)"
                )
        except Exception as e:
            logger.warning(f"Не смог отправить Excel админу {admin_id}: {e}")
    
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
    await send_daily_excel(context.bot, export_date)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Бот учёта заправки фреона*\n\n"
        "*Формат рабочего отчёта:*\n"
        "```\ne133уу\nзалил 450\nвзял 3500\n```\n\n"
        "*Финансовый отчёт (конец смены):*\n"
        "```\nОбщая 13500\nМои 2900\nПеревожу 10600\n```\n\n"
        "*Команды:*\n"
        "/stat — статистика текущей смены\n"
        "/statall — все чаты (адм.)\n"
        "/export — Excel сегодня (адм.)\n"
        "/export 18.05.2025 — Excel за дату (адм.)",
        parse_mode=ParseMode.MARKDOWN,
    )


# ======================================================================
# Автозакрытие смен в 23:59 МСК
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
    app.add_handler(CommandHandler(["help", "start"], cmd_help))

    # Все текстовые сообщения — через один обработчик
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO, handle_message))

    # Джоб автозакрытия в 23:59 МСК каждый день
    from datetime import time as dtime
    app.job_queue.run_daily(
        auto_close_shifts,
        time=dtime(hour=23, minute=59, second=0, tzinfo=MSK),
        name="auto_close",
    )

    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
