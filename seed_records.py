"""
灌一批固定種子的假資料到 detection_records，方便 UI 看列表視覺。

用法：
    python seed_records.py            # 灌 700 筆
    python seed_records.py --n 200    # 自訂數量
    python seed_records.py --wipe     # 灌之前先清空表
"""

import argparse
import random
from datetime import datetime, timedelta

from psycopg.types.json import Json

import db


SHORT_INTERVALS = [
    "Shot002_021", "Shot022_041", "Shot042_061",
    "Shot062_081", "Shot082_101", "Shot102_121",
]
# 長邊末段是 Shot182_185（實際產線邊界），須與 ui/dashboard.js 的 LONG_INTERVALS 同步
LONG_INTERVALS = SHORT_INTERVALS + [
    "Shot122_141", "Shot142_161", "Shot162_181", "Shot182_185",
]
SUBSTRATE_IDS = [
    "6T5CABM", "6T5CACM", "6T5CADM", "6T5CBBM", "6T5CBCM",
    "6T5CCBM", "6T5CDBM", "6T5DABM", "6T5DBBM", "6T5DCBM",
]

DEFAULT_PARAMS = {
    "threshold": 100, "len_ng": 10, "area_ng": 40,
    "len_gray_min": 5, "area_gray_min": 20, "merge_dist": 30,
}


def build_rows(n: int, seed: int = 42):
    rng = random.Random(seed)
    sides_short = ["Y1", "Y2"]
    sides_long = ["X1", "X2"]
    start = datetime(2024, 4, 13)
    days_span = 30

    for _ in range(n):
        side = rng.choice(sides_short + sides_long)
        is_short = side in sides_short
        intervals = SHORT_INTERVALS if is_short else LONG_INTERVALS
        shot_label = rng.choice(intervals)
        a, b = shot_label.replace("Shot", "").split("_")
        shot_number = rng.randint(int(a), int(b))

        d = start + timedelta(
            days=rng.randint(0, days_span - 1),
            hours=rng.randint(0, 23),
            minutes=rng.randint(0, 59),
        )
        ng_n = rng.choices([0, 1, 2, 3], weights=[60, 25, 10, 5])[0]
        gr_n = rng.choices([0, 1, 2], weights=[55, 35, 10])[0]
        status = "NG" if ng_n > 0 else ("GRAY" if gr_n > 0 else "OK")

        yield (
            d,                                          # inspect_ts
            d.strftime("%Y%m%d"),                       # date
            d.strftime("%H"),                           # hour
            rng.choice(SUBSTRATE_IDS),                  # substrate_id
            "CenterImage",                              # image_layer
            f"CenterImage_{shot_number:03d}.jpg",       # source_image
            shot_number,                                # shot_number
            rng.randint(1, 20),                         # segment_index
            side,                                       # side
            shot_label,                                 # shot_label
            status,                                     # defect_status
            "yake",                                     # defect_type
            ng_n,                                       # ng_count
            gr_n,                                       # gray_count
            Json([]),                                   # bboxes
            Json(DEFAULT_PARAMS),                       # detect_params
            None,                                       # segment_path
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=700)
    ap.add_argument("--wipe", action="store_true", help="灌之前先清空表")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    db.open_pool()
    try:
        with db.get_conn() as conn, conn.cursor() as cur:
            if args.wipe:
                cur.execute("DELETE FROM detection_records")
                print("[wipe] detection_records cleared (id sequence not reset)")

            sql = """
                INSERT INTO detection_records
                    (inspect_ts, date, hour, substrate_id, image_layer, source_image,
                     shot_number, segment_index, side, shot_label,
                     defect_status, defect_type, ng_count, gray_count,
                     bboxes, detect_params, segment_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """
            cur.executemany(sql, list(build_rows(args.n, args.seed)))

            cur.execute("SELECT COUNT(*) AS n FROM detection_records")
            total = cur.fetchone()["n"]
            print(f"[done] inserted {args.n} rows, table total = {total}")
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
