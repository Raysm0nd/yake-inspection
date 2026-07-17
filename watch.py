"""
watch.py — 生產端常駐監看：自動偵測產線資料夾、某小時收完就整批切割+檢測+寫 DB。

判定「某小時已收完可處理」＝ 取以下較快者：
  (a) 靜置：該小時資料夾底下「最新檔案」距今超過 quiet 分鐘（遞迴看檔案 mtime，
      因為 <hour>/<board>/<side>/<file> 深層寫檔不會更新 <hour> 目錄自身 mtime）。
  (b) 更新：出現更晚的小時或隔天資料夾 → 前面的鐵定收完，立即處理。
每小時只處理一次、記在 watch_processed.json，不重跑；缺漏/跳號/跨日皆自動涵蓋。

切割與檢測直接沿用 process.split_and_detect_single（與生產 pipeline 同一實作），
參數讀 split_config.json（測試工作台按「套用為正式參數」寫回的那組）。

用法：
    python watch.py                        # 常駐，每 poll 秒掃一次（Ctrl+C 結束）
    python watch.py --once                 # 只掃一次就結束（排程 / 測試用）
    python watch.py --quiet-minutes 15 --poll-seconds 60
"""

import os
import re
import sys
import json
import time
import logging
import argparse
from datetime import datetime
from typing import Dict, List, Tuple

from split import load_config, SPLIT_CONFIG_FILE, is_valid_image, setup_logging
from process import split_and_detect_single
import db


logger = logging.getLogger(__name__)

LEDGER_FILE = "watch_processed.json"
IMG_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
_DATE_RE = re.compile(r"^\d{8}$")
_HOUR_RE = re.compile(r"^\d{1,2}$")

DEFAULT_QUIET_MINUTES = 15
DEFAULT_POLL_SECONDS = 60


# ──────────────────────────────────────────
# 已處理清單（ledger）
# ──────────────────────────────────────────

def load_ledger(path: str = LEDGER_FILE) -> Dict:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.warning("ledger 讀取失敗，改用空白清單：%s", path)
    return {"processed": {}}


def save_ledger(ledger: Dict, path: str = LEDGER_FILE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("ledger 寫入失敗：%s", e)


# ──────────────────────────────────────────
# 掃描與判定
# ──────────────────────────────────────────

def discover_hours(input_folder: str) -> List[Tuple[str, str, str]]:
    """回傳 [(date, hour, hour_path)]，對應結構 input_folder/<date:8>/<hour>/..."""
    out: List[Tuple[str, str, str]] = []
    if not os.path.isdir(input_folder):
        return out
    for date in os.listdir(input_folder):
        dpath = os.path.join(input_folder, date)
        if not (_DATE_RE.match(date) and os.path.isdir(dpath)):
            continue
        for hour in os.listdir(dpath):
            hpath = os.path.join(dpath, hour)
            if _HOUR_RE.match(hour) and os.path.isdir(hpath):
                out.append((date, hour, hpath))
    return out


def latest_mtime(root: str) -> float:
    """遞迴取 root 底下所有檔案的最新 mtime；空夾則回 root 自身 mtime。"""
    latest = 0.0
    for dp, _, files in os.walk(root):
        for f in files:
            try:
                m = os.path.getmtime(os.path.join(dp, f))
                if m > latest:
                    latest = m
            except OSError:
                pass
    if latest == 0.0:
        try:
            latest = os.path.getmtime(root)
        except OSError:
            latest = 0.0
    return latest


def collect_valid_images(hour_path: str, cfg: Dict) -> List[str]:
    """遞迴收集該小時底下、通過 is_valid_image（前綴＋排除尾墜）的原圖。"""
    files: List[str] = []
    for dp, _, fs in os.walk(hour_path):
        for f in fs:
            if f.lower().endswith(IMG_EXT):
                full = os.path.join(dp, f)
                if is_valid_image(full, cfg):
                    files.append(full)
    return sorted(files)


def is_settled(
    date: str, hour: str, hour_path: str,
    all_hours: List[Tuple[str, str, str]], quiet_seconds: float,
) -> Tuple[bool, str]:
    """(settled?, 觸發原因)。更新資料夾優先（立即），否則看靜置。"""
    key = (int(date), int(hour))
    for (d, h, _) in all_hours:
        if (int(d), int(h)) > key:
            return True, "newer-folder"
    idle = time.time() - latest_mtime(hour_path)
    if idle > quiet_seconds:
        return True, f"quiet({idle / 60:.1f}min)"
    return False, f"active(idle {idle / 60:.1f}min)"


# ──────────────────────────────────────────
# 處理單一小時
# ──────────────────────────────────────────

def process_hour(
    date: str, hour: str, hour_path: str,
    cfg: Dict, output_folder: str, write_db: bool,
) -> Dict:
    """對一個小時的所有合格原圖：切割 + 檢測 + 輸出 NG/GRAY + 批次寫 DB。"""
    files = collect_valid_images(hour_path, cfg)
    stats = {"images": 0, "segments": 0, "ng": 0, "gray": 0,
             "saved": 0, "records": 0, "skipped": 0, "failed": 0}
    records: List[Dict] = []

    for p in files:
        r = split_and_detect_single((p, output_folder, cfg))
        if r["status"] != "OK":
            stats["failed"] += 1
            logger.warning("  ✗ %s | %s", r.get("error"), r["filename"])
            continue
        stats["images"]   += 1
        stats["segments"] += r["segments_count"]
        stats["ng"]       += r["ng_segments"]
        stats["gray"]     += r["gray_segments"]
        stats["saved"]    += r["saved_kake"]
        stats["skipped"]  += r.get("records_skipped", 0)
        records.extend(r.get("records", []))

    if write_db and records:
        try:
            stats["records"] = db.bulk_insert_records(records)
        except Exception as e:
            logger.error("  DB bulk insert 失敗：%s", e)

    return stats


def scan_once(cfg: Dict, ledger: Dict, quiet_seconds: float, write_db: bool) -> int:
    """掃一輪：找出所有已收完但未處理的小時並處理，回傳這輪處理的小時數。"""
    input_folder = cfg.get("input_folder")
    output_folder = cfg.get("split_output_folder", os.path.join("split_data", "kake"))
    all_hours = discover_hours(input_folder)
    processed = ledger["processed"]
    n_done = 0

    for (date, hour, hpath) in sorted(all_hours, key=lambda x: (int(x[0]), int(x[1]))):
        key = f"{date}/{hour}"
        if key in processed:
            continue

        settled, reason = is_settled(date, hour, hpath, all_hours, quiet_seconds)
        if not settled:
            logger.info("⏳ %s 尚未收完（%s），略過", key, reason)
            continue

        logger.info("▶ 處理 %s（觸發：%s）", key, reason)
        t0 = time.time()
        stats = process_hour(date, hour, hpath, cfg, output_folder, write_db)
        processed[key] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "trigger": reason,
            **stats,
        }
        save_ledger(ledger)
        logger.info(
            "✔ %s 完成：圖 %d｜塊 %d｜NG %d GRAY %d｜存檔 %d｜DB %d（%.1fs）",
            key, stats["images"], stats["segments"], stats["ng"],
            stats["gray"], stats["saved"], stats["records"], time.time() - t0,
        )
        n_done += 1

    return n_done


# ──────────────────────────────────────────
# CLI / 主迴圈
# ──────────────────────────────────────────

def main():
    setup_logging("watch_run.log")
    ap = argparse.ArgumentParser(description="生產端產線資料夾監看 + 自動切割檢測")
    ap.add_argument("--config", default=SPLIT_CONFIG_FILE, help=f"config 路徑（預設 {SPLIT_CONFIG_FILE}）")
    ap.add_argument("--once", action="store_true", help="只掃一次就結束（排程 / 測試用）")
    ap.add_argument("--quiet-minutes", type=float, default=None,
                    help="靜置門檻（分）；預設讀 config 的 watch_quiet_minutes，再退 15")
    ap.add_argument("--poll-seconds", type=float, default=None,
                    help="輪詢間隔（秒）；預設讀 config 的 watch_poll_seconds，再退 60")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if not cfg:
        logger.error("無法讀取 config，結束")
        sys.exit(1)
    if not cfg.get("input_folder"):
        logger.error("config 缺 input_folder，結束")
        sys.exit(1)

    quiet_minutes = (args.quiet_minutes if args.quiet_minutes is not None
                     else cfg.get("watch_quiet_minutes", DEFAULT_QUIET_MINUTES))
    poll_seconds = (args.poll_seconds if args.poll_seconds is not None
                    else cfg.get("watch_poll_seconds", DEFAULT_POLL_SECONDS))
    quiet_seconds = quiet_minutes * 60
    write_db = cfg.get("enable_db_insert", True)

    logger.info(
        "🏭 watch 啟動｜監看：%s｜靜置：%s 分｜輪詢：%s 秒｜寫DB：%s｜模式：%s",
        cfg.get("input_folder"), quiet_minutes, poll_seconds, write_db,
        "單次" if args.once else "常駐",
    )

    if write_db:
        try:
            db.open_pool()
        except Exception as e:
            logger.warning("DB pool 開啟失敗，改為不寫 DB：%s", e)
            write_db = False

    ledger = load_ledger()
    try:
        while True:
            # 每輪重讀 config，讓「套用為正式參數」即時生效（不必重啟 watch）
            cfg = load_config(args.config) or cfg
            try:
                scan_once(cfg, ledger, quiet_seconds, write_db)
            except Exception:
                logger.exception("掃描例外")
            if args.once:
                break
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        logger.info("收到中斷，結束監看")
    finally:
        if write_db:
            try:
                db.close_pool()
            except Exception:
                pass


if __name__ == "__main__":
    main()
