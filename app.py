import os, json, time, re, csv, io
from urllib.parse import urlparse, parse_qs, quote
from difflib import SequenceMatcher
from flask import Flask, request, jsonify, Response
import requests

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 3600
STATE_FILE = os.path.join(BASE_DIR, "teacher_state.json")

TEACHERS = {
    "ben": {
        "name": "דן", "color": "#1a56db", "color_light": "#e8f0fe", "voice_gender": "male",
        "results_tab": "Ben", "student_password": os.getenv("BEN_STUDENT_PASSWORD", "class2026"),
        "teacher_password": os.getenv("BEN_TEACHER_PASSWORD", "ben2026"),
        "default_threshold": int(os.getenv("BEN_THRESHOLD", "85")),
        "default_max_attempts": int(os.getenv("BEN_MAX_ATTEMPTS", "5")),
    },
    "sara": {
        "name": "שרה", "color": "#be185d", "color_light": "#fce8f3", "voice_gender": "female",
        "results_tab": "Sara", "student_password": os.getenv("SARA_STUDENT_PASSWORD", "class2026"),
        "teacher_password": os.getenv("SARA_TEACHER_PASSWORD", "sara2026"),
        "default_threshold": int(os.getenv("SARA_THRESHOLD", "85")),
        "default_max_attempts": int(os.getenv("SARA_MAX_ATTEMPTS", "5")),
    },
}
CATALOG_SHEET_ID = os.getenv("CATALOG_SHEET_ID", "134GzKi9KWNCP_avNg5Z7drhHp3Re7RRALrNrDcOeFnk")
RESULTS_SHEET_ID = os.getenv("RESULTS_SHEET_ID", "17a-y_-nL9L85Kl7zL1F1ovTGbQy7q5NlBX24C-a_6JU")
# Each teacher writes results into their own separate spreadsheet file (not just a separate
# tab) so that opening the file directly never exposes the other teacher's data.
RESULTS_SHEET_IDS = {
    "ben": RESULTS_SHEET_ID,
    "sara": os.getenv("SARA_RESULTS_SHEET_ID", "1JGWw_Jf8m3WF2-HS6sEohRmV6v2mmp29b6iSE3rgsOQ"),
}
EZRA_APP_BASE_URL = os.getenv("EZRA_APP_BASE_URL", "https://app.ezra.clap.co.il")

_cache = {}
_sessions = {}
_pending_results = []

FALLBACK_SENTENCES = [
    {"en": "I love learning English", "he": "אני אוהב ללמוד אנגלית"},
    {"en": "Today is a beautiful day", "he": "היום יום יפה"},
    {"en": "I want to speak English fluently", "he": "אני רוצה לדבר אנגלית בשטף"},
]

COMMON_VERBS = set("""
am is are was were have has had do does did go went come came get got make made know think
say said see saw take took want use find give tell work call need feel try leave put keep run
start began begin write read speak listen play help learn study live move walk talk meet ask answer
understand remember forget love like enjoy visit travel drive fly sit stand eat drink buy sell
""".split())

def _default_teacher_state():
    return {
        tid: {
            "threshold": t["default_threshold"],
            "max_attempts": t["default_max_attempts"],
            "exercise_name": "תרגול דמו",
            "csv_url": "",
            "custom_exercises": [],
            "allowed_students": [],
            "restrict_to_list": False,
            "silence_timeout_ms": 1200,
        } for tid, t in TEACHERS.items()
    }

def load_state():
    state = _default_teacher_state()
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for tid in state:
                if tid in saved:
                    state[tid].update(saved[tid])
    except Exception as e:
        print("STATE LOAD FAILED", e)
    for tid in state:
        state[tid]["threshold"] = max(80, min(100, int(state[tid].get("threshold", 85))))
        state[tid]["max_attempts"] = max(4, min(7, int(state[tid].get("max_attempts", 5))))
        state[tid]["allowed_students"] = sorted({
            str(n).strip() for n in state[tid].get("allowed_students", []) if str(n).strip()
        })
        state[tid]["restrict_to_list"] = bool(state[tid].get("restrict_to_list", False))
        state[tid]["silence_timeout_ms"] = max(400, min(3000, int(state[tid].get("silence_timeout_ms", 1200))))
    return state

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(_teacher_state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("STATE SAVE FAILED", e)

_teacher_state = load_state()

def fix_mojibake(value):
    """Repair common Google-Sheets CSV mojibake such as ×œ×§×•×— or â€“."""
    if value is None:
        return ""
    s = str(value)
    # If UTF-8 bytes were wrongly decoded as latin-1/cp1252, re-decode them.
    if any(marker in s for marker in ("×", "â", "Ã", "Â")):
        for enc in ("latin1", "cp1252"):
            try:
                repaired = s.encode(enc).decode("utf-8")
                # Keep the repair only if it actually reduced mojibake markers.
                if sum(repaired.count(m) for m in ("×", "â", "Ã", "Â")) < sum(s.count(m) for m in ("×", "â", "Ã", "Â")):
                    return repaired
            except Exception:
                pass
    return s

def safe_fetch(url, timeout=10):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        # Do not trust requests' guessed encoding for Google CSV. Decode bytes as UTF-8.
        return r.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return r.content.decode("cp1252")
        except Exception:
            return r.text
    except Exception as e:
        print("FETCH FAILED", url, e)
        return None

def teacher_public(tid):
    t, s = TEACHERS[tid], _teacher_state[tid]
    return {
        "id": tid, "name": t["name"], "color": t["color"], "color_light": t["color_light"],
        "voice_gender": t["voice_gender"], "threshold": s["threshold"], "max_attempts": s["max_attempts"],
        "exercise_name": s.get("exercise_name", "תרגול דמו"),
        "silence_timeout_ms": s.get("silence_timeout_ms", 1200),
    }

def load_catalog(lang_filter="en"):
    key = f"catalog:{lang_filter}"
    if key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]
    url = f"https://docs.google.com/spreadsheets/d/{CATALOG_SHEET_ID}/gviz/tq?tqx=out:csv&sheet=Sheet1"
    text = safe_fetch(url)
    if not text:
        return []
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            continue
        name = fix_mojibake((row[0] or "").strip().strip('"'))
        app_url = fix_mojibake((row[2] or "").strip().strip('"'))
        if "link=" not in app_url:
            continue
        params = parse_qs(urlparse(app_url).query)
        lang = params.get("lang", [""])[0]
        csv_url = extract_csv_url(app_url)
        if lang_filter and lang != lang_filter:
            continue
        if name and csv_url:
            out.append({"name": name, "url": app_url, "csv_url": csv_url, "lang": lang})
    _cache[key] = (time.time(), out)
    return out


def clean_cell(value):
    return fix_mojibake(value).replace("\ufeff", "").strip().strip('"').strip()

def looks_hebrew(text):
    return bool(re.search(r"[א-ת]", text or ""))

def looks_english(text):
    """True for English sentence/definition text, including an en dash and punctuation."""
    s = text or ""
    letters = re.findall(r"[A-Za-z]", s)
    if len(letters) < 2:
        return False
    # Avoid treating mojibake or Hebrew explanation as English just because it has one Latin label.
    heb = len(re.findall(r"[א-ת]", s))
    return len(letters) >= heb

def english_score(text):
    s = text or ""
    words = re.findall(r"[A-Za-z]+", s)
    heb = re.findall(r"[א-ת]", s)
    return len(words) * 3 + len(re.findall(r"[A-Za-z]", s)) - len(heb) * 4

def choose_en_he(row):
    """Return a safe {en, he} for any CSV row.
    Supports:
    - no header: EN, HE
    - no header reversed: HE, EN
    - rows with a word + Hebrew explanation + English definition
    - header columns named en/english/he/hebrew
    """
    cells = [clean_cell(c) for c in row]
    cells = [c for c in cells if c]
    if len(cells) < 2:
        return None

    lowered = [c.lower().strip() for c in cells]
    header_words = {"english", "en", "sentence", "hebrew", "he", "עברית", "תרגום", "url", "link", "csv"}
    if any(x in header_words for x in lowered[:3]):
        return None

    # If one cell includes both English and Hebrew via parentheses, keep the whole text as Hebrew prompt
    # and choose the best English-only definition/sentence from another cell.
    english_candidates = [(i, c, english_score(c)) for i, c in enumerate(cells) if looks_english(c)]
    hebrew_candidates = [(i, c, len(re.findall(r"[א-ת]", c))) for i, c in enumerate(cells) if looks_hebrew(c)]

    if english_candidates:
        en_i, en, _ = max(english_candidates, key=lambda x: x[2])
    else:
        return None

    # Prefer a Hebrew cell different from English. If none, use another descriptive cell as prompt.
    he = ""
    other_he = [x for x in hebrew_candidates if x[0] != en_i]
    if other_he:
        _, he, _ = max(other_he, key=lambda x: x[2])
    else:
        # No real Hebrew translation in this row. Never fall back to a pre-blanked
        # cloze variant of the sentence (e.g. "I like ____ shirt") - that would show
        # a spoiler-ish, wrong-language prompt instead of a Hebrew one. The app
        # already builds its own fill-in-the-blank later (station 3), so a
        # pre-blanked column in the sheet is redundant and should be ignored here.
        def is_blanked_variant(c):
            if "___" in c or "____" in c:
                return True
            # Also treat a near-duplicate of the English sentence (just missing a
            # word or two) as a blanked variant rather than a genuine translation.
            return looks_english(c) and similarity(c, en) >= 70
        others = [c for i, c in enumerate(cells) if i != en_i and not is_blanked_variant(c)]
        he = others[0] if others else en

    # Guard: never let Hebrew prompt become the answer to score against.
    if not looks_english(en) or len(normalize(en).split()) < 1:
        return None
    return {"en": en, "he": he}

def extract_csv_url(value):
    """Accept a raw published CSV URL, an EZRA app link with ?link=<csv>,
    or a normal Google Sheets share/edit link (e.g. copied straight from the
    browser address bar). Normal Sheets links are auto-converted to a CSV
    export URL so teachers don't need to "Publish to web" first.
    Note: the sheet still needs to be shared as "Anyone with the link -
    Viewer" for the server to be able to fetch it without signing in."""
    raw = clean_cell(value)
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        params = parse_qs(parsed.query)
        if params.get("link"):
            return params["link"][0].strip()
        if "docs.google.com" in parsed.netloc and "/spreadsheets/" in parsed.path:
            # "Publish to web" links look like /spreadsheets/d/e/<long-publish-id>/pub...
            # - note the literal "e" path segment. These (and any link that is already
            # /export or /gviz/) are already working CSV URLs and must be left untouched.
            # The bug this guards against: the sheet-ID regex below would otherwise treat
            # that literal "e" as the sheet ID and rewrite a perfectly good published CSV
            # link into a broken one (.../d/e/export?format=csv&gid=0), which silently
            # breaks every "Publish to web" exercise in the catalog - exactly what
            # happened to the existing Motke exercise list after this auto-convert
            # feature was added. Only genuine "d/<sheet-id>/edit" browser share links
            # (copied straight from the address bar) should be rewritten.
            already_csv = (
                "/spreadsheets/d/e/" in parsed.path
                or "/export" in parsed.path
                or "/gviz/" in parsed.path
                or parsed.path.rstrip("/").endswith("/pub")
            )
            if already_csv:
                return raw
            m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", parsed.path)
            if m:
                sheet_id = m.group(1)
                gid = params.get("gid", [None])[0]
                if not gid:
                    frag_m = re.search(r"gid=(\d+)", parsed.fragment or "")
                    gid = frag_m.group(1) if frag_m else "0"
                return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    except Exception:
        pass
    return raw

def load_sentences_from_csv(csv_url):
    sentences, _used_fallback = load_sentences_from_csv_ex(csv_url)
    return sentences

def load_sentences_from_csv_ex(csv_url):
    """Like load_sentences_from_csv but also reports whether the generic demo
    sentences had to be substituted because the real sheet could not be fetched
    or parsed. This used_fallback flag matters: without it, a session/teacher
    dashboard can silently label demo content with the real exercise's name,
    which is confusing and hides a sharing/URL problem (the exact bug reported:
    a student's row said "Sandra Belinsky - 25 New words" but the sentences
    actually delivered were the 3 generic FALLBACK_SENTENCES)."""
    if not csv_url:
        return FALLBACK_SENTENCES[:], True
    csv_url = extract_csv_url(csv_url)
    key = "sentences:" + csv_url
    if key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        cached_sentences, cached_fallback = _cache[key][1]
        return [dict(x) for x in cached_sentences], cached_fallback
    text = safe_fetch(csv_url)
    if not text:
        return FALLBACK_SENTENCES[:], True
    sentences = []
    seen = set()
    for row in csv.reader(io.StringIO(text)):
        item = choose_en_he(row)
        if not item:
            continue
        en_norm = normalize(item["en"])
        if not en_norm or en_norm in seen:
            continue
        seen.add(en_norm)
        sentences.append(item)
    used_fallback = not sentences
    if used_fallback:
        sentences = FALLBACK_SENTENCES[:]
    _cache[key] = (time.time(), (sentences, used_fallback))
    return [dict(x) for x in sentences], used_fallback

def normalize(text):
    return re.sub(r"[^a-z0-9\s]", "", (text or "").lower()).strip()

def similarity(spoken, correct):
    a, b = normalize(spoken), normalize(correct)
    if not a or not b:
        return 0
    if a == b:
        return 100
    return int(SequenceMatcher(None, a, b).ratio() * 100)

def has_latin(text):
    return bool(re.search(r"[A-Za-z]", text or ""))

def best_score_for_spoken(spoken, sentence_obj):
    """Score against the English field, but protect against swapped/corrupt CSV rows.
    If the CSV mapping is wrong, choose the candidate that best matches the spoken English.
    """
    candidates = []
    for key in ("en", "he"):
        val = (sentence_obj.get(key) or "").strip()
        if val:
            candidates.append((key, val, similarity(spoken, val)))
    if not candidates:
        return "en", "", 0
    # Prefer candidates that actually contain Latin letters, because speech recognition is English.
    latin = [c for c in candidates if has_latin(c[1])]
    pool = latin or candidates
    key, correct, score = max(pool, key=lambda x: x[2])
    return key, correct, score

def word_level(spoken, correct):
    """Word-level feedback from the expected sentence perspective.
    correct = word was heard in the right place; missing = expected word not heard;
    wrong = expected word was replaced; extra = extra spoken word.
    """
    sp, co = normalize(spoken).split(), normalize(correct).split()
    result = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, sp, co).get_opcodes():
        if tag == "equal":
            for w in co[j1:j2]:
                result.append({"word": w, "status": "correct"})
        elif tag == "insert":
            # Words that exist in the correct sentence but were not spoken.
            for w in co[j1:j2]:
                result.append({"word": w, "status": "missing"})
        elif tag == "delete":
            # Extra spoken words that are not in the correct sentence.
            for w in sp[i1:i2]:
                result.append({"word": w, "status": "extra"})
        elif tag == "replace":
            for w in co[j1:j2]:
                result.append({"word": w, "status": "wrong"})
            for w in sp[i1:i2]:
                if w not in co[j1:j2]:
                    result.append({"word": w, "status": "extra"})
    return result

def mastery_target_for(failures):
    if failures <= 0:
        return 0
    return min(failures + 2, 5)

def detect_cloze_word(sentence):
    words = normalize(sentence).split()
    if len(words) < 3:
        return None
    def is_verb(w):
        return (w in COMMON_VERBS or
                (w.endswith("ed") and (w[:-2] in COMMON_VERBS or w[:-1] in COMMON_VERBS)) or
                (w.endswith("ing") and w[:-3] in COMMON_VERBS) or
                (w.endswith("s") and w[:-1] in COMMON_VERBS))
    for w in words[1:]:
        if is_verb(w):
            return w
    candidates = [(len(w), w) for w in words[1:-1] if len(w) > 4]
    return sorted(candidates, reverse=True)[0][1] if candidates else None

def session_payload(s):
    # Each station (1 = initial read, 2 = Bloom mastery, 3 = cloze) has its OWN
    # independent attempt budget. They used to share one global counter, which
    # meant station 1's failures could silently eat into station 2's budget and
    # station 3 (cloze) would then never get a turn. attempts_used/attempts_left
    # here always reflect whichever station is currently active.
    cap = s.get("max_attempts", 5)
    if s["cloze_active"]:
        used, left = s.get("cloze_attempts", 0), max(0, cap - s.get("cloze_attempts", 0))
    elif s["mastery_target"] > 0:
        used, left = s.get("stage2_attempts", 0), max(0, cap - s.get("stage2_attempts", 0))
    else:
        used, left = s.get("sentence_attempts", 0), max(0, cap - s.get("sentence_attempts", 0))
    return {
        "mastery_target": s["mastery_target"],
        "mastery_consecutive": s["mastery_consecutive"],
        "mastery_remaining": max(0, s["mastery_target"] - s["mastery_consecutive"]),
        "failed_attempts": s["failed_attempts"],
        "cloze_active": s["cloze_active"],
        "cloze_word": s["cloze_word"],
        "cloze_attempts_left": max(0, cap - s.get("cloze_attempts", 0)),
        "attempts_used": used,
        "attempts_left": left,
    }

def new_session(student_id, teacher_id, student_name):
    ts = _teacher_state[teacher_id]
    csv_url = ts.get("csv_url", "")
    sentences, used_fallback = load_sentences_from_csv_ex(csv_url)
    # content_mismatch=True means a real exercise was selected (csv_url is set)
    # but its sheet could not be loaded, so generic demo sentences were used
    # instead - the session/exercise NAME still says the real exercise, so the
    # teacher dashboard and student view must both flag this clearly instead of
    # silently mislabeling demo content as the real exercise.
    content_mismatch = used_fallback and bool(csv_url.strip())
    _sessions[student_id] = {
        "student_id": student_id,
        "teacher_id": teacher_id,
        "student_name": student_name,
        "threshold": int(ts["threshold"]),
        "max_attempts": int(ts["max_attempts"]),
        "voice_gender": TEACHERS[teacher_id]["voice_gender"],
        "exercise_name": ts.get("exercise_name", "תרגול דמו"),
        "csv_url": csv_url,
        "content_mismatch": content_mismatch,
        "sentences": sentences,
        "current": 0,
        "failed_attempts": 0,
        "sentence_attempts": 0,
        "stage2_attempts": 0,
        "mastery_target": 0,
        "mastery_consecutive": 0,
        "mastery_score": 0,
        "cloze_active": False,
        "cloze_word": None,
        "cloze_attempts": 0,
        "cloze_passed": False,
        "last_mastery_target": 0,
        "review_queue": [],
        "in_review": False,
        "review_index": 0,
        "needs_review_final": [],
        "results": [],
        "exam_results": [],
        "completed": False,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


def get_gspread_client():
    """Return an authorized gspread client using GOOGLE_CREDENTIALS_JSON.
    The same service account must have Editor permission on the catalog/results sheets.
    """
    svc_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not svc_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is missing")
    import gspread
    from google.oauth2.service_account import Credentials
    creds_info = json.loads(svc_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    return gspread.authorize(creds)

def build_exercise_app_url(csv_url):
    base = (EZRA_APP_BASE_URL or "https://app.ezra.clap.co.il").rstrip("/")
    return f"{base}/?lang=en&link={quote(csv_url, safe=':/?&=%') }"

def append_exercise_to_catalog_sheet(name, csv_url):
    """Option G: Teacher UI writes the new exercise into the master Google Sheet.
    Google Sheet remains the single source of truth. Expected catalog layout:
    col A = exercise name, col B = optional notes/lang, col C = EZRA app URL with ?link=<csv>.
    """
    gc = get_gspread_client()
    sh = gc.open_by_key(CATALOG_SHEET_ID)
    try:
        ws = sh.worksheet("Sheet1")
    except Exception:
        ws = sh.sheet1

    app_url = build_exercise_app_url(csv_url)
    existing = load_catalog("en")
    for item in existing:
        if item.get("csv_url") == csv_url:
            return {**item, "already_exists": True}

    ws.append_row([name, "", app_url], value_input_option="USER_ENTERED")
    # Invalidate the in-memory catalog cache so the new row appears immediately.
    for key in list(_cache.keys()):
        if key.startswith("catalog:"):
            _cache.pop(key, None)
    return {"name": name, "url": app_url, "csv_url": csv_url, "lang": "en", "source": "google_sheet", "already_exists": False}

RESULT_HEADERS = [
    "Time", "Teacher", "Student", "Exercise", "Phase", "Sentence", "Spoken",
    "Score", "Passed", "Skipped", "Attempts", "Max Attempts",
    "Mastery Repetitions", "Mastery Status", "Mastery Score", "Cloze Passed",
    "Recording Duration MS", "Silence MS", "Words Per Minute", "Fluency Status"
]

RESULT_KEY_ALIASES = {
    "Time": "timestamp", "Teacher": "teacher_id", "Student": "student_name",
    "Exercise": "exercise", "Phase": "phase", "Sentence": "sentence", "Spoken": "spoken",
    "Score": "score", "Passed": "passed", "Skipped": "skipped", "Attempts": "attempts",
    "Max Attempts": "max_attempts", "Mastery Repetitions": "mastery_reps",
    "Mastery Status": "mastery_status", "Mastery Score": "mastery_score",
    "Cloze Passed": "cloze_passed",
    "Recording Duration MS": "recording_duration_ms",
    "Silence MS": "silence_ms",
    "Words Per Minute": "words_per_minute",
    "Fluency Status": "fluency_status",
}

def ensure_results_header(ws):
    """Keep old sheets compatible while adding the new mastery columns."""
    try:
        values = ws.get_all_values()
        if not values:
            ws.append_row(RESULT_HEADERS, value_input_option="USER_ENTERED")
            return RESULT_HEADERS
        header = values[0]
        changed = False
        for h in RESULT_HEADERS:
            if h not in header:
                header.append(h)
                changed = True
        if changed:
            ws.update("1:1", [header])
        return header
    except Exception:
        return RESULT_HEADERS

def write_result(row):
    _pending_results.append(row)
    svc_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not svc_json:
        print("RESULT STORED IN MEMORY ONLY", row)
        return False
    try:
        import gspread
        sheet_id = RESULTS_SHEET_IDS.get(row["teacher_id"], RESULTS_SHEET_ID)
        sh = get_gspread_client().open_by_key(sheet_id)
        tab = TEACHERS[row["teacher_id"]]["results_tab"]
        try:
            ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab, rows=1000, cols=24)
            ws.append_row(RESULT_HEADERS, value_input_option="USER_ENTERED")
        header = ensure_results_header(ws)
        # Older sheets may still have a legacy lowercase header row (timestamp,
        # teacher, student, ... - from before the 20-column RESULT_HEADERS design)
        # sitting to the left of the current headers, because ensure_results_header
        # only ever APPENDS missing columns rather than replacing the row. Do not
        # keep filling those legacy columns going forward - only write into columns
        # whose header is a real, recognized RESULT_HEADERS label. This stops new
        # rows from duplicating every value into two side-by-side sets of columns.
        out = []
        for h in header:
            if h in RESULT_HEADERS:
                key = RESULT_KEY_ALIASES.get(h, h)
                out.append(row.get(key, ""))
            else:
                out.append("")
        ws.append_row(out, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print("WRITE RESULT FAILED", e)
        return False

def fluency_from_metrics(spoken, score, metrics=None):
    """Lightweight fluency estimate from browser timing, not acoustic analysis.
    The browser sends recording duration and trailing silence; we combine that
    with score to label mastery/fluency consistently.
    """
    metrics = metrics or {}
    try:
        duration_ms = int(metrics.get("recording_duration_ms") or 0)
    except Exception:
        duration_ms = 0
    try:
        silence_ms = int(metrics.get("silence_ms") or 0)
    except Exception:
        silence_ms = 0
    words = len(normalize(spoken).split())
    wpm = int(round(words * 60000 / duration_ms)) if duration_ms > 0 and words else 0
    if score >= 90 and words and duration_ms and silence_ms <= 1800 and wpm >= 65:
        status = "fluent_mastery"
    elif score >= 85:
        status = "accurate_needs_fluency"
    else:
        status = "not_mastered"
    return {
        "recording_duration_ms": duration_ms,
        "silence_ms": silence_ms,
        "words_per_minute": wpm,
        "fluency_status": status,
    }

def record_and_advance(s, correct, spoken, score, passed=True, skipped=False, metrics=None):
    fluency = fluency_from_metrics(spoken, score, metrics)
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "teacher_id": s["teacher_id"], "student_name": s["student_name"], "exercise": s["exercise_name"],
        "phase": "practice", "sentence": correct, "spoken": spoken, "score": score,
        "passed": bool(passed), "skipped": bool(skipped),
        "attempts": max(1, s.get("sentence_attempts", 0)),
        "max_attempts": s.get("max_attempts", ""),
        "mastery_reps": s.get("mastery_target", 0),
        "mastery_status": "mastered" if passed and not skipped else "not_mastered",
        "mastery_score": s.get("mastery_score", score),
        "cloze_passed": bool(s.get("cloze_passed", False)),
        **fluency,
    }
    s["results"].append(row)
    write_result(row)
    s["current"] += 1
    s["failed_attempts"] = 0
    s["sentence_attempts"] = 0
    s["stage2_attempts"] = 0
    s["last_mastery_target"] = s.get("mastery_target", 0)
    s["mastery_target"] = 0
    s["mastery_consecutive"] = 0
    s["mastery_score"] = 0
    s["cloze_active"] = False
    s["cloze_word"] = None
    s["cloze_attempts"] = 0
    s["cloze_passed"] = False

@app.route("/")
def home():
    return Response(open(os.path.join(BASE_DIR, "index.html"), "rb").read(), content_type="text/html; charset=utf-8")

@app.route("/teacher")
def teacher():
    return Response(open(os.path.join(BASE_DIR, "teacher.html"), "rb").read(), content_type="text/html; charset=utf-8")

@app.get("/api/teachers")
def api_teachers():
    return jsonify({tid: teacher_public(tid) for tid in TEACHERS})

@app.post("/api/verify-student")
def verify_student():
    data = request.get_json(force=True)
    tid, name, password = data.get("teacher_id"), data.get("name", "").strip(), data.get("password", "")
    if tid not in TEACHERS or not name:
        return jsonify(ok=False, error="bad request"), 400
    expected = TEACHERS[tid]["student_password"]
    if expected and password != expected:
        return jsonify(ok=False, error="wrong password"), 401
    ts = _teacher_state[tid]
    if ts.get("restrict_to_list"):
        allowed = {n.casefold() for n in ts.get("allowed_students", [])}
        if name.casefold() not in allowed:
            return jsonify(ok=False, error="השם שלך אינו ברשימת התלמידים המורשים. פנה למורה שלך."), 403
    safe_name = re.sub(r"[^A-Za-z0-9א-ת_-]", "_", name)
    sid = f"{tid}_{safe_name}_{int(time.time())}_{os.getpid()}"
    new_session(sid, tid, name)
    return jsonify(ok=True, student_id=sid, teacher=teacher_public(tid), exercise=_sessions[sid]["exercise_name"])

@app.get("/api/question")
def question():
    s = _sessions.get(request.args.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    if s["current"] >= len(s["sentences"]):
        # Main pass is done. Before the final exam, give one extra single-attempt
        # round for any sentence that had to be skipped after 5 failed tries.
        if not s.get("in_review") and s.get("review_queue"):
            s["in_review"] = True
            s["review_index"] = 0
        if s.get("in_review") and s["review_index"] < len(s["review_queue"]):
            q = s["review_queue"][s["review_index"]]
            return jsonify({
                "done": False, "he": q["he"], "en": q["en"],
                "index": s["review_index"], "total": len(s["review_queue"]),
                "threshold": s["threshold"], "max_attempts": 1, "voice_gender": s["voice_gender"],
                "exercise": s["exercise_name"], "review_round": True, **session_payload(s)
            })
        s["completed"] = True
        total = len(s["results"])
        avg = int(sum(r["score"] for r in s["results"]) / total) if total else 0
        return jsonify(done=True, results=s["results"], avg_score=avg, total=total, exercise=s["exercise_name"])
    q = s["sentences"][s["current"]]
    return jsonify({
        "done": False, "he": q["he"], "en": q["en"], "index": s["current"], "total": len(s["sentences"]),
        "threshold": s["threshold"], "max_attempts": s["max_attempts"], "voice_gender": s["voice_gender"],
        "exercise": s["exercise_name"], "review_round": False,
        "content_mismatch": s.get("content_mismatch", False), **session_payload(s)
    })

def cap_and_advance(s, correct, spoken, score, base, passed=False, metrics=None, sentence_obj=None):
    """Hard safety valve: no sentence can consume more than max_attempts submissions.
    If the student got a passing score on the last allowed try, move on as passed;
    otherwise skip/move on so the exercise never loops forever.
    A sentence that had to be skipped this way gets one more single-attempt
    chance in a review round before the final exam (unless we're already in
    that review round, in which case it's simply flagged for the teacher).
    """
    record_and_advance(s, correct, spoken, score, bool(passed), skipped=not bool(passed), metrics=metrics)
    if not passed and sentence_obj and not s.get("in_review"):
        s["review_queue"].append(dict(sentence_obj))
    return jsonify({
        **base,
        "passed": bool(passed),
        "skipped": not bool(passed),
        "advance": True,
        "cap_reached": True,
        "message": "עברנו הלאה אחרי מספר הניסיונות שהוגדר למורה.",
        **session_payload(s),
    })

@app.post("/api/answer")
def answer():
    data = request.get_json(force=True)
    sid, spoken = data.get("student", ""), data.get("answer", "")
    metrics = data.get("metrics") or {}
    s = _sessions.get(sid)
    if not s:
        # score=None (not 0) is deliberate: the front-end treats a missing/non-numeric
        # score as "couldn't check the answer" rather than a real failed attempt.
        # This happens when the server restarted (redeploy or free-tier spin-down)
        # and lost this student's in-memory session - it is NOT a real 0%.
        return jsonify(error="session not found", score=None, passed=False, words=[], advance=False), 404
    if s["current"] >= len(s["sentences"]) and not s.get("in_review"):
        return jsonify(done=True, results=s["results"], score=0, passed=False, words=[])
    s["updated_at"] = int(time.time())

    # Review round: one single extra attempt for sentences skipped earlier.
    # This bypasses the normal listen/mastery/cloze stations entirely.
    if s.get("in_review"):
        if s["review_index"] >= len(s["review_queue"]):
            return jsonify(done=True, results=s["results"], score=0, passed=False, words=[])
        review_sentence = s["review_queue"][s["review_index"]]
        _, r_correct, r_score = best_score_for_spoken(spoken, review_sentence)
        r_passed = r_score >= 100
        r_words = word_level(spoken, r_correct)
        fluency = fluency_from_metrics(spoken, r_score, metrics)
        row = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "teacher_id": s["teacher_id"],
            "student_name": s["student_name"], "exercise": s["exercise_name"],
            "phase": "review_retry", "sentence": r_correct, "spoken": spoken, "score": r_score,
            "passed": bool(r_passed), "skipped": False, "attempts": 1, "max_attempts": 1,
            "mastery_reps": 0, "mastery_status": "review_pass" if r_passed else "needs_review",
            "mastery_score": r_score, "cloze_passed": "review", **fluency,
        }
        s["results"].append(row)
        write_result(row)
        if not r_passed:
            s["needs_review_final"].append(r_correct)
        s["review_index"] += 1
        return jsonify({
            "correct": r_correct, "spoken": spoken, "score": r_score, "passed": r_passed,
            "words": r_words, "threshold": 100, "advance": True, "station": "review",
            "max_attempts": 1, **session_payload(s)
        })

    sentence_obj = s["sentences"][s["current"]]
    correct_key, correct, score = best_score_for_spoken(spoken, sentence_obj)
    # If the CSV row was reversed, fix the session sentence from this point onward.
    if correct_key != "en":
        sentence_obj["en"], sentence_obj["he"] = sentence_obj.get(correct_key, correct), sentence_obj.get("en", "")

    # Stations 1-3 require an exact, perfect match - no missed or wrong words.
    # The teacher's configurable threshold (80-100%) is only used later, in the
    # final exam (station 4), where it's combined with the mastery/fluency data.
    passed = score >= 100
    words = word_level(spoken, correct)
    base = {
        "correct": correct, "spoken": spoken, "score": score, "passed": passed,
        "debug_expected": correct,
        "words": words, "threshold": s["threshold"], "advance": False,
        "max_attempts": s["max_attempts"],
    }

    # IMPORTANT: each station (1 = initial read, 2 = Bloom mastery, 3 = cloze) has
    # its OWN independent attempt budget, counted separately. They used to share
    # one global "sentence_attempts" counter, which meant failures on station 1
    # could quietly use up the whole budget before station 2/3 even started -
    # so a student could pass everything and still never see station 3, and the
    # app would silently skip straight to the next sentence. Never again: cloze
    # is only ever skipped if the sentence has no clozeable word at all, or if
    # its OWN budget (below) runs out.

    # Station 3: Cloze check. This must happen ONLY after the practice/mastery station is complete.
    # In cloze we hide the full English sentence and ask the student to rebuild it.
    if s["cloze_active"]:
        if passed:
            s["cloze_active"] = False
            s["cloze_word"] = None
            s["cloze_passed"] = True
            s["mastery_score"] = max(s.get("mastery_score", 0), score)
            record_and_advance(s, correct, spoken, s["mastery_score"] or score, True, metrics=metrics)
            return jsonify({**base, "station": "cloze", "cloze_done": True, "advance": True, **session_payload(s)})

        s["cloze_attempts"] += 1
        if s["cloze_attempts"] >= s["max_attempts"]:
            return cap_and_advance(s, correct, spoken, score, {**base, "station": "cloze", "cloze_failed": True}, passed=False, metrics=metrics, sentence_obj=sentence_obj)
        return jsonify({**base, "station": "cloze", "cloze_mode": True, **session_payload(s)})

    # Station 2: Bloom practice/mastery. The cloze station is NOT shown yet.
    if s["mastery_target"] > 0:
        s["stage2_attempts"] = int(s.get("stage2_attempts", 0)) + 1
        stage2_cap_reached = s["stage2_attempts"] >= s["max_attempts"]
        if passed:
            s["mastery_consecutive"] += 1
            s["mastery_score"] = max(s.get("mastery_score", 0), score)
            if s["mastery_consecutive"] >= s["mastery_target"]:
                # Practice mastery is complete. Now move to the separate cloze station -
                # ALWAYS, regardless of how many station-2 attempts that took.
                cw = detect_cloze_word(correct)
                s["mastery_target"] = 0
                s["mastery_consecutive"] = 0
                s["stage2_attempts"] = 0
                if not cw:
                    record_and_advance(s, correct, spoken, s["mastery_score"] or score, True, metrics=metrics)
                    return jsonify({**base, "station": "practice", "mastery_mode_done": True, "advance": True, **session_payload(s)})
                s["cloze_active"] = True
                s["cloze_word"] = cw
                s["cloze_attempts"] = 0
                return jsonify({**base, "station": "practice", "mastery_mode_done": True, "cloze_mode": True, **session_payload(s)})
            return jsonify({**base, "station": "practice", "mastery_mode": True, "streak_broken": False, **session_payload(s)})
        # A failed repetition mid-way through Bloom reinforcement does NOT reset
        # progress back to zero - it simply isn't counted as one of the required
        # successes. The student still just needs (target - consecutive) more
        # correct repetitions. Only running out of station 2's own attempt budget
        # ends the loop (handled below) - this never borrows from station 1 or 3.
        if stage2_cap_reached:
            return cap_and_advance(s, correct, spoken, score, {**base, "station": "practice"}, passed=False, metrics=metrics, sentence_obj=sentence_obj)
        return jsonify({**base, "station": "practice", "mastery_mode": True, "streak_broken": True, **session_payload(s)})

    # Station 1: normal practice / initial read. A passing first read enters either
    # Bloom repetition mode (if there were failures) or the separate cloze check.
    s["sentence_attempts"] = int(s.get("sentence_attempts", 0)) + 1
    cap_reached = s["sentence_attempts"] >= s["max_attempts"]
    if passed:
        target = mastery_target_for(s["failed_attempts"])
        s["mastery_score"] = score
        if target > 0:
            s["mastery_target"] = target
            s["mastery_consecutive"] = 1
            s["stage2_attempts"] = 0
            return jsonify({**base, "station": "practice", "mastery_mode": True, "first_pass": True, **session_payload(s)})

        cw = detect_cloze_word(correct)
        if cw:
            s["cloze_active"] = True
            s["cloze_word"] = cw
            s["cloze_attempts"] = 0
            return jsonify({**base, "station": "practice", "cloze_mode": True, **session_payload(s)})

        record_and_advance(s, correct, spoken, score, True, metrics=metrics)
        return jsonify({**base, "station": "practice", "advance": True, **session_payload(s)})

    s["failed_attempts"] += 1
    if cap_reached:
        return cap_and_advance(s, correct, spoken, score, base, passed=False, metrics=metrics, sentence_obj=sentence_obj)
    return jsonify({**base, **session_payload(s)})

@app.post("/api/skip")
def skip():
    data = request.get_json(force=True)
    s = _sessions.get(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    if s["current"] < len(s["sentences"]):
        correct = s["sentences"][s["current"]]["en"]
        record_and_advance(s, correct, "", 0, False, skipped=True)
    return jsonify(ok=True)

@app.post("/api/score-only")
def score_only():
    data = request.get_json(force=True)
    tid = data.get("teacher_id", "ben")
    threshold = _teacher_state.get(tid, {}).get("threshold", 85)
    spoken = data.get("spoken", "")
    correct = data.get("correct", "")
    score = similarity(spoken, correct)
    return jsonify(score=score, passed=score >= threshold, words=word_level(spoken, correct), debug_expected=correct)

@app.post("/api/exam-result")
def exam_result():
    data = request.get_json(force=True)
    s = _sessions.get(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    metrics = data.get("metrics") or {}
    fluency = fluency_from_metrics(data.get("spoken", ""), int(data.get("score", 0) or 0), metrics)
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "teacher_id": s["teacher_id"],
        "student_name": s["student_name"], "exercise": s["exercise_name"], "phase": "final_exam",
        "sentence": data.get("sentence", ""), "spoken": data.get("spoken", ""),
        "score": data.get("score", 0), "passed": data.get("passed", False), "skipped": False,
        "attempts": 1, "max_attempts": s.get("max_attempts", ""), "mastery_reps": 0,
        "mastery_status": "final_exam_pass" if data.get("passed", False) else "final_exam_fail",
        "mastery_score": data.get("score", 0), "cloze_passed": "final_exam",
        **fluency,
    }
    s["exam_results"].append(row)
    write_result(row)
    return jsonify(ok=True)

@app.post("/api/teacher-login")
def teacher_login():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    s = _teacher_state[tid]
    sheet_id = RESULTS_SHEET_IDS.get(tid, RESULTS_SHEET_ID)
    return jsonify(
        ok=True, teacher=teacher_public(tid),
        allowed_students=s.get("allowed_students", []),
        restrict_to_list=bool(s.get("restrict_to_list", False)),
        results_sheet_url=f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
    )

@app.post("/api/teacher-allowed-students")
def teacher_allowed_students():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    if "allowed_students" in data:
        raw = data.get("allowed_students", [])
        names = re.split(r"[\n,]+", raw) if isinstance(raw, str) else raw
        _teacher_state[tid]["allowed_students"] = sorted({
            str(n).strip() for n in names if str(n).strip()
        })
    if "restrict_to_list" in data:
        _teacher_state[tid]["restrict_to_list"] = bool(data.get("restrict_to_list"))
    save_state()
    return jsonify(
        ok=True,
        allowed_students=_teacher_state[tid]["allowed_students"],
        restrict_to_list=_teacher_state[tid]["restrict_to_list"],
    )

@app.post("/api/teacher-settings")
def teacher_settings():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    _teacher_state[tid]["threshold"] = max(80, min(100, int(data.get("threshold", _teacher_state[tid]["threshold"]))))
    _teacher_state[tid]["max_attempts"] = max(4, min(7, int(data.get("max_attempts", _teacher_state[tid]["max_attempts"]))))
    if "silence_timeout_ms" in data:
        _teacher_state[tid]["silence_timeout_ms"] = max(400, min(3000, int(data.get("silence_timeout_ms", 1200))))
    save_state()
    return jsonify(ok=True, teacher=teacher_public(tid))

@app.post("/api/catalog")
def api_catalog():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    # Option G: the Google Sheet is the single source of truth for the exercise catalog.
    # Legacy locally-saved exercises are intentionally not mixed into the main list,
    # so teachers do not see a different catalog per server/computer.
    sheet_items = load_catalog("en")
    return jsonify(ok=True, exercises=sheet_items, source="google_sheet")

@app.post("/api/add-exercise")
def add_exercise():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401

    name = clean_cell(data.get("name", ""))
    csv_url = extract_csv_url(data.get("csv_url", ""))
    if not name:
        return jsonify(ok=False, error="חסר שם תרגיל"), 400
    if not csv_url or not csv_url.startswith(("http://", "https://")):
        return jsonify(ok=False, error="CSV URL לא תקין"), 400

    sentences = load_sentences_from_csv(csv_url)
    if not sentences or sentences == FALLBACK_SENTENCES:
        return jsonify(
            ok=False,
            error=(
                "לא נמצאו משפטים תקינים ב-CSV. ודא שהגיליון משותף לפי "
                "\"כל מי שיש לו את הקישור — צפייה\" (Anyone with the link - Viewer), "
                "ושהקישור מצביע לגיליון (sheet/gid) הנכון."
            ),
        ), 400

    try:
        item = append_exercise_to_catalog_sheet(name, csv_url)
    except Exception as e:
        print("CATALOG APPEND FAILED", e)
        return jsonify(
            ok=False,
            error=(
                "לא הצלחתי לכתוב לגוגל שיט הראשי. ודא ש-GOOGLE_CREDENTIALS_JSON מוגדר "
                "ושה-Service Account קיבל הרשאת Editor לגיליון התרגילים."
            ),
            details=str(e),
        ), 500

    # Select the newly-added/existing sheet exercise for this teacher immediately.
    _teacher_state[tid]["exercise_name"] = item["name"]
    _teacher_state[tid]["csv_url"] = item["csv_url"]
    save_state()
    return jsonify(ok=True, exercise=item, sentence_count=len(sentences), teacher=teacher_public(tid))

@app.post("/api/set-exercise")
def set_exercise():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    csv_url = extract_csv_url(data.get("csv_url", ""))
    _teacher_state[tid]["exercise_name"] = clean_cell(data.get("name", "תרגול דמו")) or "תרגול דמו"
    _teacher_state[tid]["csv_url"] = csv_url
    save_state()
    return jsonify(ok=True, teacher=teacher_public(tid), sentence_count=len(load_sentences_from_csv(csv_url)))

@app.post("/api/teacher-results")
def teacher_results():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    rows = [r for r in _pending_results if r.get("teacher_id") == tid]
    return jsonify(ok=True, rows=rows[-200:])

@app.post("/api/teacher-students")
def teacher_students():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    students = []
    for s in _sessions.values():
        if s["teacher_id"] == tid:
            phase = "סיים" if s["current"] >= len(s["sentences"]) else ("קלוז" if s.get("cloze_active") else ("Mastery" if s.get("mastery_target", 0) > 0 else "אימון"))
            students.append({
                "name": s["student_name"], "index": s["current"], "total": len(s["sentences"]),
                "done": s["current"] >= len(s["sentences"]), "exercise": s["exercise_name"],
                "teacher_current_exercise": _teacher_state[tid].get("exercise_name", ""),
                "threshold": s["threshold"], "max_attempts": s["max_attempts"],
                "failed_attempts": s["failed_attempts"], "sentence_attempts": s.get("sentence_attempts", 0),
                "mastery_target": s["mastery_target"], "mastery_consecutive": s["mastery_consecutive"],
                "phase": phase, "created_at": s.get("created_at"), "updated_at": s.get("updated_at"),
                "needs_review": s.get("needs_review_final", []),
                "content_mismatch": s.get("content_mismatch", False),
            })
    students.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return jsonify(ok=True, teacher=teacher_public(tid), active_exercise=_teacher_state[tid].get("exercise_name", ""), students=students)

if __name__ == "__main__":
    app.run(debug=True)
