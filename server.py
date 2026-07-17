"""
FastAPI 後端：包住 process.py 的偵測核心 + 提供整合 UI 靜態檔

啟動：
    pip install fastapi uvicorn
    uvicorn server:app --reload --port 8000

瀏覽器開：http://localhost:8000/
"""

import os
import base64
import shutil
import logging
import threading
from io import BytesIO
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import numpy as np
from PIL import Image

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from process import (
    detect_defect_full, _build_detect_params, parse_segment_filename,
    split_image_to_blocks,
)
from split import (
    load_config, save_config, SPLIT_CONFIG_FILE, is_valid_image, DEFAULT_SPLIT_SIZE,
)
import db


EXTENSIONS = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff")
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# 原圖自動切塊的預覽暫存夾：載入原圖時把切好的塊丟這裡供逐塊對看。
# 放在 split_output_folder 底下（同一顆磁碟），server 關閉時整個砍掉。
_cfg = load_config(SPLIT_CONFIG_FILE) or {}
_preview_base = _cfg.get("split_output_folder") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "split_data"
)
PREVIEW_DIR = os.path.join(_preview_base, "_preview")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _reset_preview_dir():
    """清空並重建預覽暫存夾（每次載入新原圖切割前呼叫，避免混到上一批）。"""
    shutil.rmtree(PREVIEW_DIR, ignore_errors=True)
    os.makedirs(PREVIEW_DIR, exist_ok=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    db.open_pool()
    yield
    db.close_pool()
    # server 關閉時砍掉預覽暫存夾（使用者選的清理時機）
    shutil.rmtree(PREVIEW_DIR, ignore_errors=True)
    logger.info("preview dir cleaned: %s", PREVIEW_DIR)


app = FastAPI(title="Yake Glass Defect API", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DetectParams(BaseModel):
    threshold: int = 100
    len_ng: int = 10
    area_ng: int = 40
    len_gray_min: int = 5
    area_gray_min: int = 20
    merge_dist: int = 30


class DetectRequest(BaseModel):
    segment_path: str
    params: Optional[DetectParams] = None


# ──────────────────────────────────────────
# 偵測核心：直接共用 process.detect_defect_full（單一實作，避免兩份演算法漂移）
# ──────────────────────────────────────────

def _mask_to_png_b64(mask: np.ndarray) -> str:
    """白斑遮罩轉成亮黃 PNG base64（給前端 canvas overlay 用）"""
    h, w = mask.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[mask, 0] = 255   # R
    rgba[mask, 1] = 220   # G
    rgba[mask, 2] = 60    # B
    rgba[mask, 3] = 255   # A
    buf = BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _image_to_jpg_b64(arr_rgb: np.ndarray) -> str:
    buf = BytesIO()
    Image.fromarray(arr_rgb).save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# ──────────────────────────────────────────
# Endpoints
# ──────────────────────────────────────────

@app.get("/api/default-params")
def get_default_params():
    cfg = load_config(SPLIT_CONFIG_FILE) or {}
    return _build_detect_params(cfg)


@app.get("/api/default-folder")
def get_default_folder():
    cfg = load_config(SPLIT_CONFIG_FILE) or {}
    folder = cfg.get("split_output_folder") or os.path.join("split_data", "kake")
    return {"folder": folder}


class ApplyParamsRequest(BaseModel):
    """測試調好的參數 → 套用為生產正式參數（寫回 split_config.json）。"""
    threshold: int
    len_ng: int
    area_ng: int
    len_gray_min: int
    area_gray_min: int
    merge_dist: int
    split_size: Optional[int] = None  # 一併更新切割塊數（省略則保留原值）


@app.post("/api/apply-params")
def apply_params(req: ApplyParamsRequest):
    """
    把 Visual 分頁目前的參數寫回 split_config.json，供生產端（watch.py / process.py）沿用。
    偵測核心的 threshold 在 config 裡叫 white_threshold（_build_detect_params 讀這個），
    寫回時做對應轉換；其餘鍵同名。只覆寫參數欄位，config 其他設定原封不動。
    """
    cfg = load_config(SPLIT_CONFIG_FILE) or {}
    cfg["white_threshold"] = req.threshold
    cfg["len_ng"]          = req.len_ng
    cfg["area_ng"]         = req.area_ng
    cfg["len_gray_min"]    = req.len_gray_min
    cfg["area_gray_min"]   = req.area_gray_min
    cfg["merge_dist"]      = req.merge_dist
    if req.split_size is not None:
        cfg["split_size"] = req.split_size

    if not save_config(cfg, SPLIT_CONFIG_FILE):
        raise HTTPException(status_code=500, detail="寫入 split_config.json 失敗")

    saved = _build_detect_params(cfg)
    saved["split_size"] = cfg.get("split_size", DEFAULT_SPLIT_SIZE)
    logger.info("apply-params 已寫回 config: %s", saved)
    return {"ok": True, "saved": saved}


# ──────────────────────────────────────────
# 原生檔案/資料夾選擇對話框（in-process tkinter，省掉 subprocess 冷啟動）
# ──────────────────────────────────────────

import tkinter as _tk
from tkinter import filedialog as _filedialog

# FastAPI 的 sync endpoint 跑在 threadpool 執行緒（不是主執行緒）。
# Tk root 在「同一執行緒內建立+銷毀、且一次只有一個」的前提下於 Windows 可用，
# 用鎖序列化並發的 /api/pick，避免同時開出兩個 Tk root 導致崩潰。
_dialog_lock = threading.Lock()


def _open_native_dialog(kind: str) -> str:
    """開一個短命 Tk root 的原生對話框，用畢即銷毀；同一時間只允許一個。"""
    root = _tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update_idletasks()
    try:
        if kind == "folder":
            path = _filedialog.askdirectory(title="選擇區塊資料夾", parent=root)
        else:
            path = _filedialog.askopenfilename(
                title="選擇單張圖檔",
                parent=root,
                filetypes=[
                    ("影像檔", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff"),
                    ("全部", "*.*"),
                ],
            )
    finally:
        root.destroy()
    return os.path.normpath(path) if path else ""


@app.get("/api/pick")
def pick(kind: str = Query("folder")):
    """kind = folder | file。開原生對話框、回傳絕對路徑；使用者取消時 path=''。"""
    try:
        with _dialog_lock:
            path = _open_native_dialog(kind)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"File picker error: {e}")
    return {"path": path}


def _label_from_path(full: str) -> str:
    lower = full.replace("\\", "/").lower()
    if "/ng/" in lower:
        return "NG"
    if "/gray/" in lower:
        return "GRAY"
    if "/ok/" in lower:
        return "OK"
    return "?"


@app.get("/api/segments")
def list_segments(folder: str = Query(...), filter: str = Query("all")):
    """
    path 可以是資料夾（掃描所有圖檔）或單張圖檔。

    每張圖依檔名判斷：
      - **已切好的塊**（parse_segment_filename 解得出來）→ 直接列出，套用 ng/gray/ok 篩選。
      - **未切割的原圖**（解不出來）→ 只切 pipeline 會處理的那些（is_valid_image：
        valid_prefixes 相符、且尾墜不在 exclude_suffix，例如 001/186/152 就跳過），
        切成 split_size 塊丟進 PREVIEW_DIR 供左右對看；預覽塊未分類，不受 ng/gray/ok 篩選影響。
    """
    # 蒐集候選圖檔（單張或遞迴掃資料夾）
    if os.path.isfile(folder):
        if not folder.lower().endswith(EXTENSIONS):
            raise HTTPException(status_code=400, detail=f"Unsupported image type: {folder}")
        candidates = [folder]
    elif os.path.isdir(folder):
        candidates = [
            os.path.join(dp, f)
            for dp, _, files in os.walk(folder)
            for f in files
            if f.lower().endswith(EXTENSIONS)
        ]
    else:
        raise HTTPException(status_code=404, detail=f"Path not found: {folder}")

    # 分流：已切好的塊 vs 未切割原圖
    raw_images = [p for p in candidates if parse_segment_filename(p) is None]
    seg_images = [p for p in candidates if parse_segment_filename(p) is not None]

    # 原圖只切 pipeline 會處理的（valid_prefixes + 非 exclude_suffix），與生產線一致；
    # 尾墜在排除清單（001/186/152…）或前綴不符的原圖不切割、不列出。
    cfg = load_config(SPLIT_CONFIG_FILE) or {}
    splittable = [p for p in raw_images if is_valid_image(p, cfg)]

    # 切塊數也跟 pipeline 讀同一個 config 值，避免預覽與生產線的塊數漂移。
    split_size = cfg.get("split_size", DEFAULT_SPLIT_SIZE)

    # 原圖 → 自動切塊到預覽暫存夾（先清空避免混到上一批）
    preview_blocks: List[str] = []
    if splittable:
        _reset_preview_dir()
        for rp in sorted(splittable):
            preview_blocks.extend(split_image_to_blocks(rp, PREVIEW_DIR, split_size))

    flt = filter.lower()
    out: List[Dict] = []

    # 已分類的塊：套用 ng/gray/ok 篩選
    for full in seg_images:
        label = _label_from_path(full)
        if flt == "ng" and label != "NG":
            continue
        if flt == "gray" and label != "GRAY":
            continue
        if flt == "ok" and label != "OK":
            continue
        out.append({"path": full, "filename": os.path.basename(full), "folder_label": label})

    # 預覽塊：原圖現切、尚未分類，一律列出（分類會在 /api/detect 逐塊即時算）
    for full in preview_blocks:
        out.append({"path": full, "filename": os.path.basename(full), "folder_label": "?"})

    out.sort(key=lambda x: x["path"])
    return {"count": len(out), "segments": out}


@app.post("/api/detect")
def detect(req: DetectRequest):
    if not os.path.isfile(req.segment_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.segment_path}")

    params = (req.params or DetectParams()).model_dump()
    try:
        arr = np.array(Image.open(req.segment_path).convert("RGB"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load image: {e}")

    result = detect_defect_full(arr, params, return_mask=True)

    # 只回偵測結果，不落庫。落庫走 /api/save-detection（避免拖動滑桿也寫入）。
    return {
        "filename": os.path.basename(req.segment_path),
        "image": _image_to_jpg_b64(arr),
        "mask": _mask_to_png_b64(result["mask"]),
        "img_w": result["img_w"],
        "img_h": result["img_h"],
        "bboxes": result["bboxes"],
        "ng_count": result["ng_count"],
        "gray_count": result["gray_count"],
        "overall": result["status"],
        "params": params,
    }


class RecordIn(BaseModel):
    inspect_ts: Optional[str] = None
    date: Optional[str] = None
    hour: Optional[str] = None
    substrate_id: str
    image_layer: str = "CenterImage"
    source_image: str
    shot_number: int
    segment_index: int
    side: str
    shot_label: str
    defect_status: str
    defect_type: Optional[str] = "yake"
    ng_count: int = 0
    gray_count: int = 0
    bboxes: List[Dict] = []
    detect_params: Dict = {}
    segment_path: Optional[str] = None


@app.get("/api/records")
def get_records(n: int = Query(700, ge=1, le=5000)):
    """從 PostgreSQL detection_records 撈最近 N 筆。"""
    try:
        records = db.fetch_records(limit=n)
    except Exception as e:
        logger.exception("fetch_records failed")
        raise HTTPException(status_code=500, detail=f"DB fetch failed: {e}")
    return {"count": len(records), "records": records}


class SaveDetectionRequest(BaseModel):
    segment_path: str
    overall: str
    ng_count: int
    gray_count: int
    bboxes: List[Dict]
    params: Dict


@app.post("/api/save-detection")
def save_detection(req: SaveDetectionRequest):
    """
    Visual 頁面「儲存」按鈕入口：以 process.py 的 canonical parser 從 segment_path 拆欄位，
    補上偵測結果後寫進 detection_records。檔名不符規則直接 400。
    """
    from datetime import datetime

    parsed = parse_segment_filename(req.segment_path)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=f"Segment filename does not match expected format: "
                   f"{os.path.basename(req.segment_path)}",
        )

    try:
        inspect_ts = datetime.strptime(
            f"{parsed['date']}{parsed['hour']}0000", "%Y%m%d%H%M%S"
        ).isoformat()
    except ValueError:
        inspect_ts = datetime.now().isoformat()

    payload = {
        **parsed,
        "inspect_ts":    inspect_ts,
        "defect_status": req.overall,
        "defect_type":   "yake",
        "ng_count":      req.ng_count,
        "gray_count":    req.gray_count,
        "bboxes":        req.bboxes,
        "detect_params": req.params,
        "segment_path":  req.segment_path,
    }

    try:
        new_id = db.insert_record(payload)
    except Exception as e:
        logger.exception("save_detection insert failed")
        raise HTTPException(status_code=500, detail=f"DB insert failed: {e}")
    return {"id": new_id, "meta": payload}


class SaveAllRequest(BaseModel):
    segments: List[str]   # 目前 Visual 載入的全部塊路徑
    params: Dict


@app.post("/api/save-all")
def save_all(req: SaveAllRequest):
    """
    Visual「儲存全部 N 塊」入口：把目前載入的每一塊都偵測 + 寫進 detection_records。
    寫入機制與 save-detection 相同（canonical parser 拆欄位、ON CONFLICT segment_path 覆寫），
    只是套用到全部載入的塊。為避免 record 指向會被清掉的 _preview 暫存檔，
    每塊都複製到正式輸出夾 split_output_folder/<ng|gray|ok>/<date>/，segment_path 指向那裡。
    """
    from datetime import datetime

    cfg = load_config(SPLIT_CONFIG_FILE) or {}
    output_folder = cfg.get("split_output_folder") or os.path.join("split_data", "kake")
    status_subdir = {"NG": "ng", "GRAY": "gray", "OK": "ok"}
    params = (DetectParams(**req.params) if req.params else DetectParams()).model_dump()

    records: List[Dict] = []
    summary = {"total": len(req.segments), "saved": 0,
               "NG": 0, "GRAY": 0, "OK": 0, "skipped": 0, "failed": 0}

    for src in req.segments:
        parsed = parse_segment_filename(src)
        if parsed is None:
            summary["skipped"] += 1
            continue
        if not os.path.isfile(src):
            summary["skipped"] += 1
            continue

        try:
            arr = np.array(Image.open(src).convert("RGB"))
            det = detect_defect_full(arr, params)
        except Exception:
            summary["failed"] += 1
            logger.exception("save-all 偵測失敗：%s", src)
            continue

        status = det["status"]
        summary[status] += 1

        # 複製到正式輸出夾，segment_path 指向永久位置（同 pipeline 命名 / 位置）
        subdir = status_subdir.get(status, "ok")
        target_dir = os.path.join(output_folder, subdir, parsed["date"])
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(src))
        try:
            if os.path.abspath(src) != os.path.abspath(target):
                shutil.copy2(src, target)
        except Exception:
            logger.exception("save-all 複製失敗：%s → %s", src, target)
            target = src  # 複製失敗就退回原路徑，至少 record 仍寫得進去

        try:
            inspect_ts = datetime.strptime(
                f"{parsed['date']}{parsed['hour']}0000", "%Y%m%d%H%M%S"
            ).isoformat()
        except ValueError:
            inspect_ts = datetime.now().isoformat()

        records.append({
            **parsed,
            "inspect_ts":    inspect_ts,
            "defect_status": status,
            "defect_type":   "yake",
            "ng_count":      det["ng_count"],
            "gray_count":    det["gray_count"],
            "bboxes":        det["bboxes"],
            "detect_params": params,
            "segment_path":  target,
        })

    if records:
        try:
            summary["saved"] = db.bulk_insert_records(records)
        except Exception as e:
            logger.exception("save-all bulk insert 失敗")
            raise HTTPException(status_code=500, detail=f"DB bulk insert failed: {e}")

    logger.info("save-all 完成：%s", summary)
    return summary


@app.post("/api/records")
def post_record(rec: RecordIn):
    """
    寫一筆偵測結果到 detection_records。
    inspect_ts / date / hour 若未提供，會用 server 當下時間補齊。
    """
    from datetime import datetime

    payload = rec.model_dump()
    if not payload.get("inspect_ts"):
        now = datetime.now()
        payload["inspect_ts"] = now.isoformat()
        payload["date"] = payload["date"] or now.strftime("%Y%m%d")
        payload["hour"] = payload["hour"] or now.strftime("%H")

    try:
        new_id = db.insert_record(payload)
    except Exception as e:
        logger.exception("insert_record failed")
        raise HTTPException(status_code=500, detail=f"DB insert failed: {e}")
    return {"id": new_id}


@app.get("/api/hours-available")
def hours_available(date_from: Optional[str] = Query(None), date_to: Optional[str] = Query(None)):
    """
    回傳實際存在資料的 hour（0..23 字串），給 dashboard 標示時段缺口用。
    可選 date_from / date_to（yyyymmdd）縮小範圍。
    """
    try:
        hours = db.distinct_hours(date_from=date_from, date_to=date_to)
    except Exception as e:
        logger.exception("distinct_hours failed")
        raise HTTPException(status_code=500, detail=f"DB query failed: {e}")
    return {"hours": hours}


# ──────────────────────────────────────────
# 靜態檔：把 ui/ 掛在根路徑
# ──────────────────────────────────────────
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
