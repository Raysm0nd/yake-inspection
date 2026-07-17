-- ============================================================
-- Yake 玻璃瑕疵檢測系統 — PostgreSQL schema
--
-- 由 2026-07-03 實際運行中的 yake DB（information_schema + pg_indexes）
-- 反向落地。這是目前唯一的 DDL 真相來源，schema 改動請同步更新本檔。
--
-- 套用方式（需要 table owner 或 superuser，例如 postgres）：
--   psql -U postgres -d yake -f schema.sql
-- 應用程式帳號 yake_app 只需要 SELECT / INSERT / UPDATE / DELETE。
-- ============================================================

CREATE TABLE IF NOT EXISTS detection_records (
    id             BIGSERIAL PRIMARY KEY,

    -- 產線時間：inspect_ts 必須等於 to_timestamp(date || hour, 'YYYYMMDDHH24')。
    -- （pipeline 與 /api/save-detection 都由 date+hour 組出，不是處理當下時間）
    inspect_ts     TIMESTAMPTZ  NOT NULL,
    date           CHAR(8)      NOT NULL,             -- YYYYMMDD（來自產線路徑第 1 層）
    hour           CHAR(2)      NOT NULL,             -- 00..23（來自產線路徑第 2 層；非 24 小時連續）

    substrate_id   VARCHAR(64)  NOT NULL,             -- 基板 ID，e.g. C2QX45Y311011601
    image_layer    VARCHAR(32)  NOT NULL,             -- 目前恆為 CenterImage
    source_image   VARCHAR(255) NOT NULL,             -- 原圖檔名 CenterImage001_Shot002_021.jpg

    shot_number    INTEGER      NOT NULL,             -- 區間起點 + segment_index - 1（clip 到區間終點）
    segment_index  INTEGER      NOT NULL,             -- 1..split_size（預設 20）
    side           VARCHAR(4)   NOT NULL,             -- X1 / X2 / Y1 / Y2（產線路徑倒數第二層）
    shot_label     VARCHAR(32)  NOT NULL,             -- e.g. Shot002_021；長邊末段為 Shot182_185

    defect_status  VARCHAR(8)   NOT NULL,             -- NG / GRAY / OK
    defect_type    VARCHAR(32)  NOT NULL DEFAULT 'yake',
    ng_count       INTEGER      NOT NULL DEFAULT 0,   -- 此區塊內 NG bbox 數
    gray_count     INTEGER      NOT NULL DEFAULT 0,   -- 此區塊內 GRAY bbox 數

    bboxes         JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- [{x0,y0,w,h,area,status}, ...]
    detect_params  JSONB        NOT NULL DEFAULT '{}'::jsonb,  -- 產生此紀錄時的閥值快照

    segment_path   TEXT,                              -- 有存檔才寫；OK 通常為 NULL
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ── 查詢索引（與線上一致）─────────────────────────────
CREATE INDEX IF NOT EXISTS idx_dr_inspect_ts   ON detection_records (inspect_ts DESC);
CREATE INDEX IF NOT EXISTS idx_dr_date_hour    ON detection_records (date, hour);
CREATE INDEX IF NOT EXISTS idx_dr_substrate    ON detection_records (substrate_id);
CREATE INDEX IF NOT EXISTS idx_dr_source_image ON detection_records (source_image);
CREATE INDEX IF NOT EXISTS idx_dr_status       ON detection_records (defect_status)
    WHERE defect_status <> 'OK';
CREATE INDEX IF NOT EXISTS idx_dr_side_shot    ON detection_records (side, shot_label);
CREATE INDEX IF NOT EXISTS idx_dr_bboxes_gin   ON detection_records USING gin (bboxes);
CREATE INDEX IF NOT EXISTS idx_dr_params_gin   ON detection_records USING gin (detect_params);

-- ── segment_path 部分唯一索引（2026-07-03 已由 DBA 以 owner 帳號套用）──
-- db.py 的 insert_record / bulk_insert_records 依賴此索引做
-- ON CONFLICT (segment_path) WHERE segment_path IS NOT NULL DO UPDATE 覆寫。
CREATE UNIQUE INDEX IF NOT EXISTS ux_dr_segment_path
    ON detection_records (segment_path)
    WHERE segment_path IS NOT NULL;
