"""
PostgreSQL 連線與查詢 helper。

依賴：
    pip install "psycopg[binary,pool]" python-dotenv

用法：
    from db import fetch_records, insert_record
"""

import os
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

load_dotenv()
logger = logging.getLogger(__name__)


def _dsn() -> str:
    return (
        f"host={os.getenv('PG_HOST', 'localhost')} "
        f"port={os.getenv('PG_PORT', '5432')} "
        f"dbname={os.getenv('PG_DB', 'yake')} "
        f"user={os.getenv('PG_USER', 'yake_app')} "
        f"password={os.getenv('PG_PASSWORD', '')}"
    )


# 注意：psycopg_pool 的 ConnectionPool close 之後不能重新 open（PoolClosed），
# 所以這裡採 lazy 建立：open_pool() 發現池子已關閉就換一顆新的。
pool: Optional[ConnectionPool] = None


def _make_pool() -> ConnectionPool:
    return ConnectionPool(
        conninfo=_dsn(),
        min_size=int(os.getenv("PG_POOL_MIN", "1")),
        max_size=int(os.getenv("PG_POOL_MAX", "10")),
        kwargs={"row_factory": dict_row},
        open=False,
    )


def open_pool() -> None:
    global pool
    if pool is None or pool.closed:
        pool = _make_pool()
        pool.open()
        logger.info("PG pool opened: %s@%s/%s",
                    os.getenv("PG_USER"), os.getenv("PG_HOST"), os.getenv("PG_DB"))


def close_pool() -> None:
    if pool is not None and not pool.closed:
        pool.close()
        logger.info("PG pool closed")


@contextmanager
def get_conn():
    if pool is None or pool.closed:
        open_pool()
    with pool.connection() as conn:
        yield conn


RECORD_COLUMNS = (
    "id", "inspect_ts", "date", "hour",
    "substrate_id", "image_layer", "source_image",
    "shot_number", "segment_index", "side", "shot_label",
    "defect_status", "defect_type", "ng_count", "gray_count",
    "bboxes", "detect_params", "segment_path",
)


def _row_to_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """把 DB row 轉成前端期待的 JSON(inspect_ts 用 ISO 字串)。"""
    return {
        "id": row["id"],
        "inspect_ts": row["inspect_ts"].isoformat() if row["inspect_ts"] else None,
        "date": row["date"],
        "hour": row["hour"],
        "substrate_id": row["substrate_id"],
        "image_layer": row["image_layer"],
        "source_image": row["source_image"],
        "shot_number": row["shot_number"],
        "segment_index": row["segment_index"],
        "side": row["side"],
        "shot_label": row["shot_label"],
        "defect_status": row["defect_status"],
        "defect_type": row["defect_type"],
        "ng_count": row["ng_count"],
        "gray_count": row["gray_count"],
        "bboxes": row["bboxes"],
        "detect_params": row["detect_params"],
        "segment_path": row["segment_path"],
    }


def fetch_records(limit: int = 700) -> List[Dict[str, Any]]:
    """撈最近 N 筆(依 inspect_ts DESC)。"""
    sql = f"""
        SELECT {", ".join(RECORD_COLUMNS)}
        FROM detection_records
        ORDER BY inspect_ts DESC
        LIMIT %s
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (limit,))
        rows = cur.fetchall()
    return [_row_to_record(r) for r in rows]


# 覆寫語意：靠部分唯一索引 ux_dr_segment_path（segment_path IS NOT NULL，
# 2026-07-03 由 DBA 套用）做 upsert — 同路徑重存會 UPDATE 既有列（id 不變）。
# segment_path 為 NULL 的列（如 OK 未落檔）不受唯一性約束，永遠是純 INSERT。
_UPSERT_CLAUSE = """
        ON CONFLICT (segment_path) WHERE segment_path IS NOT NULL
        DO UPDATE SET
            inspect_ts    = EXCLUDED.inspect_ts,
            date          = EXCLUDED.date,
            hour          = EXCLUDED.hour,
            substrate_id  = EXCLUDED.substrate_id,
            image_layer   = EXCLUDED.image_layer,
            source_image  = EXCLUDED.source_image,
            shot_number   = EXCLUDED.shot_number,
            segment_index = EXCLUDED.segment_index,
            side          = EXCLUDED.side,
            shot_label    = EXCLUDED.shot_label,
            defect_status = EXCLUDED.defect_status,
            defect_type   = EXCLUDED.defect_type,
            ng_count      = EXCLUDED.ng_count,
            gray_count    = EXCLUDED.gray_count,
            bboxes        = EXCLUDED.bboxes,
            detect_params = EXCLUDED.detect_params
"""


def insert_record(rec: Dict[str, Any]) -> int:
    """插一筆（segment_path 非空時為 upsert 覆寫），回傳 id。JSONB 欄位自動用 Json() 包。"""
    sql = f"""
        INSERT INTO detection_records
            (inspect_ts, date, hour, substrate_id, image_layer, source_image,
             shot_number, segment_index, side, shot_label,
             defect_status, defect_type, ng_count, gray_count,
             bboxes, detect_params, segment_path)
        VALUES
            (%(inspect_ts)s, %(date)s, %(hour)s, %(substrate_id)s, %(image_layer)s, %(source_image)s,
             %(shot_number)s, %(segment_index)s, %(side)s, %(shot_label)s,
             %(defect_status)s, %(defect_type)s, %(ng_count)s, %(gray_count)s,
             %(bboxes)s, %(detect_params)s, %(segment_path)s)
        {_UPSERT_CLAUSE}
        RETURNING id
    """
    params = dict(rec)
    params["bboxes"] = Json(rec.get("bboxes") or [])
    params["detect_params"] = Json(rec.get("detect_params") or {})
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        new_id = cur.fetchone()["id"]
    return new_id


def count_records() -> int:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM detection_records")
        return cur.fetchone()["n"]


def distinct_hours(date_from: Optional[str] = None, date_to: Optional[str] = None) -> List[str]:
    """實際存在資料的 hour（0..23 補零字串），依字典序 ASC。"""
    where = []
    params: List[Any] = []
    if date_from:
        where.append("date >= %s")
        params.append(date_from)
    if date_to:
        where.append("date <= %s")
        params.append(date_to)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT DISTINCT hour FROM detection_records {where_sql} ORDER BY hour"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [r["hour"] for r in rows if r["hour"] is not None]


_INSERT_COLS = (
    "inspect_ts", "date", "hour", "substrate_id", "image_layer", "source_image",
    "shot_number", "segment_index", "side", "shot_label",
    "defect_status", "defect_type", "ng_count", "gray_count",
    "bboxes", "detect_params", "segment_path",
)


def bulk_insert_records(records: Iterable[Dict[str, Any]]) -> int:
    """
    批次插入 records（executemany）。JSONB 欄位自動用 Json() 包。回傳實際插入筆數。

    - 覆寫語意：segment_path 非空的列走 ON CONFLICT DO UPDATE（upsert），
      pipeline 重跑同一批圖時不會累積重複記錄。
    - 容錯：整批失敗（例如某筆違反 NOT NULL）時降級為逐筆插入，
      壞的跳過並記 log，不會讓整個 chunk（最多 chunk_size×split_size 筆）一起丟失。
    """
    rows: List[Tuple] = []
    for rec in records:
        rows.append((
            rec.get("inspect_ts"),
            rec.get("date"),
            rec.get("hour"),
            rec.get("substrate_id"),
            rec.get("image_layer"),
            rec.get("source_image"),
            rec.get("shot_number"),
            rec.get("segment_index"),
            rec.get("side"),
            rec.get("shot_label"),
            rec.get("defect_status"),
            rec.get("defect_type", "yake"),
            rec.get("ng_count", 0),
            rec.get("gray_count", 0),
            Json(rec.get("bboxes") or []),
            Json(rec.get("detect_params") or {}),
            rec.get("segment_path"),
        ))
    if not rows:
        return 0
    placeholders = "(" + ",".join(["%s"] * len(_INSERT_COLS)) + ")"
    sql = (f"INSERT INTO detection_records ({', '.join(_INSERT_COLS)}) "
           f"VALUES {placeholders} {_UPSERT_CLAUSE}")
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.executemany(sql, rows)
        return len(rows)
    except Exception as e:
        logger.warning("bulk insert 整批失敗（%s），降級為逐筆插入", e)
        inserted = 0
        for row in rows:
            try:
                with get_conn() as conn, conn.cursor() as cur:
                    cur.execute(sql, row)
                inserted += 1
            except Exception as e2:
                logger.error("跳過無法插入的 record（source_image=%s, seg=%s）：%s",
                             row[5], row[7], e2)
        return inserted
