"""
口コミ集計ツール - FastAPI バックエンド
"""
import io
import re
import os
import glob
import tempfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests
import gspread
from gspread.utils import rowcol_to_a1
from oauth2client.service_account import ServiceAccountCredentials
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="口コミ集計ツール")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静的ファイル (index.html) を配信
app.mount("/static", StaticFiles(directory="static"), name="static")

# ==============================================================================
# 設定（環境変数から読み込み）
# ==============================================================================
GOOGLE_JSON_KEY   = os.environ.get("GOOGLE_JSON_KEY", "")  # 後方互換：ファイルパスが設定されていれば使用
SPREADSHEET_KEY   = os.environ.get("SPREADSHEET_KEY", "")
RR_DOMAIN         = os.environ.get("RR_DOMAIN", "hntobias.rakurakuhanbai.jp")
RR_ACCOUNT        = os.environ.get("RR_ACCOUNT", "mspy4wa")
RR_TOKEN          = os.environ.get("RR_TOKEN", "")
RR_API_URL        = f"https://{RR_DOMAIN}/{RR_ACCOUNT}/api/csvexport/version/v1"

DB_CONTRACT = {
    "dbSchemaId": os.environ.get("DB_CONTRACT_SCHEMA_ID", "101185"),
    "listId":     os.environ.get("DB_CONTRACT_LIST_ID",   "101445"),
    "searchId":   os.environ.get("DB_CONTRACT_SEARCH_ID", "107189"),
    "cols": {
        "order_id": "手配番号",
        "shop":     "施工店名",
        "date":     "施工日または商品購入日",
        "tels":     ["（日程調整）施工先電話番号_1", "（日程調整）施工先電話番号_2"],
    },
}

DB_INQUIRY = {
    "dbSchemaId": os.environ.get("DB_INQUIRY_SCHEMA_ID", "101181"),
    "listId":     os.environ.get("DB_INQUIRY_LIST_ID",   "101448"),
    "searchId":   os.environ.get("DB_INQUIRY_SEARCH_ID", "107187"),
    "cols": {
        "order_id": "手配番号",
        "date":     "成約日時",
        "tels":     ["電話番号_1", "電話番号_2", "電話番号_3"],
    },
}

# 列インデックス（0始まり）
COL_TEL      = 1   # B列: 電話番号
COL_DATE     = 2   # C列: 回答日
COL_RATINGS  = [8, 9, 10, 11, 12]   # I,J,K,L,M列: 評価
COL_COMMENT  = 15  # P列: コメント
# 削除する列インデックス: A(0), D-H(3-7), O(14)
DROP_COLS    = [0, 3, 4, 5, 6, 7, 14]

NEW_COL_NAMES = ["施行日当日、施工スタッフから到着時間のご案内がございましたでしょうか。", 
                 "施工スタッフの言葉遣いや身だしなみはいかがでしたでしょうか。", 
                 "施工前の養生や施工後の清掃について、いかがでしたでしょうか。", 
                 "施工スタッフから試運転（動作確認）のご説明はいかがでしたでしょうか。", 
                 "施工後の仕上がりについて、ご確認いただきましたでしょうか。"
                 ]

# ==============================================================================
# ユーティリティ関数
# ==============================================================================

def fetch_rakuraku_csv(settings: dict) -> pd.DataFrame:
    headers = {"X-HD-apitoken": RR_TOKEN, "Content-Type": "application/json"}
    payload = {
        "dbSchemaId": settings["dbSchemaId"],
        "listId":     settings["listId"],
        "searchId":   settings["searchId"],
        "limit":      10000,
    }
    try:
        res = requests.post(RR_API_URL, headers=headers, json=payload, timeout=60)
        if res.status_code != 200:
            raise RuntimeError(f"楽楽販売 APIエラー: {res.status_code}")
        try:
            content = res.content.decode("cp932")
        except Exception:
            content = res.content.decode("utf-8", errors="ignore")
        return pd.read_csv(io.StringIO(content), dtype=str)
    except Exception as e:
        raise RuntimeError(str(e))


def normalize_phone(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace("-", "").str.replace("ー", "").str.replace(" ", "").str.strip()


def melt_phone_columns(df: pd.DataFrame, id_col: str, tel_cols: list) -> pd.DataFrame:
    valid_tel_cols = [c for c in tel_cols if c in df.columns]
    if not valid_tel_cols:
        return pd.DataFrame(columns=["key_tel", id_col])
    melted = df[[id_col] + valid_tel_cols].melt(id_vars=[id_col], value_vars=valid_tel_cols, value_name="key_tel")
    melted["key_tel"] = normalize_phone(melted["key_tel"])
    melted = melted[melted["key_tel"].notna() & (melted["key_tel"] != "") & (melted["key_tel"] != "nan")]
    return melted[["key_tel", id_col]].drop_duplicates()


def extract_rating(val) -> float | None:
    m = re.search(r"(\d+)", str(val))
    return float(m.group(1)) if m else None


def _get_gcp_creds():
    """環境変数 → JSONファイル の順で Google 認証情報を取得する。"""
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    if os.environ.get("GCP_PRIVATE_KEY"):
        keyfile_dict = {
            "type":                        os.environ["GCP_TYPE"],
            "project_id":                  os.environ["GCP_PROJECT_ID"],
            "private_key_id":              os.environ["GCP_PRIVATE_KEY_ID"],
            "private_key":                 os.environ["GCP_PRIVATE_KEY"].replace("\\n", "\n"),
            "client_email":                os.environ["GCP_CLIENT_EMAIL"],
            "client_id":                   os.environ["GCP_CLIENT_ID"],
            "auth_uri":                    "https://accounts.google.com/o/oauth2/auth",
            "token_uri":                   "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url":        os.environ["GCP_CLIENT_X509_CERT_URL"],
        }
        return ServiceAccountCredentials.from_json_keyfile_dict(keyfile_dict, scope)
    if GOOGLE_JSON_KEY and os.path.exists(GOOGLE_JSON_KEY):
        return ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_JSON_KEY, scope)
    raise RuntimeError("Google 認証情報が設定されていません。.env の GCP_* 変数を確認してください。")


def upload_to_gsheet(df: pd.DataFrame, spreadsheet_key: str, sheet_name: str):
    creds = _get_gcp_creds()
    client = gspread.authorize(creds)
    workbook = client.open_by_key(spreadsheet_key)
    try:
        sheet = workbook.worksheet(sheet_name)
    except Exception:
        sheet = workbook.add_worksheet(title=sheet_name, rows="200", cols="20")

    df_clean = df.fillna("")
    data = [df_clean.columns.tolist()] + df_clean.values.tolist()
    sheet.clear()
    sheet.update(range_name="A1", values=data, value_input_option="USER_ENTERED")

    num_rows = len(df_clean) + 1
    num_cols = len(df_clean.columns)
    full_range   = f"A1:{rowcol_to_a1(num_rows, num_cols)}"
    header_range = f"A1:{rowcol_to_a1(1, num_cols)}"
    border = {"style": "SOLID"}
    sheet.format(full_range, {
        "borders": {"top": border, "bottom": border, "left": border, "right": border},
        "horizontalAlignment": "CENTER",
        "verticalAlignment": "MIDDLE",
    })
    sheet.format(header_range, {
        "backgroundColor": {"red": 0.85, "green": 0.92, "blue": 0.97},
        "textFormat": {"bold": True},
        "horizontalAlignment": "CENTER",
    })
    if num_cols >= 3:
        sheet.format(f"{rowcol_to_a1(2,2)}:{rowcol_to_a1(num_rows, num_cols-1)}",
                     {"numberFormat": {"type": "NUMBER", "pattern": "0.0"}})


def _load_file(file_bytes: bytes, filename: str) -> pd.DataFrame:
    """1ファイルを読み込んで必要列だけ抽出したDataFrameを返す。"""
    if filename.endswith(".csv"):
        df_raw = pd.read_csv(io.BytesIO(file_bytes), dtype=str, header=None)
    else:
        df_raw = pd.read_excel(io.BytesIO(file_bytes), dtype=str, header=None)

    max_col = max(COL_COMMENT, *COL_RATINGS, COL_TEL, COL_DATE)
    if len(df_raw.columns) <= max_col:
        raise ValueError(f"「{filename}」の列数が不足しています（{len(df_raw.columns)}列）。P列({COL_COMMENT+1}列目)まで必要です。")

    df_raw.columns = range(len(df_raw.columns))
    col_map = {COL_TEL: "key_tel_raw", COL_DATE: "回答日", COL_COMMENT: "コメント"}
    for idx, name in zip(COL_RATINGS, NEW_COL_NAMES):
        col_map[idx] = name

    return df_raw.iloc[1:].reset_index(drop=True)[list(col_map.keys())].rename(columns=col_map)


def run_aggregation(files: list[tuple[bytes, str]], date_from: str, date_to: str, output_name: str = "") -> dict:
    """メイン集計処理。files は (file_bytes, filename) のリスト。"""
    # 日付範囲の決定
    if date_from and date_to:
        try:
            first_day = datetime.strptime(date_from, "%Y-%m-%d")
            last_day  = datetime.strptime(date_to,   "%Y-%m-%d")
        except ValueError:
            raise ValueError("日付は YYYY-MM-DD 形式で指定してください。")
        if first_day > last_day:
            raise ValueError("開始日は終了日より前にしてください。")
    else:
        today     = datetime.now()
        last_day  = today.replace(day=1) - timedelta(days=1)
        first_day = last_day.replace(day=1)

    sheet_name = f"{first_day.strftime('%Y%m%d')}_{last_day.strftime('%Y%m%d')}"

    # 全ファイルを読み込んで結合
    frames = [_load_file(fb, fn) for fb, fn in files]
    df_review = pd.concat(frames, ignore_index=True)

    # 回答日で日付範囲フィルタリング
    df_review["回答日"] = pd.to_datetime(df_review["回答日"], errors="coerce")
    df_review = df_review[
        (df_review["回答日"] >= pd.Timestamp(first_day)) &
        (df_review["回答日"] <= pd.Timestamp(last_day) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1))
    ].copy()

    if df_review.empty:
        raise ValueError(f"{first_day.strftime('%Y/%m/%d')} ～ {last_day.strftime('%Y/%m/%d')} に該当する回答日のデータがありません。")

    # B列（電話番号）からキー生成
    def create_key_tel(val):
        s = str(val).strip().replace("電話：", "")
        if "リンク" in s or "QR" in s:
            return None
        if not re.search(r"\d", s):
            return None
        return normalize_phone(pd.Series([s]))[0]

    df_review["key_tel"]  = df_review["key_tel_raw"].apply(create_key_tel)
    df_review["電話番号"] = df_review["key_tel_raw"].astype(str).str.strip().replace("nan", "")
    df_review = df_review.drop(columns=["key_tel_raw"])

    # 除外行（リンク・QR・電話番号なし）を先に分離
    df_excluded = df_review[df_review["key_tel"].isna()].copy()
    df_match    = df_review[df_review["key_tel"].notna()].copy()

    # 楽楽販売データ取得
    df_contract = fetch_rakuraku_csv(DB_CONTRACT)
    if not df_contract.empty and DB_CONTRACT["cols"]["date"] in df_contract.columns:
        df_contract[DB_CONTRACT["cols"]["date"]] = pd.to_datetime(df_contract[DB_CONTRACT["cols"]["date"]], errors="coerce")
        df_contract = df_contract.sort_values(by=DB_CONTRACT["cols"]["date"], ascending=False)

    df_inquiry = fetch_rakuraku_csv(DB_INQUIRY)
    if not df_inquiry.empty and DB_INQUIRY["cols"]["date"] in df_inquiry.columns:
        df_inquiry[DB_INQUIRY["cols"]["date"]] = pd.to_datetime(df_inquiry[DB_INQUIRY["cols"]["date"]], errors="coerce")
        df_inquiry = df_inquiry.sort_values(by=DB_INQUIRY["cols"]["date"], ascending=False)

    # マッチング（電話番号あり行のみ）
    contract_phones = melt_phone_columns(df_contract, DB_CONTRACT["cols"]["order_id"], DB_CONTRACT["cols"]["tels"])
    contract_phones = contract_phones.drop_duplicates(subset=["key_tel"])
    contract_master = pd.merge(
        contract_phones,
        df_contract[[DB_CONTRACT["cols"]["order_id"], DB_CONTRACT["cols"]["shop"]]],
        on=DB_CONTRACT["cols"]["order_id"],
        how="left",
    )

    merged = pd.merge(
        df_match,
        contract_master.rename(columns={DB_CONTRACT["cols"]["shop"]: "施工店名"}).drop(columns=[DB_CONTRACT["cols"]["order_id"]]),
        on="key_tel",
        how="left",
    )

    # ルートB: 問い合わせDB経由
    df_not_found = merged[merged["施工店名"].isna()].copy()
    if not df_not_found.empty and not df_inquiry.empty:
        inq_phones = melt_phone_columns(df_inquiry, DB_INQUIRY["cols"]["order_id"], DB_INQUIRY["cols"]["tels"])
        inq_phones = inq_phones.drop_duplicates(subset=["key_tel"])
        temp_inq   = pd.merge(df_not_found[["key_tel"]], inq_phones.rename(columns={DB_INQUIRY["cols"]["order_id"]: "tmp_id"}), on="key_tel", how="left")
        id_master  = df_contract[[DB_CONTRACT["cols"]["order_id"], DB_CONTRACT["cols"]["shop"]]].drop_duplicates(subset=[DB_CONTRACT["cols"]["order_id"]])
        id_master.columns = ["tmp_id", "shop_b"]
        route_b    = pd.merge(temp_inq, id_master, on="tmp_id", how="left")
        shop_map   = route_b.dropna(subset=["shop_b"]).set_index("key_tel")["shop_b"].to_dict()
        merged["施工店名"] = merged.apply(
            lambda row: shop_map.get(row["key_tel"]) if pd.isna(row["施工店名"]) else row["施工店名"], axis=1
        )

    merged = merged.drop(columns=["key_tel"])
    merged["施工店名"] = merged["施工店名"].fillna("特定不可")

    # 除外行に「特定不可」を付与して結合
    df_excluded = df_excluded.drop(columns=["key_tel"])
    df_excluded["施工店名"] = "特定不可"
    merged = pd.concat([merged, df_excluded], ignore_index=True)

    # 集計
    calc = merged.copy()
    rating_cols = []
    for col in NEW_COL_NAMES:
        if col in calc.columns:
            calc[col] = calc[col].apply(extract_rating)
            rating_cols.append(col)

    if not rating_cols:
        raise ValueError("集計対象の列が見つかりませんでした。")

    summary = pd.merge(
        calc.groupby("施工店名")[rating_cols].mean(),
        calc.groupby("施工店名").size().rename("アンケート件数"),
        left_index=True, right_index=True,
    ).round(1)
    summary["平均評価"] = summary[rating_cols].mean(axis=1).round(1)
    summary = summary.reset_index()
    summary = summary[["施工店名"] + rating_cols + ["平均評価", "アンケート件数"]]
    summary["_unk"] = summary["施工店名"].apply(lambda x: 1 if x == "不明" else 0)
    summary = summary.sort_values(["_unk", "アンケート件数"], ascending=[True, False]).drop(columns=["_unk"])

    # Excel 出力（一時ファイル）
    base_name = output_name if output_name else f"口コミ_ローデータ_{sheet_name}"
    output_filename = f"{base_name}.xlsx"
    output_path     = Path(tempfile.gettempdir()) / output_filename
    # 列順: 回答日(A) | 電話番号(B) | 施工店名(C) | コメント(D) | 評価5列(E~I)
    col_order = ["回答日", "電話番号", "施工店名", "コメント"] + NEW_COL_NAMES
    col_order = [c for c in col_order if c in merged.columns]
    merged = merged[col_order]

    # 回答日で昇順ソート
    merged = merged.sort_values("回答日", ascending=True).reset_index(drop=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        merged.to_excel(writer, sheet_name="ローデータ", index=False)

    # Google Sheets へアップロード
    if SPREADSHEET_KEY:
        upload_to_gsheet(summary, SPREADSHEET_KEY, sheet_name)

    # NaN を None に変換してJSONシリアライズ可能にする
    summary_records = summary.where(summary.notna(), other=None).to_dict(orient="records")

    return {
        "sheet_name":      sheet_name,
        "output_filename": output_filename,
        "output_path":     str(output_path),
        "summary":         summary_records,
        "total_reviews":   len(df_review),
        "matched":         int((merged["施工店名"] != "不明").sum()),
    }


# ==============================================================================
# API エンドポイント
# ==============================================================================

@app.get("/")
def root():
    return FileResponse("static/index.html")


@app.post("/api/run")
async def api_run(
    files:       list[UploadFile] = File(...),
    date_from:   str = Form(""),
    date_to:     str = Form(""),
    output_name: str = Form(""),
):
    import traceback
    file_list = [(await f.read(), f.filename) for f in files]
    try:
        result = run_aggregation(file_list, date_from.strip(), date_to.strip(), output_name.strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"予期しないエラー: {type(e).__name__}: {e}")
    return JSONResponse(result)


@app.get("/api/download/{filename}")
def api_download(filename: str):
    from urllib.parse import quote
    path = Path(tempfile.gettempdir()) / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="ファイルが見つかりません。")
    encoded = quote(filename, safe="")
    headers = {"Content-Disposition": f"attachment; filename*=UTF-8''{encoded}"}
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
