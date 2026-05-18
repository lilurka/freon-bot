"""
Экспорт отчётов в Excel.
"""

import os
from datetime import date
from typing import List, Dict

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
