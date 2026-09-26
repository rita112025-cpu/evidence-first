"""
SCADA ingestion - evidence first (v2.1)
依賴: pip install markitdown[pdf]      (套件名稱是 markitdown，不是 pymarkitdown)
用法:
    python scada_ingestion_template.py 附錄C.pdf RevC scada.db
"""
import datetime
import hashlib
import pathlib
import sqlite3
import sys
import uuid

SCHEMA_PATH = pathlib.Path(__file__).with_name("scada_evidence_schema.sql")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def new_evidence_record(file_path, revision="R0", project_code="SCADA-DEMO",
                        collector="markitdown", origin_url=None):
    p = pathlib.Path(file_path)
    if not p.is_file():
        raise FileNotFoundError(p)
    st = p.stat()
    return {
        "id": str(uuid.uuid4()),
        "source_path": str(p.resolve()),
        "file_name": p.name,
        "file_hash_sha256": sha256_file(p),
        "file_size_bytes": st.st_size,
        "file_type": p.suffix.upper().lstrip("."),
        "revision": str(revision or "").strip() or "R0",   # 只去除前後空白；大小寫不做轉換
        "timestamp_observed": datetime.datetime.now().isoformat(timespec="seconds"),
        "timestamp_modified": datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
        "origin_url": origin_url,
        "collector": collector,
        "project_code": project_code,
    }


APP_VERSION = "2.1"
SCHEMA_VERSION = 2   # DB schema 版本，與 APP_VERSION 是兩個概念


def open_db(db_path):
    db = sqlite3.connect(db_path)
    db.execute("PRAGMA foreign_keys = ON")   # 每個連線都要開
    version = db.execute("PRAGMA user_version").fetchone()[0]
    n_objects = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    # v1 資料庫的 user_version 也是 0，所以「版本 0 且已有物件」要一併拒絕
    if version != SCHEMA_VERSION and not (version == 0 and n_objects == 0):
        db.close()
        raise RuntimeError(
            f"Unsupported database (user_version={version}, objects={n_objects}). "
            f"Please create a new v{SCHEMA_VERSION} database."
        )
    db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return db


def insert_evidence(db, rec):
    """同 hash + revision 已存在則回傳既有 id，不重複寫入。"""
    row = db.execute(
        "SELECT id FROM evidence_files WHERE file_hash_sha256=? AND revision=?",
        (rec["file_hash_sha256"], rec["revision"]),
    ).fetchone()
    if row:
        return row[0], False
    cols = list(rec)
    db.execute(
        f"INSERT INTO evidence_files ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [rec[c] for c in cols],
    )
    return rec["id"], True


def split_pdf_pages(markdown_text: str):
    """MarkItDown 對 PDF 以 form feed (\\x0c) 分頁；頁碼 = 分割後的順序 (從 1 起)。
    此行為僅在 markitdown 0.1.5 + 文字型 PDF 測過；掃描 PDF、其他格式與其他版本未驗證。"""
    return [(i, t.strip()) for i, t in enumerate(markdown_text.split("\x0c"), start=1) if t.strip()]


def insert_pdf_chunks(db, evidence_id, markdown_text, method="markitdown"):
    n = 0
    for page_no, text in split_pdf_pages(markdown_text):
        db.execute(
            "INSERT INTO extracted_chunks (id, evidence_id, page_number, chunk_text_markdown, "
            "chunk_hash_sha256, extraction_method) VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), evidence_id, page_no, text, sha256_bytes(text.encode("utf-8")), method),
        )
        n += 1
    return n


def ingest_pdf(db, pdf_path, revision="R0", project_code="SCADA-DEMO"):
    from markitdown import MarkItDown          # 延後 import：只跑 hash 時不必安裝
    rec = new_evidence_record(pdf_path, revision, project_code)
    try:
        evidence_id, created = insert_evidence(db, rec)
        if not created:
            return evidence_id, 0              # 同 hash+revision 已匯入，略過
        text = MarkItDown().convert(str(pdf_path)).text_content
        n = insert_pdf_chunks(db, evidence_id, text)
        if n == 0:                             # 掃描型 / 抽不到文字：不留下 0 chunk 的 evidence，否則之後會被當成已匯入
            raise ValueError(f"No extractable text in {pdf_path}; nothing committed.")
        db.commit()                            # evidence 與 chunks 同一筆 transaction
        return evidence_id, n
    except Exception:
        db.rollback()                          # 失敗時不留下只有 evidence、沒有 chunks 的半成品
        raise


def format_citation(db, chunk_id):
    """由程式依 chunk_id 從資料庫組出引用字串，不經過 LLM。"""
    r = db.execute(
        "SELECT source_path, file_hash_sha256, revision, page_number, sheet_name, ifc_guid, cad_handle, "
        "chunk_hash_sha256 FROM v_chunk_citation WHERE chunk_id=?", (chunk_id,)
    ).fetchone()
    if r is None:
        raise KeyError(f"chunk_id not found: {chunk_id}")
    path, sha, rev, page, sheet, guid, handle, chunk_sha = r
    parts = [f"{path}", f"sha256={sha}", f"revision={rev}"]
    for label, val in (("page", page), ("sheet", sheet), ("ifc_guid", guid), ("cad_handle", handle),
                       ("chunk_id", chunk_id), ("chunk_sha256", chunk_sha)):
        if val is not None:
            parts.append(f"{label}={val}")
    return " | ".join(parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法: python scada_ingestion_template.py <file.pdf> [revision] [db_path]")
    pdf = sys.argv[1]
    rev = sys.argv[2] if len(sys.argv) > 2 else "R0"
    dbp = sys.argv[3] if len(sys.argv) > 3 else "scada.db"
    con = open_db(dbp)
    eid, n = ingest_pdf(con, pdf, rev)
    print(f"evidence_id={eid} chunks_inserted={n}")
