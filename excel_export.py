"""
Экспорт отчётов в Excel.
"""

import os
from datetime import date
from typing import List, Dict, Optional

try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False


class ExcelExporter:
    def export(self, records: List[Dict], export_date: date) -> str:
        """Создаёт Excel файл и возвращает путь к нему."""
        if not OPENPYXL_AVAILABLE:
            return self._export_csv(records, export_date)
        return self._export_xlsx(records, export_date)

    def export_multi_chat(self, chat_data: Dict[int, Dict], export_date: date) -> str:
        """
        Создаёт один Excel файл с листами по каждому чату.
        chat_data: {chat_id: {"records": [...], "stats": {...}, "fin": {...}}}
        """
        if not OPENPYXL_AVAILABLE:
            return self._export_csv_multi_chat(chat_data, export_date)
        return self._export_xlsx_multi_chat(chat_data, export_date)

    def _export_xlsx_multi_chat(self, chat_data: Dict[int, Dict], export_date: date) -> str:
        wb = openpyxl.Workbook()

        # Удаляем дефолтный лист
        default_sheet = wb.active
        wb.remove(default_sheet)

        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_align = Alignment(horizontal="center", vertical="center")
        total_font = Font(bold=True, size=11)
        total_fill = PatternFill("solid", fgColor="D6E4F0")
        thin = Side(style="thin", color="CCCCCC")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)
        headers = ["№", "Время", "Сотрудник", "Номер машины", "Фреон (г/мл)", "Сумма (руб.)"]
        col_widths = [5, 12, 18, 16, 14, 14]

        for chat_id, data in chat_data.items():
            records = data.get("records", [])
            stats = data.get("stats", {})
            fin = data.get("fin")

            # Берем имя чата из данных, если нет — используем ID
            sheet_name = data.get("name", str(chat_id))
            # Excel не любит символы []*?/\ и длину > 31
            for char in ['[', ']', '*', '?', '/', '\\', ':']:
                sheet_name = sheet_name.replace(char, '')
            ws = wb.create_sheet(title=sheet_name[:31])

            for col_idx, (header, width) in enumerate(zip(headers, col_widths), 1):
                cell = ws.cell(row=1, column=col_idx, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_align
                cell.border = border
                ws.column_dimensions[get_column_letter(col_idx)].width = width
            ws.row_dimensions[1].height = 22

            for row_idx, record in enumerate(records, 2):
                time_str = record.get("created_at", "")
                if "T" in str(time_str):
                    time_str = time_str.split("T")[1][:5]
                elif " " in str(time_str):
                    time_str = str(time_str).split(" ")[1][:5]

                row_data = [
                    row_idx - 1,
                    time_str,
                    record.get("username", ""),
                    record.get("car_number", ""),
                    record.get("freon_volume", 0),
                    record.get("money", 0),
                ]
                alt_fill = PatternFill("solid", fgColor="EBF3FB") if row_idx % 2 == 0 else None
                for col_idx, value in enumerate(row_data, 1):
                    cell = ws.cell(row=row_idx, column=col_idx, value=value)
                    cell.border = border
                    cell.alignment = Alignment(horizontal="center" if col_idx in (1, 2, 4, 5, 6) else "left")
                    if alt_fill:
                        cell.fill = alt_fill

            total_row = len(records) + 2
            total_freon = sum(r.get("freon_volume", 0) for r in records)
            total_money = sum(r.get("money", 0) for r in records)

            ws.cell(row=total_row, column=1, value="ИТОГО").font = total_font
            ws.cell(row=total_row, column=1).fill = total_fill
            ws.cell(row=total_row, column=3, value=f"{len(records)} машин").font = total_font
            ws.cell(row=total_row, column=3).fill = total_fill
            ws.cell(row=total_row, column=5, value=total_freon).font = total_font
            ws.cell(row=total_row, column=5).fill = total_fill
            ws.cell(row=total_row, column=6, value=total_money).font = total_font
            ws.cell(row=total_row, column=6).fill = total_fill
            for col_idx in range(1, 7):
                ws.cell(row=total_row, column=col_idx).border = border
                ws.cell(row=total_row, column=col_idx).alignment = Alignment(horizontal="center")

            if fin:
                fin_row = total_row + 2
                ws.cell(row=fin_row, column=1, value="Финансы:").font = total_font
                ws.cell(row=fin_row, column=3, value=f"Общая: {fin.get('fr_total', 0)}").font = total_font
                ws.cell(row=fin_row + 1, column=3, value=f"Мои: {fin.get('fr_mine', 0)}").font = total_font
                ws.cell(row=fin_row + 2, column=3, value=f"Перевожу: {fin.get('fr_transfer', 0)}").font = total_font

        # Лист "Итого" — общая сводка по всем чатам
        summary_ws = wb.create_sheet(title="Итого", index=0)
        summary_headers = ["Чат", "Машин", "Фреон (г)", "Выручка (руб.)"]
        summary_widths = [12, 12, 14, 18]
        for col_idx, (header, width) in enumerate(zip(summary_headers, summary_widths), 1):
            cell = summary_ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = border
            summary_ws.column_dimensions[get_column_letter(col_idx)].width = width
        summary_ws.row_dimensions[1].height = 22

        grand_total_cars = 0
        grand_total_freon = 0
        grand_total_money = 0

        for row_idx, (chat_id, data) in enumerate(chat_data.items(), 2):
            stats = data.get("stats", {})
            cars = stats.get("count", 0)
            freon = stats.get("total_freon", 0)
            money = stats.get("total_money", 0)
            grand_total_cars += cars
            grand_total_freon += freon
            grand_total_money += money

            alt_fill = PatternFill("solid", fgColor="EBF3FB") if row_idx % 2 == 0 else None
            chat_name = data.get("name", str(chat_id))
            for col_idx, value in enumerate([chat_name, cars, freon, money], 1):
                cell = summary_ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = border
                cell.alignment = Alignment(horizontal="center")
                if alt_fill:
                    cell.fill = alt_fill

        total_row = len(chat_data) + 2
        summary_ws.cell(row=total_row, column=1, value="ВСЕГО").font = total_font
        summary_ws.cell(row=total_row, column=1).fill = total_fill
        summary_ws.cell(row=total_row, column=2, value=grand_total_cars).font = total_font
        summary_ws.cell(row=total_row, column=2).fill = total_fill
        summary_ws.cell(row=total_row, column=3, value=grand_total_freon).font = total_font
        summary_ws.cell(row=total_row, column=3).fill = total_fill
        summary_ws.cell(row=total_row, column=4, value=grand_total_money).font = total_font
        summary_ws.cell(row=total_row, column=4).fill = total_fill
        for col_idx in range(1, 5):
            summary_ws.cell(row=total_row, column=col_idx).border = border
            summary_ws.cell(row=total_row, column=col_idx).alignment = Alignment(horizontal="center")

        filename = f"report_{export_date.strftime('%Y-%m-%d')}.xlsx"
        wb.save(filename)
        return filename

    def _export_csv_multi_chat(self, chat_data: Dict[int, Dict], export_date: date) -> str:
        filename = f"report_{export_date.strftime('%Y-%m-%d')}.csv"
        with open(filename, "w", encoding="utf-8-sig") as f:
            for chat_id, data in chat_data.items():
                f.write(f"\n=== Чат {chat_id} ===\n")
                f.write("№,Время,Сотрудник,Номер машины,Фреон,Сумма\n")
                records = data.get("records", [])
                for i, r in enumerate(records, 1):
                    f.write(f"{i},{r.get('created_at','')},{r.get('username','')},{r.get('car_number','')},{r.get('freon_volume',0)},{r.get('money',0)}\n")
                total_freon = sum(r.get("freon_volume", 0) for r in records)
                total_money = sum(r.get("money", 0) for r in records)
                f.write(f"ИТОГО,,,{len(records)} машин,{total_freon},{total_money}\n")
        return filename

    def _export_xlsx(self, records: List[Dict], export_date: date) -> str:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = export_date.strftime("%d.%m.%Y")

        # Стили
        header_font = Font(bold=True, color="FFFFFF", size=11)
        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_align = Alignment(horizontal="center", vertical="center")

        total_font = Font(bold=True, size=11)
        total_fill = PatternFill("solid", fgColor="D6E4F0")

        thin = Side(style="thin", color="CCCCCC")
        border = Border(left=thin, right=thin, top=thin, bottom=thin)

        # Заголовки
        headers = ["№", "Время", "Сотрудник", "Номер машины", "Фреон (г/мл)", "Сумма (руб.)"]
        col_widths = [5, 12, 18, 16, 14, 14]

        for col_idx, (header, width) in enumerate(zip(headers, col_widths), 1):
            cell = ws.cell(row=1, column=col_idx, value=header)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = border
            ws.column_dimensions[get_column_letter(col_idx)].width = width

        ws.row_dimensions[1].height = 22

        # Данные
        for row_idx, record in enumerate(records, 2):
            time_str = record.get("created_at", "")
            if "T" in str(time_str):
                time_str = time_str.split("T")[1][:5]
            elif " " in str(time_str):
                time_str = str(time_str).split(" ")[1][:5]

            row_data = [
                row_idx - 1,
                time_str,
                record.get("username", ""),
                record.get("car_number", ""),
                record.get("freon_volume", 0),
                record.get("money", 0),
            ]

            alt_fill = PatternFill("solid", fgColor="EBF3FB") if row_idx % 2 == 0 else None

            for col_idx, value in enumerate(row_data, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=value)
                cell.border = border
                cell.alignment = Alignment(horizontal="center" if col_idx in (1, 2, 4, 5, 6) else "left")
                if alt_fill:
                    cell.fill = alt_fill

        # Итоговая строка
        total_row = len(records) + 2
        total_freon = sum(r.get("freon_volume", 0) for r in records)
        total_money = sum(r.get("money", 0) for r in records)

        ws.cell(row=total_row, column=1, value="ИТОГО").font = total_font
        ws.cell(row=total_row, column=1).fill = total_fill
        ws.cell(row=total_row, column=3, value=f"{len(records)} машин").font = total_font
        ws.cell(row=total_row, column=3).fill = total_fill
        ws.cell(row=total_row, column=5, value=total_freon).font = total_font
        ws.cell(row=total_row, column=5).fill = total_fill
        ws.cell(row=total_row, column=6, value=total_money).font = total_font
        ws.cell(row=total_row, column=6).fill = total_fill

        for col_idx in range(1, 7):
            ws.cell(row=total_row, column=col_idx).border = border
            ws.cell(row=total_row, column=col_idx).alignment = Alignment(horizontal="center")

        # Сохраняем
        filename = f"report_{export_date.strftime('%Y-%m-%d')}.xlsx"
        wb.save(filename)
        return filename

    def _export_csv(self, records: List[Dict], export_date: date) -> str:
        """Резервный вариант — CSV."""
        filename = f"report_{export_date.strftime('%Y-%m-%d')}.csv"
        with open(filename, "w", encoding="utf-8-sig") as f:
            f.write("№,Время,Сотрудник,Номер машины,Фреон,Сумма\n")
            for i, r in enumerate(records, 1):
                f.write(f"{i},{r.get('created_at','')},{r.get('username','')},{r.get('car_number','')},{r.get('freon_volume',0)},{r.get('money',0)}\n")
            total_freon = sum(r.get("freon_volume", 0) for r in records)
            total_money = sum(r.get("money", 0) for r in records)
            f.write(f"ИТОГО,,,{len(records)} машин,{total_freon},{total_money}\n")
        return filename
