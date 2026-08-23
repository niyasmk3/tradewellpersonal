"""The stdlib .xlsx writer: a valid OOXML package with typed cells."""
from __future__ import annotations

import io
import os
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.closing import xlsx

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def test_workbook_is_a_valid_package_with_typed_cells():
    rows = [{"date": "2026-08-20", "net_rs": -3864.14, "tier": "A", "narrow": True,
             "card_red": ["volexp", "bridge"], "skipped": None},
            {"date": "2026-08-21", "net_rs": 4414.0, "tier": None, "narrow": False,
             "card_red": [], "skipped": None}]
    cols = ["date", "net_rs", "tier", "narrow", "card_red", "skipped"]
    body = xlsx.workbook(rows, cols, sheet="Closing ledger")
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
                "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml"} <= names
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
        for part in names:
            ET.fromstring(z.read(part))          # every part is well-formed XML
    data_rows = sheet.findall(".//m:sheetData/m:row", NS)
    assert len(data_rows) == 3                   # header + 2
    cells = {c.get("r"): c for c in data_rows[1].findall("m:c", NS)}
    assert cells["A2"].get("t") == "inlineStr"
    assert cells["B2"].get("t") is None and cells["B2"].find("m:v", NS).text == "-3864.14"
    assert cells["D2"].get("t") == "b" and cells["D2"].find("m:v", NS).text == "1"
    assert cells["E2"].find(".//m:t", NS).text == "volexp, bridge"
    assert "F2" not in cells                     # None -> no cell
    assert "C3" not in cells                     # None tier -> no cell


def test_column_letters_past_z():
    assert [xlsx._col(i) for i in (0, 25, 26, 27, 51, 52, 701, 702)] == \
        ["A", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"]
