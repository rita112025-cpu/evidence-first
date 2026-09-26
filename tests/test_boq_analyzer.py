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

    def test_rerun_keeps_counts_stable(self):
        p = self.pdf([[HEAD, ["1", "RTU-01", "", "set", "2", ""]]])
        boq.analyze_boq(self.db, p, "RevC")
        res = boq.analyze_boq(self.db, p, "RevC")
        self.assertEqual(res["inserted"], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM engineering_entities").fetchone()[0], 1)

    def test_no_header_raises_and_leaves_nothing(self):
        p = self.pdf([[["a", "b"], ["c", "d"]]])
        with self.assertRaises(ValueError):
            boq.analyze_boq(self.db, p, "RevC")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM evidence_files").fetchone()[0], 0)

    def test_every_entity_has_chunk_and_citation(self):
        p = self.pdf([[HEAD, ["1", "RTU-01", "", "set", "2", ""], ["2", "Cable Tray TR-200", "", "m", "5", ""]]])
        boq.analyze_boq(self.db, p, "RevC")      # 沒先跑 ingest_pdf
        cids = [r[0] for r in self.db.execute("SELECT chunk_id FROM engineering_entities")]
        self.assertEqual(len(cids), 2)
        self.assertNotIn(None, cids)
        cit = ing.format_citation(self.db, cids[0])
        self.assertIn("revision=RevC", cit)
        self.assertIn("page=1", cit)
        self.assertIn("chunk_sha256=", cit)

    def test_two_tables_same_page_no_id_collision(self):
        p = self.pdf([[HEAD, ["1", "RTU-01", "", "set", "2", ""], ["2", "RTU-02", "", "set", "3", ""]]], "a.pdf")
        # 同一頁放兩個 BOQ table
        q = os.path.join(self.d, "two.pdf")
        doc = SimpleDocTemplate(q, pagesize=A4)
        from reportlab.platypus import Spacer
        doc.build([Table([HEAD, ["1", "RTU-01", "", "set", "2", ""], ["2", "Pump P-100", "", "pc", "1", ""]], style=STYLE),
                   Spacer(1, 40),
                   Table([HEAD, ["1", "Valve V-200", "", "pc", "4", ""], ["2", "Fan F-300", "", "pc", "6", ""]], style=STYLE)])
        res = boq.analyze_boq(self.db, q, "RevC")
        self.assertEqual((res["rows_found"], res["inserted"]), (4, 4))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM engineering_entities").fetchone()[0], 4)
        tables = {json.loads(r[0])["table"] for r in self.db.execute("SELECT attributes_json FROM engineering_entities")}
        self.assertEqual(tables, {1, 2})

    def test_non_boq_table_same_column_count_not_parsed(self):
        q = os.path.join(self.d, "mix.pdf")
        doc = SimpleDocTemplate(q, pagesize=A4)
        from reportlab.platypus import Spacer
        doc.build([Table([HEAD, ["1", "RTU-01", "", "set", "2", ""]], style=STYLE), Spacer(1, 40),
                   Table([["Rev", "Date", "By", "Chk", "App", "Note"], ["A", "2024-01-01", "X", "Y", "Z", "first"]], style=STYLE)])
        res = boq.analyze_boq(self.db, q, "RevC")
        self.assertEqual(res["rows_found"], 1)
        self.assertEqual(self.db.execute("SELECT entity_name FROM engineering_entities").fetchall(), [("RTU-01",)])
        self.assertTrue(any("表頭出現前" in s for s in res["skipped"]))

    def test_rerun_rebuilds_stale_results(self):
        p = self.pdf([[HEAD, ["1", "RTU-01", "", "set", "2", ""]]])
        boq.analyze_boq(self.db, p, "RevC")
        self.db.execute("UPDATE engineering_entities SET entity_name='WRONG', tag=NULL")   # 模擬舊版解析錯誤
        self.db.commit()
        boq.analyze_boq(self.db, p, "RevC")
        self.assertEqual(self.db.execute("SELECT entity_name, tag FROM engineering_entities").fetchall(),
                         [("RTU-01", "RTU-01")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM extracted_chunks").fetchone()[0], 1)  # 舊 chunk 已清


if __name__ == "__main__":
    unittest.main()
