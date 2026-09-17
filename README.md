# FootballAnalysis 足球篩查（雲端版）

足球賽事盤口篩查工具：未來 48 小時賽事 × 20 項歷史篩查（同初盤/尾盤/水位、勝和負比例、
排名差距、盤口分佈＋水位分佈、組合篩查等），附「我的選擇」上/下盤記錄同勝出率統計。

## 部署（Render）
1. Render Dashboard → **New → Blueprint** → 連接本 repo
2. Render 會讀 `render.yaml` 自動起 Docker 服務
3. 數據庫 `football.db`（約 111MB）喺 build 階段由 GitHub Release asset 下載

## 更新數據庫
`football.db` 由本機爬蟲每日更新。更新雲端：
1. 上傳新 `football.db` 做 Release asset（tag 用日期，例如 `db-20260918`）
2. 改 `render.yaml` 入面 `DB_URL` 指向新 asset → commit → Render 自動重新部署

## 本機開發
```
pip install -r requirements.txt
python app.py        # http://localhost:7100
```

## 注意
- 免費 plan 閒置會瞓著，首次訪問要等 30-60 秒冷啟動
- 「獲取賠率」功能喺雲端可能受球探網 IP 限制；本機用無問題
