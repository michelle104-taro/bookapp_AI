import os
import re
import json
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher

import requests
import streamlit as st
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ★★★ 発行したGASのURL ★★★
GAS_URL = "https://script.google.com/macros/s/AKfycbyjCGR7_X5dDvnHjsK-CosihwtmeQqS_Vo4xSWVKQiNY8k8nwBCdjv05yx4XA7rCA_HrA/exec"

VERIFY_SSL = False
PLACEHOLDER_IMG = "https://placehold.co/130x180/e2e8f0/a0aec0?text=No+Image"

st.set_page_config(page_title="My Bookshelf", layout="wide")

# ==========================================
# 🔑 Gemini API設定
# ==========================================
def load_api_key():
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    try:
        return st.secrets.get("GEMINI_API_KEY", "")
    except Exception:
        return ""

GEMINI_API_KEY = load_api_key() or ""

GEMINI_MODELS = [
    ("gemini-3.5-flash-lite", "minimal"),
    ("gemini-3.1-flash-lite", "minimal"),
    ("gemini-3.8-flash", "low"),
    ("gemini-1.5-flash", ""),
]
GEMINI_MAX_OUTPUT_TOKENS = 1500
GEMINI_PER_TRY_TIMEOUT = 25
GEMINI_TOTAL_BUDGET = 40

# ==========================================
# 🎨 ベースCSS（アプリ全体の文字色・ボタン保護）
# ==========================================
st.markdown("""
<style>
/* アプリ全体の背景と基本の文字色 */
.stApp {
    background-color: #f9f6f0 !important;
}

/* 基本の文字色を強制的に黒（ダークモードでの白飛び防止） */
p, span, label, h1, h2, h3, h4, h5, h6, li, div {
    color: #2c2c2c !important;
}

/* 🔘 すべてのボタンの配色を強制固定（背景と同化させない） */
button[kind="secondary"] {
    background-color: #ffffff !important;
    border: 1px solid #cccccc !important;
}
button[kind="secondary"] * {
    color: #2c2c2c !important;
}
button[kind="primary"] {
    background-color: #ff4b4b !important;
    border: 1px solid #ff4b4b !important;
}
button[kind="primary"] * {
    color: #ffffff !important;
}

/* 表紙画像の影と角丸 */
div[data-testid="stImage"] img {
    box-shadow: 5px 5px 10px rgba(0,0,0,0.4);
    border-radius: 2px;
}
</style>
""", unsafe_allow_html=True)

if "page" not in st.session_state:
    st.session_state.page = "bookshelf"
if "selected_book" not in st.session_state:
    st.session_state.selected_book = None
if "editing_memo_id" not in st.session_state:
    st.session_state.editing_memo_id = None
if "confirm_delete_id" not in st.session_state:
    st.session_state.confirm_delete_id = None
if "flash" not in st.session_state:
    st.session_state.flash = ""

# ==========================================
# 共通関数
# ==========================================
def fetch_books():
    try:
        res = requests.get(GAS_URL, params={"action": "getBooks"}, timeout=10, verify=VERIFY_SSL)
        return res.json()
    except Exception:
        return []

def get_field(book, keys, default=""):
    for k in keys:
        v = book.get(k)
        if v:
            return str(v)
    return default

def get_book_id(book):
    if not isinstance(book, dict):
        return ""
    for k in ["本のID", "ID", "id", "bookId", "本のid"]:
        v = book.get(k)
        if v:
            return str(v).strip()
    for k, v in book.items():
        if v and "book_" in str(v):
            return str(v).strip()
    return ""

def gas_post(payload):
    try:
        res = requests.post(GAS_URL, json=payload, timeout=30, allow_redirects=True, verify=VERIFY_SSL)
        if res.status_code != 200:
            return False, f"HTTP {res.status_code}"
        try:
            data = res.json()
        except Exception:
            return True, ""
        if isinstance(data, dict) and data.get("status") == "error":
            return False, data.get("message", "GAS側でエラーが発生しました")
        return True, ""
    except Exception as e:
        return False, str(e)

# ---------- メモ・Insight（GAS連携）----------
def fetch_memos(book_id):
    try:
        res = requests.get(GAS_URL, params={"action": "getMemos", "bookId": str(book_id)}, timeout=10, verify=VERIFY_SSL)
        if res.status_code != 200:
            return []
        data = res.json()
        return data if isinstance(data, list) else []
    except Exception:
        return []

def add_memo(book_id, memo_text):
    return gas_post({"action": "addMemo", "bookId": str(book_id), "memo": memo_text})

def update_memo(memo_id, memo_text):
    return gas_post({"action": "updateMemo", "memoId": memo_id, "memo": memo_text})

def delete_memo(memo_id):
    return gas_post({"action": "deleteMemo", "memoId": memo_id})

def fetch_insight(book_id):
    try:
        res = requests.get(GAS_URL, params={"action": "getInsight", "bookId": str(book_id)}, timeout=10, verify=VERIFY_SSL)
        data = res.json()
        if isinstance(data, dict):
            return data.get("insight", "") or "", data.get("updated", "") or ""
    except Exception:
        pass
    return "", ""

def save_insight(book_id, insight_text):
    return gas_post({"action": "saveInsight", "bookId": str(book_id), "insight": insight_text})

# ==========================================
# 🤖 Gemini呼び出し
# ==========================================
def gemini_generate(prompt, json_mode=False, max_tokens=None,
                    per_try_timeout=None, total_budget=None):
    per_try_timeout = per_try_timeout or GEMINI_PER_TRY_TIMEOUT
    total_budget = total_budget or GEMINI_TOTAL_BUDGET
    max_tokens = max_tokens or GEMINI_MAX_OUTPUT_TOKENS
    if not GEMINI_API_KEY:
        return None, "GEMINI_API_KEYが設定されていません。", ""

    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    start = time.time()
    deadline = start + total_budget
    errors = []

    for model, level in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        use_thinking = bool(level)
        use_json = json_mode
        tries = 0
        while tries < 2:
            remaining = deadline - time.time()
            if remaining < 2:
                return None, f"{total_budget}秒の持ち時間内に完了できませんでした → " + " / ".join(errors), ""

            gen_config = {
                "temperature": 0.2 if json_mode else 0.3,
                "maxOutputTokens": max_tokens,
            }
            if use_thinking:
                gen_config["thinkingConfig"] = {"thinkingLevel": level}
            if use_json:
                gen_config["responseMimeType"] = "application/json"
            body = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": gen_config,
            }
            try:
                res = requests.post(url, headers=headers, json=body,
                                    timeout=min(per_try_timeout, remaining),
                                    verify=VERIFY_SSL)
            except requests.exceptions.Timeout:
                errors.append(f"{model}: {int(min(per_try_timeout, remaining))}秒で見切り")
                break
            except Exception as e:
                return None, f"通信エラー: {e}", ""

            if res.status_code == 400:
                low = res.text.lower()
                if use_thinking and "thinking" in low:
                    use_thinking = False
                    continue
                if use_json and ("mime" in low or "json" in low):
                    use_json = False
                    continue

            if res.status_code in (429, 500, 503):
                errors.append(f"{model}: 混雑({res.status_code})")
                tries += 1
                time.sleep(1)
                continue

            if res.status_code == 404:
                errors.append(f"{model}: 利用不可(404)")
                break

            if res.status_code != 200:
                return None, f"Gemini APIエラー {res.status_code}: {res.text[:300]}", ""

            try:
                parts = res.json()["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
            except (KeyError, IndexError, TypeError):
                return None, "Geminiから回答が得られませんでした（出力上限や安全フィルタの可能性）。", ""
            if not text.strip():
                return None, "Geminiの回答が空でした。もう一度試してね。", ""

            info = f"{model} / {round(time.time() - start, 1)}秒"
            return text, "", info

    return None, "どのモデルも応答できませんでした → " + " / ".join(errors), ""

def call_gemini(prompt):
    text, err, info = gemini_generate(prompt)
    if text:
        st.session_state.gemini_info = info
    return text, err

def build_insight_prompt(title, author, summary, memos):
    memo_lines = "\n".join(f"- {m.get('メモ','')}" for m in memos)
    return f"""読書コーチとして、下の情報から超端的なInsightを日本語Markdownで作成してください。
全体で400字以内。前置き・あいさつは不要です。

【本】{title}（{author}）
【概要】{summary or '（なし）'}
【読者のメモ】
{memo_lines or '（なし）'}

# 出力形式
## ① 3行要約
## ② コアインサイト
## ③ 実践アクション
"""

# ==========================================
# 🌟 書籍データ＆楽天ブックスしらみつぶし検索エンジン
# ==========================================
def normalize(text):
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s\-_:：・、。,，!！?？「」『』()（）]", "", text)

def titles_match(a, b):
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.6

def to_isbn13(raw):
    s = re.sub(r"[^0-9Xx]", "", str(raw or ""))
    if len(s) == 13 and s.isdigit():
        return s
    if len(s) == 10:
        core = "978" + s[:9]
        if not core.isdigit():
            return ""
        total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
        return core + str((10 - total % 10) % 10)
    return ""

def is_valid_isbn13(s):
    if not re.fullmatch(r"97[89]\d{10}", s or ""):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(s[:12]))
    return (10 - total % 10) % 10 == int(s[12])

def image_exists(url):
    if not url:
        return False
    try:
        r = requests.head(url, timeout=2, verify=VERIFY_SSL)
        if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
            return True
        r = requests.get(url, timeout=2, verify=VERIFY_SSL, stream=True)
        ok = r.status_code == 200 and "image" in r.headers.get("Content-Type", "")
        r.close()
        return ok
    except Exception:
        return False

def lookup_google_books(title, author=""):
    try:
        q = f"intitle:{title}"
        if author:
            q += f"+inauthor:{author}"
        url = f"https://www.googleapis.com/books/v1/volumes?q={q}&maxResults=3&langRestrict=ja"
        res = requests.get(url, timeout=4)
        if res.status_code == 200:
            data = res.json()
            for item in data.get("items", []):
                info = item.get("volumeInfo", {})
                t = info.get("title", "")
                if titles_match(title, t):
                    isbn = ""
                    for ident in info.get("industryIdentifiers", []):
                        if ident.get("type") in ["ISBN_13", "ISBN_10"]:
                            isbn = to_isbn13(ident.get("identifier", ""))
                            if isbn:
                                break
                    img_links = info.get("imageLinks", {})
                    img_url = img_links.get("thumbnail") or img_links.get("smallThumbnail") or ""
                    if img_url.startswith("http:"):
                        img_url = img_url.replace("http:", "https:")
                    
                    return {
                        "isbn": isbn,
                        "imageUrl": img_url,
                        "summary": info.get("description", ""),
                        "author": ", ".join(info.get("authors", []))
                    }
    except Exception:
        pass
    return None

def openbd_get(isbn):
    try:
        res = requests.get("https://api.openbd.jp/v1/get", params={"isbn": isbn}, timeout=4, verify=VERIFY_SSL)
        if res.status_code != 200:
            return None
        data = res.json()
        if not data or not data[0]:
            return None
        d = data[0]
        s = d.get("summary") or {}
        summary_text = ""
        try:
            for t in d["onix"]["CollateralDetail"]["TextContent"]:
                if t.get("Text"):
                    summary_text = t["Text"]
                    break
        except (KeyError, TypeError):
            pass
        return {
            "title": s.get("title", "") or "",
            "author": s.get("author", "") or "",
            "cover": s.get("cover", "") or "",
            "summary": summary_text,
        }
    except Exception:
        return None

def lookup_isbn_ndl(title, author):
    if not title:
        return ""
    try:
        res = requests.get("https://ndlsearch.ndl.go.jp/api/opensearch",
                           params={"title": title, "cnt": 20},
                           timeout=5, verify=VERIFY_SSL)
        if res.status_code != 200:
            return ""
        root = ET.fromstring(res.content)
        xsi_type = "{http://www.w3.org/2001/XMLSchema-instance}type"
        dc = {"dc": "http://purl.org/dc/elements/1.1/"}
        author_key = normalize(author)
        best, best_score = "", -1
        for item in root.iter("item"):
            isbn = ""
            for ident in item.findall("dc:identifier", dc):
                if ident.get(xsi_type) == "dcndl:ISBN" and ident.text:
                    isbn = to_isbn13(ident.text)
                    if isbn:
                        break
            if not isbn:
                continue
            t = item.findtext("title") or ""
            if not titles_match(title, t):
                continue
            a = normalize(item.findtext("author") or "")
            score = 1
            if author_key and author_key in a:
                score += 2
            if normalize(t) == normalize(title):
                score += 1
            if score > best_score:
                best, best_score = isbn, score
        return best
    except Exception:
        return ""

def resolve_cover_with_log(isbn, openbd_cover, title="", author=""):
    logs = []
    
    if isbn:
        dirs_to_check = []
        if len(isbn) >= 13:
            dirs_to_check.append(isbn[-4:])
            dirs_to_check.append(isbn[5:9])
            dirs_to_check.append(isbn[4:8])
        
        known_dirs = ["5819", "6362", "6587", "7154", "7324", "7571"]
        for d in known_dirs:
            if d not in dirs_to_check:
                dirs_to_check.append(d)

        suffixes = ["_1_4.jpg", "_1_3.jpg", "_1_2.jpg", "_1.jpg", ".jpg"]

        for d in dirs_to_check:
            for suf in suffixes:
                url = f"https://shop.r10s.jp/book/cabinet/{d}/{isbn}{suf}"
                logs.append(f"🔍 楽天ブックス確認 [dir:{d}, suf:{suf}]: {url}")
                if image_exists(url):
                    logs.append(f"🟢 [成功] 楽天ブックスの画像にヒットしました ({url})")
                    return url, logs
                else:
                    logs.append(f"🔴 [不一致/次へ]")

    if openbd_cover:
        logs.append(f"🔍 OpenBD直接画像URLを確認中: {openbd_cover}")
        if image_exists(openbd_cover):
            logs.append(f"🟢 [成功] OpenBD直接画像を採用しました")
            return openbd_cover, logs

    if isbn:
        url = f"https://ndlsearch.ndl.go.jp/thumbnail/{isbn}.jpg"
        logs.append(f"🔍 国立国会図書館サムネイルを確認中: {url}")
        if image_exists(url):
            logs.append(f"🟢 [成功] 国立国会図書館サムネイルを採用しました")
            return url, logs

    if title:
        logs.append(f"🔍 Google Books APIへタイトル検索を実行中...")
        gb_data = lookup_google_books(title, author)
        if gb_data and gb_data.get("imageUrl"):
            gb_url = gb_data["imageUrl"]
            logs.append(f"🔍 Google Books画像URLを確認中: {gb_url}")
            if image_exists(gb_url):
                logs.append(f"🟢 [成功] Google Books API画像を採用しました")
                return gb_url, logs

    logs.append(f"⚠️ 【結論】有効な画像が見つからなかったため、No Imageになります。")
    return "", logs

def enrich_candidate(c):
    title = c["title"]
    author = c["author"]
    isbn = ""
    info = None

    isbn_ai = to_isbn13(c.get("isbn", ""))
    if isbn_ai and is_valid_isbn13(isbn_ai):
        d = openbd_get(isbn_ai)
        if d and d["title"] and titles_match(title, d["title"]):
            isbn, info = isbn_ai, d

    if not isbn:
        isbn_ndl = lookup_isbn_ndl(title, author)
        if isbn_ndl:
            isbn = isbn_ndl
            d = openbd_get(isbn_ndl)
            if d:
                info = d

    gb_info = None
    if not isbn or not (info and info.get("cover")):
        gb_info = lookup_google_books(title, author)
        if gb_info and gb_info.get("isbn") and not isbn:
            isbn = gb_info["isbn"]

    verified = bool(isbn)
    openbd_cover = info["cover"] if info else ""
    cover, cover_logs = resolve_cover_with_log(isbn, openbd_cover, title, author)

    summary = ""
    if info and info.get("summary"):
        summary = info["summary"]
    elif gb_info and gb_info.get("summary"):
        summary = gb_info["summary"]
    elif c.get("summary"):
        summary = "【AI推定】" + c["summary"]

    final_author = author
    if info and info.get("author"):
        final_author = info["author"]
    elif gb_info and gb_info.get("author"):
        final_author = gb_info["author"]

    return {
        "title": title,
        "author": final_author or "著者不明",
        "isbn": isbn,
        "imageUrl": cover or PLACEHOLDER_IMG,
        "has_cover": bool(cover),
        "summary": summary,
        "verified": verified,
        "logs": cover_logs,
    }

def safe_enrich(c):
    try:
        return enrich_candidate(c)
    except Exception as e:
        return {
            "title": c["title"], "author": c["author"] or "著者不明", "isbn": "",
            "imageUrl": PLACEHOLDER_IMG, "has_cover": False,
            "summary": ("【AI推定】" + c["summary"]) if c.get("summary") else "",
            "verified": False,
            "logs": [f"❌ 例外発生: {e}"],
        }

SEARCH_PROMPT = """あなたは日本の書籍に詳しいベテラン書店員です。
ユーザーの入力に当てはまる、実在する日本語の書籍を、関連度が高い順に最大5冊挙げてください。

# ルール
- 実在すると確信できる書籍だけを挙げる。自信がないものは含めない（0冊でもよい）
- 入力が特定の1冊を明確に指している場合は、その1冊だけでよい（同じ本の別の版は含めない）
- title は正式なタイトル（サブタイトルは含めない）、author は主な著者名のみ
- isbn は13桁を確実に知っている場合のみ記入。少しでも不確かなら "" にする（推測や創作は禁止）
- summary は本の内容を60字以内で。不確かなら ""
- 出力はJSON配列のみ。前置き・解説・コードブロックは不要

# 出力例
[{"title": "…", "author": "…", "isbn": "…", "summary": "…"}]

# ユーザーの入力
{KEYWORD}
"""

def parse_json_array(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I).strip()
    data = None
    try:
        data = json.loads(t)
    except Exception:
        m = re.search(r"\[.*\]", t, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None
    if isinstance(data, dict):
        data = data.get("books") or data.get("items") or [data]
    return data if isinstance(data, list) else None

def search_books_ai(keyword):
    prompt = SEARCH_PROMPT.replace("{KEYWORD}", keyword)
    text, err, info = gemini_generate(prompt, json_mode=True, max_tokens=2000)
    if not text:
        return [], err, ""

    data = parse_json_array(text)
    if data is None:
        return [], "AIの回答を読み取れませんでした。もう一度試してね。", info

    cands = []
    for item in data[:5]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        author = item.get("author", "")
        if isinstance(author, list):
            author = "、".join(str(a) for a in author)
        cands.append({
            "title": title,
            "author": str(author).strip(),
            "isbn": str(item.get("isbn", "")),
            "summary": str(item.get("summary", "")).strip(),
        })
    if not cands:
        return [], "", info

    with ThreadPoolExecutor(max_workers=5) as ex:
        enriched = list(ex.map(safe_enrich, cands))

    results, seen = [], set()
    for b in enriched:
        key = b["isbn"] or normalize(b["title"])
        if key in seen:
            continue
        seen.add(key)
        results.append(b)

    n_ok = sum(1 for b in results if b["verified"] or b["has_cover"])
    return results, "", f"{info} / 書誌・画像データ確認 {n_ok}/{len(results)}冊"

# ==========================================
# 画面構成
# ==========================================
if st.session_state.page == "bookshelf":
    # 📚 本棚画面だけに「木目調の棚」のCSSを追加読込する
    st.markdown("""
    <style>
    /* 🪵 本棚の木目調ブロック（茶色の棚）のスタイル適用 */
    div[data-testid="stHorizontalBlock"] {
        background-color: #C19A6B !important; 
        padding: 20px 10px 15px 10px !important;
        border-bottom: 12px solid #8B5A2B !important; 
        box-shadow: inset 0px -10px 15px rgba(0,0,0,0.15), 0px 5px 10px rgba(0,0,0,0.2) !important;
        border-radius: 3px !important;
        margin-bottom: 25px !important;
    }

    /* 本棚の中の文字（タイトル等）を白にして見やすく */
    div[data-testid="stHorizontalBlock"] p,
    div[data-testid="stHorizontalBlock"] span,
    div[data-testid="stHorizontalBlock"] h1,
    div[data-testid="stHorizontalBlock"] div {
        color: #ffffff !important;
    }
    
    /* ⚠️ ただし、本棚の中のボタンの文字は「黒」を死守する */
    div[data-testid="stHorizontalBlock"] button * {
        color: #2c2c2c !important;
    }

    /* 🔍 エクスパンダー（検索結果など）の背景は木目にしない */
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] {
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
        padding: 0 !important;
    }
    
    /* 検索結果の文字は黒に戻す */
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] p,
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] span {
        color: #2c2c2c !important;
    }
    </style>
    """, unsafe_allow_html=True)

    st.title("📚 わたしの本棚")

    with st.expander("➕ AI書店員に本を探してもらう", expanded=True):
        st.write("本のタイトルや著者名を入力すると、ISBNを特定し、楽天ブックスの各種フォルダ＆サフィックスパターンをしらみつぶしに総当たりして表紙画像を取得します！")

        search_keyword = st.text_input("検索キーワード",
                                       placeholder="例：嫌われる勇気 / 付加価値のつくりかた / シン・営業力")
        if st.button("🔍 AIに候補を探してもらう"):
            if search_keyword.strip():
                with st.spinner("AI書店員が書籍をピックアップし、楽天ブックスをしらみつぶし総当たり調査中..."):
                    results, err_msg, info = search_books_ai(search_keyword.strip())
                    st.session_state.search_results = results
                    st.session_state.search_error = err_msg
                    st.session_state.search_info = info
            else:
                st.warning("検索キーワードを入力してね。")

        if "search_results" in st.session_state:
            results = st.session_state.search_results
            err_msg = st.session_state.get("search_error", "")
            info = st.session_state.get("search_info", "")

            if results:
                st.write("---")
                st.write(f"🎯 **AIが {len(results)} 冊の本をピックアップしたよ！追加したい本を選んでね：**")
                if info:
                    st.caption(f"⚡ 検索: {info}")

                cols = st.columns(2)
                for idx, book in enumerate(results):
                    with cols[idx % 2]:
                        with st.container(border=True):
                            st.image(book["imageUrl"], width=100)
                            st.markdown(f"**{book['title']}**")
                            st.write(f"✍️ {book['author']}")
                            if book["verified"] or book["has_cover"]:
                                st.caption("✅ 書誌・表紙画像確認済み")
                            else:
                                st.caption("⚠️ AI推定（表紙未確認）。表紙画像が無い時は下で指定してね")
                            if book["summary"]:
                                s = book["summary"]
                                st.caption(s[:100] + "..." if len(s) > 100 else s)

                            custom_img = ""
                            if not book["has_cover"]:
                                custom_img = st.text_input("🖼️ 表紙画像のURL（任意）",
                                                           key=f"img_{idx}",
                                                           placeholder="空欄なら「画像なし」で登録")

                            with st.popover("🔍 画像取得のプロセスログを見る"):
                                for log_line in book.get("logs", []):
                                    st.code(log_line)

                            if st.button("✅ この本を本棚に追加", key=f"add_book_{idx}"):
                                payload = {
                                    "action": "addBook",
                                    "title": book.get("title", ""),
                                    "author": book.get("author", ""),
                                    "imageUrl": custom_img.strip() or book.get("imageUrl", PLACEHOLDER_IMG),
                                    "summary": book.get("summary", "") or "概要なし",
                                }
                                with st.spinner("スプレッドシートに追加中..."):
                                    ok, err = gas_post(payload)
                                if ok:
                                    st.session_state.flash = f"「{book['title']}」を本棚に追加したよ！"
                                    del st.session_state.search_results
                                    st.rerun()
                                else:
                                    st.error(f"追加エラー: {err}")
            else:
                if err_msg:
                    st.error(err_msg)
                else:
                    st.info("AIが本を見つけられなかったよ。キーワードを変えて試してね。")

    with st.popover("📝 見つからない時は手動で登録する"):
        manual_title = st.text_input("タイトル（必須）")
        manual_author = st.text_input("著者名")
        manual_image = st.text_input("画像URL（Web上の画像リンク）")
        manual_summary = st.text_area("概要・メモ")
        if st.button("手動で本棚に追加"):
            if manual_title.strip():
                ok, err = gas_post({
                    "action": "addBook",
                    "title": manual_title.strip(),
                    "author": manual_author.strip() or "著者不明",
                    "imageUrl": manual_image.strip() or PLACEHOLDER_IMG,
                    "summary": manual_summary.strip() or "概要なし",
                })
                if ok:
                    st.session_state.flash = f"「{manual_title.strip()}」を手動で追加したよ！"
                    st.rerun()
                else:
                    st.error(f"追加エラー: {err}")
            else:
                st.warning("タイトルを入力してね。")

    if st.session_state.flash:
        st.success(st.session_state.flash)
        st.session_state.flash = ""

    st.write("---")

    # ==========================================
    # 📚 本棚リストの表示
    # ==========================================
    books = fetch_books()
    if isinstance(books, list) and len(books) > 0:
        cols_per_row = 4
        for i in range(0, len(books), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                if i + j < len(books):
                    book = books[i + j]
                    with cols[j]:
                        img_url = str(book.get("画像URL", "") or "").strip()
                        if img_url.startswith("http"):
                            st.image(img_url, width=120)
                        else:
                            st.image(PLACEHOLDER_IMG, width=120)

                        st.write(f"**{book.get('タイトル', 'タイトル不明')}**")
                        if st.button("詳細・メモ", key=f"btn_{get_book_id(book) or i}_{j}"):
                            st.session_state.selected_book = book
                            st.session_state.editing_memo_id = None
                            st.session_state.confirm_delete_id = None
                            st.session_state.page = "detail"
                            st.rerun()
    else:
        st.info("まだ本棚に本がないよ。上の検索欄から本を探して追加してみよう！")

# ==========================================
# 📖 詳細・メモ画面
# ==========================================
elif st.session_state.page == "detail":
    book = st.session_state.selected_book
    book_id = get_book_id(book)
    title = get_field(book, ["タイトル"], "タイトル不明")
    author = get_field(book, ["著者", "著者名", "作者"], "著者不明")
    summary = get_field(book, ["概要", "あらすじ", "サマリー"], "")

    if st.button("⬅️ 本棚に戻る"):
        st.session_state.page = "bookshelf"
        st.session_state.selected_book = None
        st.session_state.editing_memo_id = None
        st.session_state.confirm_delete_id = None
        st.rerun()

    memo_key = f"memos_{book_id}"
    insight_key = f"insight_{book_id}"
    insight_time_key = f"insight_time_{book_id}"
    show_key = f"show_insight_{book_id}"

    if memo_key not in st.session_state:
        st.session_state[memo_key] = fetch_memos(book_id)
    if insight_key not in st.session_state:
        text_i, time_i = fetch_insight(book_id)
        st.session_state[insight_key] = text_i
        st.session_state[insight_time_key] = time_i

    memos = st.session_state[memo_key]
    insight_text = st.session_state[insight_key]

    if st.session_state.flash:
        st.success(st.session_state.flash)
        st.session_state.flash = ""

    col_main, col_btn = st.columns([4, 1])
    with col_main:
        st.title(f"📖 {title}")
        st.write(f"✍️ **著者:** {author}")
        st.markdown("##### 📄 概要")
        st.write(summary or "概要データがありません。")
    with col_btn:
        gen_clicked = st.button("✨ Insight", use_container_width=True, type="primary")
        view_clicked = st.button("📋 Insight結果", use_container_width=True, disabled=not insight_text)

    if gen_clicked:
        with st.spinner("Geminiが読書メモを整理しています..."):
            prompt = build_insight_prompt(title, author, summary, memos)
            new_text, err = call_gemini(prompt)
        if new_text:
            ok, save_err = save_insight(book_id, new_text)
            st.session_state[insight_key] = new_text
            st.session_state[insight_time_key] = time.strftime("%Y/%m/%d %H:%M")
            st.session_state[show_key] = True
            st.session_state.flash = (
                "Insightを生成して保存したよ！" if ok
                else f"Insightは生成できたけど、保存に失敗したよ: {save_err}"
            )
            st.rerun()
        else:
            st.error(err)

    if view_clicked:
        st.session_state[show_key] = not st.session_state.get(show_key, False)

    if insight_text and st.session_state.get(show_key, False):
        with st.container(border=True):
            st.markdown("### ✨ AI Insight")
            st.caption(f"🕒 更新: {st.session_state.get(insight_time_key, '')}")
            gem_info = st.session_state.get("gemini_info", "")
            if gem_info:
                st.caption(f"⚡ 生成: {gem_info}")
            st.markdown(insight_text)

    st.divider()

    st.subheader("✍️ 読書メモ")
    with st.form("memo_form", clear_on_submit=True):
        memo_text = st.text_area(
            "気づき・引用・仕事や生活への活かし方を自由に書いてね",
            height=130,
            placeholder="例：「課題の分離」は職場の人間関係にも使える。自分が変えられるのは自分の行動だけ。",
        )
        submitted = st.form_submit_button("📝 Input")

    if submitted:
        if memo_text.strip():
            ok, err = add_memo(book_id, memo_text.strip())
            if ok:
                st.session_state.pop(memo_key, None)
                st.session_state.flash = "メモを保存したよ！"
                st.rerun()
            else:
                st.error(f"保存に失敗したよ: {err}")
        else:
            st.warning("メモが空だよ。")

    st.markdown(f"##### 🗂️ メモ一覧（{len(memos)}件）")
    if memos:
        for m in reversed(memos):
            memo_id = str(m.get("メモID", ""))
            with st.container(border=True):
                st.caption(f"🕒 {m.get('日時', '')}")

                if st.session_state.editing_memo_id == memo_id:
                    edited = st.text_area("メモを編集", value=m.get("メモ", ""), height=110, key=f"edit_text_{memo_id}")
                    c1, c2, _ = st.columns([1, 1, 4])
                    with c1:
                        if st.button("✅ 更新", key=f"upd_{memo_id}"):
                            if edited.strip():
                                ok, err = update_memo(memo_id, edited.strip())
                                if ok:
                                    st.session_state.pop(memo_key, None)
                                    st.session_state.editing_memo_id = None
                                    st.session_state.flash = "メモを更新したよ！"
                                    st.rerun()
                                else:
                                    st.error(f"更新に失敗したよ: {err}")
                            else:
                                st.warning("メモが空だよ。")
                    with c2:
                        if st.button("✖️ キャンセル", key=f"cancel_{memo_id}"):
                            st.session_state.editing_memo_id = None
                            st.rerun()

                elif st.session_state.confirm_delete_id == memo_id:
                    st.write(m.get("メモ", ""))
                    st.warning("This memo will be deleted. (Cannot be undone)")
                    c1, c2, _ = st.columns([1, 1, 4])
                    with c1:
                        if st.button("はい", key=f"yes_{memo_id}", type="primary"):
                            ok, err = delete_memo(memo_id)
                            if ok:
                                st.session_state.pop(memo_key, None)
                                st.session_state.confirm_delete_id = None
                                st.session_state.flash = "メモを削除したよ。"
                                st.rerun()
                            else:
                                st.error(f"削除に失敗したよ: {err}")
                    with c2:
                        if st.button("いいえ", key=f"no_{memo_id}"):
                            st.session_state.confirm_delete_id = None
                            st.rerun()

                else:
                    st.write(m.get("メモ", ""))
                    c1, c2, _ = st.columns([1, 1, 4])
                    with c1:
                        if st.button("✏️ 編集", key=f"edit_{memo_id}"):
                            st.session_state.editing_memo_id = memo_id
                            st.session_state.confirm_delete_id = None
                            st.rerun()
                    with c2:
                        if st.button("🗑️ 削除", key=f"del_{memo_id}"):
                            st.session_state.confirm_delete_id = memo_id
                            st.session_state.editing_memo_id = None
                            st.rerun()
    else:
        st.info("まだメモがないよ。最初の気づきを書いてみよう！")
