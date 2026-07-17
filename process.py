"""
整合圖片切割與白斑瑕疵檢測

流程：
1. 依 split_config.json 的設定掃描指定日期範圍內的圖片
2. 每張圖片切成 split_size 個區塊（沿用 split.py 的邏輯）
3. 對每個區塊執行 test.py 的白斑檢測（NG / GRAY / OK）
4. 把區塊依檢測結果輸出到：
     - NG   → split_output_folder/ng/<日期>/
     - GRAY → split_output_folder/gray/<日期>/
     - OK   → split_output_folder/ok/<日期>/

用法：
    python process.py                  # 全部圖片
    python process.py --limit 50       # 每個日期範圍只處理前 50 張
    python process.py --limit all      # 全部圖片（同預設）
    python process.py --refresh-cache  # 清除舊快取重新掃描

config 額外可選欄位（沿用 split_config.json）：
    "defect_status_to_save":  ["NG", "GRAY", "OK"]  # 要輸出的等級
    "white_threshold":        100                  # 亮度白斑門檻
    "len_gray_min":           5
    "len_ng":                 10
    "area_gray_min":          20
    "area_ng":                40
    "merge_dist":             30
"""

import os
import gc
import sys
import math
import time
import logging
import argparse
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support

import cv2
import numpy as np
from scipy import ndimage

from split import (
    SPLIT_CONFIG_FILE, SPLIT_LOG_FILE, SPLIT_CACHE_FILE, DEFAULT_SPLIT_SIZE,
    setup_logging, load_config, load_split_log, save_split_log,
    load_file_cache, save_file_cache,
    extract_path_meta, read_image, save_image,
    scan_images_by_date,
)


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────
# 白斑檢測（來自 test.py，移除 GUI / matplotlib）
# ──────────────────────────────────────────

DEFAULT_THRESHOLD     = 100
DEFAULT_LEN_GRAY_MIN  = 5
DEFAULT_LEN_NG        = 10
DEFAULT_AREA_GRAY_MIN = 20
DEFAULT_AREA_NG       = 40
DEFAULT_MERGE_DIST    = 30


def _make_uf(n: int) -> List[int]:
    return list(range(n))


def _find(uf: List[int], x: int) -> int:
    while uf[x] != x:
        uf[x] = uf[uf[x]]
        x = uf[x]
    return x


def _union(uf: List[int], x: int, y: int) -> None:
    uf[_find(uf, x)] = _find(uf, y)


def _box_dist(b1: Tuple[int, int, int, int], b2: Tuple[int, int, int, int]) -> float:
    dx = max(0, max(b1[0], b2[0]) - min(b1[2], b2[2]))
    dy = max(0, max(b1[1], b2[1]) - min(b1[3], b2[3]))
    return math.sqrt(dx * dx + dy * dy)


def _merge_boxes(raw_boxes: List[Tuple[int, int, int, int]],
                 max_dist: float) -> List[Tuple[int, int, int, int]]:
    n = len(raw_boxes)
    if n == 0:
        return []
    uf = _make_uf(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _box_dist(raw_boxes[i], raw_boxes[j]) <= max_dist:
                _union(uf, i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(_find(uf, i), []).append(i)

    merged = []
    for idxs in groups.values():
        x0 = min(raw_boxes[k][0] for k in idxs)
        y0 = min(raw_boxes[k][1] for k in idxs)
        x1 = max(raw_boxes[k][2] for k in idxs)
        y1 = max(raw_boxes[k][3] for k in idxs)
        merged.append((x0, y0, x1, y1))
    return merged


def _classify_spot(w: int, h: int, area: int, params: Dict) -> str:
    len_ng        = params["len_ng"]
    len_gray_min  = params["len_gray_min"]
    area_ng       = params["area_ng"]
    area_gray_min = params["area_gray_min"]

    if w >= len_ng or h >= len_ng or area >= area_ng:
        return "NG"
    if (len_gray_min < w < len_ng) or \
       (len_gray_min < h < len_ng) or \
       (area_gray_min <= area < area_ng):
        return "GRAY"
    return "OK"


def detect_defect_full(img_bgr: np.ndarray, params: Dict, return_mask: bool = False) -> Dict:
    """
    對單一影像做白斑檢測，回傳完整結果：
        {status, ng_count, gray_count, bboxes: [{x0,y0,w,h,area,status}, ...]}
    return_mask=True 時額外附上 {mask, img_w, img_h}（server.py /api/detect 用）。
    亮度用三通道平均，BGR / RGB 順序不影響結果。
    """
    gray = np.mean(img_bgr, axis=2)
    mask = gray > params["threshold"]
    labeled, _ = ndimage.label(mask)
    slices = ndimage.find_objects(labeled)

    raw_boxes: List[Tuple[int, int, int, int]] = []
    for sl in slices:
        if sl is None:
            continue
        y0, y1 = sl[0].start, sl[0].stop
        x0, x1 = sl[1].start, sl[1].stop
        raw_boxes.append((x0, y0, x1, y1))

    merged = _merge_boxes(raw_boxes, max_dist=params["merge_dist"])

    bboxes: List[Dict] = []
    ng_count = 0
    gray_count = 0
    for (x0, y0, x1, y1) in merged:
        w = x1 - x0
        h = y1 - y0
        area = int(np.sum(mask[y0:y1, x0:x1]))
        status = _classify_spot(w, h, area, params)
        if status == "NG":
            ng_count += 1
        elif status == "GRAY":
            gray_count += 1
        bboxes.append({
            "x0": int(x0), "y0": int(y0),
            "w": int(w), "h": int(h),
            "area": area, "status": status,
        })

    overall = "NG" if ng_count > 0 else ("GRAY" if gray_count > 0 else "OK")
    result = {
        "status": overall,
        "ng_count": ng_count,
        "gray_count": gray_count,
        "bboxes": bboxes,
    }
    if return_mask:
        result["mask"] = mask
        result["img_w"] = img_bgr.shape[1]
        result["img_h"] = img_bgr.shape[0]
    return result


# ──────────────────────────────────────────
# 切割 + 檢測（單張圖片，供子進程呼叫）
# ──────────────────────────────────────────

def _build_detect_params(cfg: Dict) -> Dict:
    return {
        "threshold":     cfg.get("white_threshold", DEFAULT_THRESHOLD),
        "len_gray_min":  cfg.get("len_gray_min",    DEFAULT_LEN_GRAY_MIN),
        "len_ng":        cfg.get("len_ng",          DEFAULT_LEN_NG),
        "area_gray_min": cfg.get("area_gray_min",   DEFAULT_AREA_GRAY_MIN),
        "area_ng":       cfg.get("area_ng",         DEFAULT_AREA_NG),
        "merge_dist":    cfg.get("merge_dist",      DEFAULT_MERGE_DIST),
    }


import re as _re

_SHOT_LABEL_RE = _re.compile(r"(Shot(\d+)_(\d+))")
_VALID_SIDES = {"X1", "X2", "Y1", "Y2"}


def _parse_shot_label(base_name: str) -> Optional[str]:
    """從檔名內擷取 shot_label，例：CenterImage001_Shot002_021 → 'Shot002_021'"""
    m = _SHOT_LABEL_RE.search(base_name)
    return m.group(1) if m else None


def _shot_number_for_segment(shot_label: Optional[str], segment_index: int) -> Optional[int]:
    """
    根據 shot_label 的區間起點 + segment_index 推 shot_number。
    e.g. Shot002_021 + segment_index=5 → shot_number=6
    區間比 split_size 短時（如長邊末段 Shot182_185 不足 20 個 shot 卻切 20 塊）
    超過的段落會 clip 到 interval_end。
    """
    if not shot_label:
        return None
    m = _SHOT_LABEL_RE.match(shot_label)
    if not m:
        return None
    start, end = int(m.group(2)), int(m.group(3))
    return min(end, start + max(0, segment_index - 1))


def _derive_side_from_path(image_path: str) -> Optional[str]:
    """
    真實路徑結構為 .../<date>/<hour>/<board_id>/<side>/<file>，倒數第二層即 side。
    找不到符合 X1/X2/Y1/Y2 就回 None。
    """
    parts = image_path.replace("/", "\\").split("\\")
    if len(parts) >= 2 and parts[-2] in _VALID_SIDES:
        return parts[-2]
    return None


def parse_segment_filename(path: str) -> Optional[Dict]:
    """
    把 split_and_detect_single 產出的 segment 檔名解析成所有 DB 欄位。

    命名格式：{date:8}_{hour:2}_{board_id}_{side}_{base_name...}_{seg_idx:02d}{ext}
    範例：20250401_00_C2QX45Y311011601_Y2_CenterImage001_Shot002_021_10.jpg

    - side 直接來自檔名第 4 段（X1/X2/Y1/Y2）；不再猜。
    - shot_number = shot_label 起點 + segment_index - 1（clip 到區間末端）。
    - source_image = 原圖檔名（base_name + segment 的副檔名）。
    - image_layer 從 base_name 開頭字母段擷取，預設 CenterImage。

    無法解析回 None（呼叫端決定要 skip 還是回 400）。
    """
    base_and_ext = os.path.basename(path)
    base, ext = os.path.splitext(base_and_ext)
    parts = base.split("_")
    if len(parts) < 6:
        return None

    date, hour, board_id, side = parts[0], parts[1], parts[2], parts[3]
    if side not in _VALID_SIDES:
        return None

    try:
        segment_index = int(parts[-1])
    except ValueError:
        return None

    base_name = "_".join(parts[4:-1])
    shot_label = _parse_shot_label(base_name)
    shot_number = _shot_number_for_segment(shot_label, segment_index)

    m = _re.match(r"^([A-Za-z]+)", base_name)
    image_layer = m.group(1) if m else "CenterImage"

    return {
        "date":          date,
        "hour":          hour,
        "substrate_id":  board_id,
        "side":          side,
        "image_layer":   image_layer,
        "source_image":  base_name + ext,
        "shot_number":   shot_number,
        "shot_label":    shot_label,
        "segment_index": segment_index,
    }


def split_image_to_blocks(
    image_path: str,
    out_dir: str,
    split_size: int = DEFAULT_SPLIT_SIZE,
) -> List[str]:
    """
    純切割：把單張（通常是未切割的產線原圖）沿長邊切成 split_size 塊，
    **全部塊都寫到 out_dir**（不偵測、不分類、不寫 DB），回傳輸出的塊路徑清單。

    給 Visual 分頁的「載入原圖 → 自動切塊預覽」用；切割規則與 split_and_detect_single
    完全一致（part_width = width // split_size，末塊吃餘數），確保預覽塊跟 pipeline 一模一樣。
    命名沿用 segment 格式，meta 抓不到的欄位以 NA 補（預覽用途，不進 DB）。
    """
    img_bgr = read_image(image_path)
    if img_bgr is None:
        return []

    width = img_bgr.shape[1]
    part_width = width // split_size
    if part_width == 0:
        return []

    meta = extract_path_meta(image_path)
    date  = meta.get("date")     or "NA"
    hour  = meta.get("hour")     or "NA"
    board = meta.get("board_id") or "NA"
    side  = _derive_side_from_path(image_path) or meta.get("position") or "NA"
    base_name, ext = os.path.splitext(os.path.basename(image_path))

    os.makedirs(out_dir, exist_ok=True)
    out_paths: List[str] = []
    for i in range(split_size):
        left  = i * part_width
        right = (i + 1) * part_width if i < split_size - 1 else width
        crop  = img_bgr[:, left:right]
        fn = f"{date}_{hour}_{board}_{side}_{base_name}_{i + 1:02d}{ext}"
        p = os.path.join(out_dir, fn)
        if save_image(p, crop):
            out_paths.append(p)
    return out_paths


def split_and_detect_single(args: Tuple) -> Dict:
    """
    對單張圖片做：切割 → 對每塊做白斑檢測 → 只把瑕疵塊寫到 output_folder
    子進程執行；DB 寫入由父進程集中做，這裡只回傳 records list。
    """
    image_path, output_folder, cfg = args

    result = {
        "filename": os.path.basename(image_path),
        "status": "OK",
        "segments_count": 0,
        "ng_segments": 0,
        "gray_segments": 0,
        "saved_kake": 0,
        "records": [],
        "records_skipped": 0,   # 因必填欄位缺漏而不寫 DB 的塊數
        "error": None,
    }

    img_bgr = read_image(image_path)
    if img_bgr is None:
        result["status"] = "FAILED"
        result["error"] = "無法讀取圖片"
        return result

    meta = extract_path_meta(image_path)
    board_id = meta.get("board_id", "UNKNOWN")
    date     = meta.get("date",     "UNKNOWN")
    hour     = meta.get("hour",     "UNKNOWN")
    position = meta.get("position", "UNKNOWN")
    base_name, ext = os.path.splitext(os.path.basename(image_path))

    width = img_bgr.shape[1]
    split_size = cfg.get("split_size", DEFAULT_SPLIT_SIZE)
    part_width = width // split_size

    if part_width == 0:
        result["status"] = "FAILED"
        result["error"] = f"圖片寬度過小 ({width} px)"
        return result

    detect_params = _build_detect_params(cfg)
    save_targets  = set(cfg.get("defect_status_to_save", ["NG", "GRAY", "OK"]))
    status_subdir = {"NG": "ng", "GRAY": "gray", "OK": "ok"}

    # inspect_ts = 產線時間（date+hour），不是 pipeline 執行當下時間；
    # 路徑 meta 不完整時退回 now() 並由下方 db_writable 判斷擋掉 DB 寫入。
    try:
        inspect_ts = datetime.strptime(f"{date}{hour}", "%Y%m%d%H").isoformat()
    except ValueError:
        inspect_ts = datetime.now().isoformat()

    source_image = os.path.basename(image_path)
    shot_label = _parse_shot_label(base_name)
    # 真實 pipeline：position 資料夾就是 side（X1/X2/Y1/Y2）
    side = _derive_side_from_path(image_path)
    image_layer = "CenterImage"

    # detection_records 幾乎全欄 NOT NULL：任一必填 meta 缺漏就整張圖不寫 DB
    # （檔案照樣輸出），避免一筆壞資料把整個 chunk 的 bulk insert 拖下水。
    db_writable = all([
        date != "UNKNOWN", hour != "UNKNOWN", board_id != "UNKNOWN",
        side is not None, shot_label is not None,
    ])

    for i in range(split_size):
        left  = i * part_width
        right = (i + 1) * part_width if i < split_size - 1 else width
        crop  = img_bgr[:, left:right]
        result["segments_count"] += 1

        det = detect_defect_full(crop, detect_params)
        status = det["status"]
        if status == "NG":
            result["ng_segments"] += 1
        elif status == "GRAY":
            result["gray_segments"] += 1

        seg_disk_path: Optional[str] = None
        if status in save_targets and status in status_subdir:
            target_dir = os.path.join(output_folder, status_subdir[status], date)
            os.makedirs(target_dir, exist_ok=True)
            segment_filename = f"{date}_{hour}_{board_id}_{position}_{base_name}_{i+1:02d}{ext}"
            seg_disk_path = os.path.join(target_dir, segment_filename)
            if save_image(seg_disk_path, crop):
                result["saved_kake"] += 1
            else:
                seg_disk_path = None

        segment_index = i + 1
        shot_number = _shot_number_for_segment(shot_label, segment_index)

        if not db_writable:
            result["records_skipped"] += 1
            continue

        result["records"].append({
            "inspect_ts":    inspect_ts,
            "date":          date,
            "hour":          hour,
            "substrate_id":  board_id,
            "image_layer":   image_layer,
            "source_image":  source_image,
            "shot_number":   shot_number,
            "segment_index": segment_index,
            "side":          side,
            "shot_label":    shot_label,
            "defect_status": status,
            "defect_type":   "yake",
            "ng_count":      det["ng_count"],
            "gray_count":    det["gray_count"],
            "bboxes":        det["bboxes"],
            "detect_params": detect_params,
            "segment_path":  seg_disk_path,
        })

    del img_bgr
    gc.collect()
    return result


# ──────────────────────────────────────────
# 管道主流程
# ──────────────────────────────────────────

def run_pipeline(input_folder: str, output_folder: str,
                 date_start: str, date_end: str, cfg: Dict, log_data: Dict,
                 cache_data: Optional[Dict] = None,
                 limit: Optional[int] = None) -> Tuple[Dict, Dict]:
    """
    掃描 → 切割 → 檢測 → 輸出瑕疵塊
    limit: None 表示不限張數；正整數表示僅處理該日期範圍前 N 張
    """
    os.makedirs(output_folder, exist_ok=True)

    logger.info("🔍 掃描圖片中...")
    enable_cache = cfg.get("enable_file_cache", True)
    image_paths, cache_data = scan_images_by_date(
        input_folder, date_start, date_end, cfg, cache_data, enable_cache
    )

    if not image_paths:
        logger.error("❌ 找不到任何符合條件的圖片！")
        return {
            "total": 0, "success": 0, "failed": 0,
            "segments": 0, "ng_segments": 0, "gray_segments": 0, "saved_kake": 0
        }, cache_data

    if limit is not None and limit > 0 and len(image_paths) > limit:
        original = len(image_paths)
        image_paths = image_paths[:limit]
        logger.info(f"🔢 套用張數限制：{original} → {limit}")

    logger.info(f"📂 輸入資料夾          : {input_folder}")
    logger.info(f"🚨 瑕疵輸出資料夾      : {output_folder}")
    logger.info(f"📅 日期範圍            : {date_start} ~ {date_end}")
    logger.info(f"🎯 輸出瑕疵等級        : {cfg.get('defect_status_to_save', ['NG', 'GRAY'])}")
    logger.info(f"🖼  待處理圖片數        : {len(image_paths)}")
    logger.info("=" * 65)

    chunk_size  = cfg.get("chunk_size",  200)
    max_workers = cfg.get("max_workers", 4)
    args_list = [(p, output_folder, cfg) for p in image_paths]

    enable_db_insert = cfg.get("enable_db_insert", True)
    db_module = None
    if enable_db_insert:
        try:
            import db as db_module
            db_module.open_pool()
        except Exception as e:
            logger.warning(f"⚠️  DB pool 開啟失敗，將跳過寫入：{e}")
            db_module = None

    total = len(image_paths)
    done = 0
    success_count = 0
    failed_count = 0
    total_segments = 0
    total_ng = 0
    total_gray = 0
    total_saved = 0
    total_skipped = 0
    total_db_inserted = 0

    # executor 建一次重複用（Windows spawn 子進程成本高）；
    # chunk 只是 DB 批次寫入的分段，不需要每段重建進程池。
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        for chunk_start in range(0, total, chunk_size):
            chunk = args_list[chunk_start:chunk_start + chunk_size]
            chunk_records: List[Dict] = []

            futures = {executor.submit(split_and_detect_single, a): a[0] for a in chunk}

            for future in as_completed(futures):
                done += 1
                try:
                    result = future.result()
                except Exception as e:
                    logger.error(f"  [{done:4d}/{total}] ❌ 子進程例外: {e}")
                    failed_count += 1
                    continue

                if result["status"] == "OK":
                    success_count  += 1
                    total_segments += result["segments_count"]
                    total_ng       += result["ng_segments"]
                    total_gray     += result["gray_segments"]
                    total_saved    += result["saved_kake"]
                    skipped = result.get("records_skipped", 0)
                    total_skipped  += skipped
                    chunk_records.extend(result.get("records", []))
                    logger.info(
                        f"  [{done:4d}/{total}] ✅ 切{result['segments_count']:2d}塊 "
                        f"NG={result['ng_segments']:2d} GRAY={result['gray_segments']:2d} "
                        f"→輸出={result['saved_kake']:2d} | {result['filename']}"
                        + (f" ⚠️ meta 不完整，{skipped} 塊不寫 DB" if skipped else "")
                    )
                else:
                    failed_count += 1
                    logger.warning(
                        f"  [{done:4d}/{total}] ❌ {result['error']} | {result['filename']}"
                    )

            if db_module and chunk_records:
                try:
                    inserted = db_module.bulk_insert_records(chunk_records)
                    total_db_inserted += inserted
                    logger.info(f"  💾 DB 寫入 chunk：{inserted} 筆（累計 {total_db_inserted}）")
                except Exception as e:
                    logger.error(f"  ❌ DB bulk insert 失敗：{e}")

    if db_module:
        try:
            db_module.close_pool()
        except Exception:
            pass

    log_data["processed_dates"].append({
        "date_start":    date_start,
        "date_end":      date_end,
        "timestamp":     datetime.now().isoformat(),
        "images_split":  success_count,
        "segments":      total_segments,
        "ng_segments":   total_ng,
        "gray_segments": total_gray,
        "saved_kake":    total_saved,
        "limit":         limit,
    })
    log_data["total_images_split"] += success_count
    log_data["total_segments"]     += total_segments

    logger.info("")
    logger.info("=" * 65)
    logger.info(f"📊 處理完成統計")
    logger.info("-" * 65)
    logger.info(f"  總圖片數              : {total}")
    logger.info(f"  ✅ 成功               : {success_count}")
    logger.info(f"  ❌ 失敗               : {failed_count}")
    logger.info(f"  🔲 總切割區塊數       : {total_segments}")
    logger.info(f"  🚨 NG   區塊          : {total_ng}")
    logger.info(f"  ⚠️  GRAY 區塊          : {total_gray}")
    logger.info(f"  💾 已輸出區塊         : {total_saved}")
    logger.info(f"  ⏭  meta 缺漏未寫 DB   : {total_skipped}")
    logger.info(f"  🗄  DB 寫入 records    : {total_db_inserted}")
    logger.info("=" * 65)

    return {
        "total":         total,
        "success":       success_count,
        "failed":        failed_count,
        "segments":      total_segments,
        "ng_segments":   total_ng,
        "gray_segments": total_gray,
        "saved_kake":    total_saved,
        "db_inserted":   total_db_inserted,
    }, cache_data


# ──────────────────────────────────────────
# CLI
# ──────────────────────────────────────────

def parse_limit(value: Optional[str]) -> Optional[int]:
    """解析 --limit 參數：'all' / 空 / 0 / 'none' → None；正整數 → int"""
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("all", "0", "", "none"):
        return None
    try:
        n = int(v)
        return n if n > 0 else None
    except ValueError:
        raise argparse.ArgumentTypeError(f"--limit 必須是整數或 'all'，收到: {value!r}")


def main():
    freeze_support()
    setup_logging("process_run.log")

    parser = argparse.ArgumentParser(
        description="圖片切割 + 白斑瑕疵檢測整合管道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--limit", default="all",
        help="每個日期範圍要處理的圖片張數，可為正整數或 'all'（預設: all）",
    )
    parser.add_argument(
        "--refresh-cache", action="store_true",
        help="清除舊快取並重新掃描檔案清單",
    )
    parser.add_argument(
        "--config", default=SPLIT_CONFIG_FILE,
        help=f"配置檔路徑（預設: {SPLIT_CONFIG_FILE}）",
    )
    args = parser.parse_args()

    try:
        limit = parse_limit(args.limit)
    except argparse.ArgumentTypeError as e:
        logger.error(str(e))
        sys.exit(2)

    logger.info("🚀 開始 切割 + 瑕疵檢測 管道")
    logger.info(f"📌 數量設定: {'全部圖片' if limit is None else f'每範圍前 {limit} 張'}")

    cfg = load_config(args.config)
    if not cfg:
        logger.error("無法讀取配置文件，程序終止")
        return

    log_data   = load_split_log(SPLIT_LOG_FILE)
    cache_data = load_file_cache(SPLIT_CACHE_FILE)

    input_folder  = cfg.get("input_folder")
    output_folder = cfg.get("split_output_folder", os.path.join("split_data", "kake"))
    date_ranges   = cfg.get("date_ranges", [])
    enable_cache  = cfg.get("enable_file_cache", True)

    if not input_folder:
        logger.error("配置文件中缺少 input_folder")
        return
    if not date_ranges:
        logger.error("配置文件中缺少 date_ranges")
        return

    if args.refresh_cache:
        logger.info("🔄 --refresh-cache 已指定，清除舊快取")
        cache_data = {"source_path": "", "last_global_scan": "", "date_caches": {}}

    start_time = time.time()

    for date_range in date_ranges:
        date_start = date_range.get("date_start")
        date_end   = date_range.get("date_end")
        range_name = date_range.get("name", f"{date_start}~{date_end}")

        if not date_start or not date_end:
            logger.warning(f"日期範圍設定不完整: {date_range}")
            continue

        logger.info(f"\n📅 處理日期範圍: {range_name}")
        _, cache_data = run_pipeline(
            input_folder, output_folder,
            date_start, date_end, cfg, log_data, cache_data,
            limit=limit,
        )

    duration = time.time() - start_time

    save_split_log(log_data, SPLIT_LOG_FILE)
    if enable_cache:
        save_file_cache(cache_data, SPLIT_CACHE_FILE)
        logger.info(f"📦 快取已保存: {SPLIT_CACHE_FILE}")

    logger.info(f"⏱️  總耗時: {duration:.2f} 秒")
    logger.info("✨ 切割 + 瑕疵檢測 管道已完成！")


if __name__ == "__main__":
    main()
