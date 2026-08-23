"""Minimal .xlsx writer — stdlib only, for the ledger download.

An OOXML workbook is a zip of five XML parts. Numbers go in as numeric cells,
booleans as booleans, everything else as inline strings; None is an empty
cell. No styles part is written (Excel supplies defaults), which keeps this
dependency-free on a box that only has Python 3.9 and no openpyxl.
"""
from __future__ import annotations

import io
import json
import zipfile
from xml.sax.saxutils import escape

_CT = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
       '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
       '</Types>')
_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
         '</Relationships>')
_WB_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '</Relationships>')


def _col(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        if value != value or value in (float("inf"), float("-inf")):  # NaN/inf
            return ""
        return f'<c r="{ref}"><v>{value!r}</v></c>'
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    elif isinstance(value, dict):
        text = json.dumps(value)
    else:
        text = str(value)
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def workbook(rows: list, columns: list, sheet: str = "Ledger") -> bytes:
    """Rows are dicts; `columns` fixes the order. Returns the .xlsx bytes."""
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
             'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetData>']
    header = "".join(_cell(f"{_col(i)}1", c) for i, c in enumerate(columns))
    lines.append(f'<row r="1">{header}</row>')
    for r, row in enumerate(rows, start=2):
        cells = "".join(_cell(f"{_col(i)}{r}", row.get(c)) for i, c in enumerate(columns))
        lines.append(f'<row r="{r}">{cells}</row>')
    lines.append("</sheetData></worksheet>")
    title = escape(sheet[:31]).replace('"', "")
    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
          f'<sheets><sheet name="{title}" sheetId="1" r:id="rId1"/></sheets></workbook>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/worksheets/sheet1.xml", "".join(lines))
    return buf.getvalue()
