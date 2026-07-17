"""
圖片切割模組

負責將原始圖片根據指定的日期範圍進行切割，並將切割後的區塊儲存到 split_data 資料夾。
支援日期範圍配置和已處理日期追蹤。
"""

import os
import re
import gc
import sys
import glob
import json
import shutil
import logging
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import freeze_support

import cv2
import numpy as np
from PIL import Image


# ──────────────────────────────────────────
# 常量定義
# ──────────────────────────────────────────

SPLIT_CONFIG_FILE = "split_config.json"
SPLIT_LOG_FILE = "split_log.json"
SPLIT_CACHE_FILE = "split_cache.json"
DEFAULT_SPLIT_SIZE = 20


# ──────────────────────────────────────────
# Logging 設定
# ──────────────────────────────────────────

def setup_logging(log_path: Optional[str] = None) -> None:
    handlers = [logging.StreamHandler()]
    if log_path:
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────
# 工具函式
# ──────────────────────────────────────────

def load_config(config_file: str = SPLIT_CONFIG_FILE) -> Dict:
    """讀取分割配置"""
    if not os.path.exists(config_file):
        logger.error(f"配置文件不存在: {config_file}")
        return {}

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"讀取配置文件失敗: {e}")
        return {}


def save_config(cfg: Dict, config_file: str = SPLIT_CONFIG_FILE) -> bool:
    """把整份 config 寫回 JSON（保留既有欄位；呼叫端先 load、改鍵、再 save）。"""
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"寫入配置文件失敗: {e}")
        return False


def load_split_log(log_file: str = SPLIT_LOG_FILE) -> Dict:
    """讀取分割記錄"""
    if not os.path.exists(log_file):
        return {
            "last_updated": datetime.now().isoformat(),
            "processed_dates": [],
            "total_images_split": 0,
            "total_segments": 0
        }

    try:
        with open(log_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"讀取分割記錄失敗: {e}，將重新建立")
        return {
            "last_updated": datetime.now().isoformat(),
            "processed_dates": [],
            "total_images_split": 0,
            "total_segments": 0
        }


def save_split_log(log_data: Dict, log_file: str = SPLIT_LOG_FILE) -> None:
    """保存分割記錄"""
    log_data["last_updated"] = datetime.now().isoformat()
    try:
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(log_data, f, indent=2, ensure_ascii=False)
        logger.info(f"分割記錄已保存: {log_file}")
    except Exception as e:
        logger.error(f"保存分割記錄失敗: {e}")


def load_file_cache(cache_file: str = SPLIT_CACHE_FILE) -> Dict:
    """讀取文件清單快取"""
    if not os.path.exists(cache_file):
        return {
            "source_path": "",
            "last_global_scan": "",
            "date_caches": {}
        }

    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"讀取快取失敗: {e}，將重新建立")
        return {
            "source_path": "",
            "last_global_scan": "",
            "date_caches": {}
        }


def save_file_cache(cache_data: Dict, cache_file: str = SPLIT_CACHE_FILE) -> None:
    """保存文件清單快取"""
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, indent=2, ensure_ascii=False)
        logger.debug(f"文件清單快取已保存: {cache_file}")
    except Exception as e:
        logger.error(f"保存快取失敗: {e}")


def get_dir_mtime(dir_path: str) -> float:
    """取得目錄的修改時間戳"""
    try:
        return os.path.getmtime(dir_path) if os.path.exists(dir_path) else 0.0
    except Exception:
        return 0.0


def should_refresh_cache(cache_data: Dict, input_folder: str, date: str) -> bool:
    """判斷是否需要重新掃描指定日期的快取"""
    if cache_data["source_path"] != input_folder:
        return True  # 來源路徑不同，需要重新掃描

    date_cache = cache_data.get("date_caches", {}).get(date)
    if not date_cache:
        return True  # 該日期未快取，需要掃描

    # 檢查 NAS 上該日期目錄的修改時間
    date_dir = os.path.join(input_folder, date)
    if not os.path.exists(date_dir):
        return True

    current_mtime = get_dir_mtime(date_dir)
    cached_mtime = date_cache.get("nas_mtime", 0)

    if current_mtime != cached_mtime:
        logger.info(f"偵測到日期 {date} 的 NAS 目錄已變更，將重新掃描")
        return True

    return False


def extract_path_meta(file_path: str) -> Dict[str, str]:
    """從檔案路徑提取元數據"""
    parts = file_path.replace("/", "\\").split("\\")

    if len(parts) < 4:
        return {}

    meta = {
        "hour":     parts[-4],
        "board_id": parts[-3],
        "position": parts[-2],
    }

    if len(parts) >= 5 and re.match(r"\d{8}", parts[-5]):
        meta["date"] = parts[-5]

    return meta


def is_valid_image(file_path: str, cfg: Dict) -> bool:
    """檢查圖片是否符合處理條件"""
    filename = os.path.basename(file_path)
    name, _ = os.path.splitext(filename)

    valid_prefixes = cfg.get("valid_prefixes", ["LowerImage", "UpperImage"])
    if isinstance(valid_prefixes, list):
        valid_prefixes = tuple(valid_prefixes)
    if not filename.startswith(valid_prefixes):
        return False

    match = re.search(r"(\d+)$", name)
    if match and match.group(1) in cfg.get("exclude_suffix", []):
        return False

    return True


def read_image(image_path: str) -> Optional[np.ndarray]:
    """讀取圖片並轉換為 BGR 格式"""
    try:
        pil_img = Image.open(image_path).convert("RGB")
        return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    except Exception as e:
        logger.debug(f"讀取圖片失敗 [{image_path}]: {e}")
        return None


def save_image(output_path: str, img: np.ndarray) -> bool:
    """儲存圖片"""
    success = cv2.imwrite(output_path, img)
    if not success:
        logger.warning(f"⚠️  儲存圖片失敗：{output_path}")
    return success


# ──────────────────────────────────────────
# 核心切割函式
# ──────────────────────────────────────────

def split_single_image(args: Tuple) -> Dict:
    """
    處理單張圖片的切割

    Returns:
        包含切割結果的字典
    """
    image_path, output_folder, cfg = args

    result = {
        "filename": os.path.basename(image_path),
        "status": "OK",
        "segments_count": 0,
        "error": None,
    }

    img_bgr = read_image(image_path)
    if img_bgr is None:
        result["status"] = "FAILED"
        result["error"] = "無法讀取圖片"
        return result

    meta = extract_path_meta(image_path)
    board_id = meta.get("board_id", "UNKNOWN")
    date = meta.get("date", "UNKNOWN")
    hour = meta.get("hour", "UNKNOWN")
    position = meta.get("position", "UNKNOWN")
    base_name, ext = os.path.splitext(os.path.basename(image_path))

    width = img_bgr.shape[1]
    split_size = cfg.get("split_size", DEFAULT_SPLIT_SIZE)
    part_width = width // split_size

    if part_width == 0:
        logger.warning(f"圖片寬度 ({width} px) 過小，無法切割：{image_path}")
        result["status"] = "FAILED"
        result["error"] = "圖片寬度過小"
        return result

    # 依日期創建子資料夾
    date_output_folder = os.path.join(output_folder, date)
    os.makedirs(date_output_folder, exist_ok=True)

    for i in range(split_size):
        left = i * part_width
        right = (i + 1) * part_width if i < split_size - 1 else width
        crop = img_bgr[:, left:right]

        # 儲存切割的區塊
        segment_filename = f"{date}_{hour}_{board_id}_{position}_{base_name}_{i+1:02d}{ext}"
        segment_path = os.path.join(date_output_folder, segment_filename)

        if save_image(segment_path, crop):
            result["segments_count"] += 1

    del img_bgr
    gc.collect()

    return result


def scan_images_by_date(input_folder: str, date_start: str, date_end: str, cfg: Dict,
                        cache_data: Optional[Dict] = None, enable_cache: bool = True) -> Tuple[List[str], Dict]:
    """
    掃描指定日期範圍內的圖片，支持快取機制

    Returns:
        (圖片路徑列表, 更新後的快取資料)
    """
    if cache_data is None:
        cache_data = {"source_path": "", "last_global_scan": "", "date_caches": {}}

    extensions = ["*.jpg", "*.jpeg", "*.png", "*.bmp", "*.tiff", "*.tif"]

    def is_in_date_range(file_path: str) -> bool:
        meta = extract_path_meta(file_path)
        date = meta.get("date", "")
        return date_start <= date <= date_end

    all_files = []

    # 按日期範圍逐個掃描或讀取快取
    def generate_date_range(start: str, end: str) -> List[str]:
        """生成日期範圍內的所有日期（YYYYMMDD）"""
        from datetime import datetime, timedelta
        start_dt = datetime.strptime(start, "%Y%m%d")
        end_dt = datetime.strptime(end, "%Y%m%d")
        dates = []
        current = start_dt
        while current <= end_dt:
            dates.append(current.strftime("%Y%m%d"))
            current += timedelta(days=1)
        return dates

    date_list = generate_date_range(date_start, date_end)
    scanned_files = []

    for target_date in date_list:
        need_scan = not enable_cache or should_refresh_cache(cache_data, input_folder, target_date)

        if not need_scan:
            # 使用快取
            cached_files = cache_data.get("date_caches", {}).get(target_date, {}).get("files", [])
            scanned_files.extend(cached_files)
            logger.debug(f"使用快取: 日期 {target_date} ({len(cached_files)} 個文件)")
        else:
            # 掃描該日期
            date_dir = os.path.join(input_folder, target_date)
            if not os.path.exists(date_dir):
                logger.debug(f"日期目錄不存在: {date_dir}")
                continue

            date_files = []
            for ext in extensions:
                date_files += glob.glob(os.path.join(date_dir, "**", ext), recursive=True)
                date_files += glob.glob(os.path.join(date_dir, "**", ext.upper()), recursive=True)

            date_files = sorted(set(date_files))

            # 驗證文件
            valid_files = [
                f for f in date_files
                if is_valid_image(f, cfg)
            ]

            scanned_files.extend(valid_files)

            # 更新快取
            mtime = get_dir_mtime(date_dir)
            cache_data["date_caches"][target_date] = {
                "cached_at": datetime.now().isoformat(),
                "nas_mtime": mtime,
                "file_count": len(valid_files),
                "files": valid_files
            }
            logger.debug(f"掃描並快取: 日期 {target_date} ({len(valid_files)} 個文件)")

    # 更新全局資訊
    cache_data["source_path"] = input_folder
    cache_data["last_global_scan"] = datetime.now().isoformat()

    all_files = sorted(set(scanned_files))
    return all_files, cache_data


def split_images(input_folder: str, output_folder: str, date_start: str,
                date_end: str, cfg: Dict, log_data: Dict, cache_data: Optional[Dict] = None) -> Tuple[Dict, Dict]:
    """
    切割圖片主函式

    Returns:
        (包含統計資訊的字典, 更新後的快取資料)
    """
    os.makedirs(output_folder, exist_ok=True)

    logger.info("🔍 掃描圖片中...")
    enable_cache = cfg.get("enable_file_cache", True)
    image_paths, cache_data = scan_images_by_date(input_folder, date_start, date_end, cfg, cache_data, enable_cache)

    if not image_paths:
        logger.error("❌ 找不到任何符合條件的圖片！")
        return {"total": 0, "success": 0, "failed": 0, "segments": 0}, cache_data

    logger.info(f"📂 輸入資料夾      : {input_folder}")
    logger.info(f"📁 切割輸出資料夾 : {output_folder}")
    logger.info(f"📅 日期範圍        : {date_start} ~ {date_end}")
    logger.info(f"🖼  符合圖片數      : {len(image_paths)}")
    logger.info("=" * 65)

    chunk_size = cfg.get("chunk_size", 200)
    max_workers = cfg.get("max_workers", 4)
    args_list = [(p, output_folder, cfg) for p in image_paths]

    total = len(image_paths)
    done = 0
    success_count = 0
    failed_count = 0
    total_segments = 0

    for chunk_start in range(0, total, chunk_size):
        chunk = args_list[chunk_start : chunk_start + chunk_size]

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(split_single_image, args): args[0] for args in chunk}

            for future in as_completed(futures):
                done += 1
                try:
                    result = future.result()
                except Exception as e:
                    logger.error(f"  [%4d/{total}] ❌ 子進程例外: {e}", done)
                    failed_count += 1
                    continue

                if result["status"] == "OK":
                    success_count += 1
                    total_segments += result["segments_count"]
                    logger.info(
                        f"  [{done:4d}/{total}] ✅ {result['segments_count']:2d} 個區塊 | {result['filename']}"
                    )
                else:
                    failed_count += 1
                    logger.warning(
                        f"  [{done:4d}/{total}] ❌ {result['error']} | {result['filename']}"
                    )

    # 更新記錄
    log_data["processed_dates"].append({
        "date_start": date_start,
        "date_end": date_end,
        "timestamp": datetime.now().isoformat(),
        "images_split": success_count,
        "segments": total_segments
    })
    log_data["total_images_split"] += success_count
    log_data["total_segments"] += total_segments

    logger.info("")
    logger.info("=" * 65)
    logger.info(f"📊 切割完成統計")
    logger.info("-" * 65)
    logger.info(f"  總圖片數       : {total}")
    logger.info(f"  ✅ 成功        : {success_count}")
    logger.info(f"  ❌ 失敗        : {failed_count}")
    logger.info(f"  🔲 總區塊數    : {total_segments}")
    logger.info("=" * 65)

    return {
        "total": total,
        "success": success_count,
        "failed": failed_count,
        "segments": total_segments,
    }, cache_data


# ──────────────────────────────────────────
# 主程式入口
# ──────────────────────────────────────────

def main():
    """主程式入口"""
    freeze_support()
    setup_logging("split_run.log")

    logger.info("🚀 開始圖片切割程序")

    # 解析命令列參數
    refresh_cache = "--refresh-cache" in sys.argv

    # 讀取配置
    cfg = load_config(SPLIT_CONFIG_FILE)
    if not cfg:
        logger.error("無法讀取配置文件，程序終止")
        return

    log_data = load_split_log(SPLIT_LOG_FILE)
    cache_data = load_file_cache(SPLIT_CACHE_FILE)

    input_folder = cfg.get("input_folder")
    output_folder = cfg.get("split_output_folder", "split_data")
    date_ranges = cfg.get("date_ranges", [])
    enable_cache = cfg.get("enable_file_cache", True)

    if not input_folder:
        logger.error("配置文件中缺少 input_folder")
        return

    if not date_ranges:
        logger.error("配置文件中缺少 date_ranges")
        return

    # 清除快取（如果指定 --refresh-cache）
    if refresh_cache:
        logger.info("🔄 --refresh-cache 參數已指定，將清除舊快取並重新掃描")
        cache_data = {"source_path": "", "last_global_scan": "", "date_caches": {}}

    # 記錄掃描開始時間
    scan_start_time = time.time()

    for date_range in date_ranges:
        date_start = date_range.get("date_start")
        date_end = date_range.get("date_end")
        range_name = date_range.get("name", f"{date_start}~{date_end}")

        if not date_start or not date_end:
            logger.warning(f"日期範圍設定不完整: {date_range}")
            continue

        logger.info(f"\n📅 處理日期範圍: {range_name}")
        result, cache_data = split_images(input_folder, output_folder, date_start, date_end, cfg, log_data, cache_data)

    # 記錄掃描耗時
    scan_duration = time.time() - scan_start_time

    # 保存記錄
    save_split_log(log_data, SPLIT_LOG_FILE)

    # 保存快取
    if enable_cache:
        save_file_cache(cache_data, SPLIT_CACHE_FILE)
        logger.info(f"📦 快取已保存: {SPLIT_CACHE_FILE}")

    logger.info(f"⏱️  總耗時: {scan_duration:.2f} 秒")
    logger.info("✨ 圖片切割程序已完成！")


if __name__ == "__main__":
    main()
