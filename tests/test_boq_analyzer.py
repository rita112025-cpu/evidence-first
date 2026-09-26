"""用 reportlab 產生假 BOQ PDF 測試 (需要 pip install reportlab pdfplumber)。執行: python -m unittest discover tests"""
import os, sys, tempfile, unittest, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, PageBreak
from reportlab.lib import colors
import scada_ingestion_template as ing
import scada_boq_analyzer as boq

HEAD = ["Item", "Description", "Spec", "Unit", "Qty", "Remarks"]
STYLE = TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)])


def make_pdf(path, pages):
    doc = SimpleDocTemplate(path, pagesize=A4)
    story = []
    for i, data in enumerate(pages):
        if i:
            story.append(PageBreak())
        story.append(Table(data, style=STYLE))
    doc.build(story)


class BoqTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = ing.open_db(os.path.join(self.d, "t.db"))

    def pdf(self, pages, name="boq.pdf"):
        p = os.path.join(self.d, name)
        make_pdf(p, pages)
        return p

    def test_multipage_header_only_first_page_and_flags(self):
        p = self.pdf([
            [HEAD, ["1", "RTU-01 Remote Terminal", "IP65", "set", "2", ""],
                   ["2", "Cable Tray TR-200", "300mm", "m", "1,250", "galv"]],
            [["3", "Junction box", "", "pc", "TBD", ""]],      # 第 2 頁沒有表頭、qty 不是數字
        ])
        res = boq.analyze_boq(self.db, p, "RevC")
        self.assertEqual((res["rows_found"], res["inserted"]), (3, 3))
        self.assertEqual(len(res["warnings"]), 1)
        rows = self.db.execute("SELECT entity_name, tag, boq_exists, cad_exists, ifc_exists, attributes_json "
                               "FROM engineering_entities ORDER BY entity_name").fetchall()
        by = {r[0]: r for r in rows}
        self.assertEqual(by["Cable Tray TR-200"][1], "TR-200")
        self.assertEqual(json.loads(by["Cable Tray TR-200"][5])["qty"], 1250.0)
        self.assertIsNone(by["Junction box"][1])
        self.assertEqual(json.loads(by["Junction box"][5])["page"], 2)
        self.assertIsNone(json.loads(by["Junction box"][5])["qty"])
        for r in rows:                       # 尚未比對 -> 進 v_unchecked，不進 v_missing_check
            self.assertEqual((r[2], r[3], r[4]), (1, None, None))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v_unchecked").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM v_missing_check").fetchone()[0], 0)

    def test_rerun_is_idempotent(self):
        p = self.pdf([[HEAD, ["1", "RTU-01", "", "set", "2", ""]]])
        boq.analyze_boq(self.db, p, "RevC")
        res = boq.analyze_boq(self.db, p, "RevC")
        self.assertEqual(res["inserted"], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM engineering_entities").fetchone()[0], 1)

    def test_no_header_raises_and_leaves_nothing(self):
        p = self.pdf([[["a", "b"], ["c", "d"]]])
        with self.assertRaises(ValueError):
            boq.analyze_boq(self.db, p, "RevC")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0], 0)

    def test_chunk_link_when_page_chunks_exist(self):
        p = self.pdf([[HEAD, ["1", "RTU-01", "", "set", "2", ""]]])
        rec = ing.new_evidence_record(p, "RevC")
        eid, _ = ing.insert_evidence(self.db, rec)
        ing.insert_pdf_chunks(self.db, eid, "page one text"); self.db.commit()
        boq.analyze_boq(self.db, p, "RevC")
        self.assertIsNotNone(self.db.execute("SELECT chunk_id FROM engineering_entities").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
