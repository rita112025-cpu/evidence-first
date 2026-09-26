-- SCADA evidence-first schema (v2.1)  — 檔尾設定 PRAGMA user_version = 2
-- 不支援把 v1 資料庫就地升級；請建立新的 v2 資料庫。
-- 原始檔 (evidence_files) 是唯一證據；extracted_chunks / engineering_entities 都是衍生資料。
-- 使用前每個連線都要執行: PRAGMA foreign_keys = ON;  (SQLite 預設關閉，不開則外鍵不生效)

-- 1. 原始檔
CREATE TABLE IF NOT EXISTS evidence_files (
    id                  TEXT PRIMARY KEY,
    source_path         TEXT NOT NULL,
    file_name           TEXT NOT NULL,
    file_hash_sha256    TEXT NOT NULL,
    file_size_bytes     INTEGER,
    file_type           TEXT,
    revision            TEXT NOT NULL DEFAULT 'R0',   -- NOT NULL：避免 UNIQUE 對 NULL 失效造成重複匯入
    timestamp_observed  TEXT DEFAULT CURRENT_TIMESTAMP,
    timestamp_modified  TEXT,
    origin_url          TEXT,
    collector           TEXT,
    project_code        TEXT,
    UNIQUE (file_hash_sha256, revision)
);

-- 2. 衍生文字片段，可回溯到 evidence_files
CREATE TABLE IF NOT EXISTS extracted_chunks (
    id                    TEXT PRIMARY KEY,
    evidence_id           TEXT NOT NULL REFERENCES evidence_files(id),
    page_number           INTEGER,
    sheet_name            TEXT,
    ifc_guid              TEXT,
    cad_handle            TEXT,
    tag                   TEXT,
    chunk_text_markdown   TEXT NOT NULL,
    chunk_hash_sha256     TEXT,
    extraction_method     TEXT,
    created_at            TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_chunks_evidence ON extracted_chunks(evidence_id, page_number);
CREATE INDEX IF NOT EXISTS ix_chunks_tag      ON extracted_chunks(tag);

-- 3. Analyzer 解析出的實體
--    boq_exists / cad_exists / ifc_exists：1=有、0=確認沒有、NULL=尚未比對
CREATE TABLE IF NOT EXISTS engineering_entities (
    id                TEXT PRIMARY KEY,
    evidence_id       TEXT NOT NULL REFERENCES evidence_files(id),
    chunk_id          TEXT REFERENCES extracted_chunks(id),
    entity_type       TEXT NOT NULL,        -- RTU / BOQ_ITEM / CAD_BLOCK / IFC_OBJECT / SPEC_CLAUSE
    entity_name       TEXT NOT NULL,
    tag               TEXT,                 -- Tag 編號；IFC_OBJECT 沒有 tag 時為 NULL 或空字串
    attributes_json   TEXT,
    boq_exists        INTEGER CHECK (boq_exists IN (0,1)),
    cad_exists        INTEGER CHECK (cad_exists IN (0,1)),
    ifc_exists        INTEGER CHECK (ifc_exists IN (0,1)),
    spec_ref          TEXT,
    validation_status TEXT CHECK (validation_status IN ('OK','MISSING','MISMATCH'))
);
CREATE INDEX IF NOT EXISTS ix_entities_name ON engineering_entities(entity_name);
CREATE INDEX IF NOT EXISTS ix_entities_tag  ON engineering_entities(tag);

-- 4a. 已確認缺漏（旗標明確為 0）
CREATE VIEW IF NOT EXISTS v_missing_check AS
SELECT e.entity_name, e.entity_type, e.tag,
       e.boq_exists, e.cad_exists, e.ifc_exists,
       f.source_path, f.file_hash_sha256, f.revision
FROM engineering_entities e
JOIN evidence_files f ON f.id = e.evidence_id
WHERE e.boq_exists = 0 OR e.cad_exists = 0 OR e.ifc_exists = 0;

-- 4b. 尚未比對（任一旗標為 NULL）— 不能當成「沒問題」
CREATE VIEW IF NOT EXISTS v_unchecked AS
SELECT e.entity_name, e.entity_type, e.tag,
       e.boq_exists, e.cad_exists, e.ifc_exists,
       f.source_path, f.file_hash_sha256, f.revision
FROM engineering_entities e
JOIN evidence_files f ON f.id = e.evidence_id
WHERE e.boq_exists IS NULL OR e.cad_exists IS NULL OR e.ifc_exists IS NULL;

-- 4c. IFC 物件沒有 Tag
CREATE VIEW IF NOT EXISTS v_ifc_no_tag AS
SELECT e.entity_name, e.entity_type,
       f.source_path, f.file_hash_sha256, f.revision
FROM engineering_entities e
JOIN evidence_files f ON f.id = e.evidence_id
WHERE e.entity_type = 'IFC_OBJECT' AND (e.tag IS NULL OR TRIM(e.tag) = '');

-- 4d. 給 RAG / 回答引用：每個片段都帶 source_path + hash + revision + page
CREATE VIEW IF NOT EXISTS v_chunk_citation AS
SELECT c.id AS chunk_id, c.tag, c.page_number, c.sheet_name, c.ifc_guid, c.cad_handle,
       c.chunk_text_markdown, c.chunk_hash_sha256,
       f.source_path, f.file_hash_sha256, f.revision
FROM extracted_chunks c
JOIN evidence_files f ON f.id = c.evidence_id;

PRAGMA user_version = 2;
