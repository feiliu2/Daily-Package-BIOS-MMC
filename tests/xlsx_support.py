"""Write minimal .xlsx files for tests using only the standard library."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

HEADER = ["Original Package", "Original Binary", "Original Folder", "Target Name",
          "Package file"]


def _column(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


def make_xlsx(
    rows: list[list[str]], *, sheet: str = "config", inline: bool = False,
    leading_sheet: str | None = "Sheet0",
) -> bytes:
    strings: list[str] = []
    sheet_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column, value in enumerate(row):
            if value == "":
                continue  # Excel omits empty cells; parsers must honour references.
            reference = f"{_column(column)}{row_number}"
            if inline:
                cells.append(
                    f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
                )
            else:
                strings.append(value)
                cells.append(f'<c r="{reference}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheets = []
    relationships = []
    if leading_sheet:
        sheets.append(f'<sheet name="{leading_sheet}" sheetId="1" r:id="rId1"/>')
        relationships.append(
            '<Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>'
        )
    sheets.append(f'<sheet name="{sheet}" sheetId="2" r:id="rId2"/>')
    relationships.append(
        '<Relationship Id="rId2" Type="worksheet" Target="worksheets/sheet2.xml"/>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{main}" xmlns:r="{rel}"><sheets>{"".join(sheets)}'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships">{"".join(relationships)}</Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{main}"><sheetData/></worksheet>',
        )
        archive.writestr(
            "xl/worksheets/sheet2.xml",
            f'<worksheet xmlns="{main}"><sheetData>{"".join(sheet_rows)}'
            "</sheetData></worksheet>",
        )
        if not inline:
            items = "".join(f"<si><t>{escape(value)}</t></si>" for value in strings)
            archive.writestr(
                "xl/sharedStrings.xml", f'<sst xmlns="{main}">{items}</sst>'
            )
    return buffer.getvalue()
