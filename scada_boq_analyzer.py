"""
SCADA Analyzer - BOQ (PDF 表格) -> engineering_entities   (v0.2)
依賴: pip install pdfplumber
用法:
    python scada_boq_analyzer.py BOQ.pdf RevC scada.db

設計原則 (evidence-first):
  * 每個 BOQ 列 = 一筆 entity_type='BOQ_ITEM'，boq_exists=1；cad_exists / ifc_exists 留 NULL
    (=尚未比對，會出現在 v_unchecked，不會被當成「沒問題」)。
  * 每個 BOQ 列自己建一筆 extracted_chunks (extraction_method='boq_pdfplumber')，entity.chunk_id 必填，
    所以可直接 format_citation(chunk_id)。位置 = page + table + row，寫進 attributes_json。
  * 無法解析的欄位不丟棄、不猜測：保留原始文字，並在 attributes_json.parse_warnings 記錄。
  * 全部寫入在同一筆 transaction。衍生資料可重建：重跑會先刪掉該 evidence 的 BOQ_ITEM 與 BOQ chunk 再重新產生，
    因此 Analyzer 升版後重跑會更新舊結果。注意：這也會清掉之後比對寫入的 cad_exists / ifc_exists，
    所以比對步驟必須在 BOQ 解析之後執行。
  * 表頭只在「同一個 table」內有效；唯一允許的跨頁沿用：下一頁的第一個 table、且欄數相同 (視為續表)。
"""
import json
import re
import sys
import uuid

import scada_ingestion_template as ing

# 表頭別名 (小寫、去空白後比對)。欄位對應不到就是 None，不硬猜。
HEADER_ALIASES = {
    "item_no":     ["項次", "項目編號", "編號", "item", "itemno", "item no", "no", "no.", "s/n", "序號"],
    "description": ["說明", "品名", "項目", "工作項目", "名稱", "description", "item description", "設備名稱"],
    "spec":        ["規格", "型式", "規範", "specification", "spec", "model"],
    "unit":        ["單位", "unit", "uom"],
    "qty":         ["數量", "qty", "qty.", "quantity"],
    "remarks":     ["備註", "remarks", "remark", "note", "notes"],
}
# 例: RTU-01 / TR-200 / PLC_3A / CT-12-A。刻意保守：兩到六個大寫字母 + 分隔 + 數字開頭。
TAG_RE = re.compile(r"\b[A-Z]{2,6}[-_]\d[\w-]*\b")
NUM_RE = re.compile(r"^-?\d[\d,]*(\.\d+)?$")


def _norm(s):
    return re.sub(r"\s+", "", (s or "").lower())


def _clean(cell):
    return re.sub(r"\s+", " ", cell).strip() if cell else ""


def map_header(row):
    """回傳 {欄位名: 欄 index}；至少要有 description 與 qty 才算表頭，否則回 None。"""
    m = {}
    for idx, cell in enumerate(row):
        n = _norm(cell)
        if not n:
            continue
        for field, aliases in HEADER_ALIASES.items():
            if field not in m and n in [_norm(a) for a in aliases]:
                m[field] = idx
                break
    return m if "description" in m and "qty" in m else None


def parse_qty(text):
    t = (text or "").strip()
    if NUM_RE.match(t):
        return float(t.replace(",", ""))
    return None


def extract_boq_rows(pdf_path):
    """回傳 (rows, skipped)。rows: 每列 dict；skipped: 被略過列的說明 (不會靜默丟掉)。"""
    import pdfplumber
    rows, skipped = [], []
    carry = None                                 # 上一頁最後一個 table 的表頭，只給下一頁第一個 table 續用
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_no, page in enumerate(pdf.pages, start=1):
            header = None
            for table_no, table in enumerate(page.extract_tables(), start=1):
                header = carry if table_no == 1 else None
                for r_idx, raw in enumerate(table, start=1):
                    cells = [_clean(c) for c in raw]
                    if not any(cells):
                        continue
                    hm = map_header(cells)
                    if hm:                       # 新表頭 (跨頁重複表頭也會走這裡)
                        header = (hm, len(cells))
                        continue
                    if header is None:
                        skipped.append(f"p{page_no} t{table_no} r{r_idx}: 表頭出現前的列，已略過: {cells}")
                        continue
                    hm, ncols = header
                    if len(cells) != ncols:      # 欄數不同：不敢套用舊表頭
                        skipped.append(f"p{page_no} t{table_no} r{r_idx}: 欄數 {len(cells)} != 表頭 {ncols}，已略過: {cells}")
                        continue
                    get = lambda f: cells[hm[f]] if f in hm else ""
                    desc = get("description")
                    if not desc:
                        skipped.append(f"p{page_no} t{table_no} r{r_idx}: 無說明欄，已略過: {cells}")
                        continue
                    qty_text = get("qty")
                    qty = parse_qty(qty_text)
                    warnings = []
                    if qty is None:
                        warnings.append(f"qty 無法解析為數字: {qty_text!r}")
                    tags = TAG_RE.findall(" ".join([desc, get("spec"), get("remarks")]))
                    rows.append({
                        "page": page_no, "table": table_no, "row": r_idx,
                        "item_no": get("item_no"), "description": desc, "spec": get("spec"),
                        "unit": get("unit"), "qty": qty, "qty_text": qty_text,
                        "remarks": get("remarks"), "tags": tags, "parse_warnings": warnings,
                    })
            carry = header                       # 本頁最後一個 table 沒有 BOQ 表頭時為 None，不會誤傳到下一頁
    return rows, skipped


BOQ_CHUNK_METHOD = "boq_pdfplumber"


def row_markdown(r):
    """該列的可讀文字 (即 chunk 內容)，只含 PDF 上讀到的值。"""
    parts = [("Item", r["item_no"]), ("Description", r["description"]), ("Spec", r["spec"]),
             ("Unit", r["unit"]), ("Qty", r["qty_text"]), ("Remarks", r["remarks"])]
    return "\n".join(f"{k}: {v}" for k, v in parts if v)


def clear_boq_derived(db, evidence_id):
    """刪除該 evidence 先前由本 Analyzer 產生的資料 (先 entity 後 chunk，因外鍵)。"""
    db.execute("DELETE FROM engineering_entities WHERE evidence_id=? AND entity_type='BOQ_ITEM'", (evidence_id,))
    db.execute("DELETE FROM extracted_chunks WHERE evidence_id=? AND extraction_method=?",
               (evidence_id, BOQ_CHUNK_METHOD))


def store_boq_entities(db, evidence_id, rows):
    """重建該 evidence 的 BOQ chunk 與 BOQ_ITEM，回傳筆數。呼叫端負責 commit / rollback。"""
    clear_boq_derived(db, evidence_id)
    for r in rows:
        key = f"{evidence_id}:{r['page']}:{r['table']}:{r['row']}"
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "boq-chunk:" + key))
        eid = str(uuid.uuid5(uuid.NAMESPACE_URL, "boq:" + key))
        text = row_markdown(r)
        db.execute(
            "INSERT INTO extracted_chunks (id, evidence_id, page_number, tag, chunk_text_markdown, "
            "chunk_hash_sha256, extraction_method) VALUES (?,?,?,?,?,?,?)",
            (chunk_id, evidence_id, r["page"], r["tags"][0] if r["tags"] else None, text,
             ing.sha256_bytes(text.encode("utf-8")), BOQ_CHUNK_METHOD))
        attrs = {k: r[k] for k in ("page", "table", "row", "item_no", "spec", "unit", "qty", "qty_text",
                                   "remarks", "tags", "parse_warnings")}
        db.execute(
            "INSERT INTO engineering_entities "
            "(id, evidence_id, chunk_id, entity_type, entity_name, tag, attributes_json, boq_exists) "
            "VALUES (?,?,?,?,?,?,?,1)",
            (eid, evidence_id, chunk_id, "BOQ_ITEM", r["description"],
             r["tags"][0] if r["tags"] else None, json.dumps(attrs, ensure_ascii=False)))
    return len(rows)


def analyze_boq(db, pdf_path, revision="R0", project_code="SCADA-DEMO"):
    """回傳 dict: evidence_id / rows_found / inserted / skipped / warnings。0 列時拋 ValueError。"""
    rec = ing.new_evidence_record(pdf_path, revision, project_code, collector="boq_analyzer")
    try:
        evidence_id, _ = ing.insert_evidence(db, rec)
        rows, skipped = extract_boq_rows(pdf_path)
        if not rows:
            raise ValueError(f"No BOQ rows found in {pdf_path} (no recognizable header with description+qty?)")
        inserted = store_boq_entities(db, evidence_id, rows)   # 重建：先清舊的再寫
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"evidence_id": evidence_id, "rows_found": len(rows), "inserted": inserted,
            "skipped": skipped, "warnings": [f"p{r['page']} t{r['table']} r{r['row']}: {w}" for r in rows for w in r["parse_warnings"]]}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python scada_boq_analyzer.py <boq.pdf> [revision] [db_path]")
    con = ing.open_db(sys.argv[3] if len(sys.argv) > 3 else "scada.db")
    res = analyze_boq(con, sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "R0")
    print(f"evidence_id={res['evidence_id']} rows_found={res['rows_found']} inserted={res['inserted']}")
    for line in res["skipped"] + res["warnings"]:
        print("  !", line)
