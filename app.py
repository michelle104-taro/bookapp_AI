import html
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

# ★★★ 改善機能のスイッチ ★★★
ENABLE_DESIGN_TWEAKS = True   # 見た目の仕上げ。崩れる時は False で元の見た目に戻る
USE_BUNDLE_API = False        # GASに getBookBundle を追加したら True（メモ＋Insightを1回で取得）
BOOKS_CACHE_SECONDS = 600     # 本棚データの自動更新間隔（秒）

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
    ("gemini-2.5-flash-lite", ""),
]
GEMINI_MAX_OUTPUT_TOKENS = 1500
GEMINI_PER_TRY_TIMEOUT = 25
GEMINI_TOTAL_BUDGET = 40

# ==========================================
# 🎨 ベースCSS（アプリ全体の文字色・ボタン保護）
# ==========================================
st.markdown("""
<style>
.stApp {
    background-color: #f9f6f0 !important;
}

p, span, label, h1, h2, h3, h4, h5, h6, li, div {
    color: #2c2c2c !important;
}

button[kind="secondary"], button[kind="secondaryFormSubmit"] {
    background-color: #ffffff !important;
    border: 1px solid #cccccc !important;
}
button[kind="secondary"] *, button[kind="secondaryFormSubmit"] * {
    color: #2c2c2c !important;
}
button[kind="primary"], button[kind="primaryFormSubmit"] {
    background-color: #ff4b4b !important;
    border: 1px solid #ff4b4b !important;
}
button[kind="primary"] *, button[kind="primaryFormSubmit"] * {
    color: #ffffff !important;
}

div[data-testid="stImage"] img {
    box-shadow: 5px 5px 10px rgba(0,0,0,0.4);
    border-radius: 2px;
}
</style>
""", unsafe_allow_html=True)

if ENABLE_DESIGN_TWEAKS:
    st.markdown("""
    <style>
    /* 表紙の縦横比をそろえる（棚が整って見える） */
    div[data-testid="stImage"] img {
        aspect-ratio: 2 / 3;
        object-fit: cover;
    }
    </style>
    """, unsafe_allow_html=True)

# ==========================================
# 状態の初期化
# ==========================================
_defaults = {
    "page": "bookshelf",
    "selected_book": None,
    "editing_memo_id": None,
    "confirm_delete_id": None,
    "confirm_delete_book": False,
    "flash": "",
    "books_cache": None,
    "books_cache_time": 0.0,
    "search_cache": {},
    "restored": False,
}
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ==========================================
# 共通関数
# ==========================================
def fetch_books():
    """成功: 本のリスト / 失敗: None"""
    try:
        res = requests.get(GAS_URL, params={"action": "getBooks"}, timeout=10, verify=VERIFY_SSL)
        data = res.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None

def invalidate_books():
    st.session_state.books_cache = None

def load_books(force=False):
    """本棚データをキャッシュ付きで取得。戻り値: (本のリスト, 取得成功か)"""
    cache = st.session_state.get("books_cache")
    cached_at = st.session_state.get("books_cache_time", 0.0)
    fresh = cache is not None and (time.time() - cached_at) < BOOKS_CACHE_SECONDS
    if fresh and not force:
        return cache, True

    with st.spinner("📚 本棚を読み込み中..."):
        data = fetch_books()
    if data is None:
        return (cache if cache is not None else []), False
    st.session_state.books_cache = data
    st.session_state.books_cache_time = time.time()
    return data, True

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

# ---------- 画面遷移（URLに本のIDを持たせて、F5でも戻れるようにする）----------
def open_book(book):
    st.session_state.selected_book = book
    st.session_state.editing_memo_id = None
    st.session_state.confirm_delete_id = None
    st.session_state.confirm_delete_book = False
    st.session_state.page = "detail"
    bid = get_book_id(book)
    if bid:
        try:
            st.query_params["book"] = bid
        except Exception:
            pass

def close_book():
    st.session_state.page = "bookshelf"
    st.session_state.selected_book = None
    st.session_state.editing_memo_id = None
    st.session_state.confirm_delete_id = None
    st.session_state.confirm_delete_book = False
    try:
        st.query_params.clear()
    except Exception:
        pass

def restore_from_url():
    """F5（再読み込み）しても、開いていた本の画面に戻る"""
    if st.session_state.restored:
        return
    st.session_state.restored = True
    try:
        qb = st.query_params.get("book", "")
    except Exception:
        return
    if not qb:
        return
    books, _ = load_books()
    for b in books:
        if get_book_id(b) == str(qb):
            st.session_state.selected_book = b
            st.session_state.page = "detail"
            return
    try:
        st.query_params.clear()
    except Exception:
        pass

# ---------- 本・メモ・Insight（GAS連携）----------
def delete_book(book_id):
    return gas_post({"action": "deleteBook", "bookId": str(book_id)})

def fetch_memos(book_id):
    """成功: メモのリスト / 失敗: None"""
    try:
        res = requests.get(GAS_URL, params={"action": "getMemos", "bookId": str(book_id)}, timeout=10, verify=VERIFY_SSL)
        if res.status_code != 200:
            return None
        data = res.json()
        return data if isinstance(data, list) else None
    except Exception:
        return None

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

def fetch_detail_data(book_id):
    """メモとInsightを同時に取得。戻り値: (メモ or None, Insight文, 更新日時)"""
    if USE_BUNDLE_API:
        try:
            res = requests.get(GAS_URL, params={"action": "getBookBundle", "bookId": str(book_id)},
                               timeout=10, verify=VERIFY_SSL)
            data = res.json()
            if isinstance(data, dict) and isinstance(data.get("memos"), list):
                ins = data.get("insight") or {}
                return data["memos"], (ins.get("insight", "") or ""), (ins.get("updated", "") or "")
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_memos = ex.submit(fetch_memos, book_id)
        f_ins = ex.submit(fetch_insight, book_id)
        memos = f_memos.result()
        text_i, time_i = f_ins.result()
    return memos, text_i, time_i

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
# 🌟 書籍データ＆表紙の検索エンジン（同時実行で高速化）
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

def first_existing(urls, max_workers=8):
    urls = [u for u in urls if u]
    if not urls:
        return ""
    with ThreadPoolExecutor(max_workers=min(max_workers, len(urls))) as ex:
        flags = list(ex.map(image_exists, urls))
    for u, ok in zip(urls, flags):
        if ok:
            return u
    return ""

def lookup_google_books(title, author=""):
    try:
        q = f"intitle:{title}"
        if author:
            q += f" inauthor:{author}"
        res = requests.get("https://www.googleapis.com/books/v1/volumes",
                           params={"q": q, "maxResults": 3, "langRestrict": "ja"},
                           timeout=4, verify=VERIFY_SSL)
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

def resolve_cover_with_log(isbn, openbd_cover, title="", author="", gb_info=None, gb_tried=False):
    logs = []
    suffixes = ["_1_4.jpg", "_1_3.jpg", "_1_2.jpg", "_1.jpg", ".jpg"]

    if isbn:
        stage_a = []
        if len(isbn) >= 13:
            stage_a += [f"https://shop.r10s.jp/book/cabinet/{isbn[-4:]}/{isbn}{s}" for s in suffixes]
        if openbd_cover:
            stage_a.append(openbd_cover)
        stage_a.append(f"https://ndlsearch.ndl.go.jp/thumbnail/{isbn}.jpg")
        url = first_existing(stage_a)
        if url:
            return url, logs

        stage_b = []
        if len(isbn) >= 13:
            for d in (isbn[5:9], isbn[4:8]):
                if d != isbn[-4:]:
                    stage_b += [f"https://shop.r10s.jp/book/cabinet/{d}/{isbn}{s}" for s in suffixes]
        url = first_existing(stage_b)
        if url:
            return url, logs
    elif openbd_cover and image_exists(openbd_cover):
        return openbd_cover, logs

    if title:
        if gb_info is None and not gb_tried:
            gb_info = lookup_google_books(title, author)
        if gb_info and gb_info.get("imageUrl") and image_exists(gb_info["imageUrl"]):
            return gb_info["imageUrl"], logs

    return "", logs

def enrich_candidate(c):
    title = c["title"]
    author = c["author"]
    isbn = ""
    info = None

    isbn_ai = to_isbn13(c.get("isbn", ""))
    ai_ok = bool(isbn_ai and is_valid_isbn13(isbn_ai))
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_bd = ex.submit(openbd_get, isbn_ai) if ai_ok else None
        f_ndl = ex.submit(lookup_isbn_ndl, title, author)
        d_ai = f_bd.result() if f_bd else None
        isbn_ndl = f_ndl.result()

    if d_ai and d_ai["title"] and titles_match(title, d_ai["title"]):
        isbn, info = isbn_ai, d_ai

    if not isbn and isbn_ndl:
        isbn = isbn_ndl
        d = openbd_get(isbn_ndl)
        if d:
            info = d

    gb_info = None
    gb_tried = False
    if not isbn or not (info and info.get("cover")):
        gb_info = lookup_google_books(title, author)
        gb_tried = True
        if gb_info and gb_info.get("isbn") and not isbn:
            isbn = gb_info["isbn"]

    verified = bool(isbn)
    openbd_cover = info["cover"] if info else ""
    cover, cover_logs = resolve_cover_with_log(isbn, openbd_cover, title, author,
                                               gb_info=gb_info, gb_tried=gb_tried)

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
# 起動時：URLに本のIDがあれば、その画面を復元
# ==========================================
restore_from_url()

# ==========================================
# 画面構成
# ==========================================
if st.session_state.page == "bookshelf":
    # 📚 本棚画面だけに「木目調の棚」のCSSを追加読込する
    st.markdown("""
    <style>
    /* 本棚の木目スタイル */
    div[data-testid="stHorizontalBlock"] {
        background-color: #C19A6B !important; 
        padding: 15px 8px 12px 8px !important;
        border-bottom: 10px solid #8B5A2B !important; 
        box-shadow: inset 0px -10px 15px rgba(0,0,0,0.15), 0px 5px 10px rgba(0,0,0,0.2) !important;
        border-radius: 3px !important;
        margin-bottom: 20px !important;
    }

    div[data-testid="stHorizontalBlock"] p,
    div[data-testid="stHorizontalBlock"] span,
    div[data-testid="stHorizontalBlock"] h1,
    div[data-testid="stHorizontalBlock"] div {
        color: #ffffff !important;
    }

    div[data-testid="stHorizontalBlock"] button * {
        color: #2c2c2c !important;
    }

    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] {
        background-color: transparent !important;
        border: none !important;
        box-shadow: none !important;
        padding: 0 !important;
    }

    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] p,
    div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] span {
        color: #2c2c2c !important;
    }

    /* 📱 スマホ(iPhone 100%文字サイズ)の完全横並び用レスポンシブCSS */
    @media (max-width: 768px) {
        /* 本棚を2列で横並びにする（要素間隔をコンパクトに） */
        div[data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: row !important;
            flex-wrap: wrap !important;
            gap: 4px !important;
        }
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"],
        div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            width: calc(48% - 2px) !important;
            min-width: calc(48% - 2px) !important;
            max-width: calc(48% - 2px) !important;
            flex: 0 0 calc(48% - 2px) !important;
            padding: 0 2px !important;
            margin-bottom: 10px !important;
        }

        /* 本棚内のボタンの余白と幅をコンパクト化 */
        div[data-testid="stHorizontalBlock"] button {
            padding: 4px 8px !important;
            font-size: 0.8rem !important;
            min-height: 0 !important;
        }
        
        /* エクスパンダー内（AI検索結果）は1列に戻す */
        div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] {
            display: flex !important;
            flex-direction: column !important;
        }
        div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] > div[data-testid="column"],
        div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
            width: 100% !important;
            min-width: 100% !important;
            max-width: 100% !important;
        }
    }
    </style>
    """, unsafe_allow_html=True)

    if ENABLE_DESIGN_TWEAKS:
        st.markdown("""
        <style>
        /* 棚板にグラデーション */
        div[data-testid="stHorizontalBlock"] {
            background-image: linear-gradient(180deg, #CFA877 0%, #C19A6B 55%, #B58A57 100%) !important;
        }
        div[data-testid="stExpander"] div[data-testid="stHorizontalBlock"] {
            background-image: none !important;
        }
        /* 本のタイトルは2行まで */
        .book-title {
            font-size: 0.8rem;
            font-weight: 700;
            line-height: 1.3;
            height: 2.6em;
            overflow: hidden;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            margin: 4px 0 4px 0;
        }
        </style>
        """, unsafe_allow_html=True)

    st.title("📚 わたしの本棚")

    # 本棚データ（キャッシュ使用）
    books, books_ok = load_books()
    shelf_keys = {normalize(get_field(b, ["タイトル"])) for b in books if isinstance(b, dict)}

    # ① デフォルトで閉じる
    with st.expander("➕ AI書店員に本を探してもらう", expanded=False):
        with st.form("search_form", clear_on_submit=False):
            search_keyword = st.text_input("本のタイトル・著者",
                                           placeholder="例：嫌われる勇気 / 付加価値のつくりかた / シン・営業力")
            search_submitted = st.form_submit_button("🔍 AIに候補を探してもらう")

        if search_submitted:
            kw = search_keyword.strip()
            if kw:
                cache_key = normalize(kw)
                cached = st.session_state.search_cache.get(cache_key)
                if cached:
                    results, err_msg, info = cached
                    info = (info + " / キャッシュ") if info else "キャッシュ"
                else:
                    with st.spinner("AIが本を探しに行っています。"):
                        results, err_msg, info = search_books_ai(kw)
                    if results and not err_msg:
                        st.session_state.search_cache[cache_key] = (results, err_msg, info)
                st.session_state.search_results = results
                st.session_state.search_error = err_msg
                st.session_state.search_info = info
            else:
                st.warning("検索キーワードを入力してね。")

        if "search_results" in st.session_state:
            results = st.session_state.search_results
            err_msg = st.session_state.get("search_error", "")

            if results:
                st.write("---")
                st.write(f"🎯 **AIが {len(results)} 冊の本をピックアップしたよ！追加したい本を選んでね：**")

                cols = st.columns(2)
                for idx, book in enumerate(results):
                    with cols[idx % 2]:
                        with st.container(border=True):
                            st.image(book["imageUrl"], width=100)
                            st.markdown(f"**{book['title']}**")
                            st.write(f"✍️ {book['author']}")

                            if normalize(book["title"]) in shelf_keys:
                                st.caption("📚 すでに本棚にあるよ")

                            if book["summary"]:
                                s = book["summary"]
                                st.caption(s[:100] + "..." if len(s) > 100 else s)

                            custom_img = ""
                            if not book["has_cover"]:
                                custom_img = st.text_input("🖼️ 表紙画像のURL（任意）",
                                                           key=f"img_{idx}",
                                                           placeholder="空欄なら「画像なし」で登録")

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
                                    invalidate_books()
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

            st.write("---")
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
                            invalidate_books()
                            st.session_state.flash = f"「{manual_title.strip()}」を手動で追加したよ！"
                            if "search_results" in st.session_state:
                                del st.session_state.search_results
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
    if not books_ok and not books:
        st.warning("本棚の取得に失敗したよ。下の「🔄 本棚を更新」でもう一度試してね。")
    elif not books_ok:
        st.warning("最新の本棚を取得できなかったので、前回の表示を出しているよ。")

    if isinstance(books, list) and len(books) > 0:
        st.caption(f"📚 {len(books)}冊")

        view_books = list(books)
        filter_text = st.text_input("🔎 本棚の中を絞り込み", key="shelf_filter",
                                    placeholder="タイトルや著者の一部")
        sort_mode = st.radio("並び順", ["追加順", "新しい順", "タイトル順"],
                             horizontal=True, key="shelf_sort")

        if filter_text.strip():
            nq = normalize(filter_text)
            view_books = [
                b for b in view_books
                if nq in normalize(get_field(b, ["タイトル"]) + get_field(b, ["著者", "著者名", "作者"]))
            ]
        if sort_mode == "新しい順":
            view_books.reverse()
        elif sort_mode == "タイトル順":
            view_books.sort(key=lambda b: normalize(get_field(b, ["タイトル"])))

        if not view_books:
            st.info("条件に合う本が見つからなかったよ。")

        # スマホでの画像を少しコンパクト(90px)にして横幅に余裕を持たせる
        cols_per_row = 5
        for i in range(0, len(view_books), cols_per_row):
            cols = st.columns(cols_per_row, gap="small")
            for j in range(cols_per_row):
                if i + j < len(view_books):
                    book = view_books[i + j]
                    with cols[j]:
                        img_url = str(book.get("画像URL", "") or "").strip()
                        if img_url.startswith("http"):
                            st.image(img_url, width=90)
                        else:
                            st.image(PLACEHOLDER_IMG, width=90)

                        book_title = book.get("タイトル", "タイトル不明")
                        if ENABLE_DESIGN_TWEAKS:
                            st.markdown(f"<div class='book-title'>{html.escape(str(book_title))}</div>",
                                        unsafe_allow_html=True)
                        else:
                            st.write(f"**{book_title}**")

                        if st.button("詳細・メモ", key=f"btn_{get_book_id(book) or i}_{j}"):
                            open_book(book)
                            st.rerun()
    elif books_ok:
        st.info("まだ本棚に本がないよ。上の「➕ AI書店員に本を探してもらう」から追加してみよう！")

    if st.button("🔄 本棚を更新"):
        invalidate_books()
        st.rerun()

# ==========================================
# 📖 詳細・メモ画面
# ==========================================
elif st.session_state.page == "detail":
    
    # 📱 スマホでも100%絶対に縦に崩れないCSS定義
    st.markdown("""
    <style>
    /* メモカード（枠線）の上下余白を極限まで詰める */
    div[data-testid="stVerticalBlockBorderWrapper"] {
        padding: 10px 12px 8px 12px !important;
        margin-bottom: 6px !important;
        border-radius: 8px !important;
    }
    
    /* カード内のテキストや日付の余白をカット */
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlock"] {
        gap: 2px !important;
    }

    /* ボタン群を包むコンテナを「横並び（row）」に固定 */
    .memo-btn-box {
        display: flex !important;
        flex-direction: row !important;
        align-items: center !important;
        justify-content: flex-start !important;
        gap: 8px !important;
        margin-top: 6px !important;
    }

    /* ボタン自体のサイズカット（コンパクト化） */
    .memo-btn-box button {
        padding: 2px 10px !important;
        min-height: 28px !important;
        height: 28px !important;
        font-size: 0.78rem !important;
        line-height: 1.0 !important;
        border-radius: 4px !important;
        width: auto !important;
    }

    /* 「編集」ボタン（ほんのりオレンジ） */
    .memo-btn-box button[kind="secondary"]:first-child,
    .memo-btn-box div:first-child button {
        background-color: #fff3e0 !important;
        border: 1px solid #ffb74d !important;
    }
    .memo-btn-box button[kind="secondary"]:first-child *,
    .memo-btn-box div:first-child button * {
        color: #e65100 !important;
        font-weight: 600 !important;
    }

    /* 「削除」ボタン（グレー枠） */
    .memo-btn-box button[kind="secondary"]:last-child,
    .memo-btn-box div:last-child button {
        background-color: #ffffff !important;
        border: 1px solid #dcdcdc !important;
    }
    .memo-btn-box button[kind="secondary"]:last-child *,
    .memo-btn-box div:last-child button * {
        color: #666666 !important;
    }
    </style>
    """, unsafe_allow_html=True)
    
    book = st.session_state.selected_book
    book_id = get_book_id(book)
    title = get_field(book, ["タイトル"], "タイトル不明")
    author = get_field(book, ["著者", "著者名", "作者"], "著者不明")
    summary = get_field(book, ["概要", "あらすじ", "サマリー"], "")

    if st.button("⬅️ 本棚に戻る"):
        close_book()
        st.rerun()

    memo_key = f"memos_{book_id}"
    insight_key = f"insight_{book_id}"
    insight_time_key = f"insight_time_{book_id}"
    show_key = f"show_insight_{book_id}"

    need_memos = memo_key not in st.session_state
    need_insight = insight_key not in st.session_state
    if need_memos or need_insight:
        with st.spinner("読み込み中..."):
            m_new, ti_new, tt_new = None, None, None
            if need_memos and need_insight:
                m_new, ti_new, tt_new = fetch_detail_data(book_id)
            elif need_memos:
                m_new = fetch_memos(book_id)
            else:
                ti_new, tt_new = fetch_insight(book_id)
        if need_memos and m_new is not None:
            st.session_state[memo_key] = m_new
        if need_insight and ti_new is not None:
            st.session_state[insight_key] = ti_new
            st.session_state[insight_time_key] = tt_new

    memos = st.session_state.get(memo_key)
    if memos is None:
        memos = []
        st.warning("メモの取得に失敗したよ。「🔄 最新の状態に更新」でもう一度試してね。")
    insight_text = st.session_state.get(insight_key, "")

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
    if st.button("🔄 最新の状態に更新", key="refresh_detail"):
        st.session_state.pop(memo_key, None)
        st.session_state.pop(insight_key, None)
        st.session_state.pop(insight_time_key, None)
        st.rerun()

    if memos:
        for m in reversed(memos):
            memo_id = str(m.get("メモID", ""))
            with st.container(border=True):
                st.caption(f"🕒 {m.get('日時', '')}")

                if st.session_state.editing_memo_id == memo_id:
                    edited = st.text_area("メモを編集", value=m.get("メモ", ""), height=110, key=f"edit_text_{memo_id}")
                    # ボタン配置（st.columnsではなく自前フレックス）
                    st.markdown('<div class="memo-btn-box">', unsafe_allow_html=True)
                    btn_upd = st.button("✅ 更新", key=f"upd_{memo_id}")
                    btn_can = st.button("✖️ キャンセル", key=f"cancel_{memo_id}")
                    st.markdown('</div>', unsafe_allow_html=True)

                    if btn_upd:
                        if edited.strip():
                            ok, err = update_memo(memo_id, edited.strip())
                            if ok:
                                for x in memos:
                                    if str(x.get("メモID", "")) == memo_id:
                                        x["メモ"] = edited.strip()
                                st.session_state.editing_memo_id = None
                                st.session_state.flash = "メモを更新したよ！"
                                st.rerun()
                            else:
                                st.error(f"更新に失敗したよ: {err}")
                        else:
                            st.warning("メモが空だよ。")
                    if btn_can:
                        st.session_state.editing_memo_id = None
                        st.rerun()

                elif st.session_state.confirm_delete_id == memo_id:
                    st.write(m.get("メモ", ""))
                    st.warning("このメモを削除しますか？（元に戻せません）")
                    st.markdown('<div class="memo-btn-box">', unsafe_allow_html=True)
                    btn_yes = st.button("はい", key=f"yes_{memo_id}", type="primary")
                    btn_no = st.button("いいえ", key=f"no_{memo_id}")
                    st.markdown('</div>', unsafe_allow_html=True)

                    if btn_yes:
                        ok, err = delete_memo(memo_id)
                        if ok:
                            st.session_state[memo_key] = [
                                x for x in memos if str(x.get("メモID", "")) != memo_id
                            ]
                            st.session_state.confirm_delete_id = None
                            st.session_state.flash = "メモを削除したよ。"
                            st.rerun()
                        else:
                            st.error(f"削除に失敗したよ: {err}")
                    if btn_no:
                        st.session_state.confirm_delete_id = None
                        st.rerun()

                else:
                    st.write(m.get("メモ", ""))
                    # ★★★ ポイント: st.columnsをやめて自作HTMLタグで包む ★★★
                    st.markdown('<div class="memo-btn-box">', unsafe_allow_html=True)
                    btn_edit = st.button("編集", key=f"edit_{memo_id}")
                    btn_del = st.button("削除", key=f"del_{memo_id}")
                    st.markdown('</div>', unsafe_allow_html=True)

                    if btn_edit:
                        st.session_state.editing_memo_id = memo_id
                        st.session_state.confirm_delete_id = None
                        st.rerun()
                    if btn_del:
                        st.session_state.confirm_delete_id = memo_id
                        st.session_state.editing_memo_id = None
                        st.rerun()
    else:
        st.info("まだメモがないよ。最初の気づきを書いてみよう！")

    st.divider()
    with st.expander("⚙️ 管理（本の削除）", expanded=bool(st.session_state.get("confirm_delete_book"))):
        if st.session_state.get("confirm_delete_book"):
            st.warning("⚠️ 本当にこの本を削除しますか？（本データ・メモ・Insight全て削除されます）")
            c1, c2 = st.columns([1, 4])
            with c1:
                if st.button("はい、削除します", type="primary"):
                    with st.spinner("削除中..."):
                        ok, err = delete_book(book_id)
                    if ok:
                        invalidate_books()
                        close_book()
                        st.session_state.flash = f"「{title}」を本棚から削除したよ。"
                        st.rerun()
                    else:
                        st.error(f"削除に失敗したよ: {err}")
            with c2:
                if st.button("キャンセル"):
                    st.session_state.confirm_delete_book = False
                    st.rerun()
        else:
            if st.button("🗑️ この本を本棚から削除する"):
                st.session_state.confirm_delete_book = True
                st.rerun()