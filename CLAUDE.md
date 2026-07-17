# CLAUDE.md — Yake 玻璃瑕疵檢測系統

> 給 AI / 工程師的專案 briefing（精簡版）。
> **完整圖文交接文件在 [docs/handover.html](docs/handover.html)**：架構圖、每支程式的函式與參數詳解、命名規則圖解、API 對照、排錯表都在那裡，本檔只留最常用的摘要。
> `legacy/` 內是已淘汰的舊架構檔（含舊版 ui/CLAUDE.md），不再維護、勿參考。

---

## 1. 專案是什麼

**Yake** 是玻璃白斑瑕疵檢測與可視化系統：

- **Pipeline（process.py）＝生產線**：掃描產線原圖 → 沿長邊切 20 塊 → 每塊白斑檢測（NG/GRAY/OK）→ 分流存檔 + 批次寫 PostgreSQL。
- **Visual 分頁＝品管站**：載入切割區塊、即時調閥值二次確認、手動存回 DB（覆寫語意）。
- **Dashboard 分頁＝戰情室**：從 DB 撈記錄畫玻璃 4 邊（Y1/Y2/X1/X2）× Shot 區間分布 + 基板明細。
- **PostgreSQL `detection_records`＝唯一資料真相**，兩個 UI 都消費同一張表。

## 2. 環境

Windows 11 ・ Python 3.10（MS Store）・ PostgreSQL 17（DB=`yake`, user=`yake_app`，**非 table owner，不能做 DDL**）・前端 vanilla JS + Canvas（無打包器）。

```
pip install fastapi uvicorn "psycopg[binary,pool]" python-dotenv
pip install pillow numpy scipy opencv-python
```

`.env`（勿 commit）：PG_HOST/PG_PORT/PG_DB/PG_USER/PG_PASSWORD/PG_POOL_MIN/PG_POOL_MAX。

## 3. 檔案角色

| 檔案 | 角色 |
|---|---|
| [server.py](server.py) | FastAPI 後端：`/api/*` + StaticFiles 掛 `ui/` |
| [process.py](process.py) | Pipeline 主體 + 偵測核心 `detect_defect_full()` + canonical parser `parse_segment_filename()`（**唯一實作，server 共用**） |
| [split.py](split.py) | 檔案掃描、日期範圍、config/log/cache 讀寫 |
| [db.py](db.py) | PG 連線池（lazy，可重開）+ CRUD；insert 皆為 `ON CONFLICT (segment_path) DO UPDATE` 覆寫 |
| [schema.sql](schema.sql) | `detection_records` 完整 DDL（19 欄 + 10 索引，含 `ux_dr_segment_path` 唯一索引，已全數套用） |
| [seed_records.py](seed_records.py) | 灌固定種子假資料 |
| [split_config.json](split_config.json) | Pipeline 全部參數（欄位說明見 handover.html §7） |
| [ui/](ui/) | index.html + app.js（殼）+ visual.js + dashboard.js + styles.css |
| [docs/handover.html](docs/handover.html) | **完整交接文件** |
| [legacy/](legacy/) | 舊架構墓園（見其 README.md） |

## 4. 不能改壞的核心規則

1. **`/api/detect` 絕不寫 DB**（拖滑桿會炸出垃圾記錄）。寫入只有兩個入口：pipeline 的 `bulk_insert_records()`、Visual 的 `POST /api/save-detection`。兩者都是覆寫語意（`ON CONFLICT (segment_path) DO UPDATE`，id 不變）。
2. **Segment 檔名**：`{date:8}_{hour:2}_{board_id}_{side}_{base_name}_{seg:02d}.jpg`，base_name 內含底線，**只能用 `process.parse_segment_filename()` 解析**，不可自己 split。
3. **Shot 區間**：短邊（Y1/Y2）6 區間、長邊（X1/X2）10 區間，長邊末段是 **`Shot182_185`**（不是 196）。此常量在 `ui/dashboard.js` 與 `seed_records.py` 各一份，**改規格必須同步**。
4. `shot_number = 區間起點 + segment_index - 1`（clip 到區間終點）；`side` 取產線路徑倒數第二層，抓不到就**跳過 DB 寫入**（不准猜）。
5. DB 幾乎全欄 NOT NULL：meta 缺漏的圖檔案照常輸出、record 跳過（log 會統計「meta 缺漏未寫 DB」）。
6. uvicorn **不可 `--workers 2+`**（`/api/pick` 的 tkinter 對話框會壞；並發已用鎖序列化）。

## 5. 開發速查

```powershell
python -m uvicorn server:app --reload --port 8000 --host 127.0.0.1   # Web UI
python process.py --limit 50                                        # pipeline 試跑
python seed_records.py --n 700 --wipe                                # 假資料
python -c "import db; db.open_pool(); print(db.count_records())"     # DB 健檢
```

排錯對照表見 handover.html §13（port 佔用、對話框卡住、「⚠ 模擬資料」徽章、400 格式錯誤等）。

## 6. 近期歷史

- **2026-07-03 全面檢查與修復（12 項）**：pool 重開 bug（多 date_range 寫入靜默失效）、inspect_ts 改用產線時間（並校正既有 3498 筆）、Shot182_196 殘留同步、NOT NULL 防呆雙保險、移除 `_placeholder_side` 等 4 個 legacy 函式、Visual 掉請求、時段篩選 0 值、重複儲存改覆寫（既有重複已清）、模擬資料徽章、偵測核心去重複（server 共用 process）、schema.sql 落地、executor 復用。同日整理資料夾（legacy/ 集中、刪 log/cache/pycache）。詳見 handover.html §11。
- **2026-07-02 修 5 bug**：統一 canonical parser、/api/detect 去 auto-insert、save-detection 改用 parser、Dashboard Shot182_185 修正、hours-available 缺口提示。

## 7. 待辦（優先序）

1. ~~ux_dr_segment_path 唯一索引 + ON CONFLICT 升級~~ ✅ 2026-07-03 完成（DBA 已建索引，db.py 已升級 upsert）。
2. **真實座標映射**：bboxes + shot_number → 玻璃物理座標（Dashboard 目前是邊緣帶隨機撒點，非真實位置）。
3. **smoke test 收進 repo**（tests/smoke.py：parser、detect、pool 重開、覆寫、live endpoint）。
4. defect_type 擴充（目前恆為 yake，schema 已預留）。

---
_Last updated: 2026-07-03_
