# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

口コミ集計ツール — お客様レビューデータ（Excel/CSV）を読み込み、楽楽販売のCRM APIで電話番号から工事店を特定し、評価を集計してGoogleスプレッドシートへアップロード・Excelレポートを生成するWebアプリ。

## Development

```bash
# 依存関係のインストール
pip install -r requirements.txt

# 開発サーバー起動 (hot reload)
uvicorn app:app --reload

# アクセス: http://localhost:8000
```

`.env` ファイルに以下の環境変数が必要:
- `RR_TOKEN` — 楽楽販売APIトークン
- `RR_DB_CONTRACT`, `RR_DB_INQUIRY` — DBスキーマID
- `GOOGLE_SERVICE_ACCOUNT_JSON` — Googleサービスアカウント認証情報
- `SPREADSHEET_KEY` — 出力先スプレッドシートID

## Architecture

単一の `app.py`（FastAPI）と `static/index.html`（Vanilla JS）の2ファイル構成。

### データ処理フロー

1. フロントエンドでファイル選択・日付範囲指定 → `POST /api/run` へ送信
2. `_load_file()` でExcel/CSVを読み込み、カラム抽出・電話番号正規化
3. 楽楽販売 **契約DB** (`DB_CONTRACT`) に電話番号でマッチング
4. 未マッチの場合は **問合DB** (`DB_INQUIRY`) にフォールバック
5. `extract_rating()` でスコアを数値抽出し、工事店ごとに集計
6. Googleスプレッドシートへアップロード + Excelファイル生成
7. `GET /api/download/{filename}` でクライアントへ返却

### 入力ファイルのカラム構造

| カラム | 内容 |
|--------|------|
| B (index 1) | 電話番号 (key_tel) |
| C (index 2) | 回答日 |
| I–M (index 8–12) | 評価設問 Q1〜Q5 |
| P (index 15) | コメント |

### 楽楽販売DB設定

- **契約DB** (スキーマID 101185): 工事店名・電話番号2フィールド
- **問合DB** (スキーマID 101181): 電話番号3フィールド（契約DBに存在しない場合のフォールバック）

マッチしなかったレコードは「特定不可」として集計される。

### APIエンドポイント

| Method | Path | 用途 |
|--------|------|------|
| GET | `/` | フロントエンドHTML返却 |
| POST | `/api/run` | 集計パイプライン実行 |
| GET | `/api/download/{filename}` | Excelファイルダウンロード |
