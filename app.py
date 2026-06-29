import os, json, time, re, csv, io
from difflib import SequenceMatcher
from flask import Flask, request, jsonify, Response
import requests

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 3600
STATE_FILE = os.path.join(BASE_DIR, "teacher_state.json")

TEACHERS = {
    "ben": {
        "name": "בן", "color": "#1a56db", "color_light": "#e8f0fe", "voice_gender": "male",
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
        "exercise_name": s.get("exercise_name", "תרגול דמו")
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
        from urllib.parse import urlparse, parse_qs
        params = parse_qs(urlparse(app_url).query)
        lang = params.get("lang", [""])[0]
        csv_url = params.get("link", [""])[0]
        if lang_filter and lang != lang_filter:
            continue
        if name and csv_url:
            out.append({"name": name, "url": app_url, "csv_url": csv_url, "lang": lang})
    _cache[key] = (time.time(), out)
    return out

def load_sentences_from_csv(csv_url):
    if not csv_url:
        return FALLBACK_SENTENCES[:]
    key = "sentences:" + csv_url
    if key in _cache and time.time() - _cache[key][0] < CACHE_TTL:
        return [dict(x) for x in _cache[key][1]]
    text = safe_fetch(csv_url)
    if not text:
        return FALLBACK_SENTENCES[:]
    sentences = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        a, b = fix_mojibake(row[0].strip()), fix_mojibake(row[1].strip())
        if not a or not b or a.lower() in ("english", "en", "sentence"):
            continue
        if re.search(r"[א-ת]", a) and re.search(r"[A-Za-z]", b):
            he, en = a, b
        else:
            en, he = a, b
        if len(en.split()) >= 2:
            sentences.append({"en": en, "he": he})
    if not sentences:
        sentences = FALLBACK_SENTENCES[:]
    _cache[key] = (time.time(), sentences)
    return [dict(x) for x in sentences]

def normalize(text):
    return re.sub(r"[^a-z0-9\s]", "", (text or "").lower()).strip()

def similarity(spoken, correct):
    a, b = normalize(spoken), normalize(correct)
    if not a or not b:
        return 0
    return int(SequenceMatcher(None, a, b).ratio() * 100)

def word_level(spoken, correct):
    sp, co = normalize(spoken).split(), normalize(correct).split()
    result = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, sp, co).get_opcodes():
        if tag == "equal":
            for w in co[j1:j2]: result.append({"word": w, "status": "correct"})
        elif tag in ("replace", "delete"):
            for w in co[j1:j2]: result.append({"word": w, "status": "wrong"})
        elif tag == "insert":
            for w in sp[i1:i2]: result.append({"word": w, "status": "extra"})
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
    return {
        "mastery_target": s["mastery_target"],
        "mastery_consecutive": s["mastery_consecutive"],
        "mastery_remaining": max(0, s["mastery_target"] - s["mastery_consecutive"]),
        "failed_attempts": s["failed_attempts"],
        "cloze_active": s["cloze_active"],
        "cloze_word": s["cloze_word"],
        "cloze_attempts_left": max(0, 3 - s["cloze_attempts"]),
    }

def new_session(student_id, teacher_id, student_name):
    ts = _teacher_state[teacher_id]
    sentences = load_sentences_from_csv(ts.get("csv_url", ""))
    _sessions[student_id] = {
        "student_id": student_id,
        "teacher_id": teacher_id,
        "student_name": student_name,
        "threshold": int(ts["threshold"]),
        "max_attempts": int(ts["max_attempts"]),
        "voice_gender": TEACHERS[teacher_id]["voice_gender"],
        "exercise_name": ts.get("exercise_name", "תרגול דמו"),
        "csv_url": ts.get("csv_url", ""),
        "sentences": sentences,
        "current": 0,
        "failed_attempts": 0,
        "mastery_target": 0,
        "mastery_consecutive": 0,
        "mastery_score": 0,
        "cloze_active": False,
        "cloze_word": None,
        "cloze_attempts": 0,
        "results": [],
        "exam_results": [],
        "completed": False,
        "created_at": int(time.time()),
    }

def write_result(row):
    _pending_results.append(row)
    svc_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not svc_json:
        print("RESULT STORED IN MEMORY ONLY", row)
        return False
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds_info = json.loads(svc_json)
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
        sh = gspread.authorize(creds).open_by_key(RESULTS_SHEET_ID)
        tab = TEACHERS[row["teacher_id"]]["results_tab"]
        try:
            ws = sh.worksheet(tab)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab, rows=1000, cols=20)
            ws.append_row(["timestamp", "teacher", "student", "exercise", "phase", "sentence", "spoken", "score", "passed", "attempts", "mastery_reps"])
        ws.append_row([row.get(k, "") for k in ["timestamp", "teacher_id", "student_name", "exercise", "phase", "sentence", "spoken", "score", "passed", "attempts", "mastery_reps"]])
        return True
    except Exception as e:
        print("WRITE RESULT FAILED", e)
        return False

def record_and_advance(s, correct, spoken, score, passed=True, skipped=False):
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "teacher_id": s["teacher_id"], "student_name": s["student_name"], "exercise": s["exercise_name"],
        "phase": "practice", "sentence": correct, "spoken": spoken, "score": score,
        "passed": bool(passed), "skipped": bool(skipped),
        "attempts": s["failed_attempts"] + max(1, s["mastery_target"]),
        "mastery_reps": s["mastery_target"],
    }
    s["results"].append(row)
    write_result(row)
    s["current"] += 1
    s["failed_attempts"] = 0
    s["mastery_target"] = 0
    s["mastery_consecutive"] = 0
    s["mastery_score"] = 0
    s["cloze_active"] = False
    s["cloze_word"] = None
    s["cloze_attempts"] = 0

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
        s["completed"] = True
        total = len(s["results"])
        avg = int(sum(r["score"] for r in s["results"]) / total) if total else 0
        return jsonify(done=True, results=s["results"], avg_score=avg, total=total, exercise=s["exercise_name"])
    q = s["sentences"][s["current"]]
    return jsonify({
        "done": False, "he": q["he"], "en": q["en"], "index": s["current"], "total": len(s["sentences"]),
        "threshold": s["threshold"], "max_attempts": s["max_attempts"], "voice_gender": s["voice_gender"],
        "exercise": s["exercise_name"], **session_payload(s)
    })

@app.post("/api/answer")
def answer():
    data = request.get_json(force=True)
    sid, spoken = data.get("student", ""), data.get("answer", "")
    s = _sessions.get(sid)
    if not s:
        return jsonify(error="session not found"), 404
    if s["current"] >= len(s["sentences"]):
        return jsonify(done=True, results=s["results"])
    correct = s["sentences"][s["current"]]["en"]
    score = similarity(spoken, correct)
    passed = score >= s["threshold"]
    words = word_level(spoken, correct)
    base = {"correct": correct, "spoken": spoken, "score": score, "passed": passed, "words": words, "threshold": s["threshold"], "advance": False}

    if s["cloze_active"]:
        if passed:
            s["cloze_active"] = False
            s["cloze_word"] = None
            if s["mastery_target"] > 0:
                s["mastery_consecutive"] = 1
                s["mastery_score"] = score
                return jsonify({**base, "cloze_done": True, "mastery_mode": True, "first_pass": True, **session_payload(s)})
            record_and_advance(s, correct, spoken, s["mastery_score"] or score, True)
            return jsonify({**base, "cloze_done": True, "advance": True, **session_payload(s)})
        s["cloze_attempts"] += 1
        if s["cloze_attempts"] >= 3:
            s["cloze_active"] = False
            s["cloze_word"] = None
            if s["mastery_target"] > 0:
                return jsonify({**base, "cloze_done": True, "mastery_mode": True, "first_pass": True, **session_payload(s)})
            record_and_advance(s, correct, spoken, s["mastery_score"] or score, True)
            return jsonify({**base, "cloze_done": True, "advance": True, **session_payload(s)})
        return jsonify({**base, "cloze_mode": True, **session_payload(s)})

    if s["mastery_target"] > 0:
        if passed:
            s["mastery_consecutive"] += 1
            s["mastery_score"] = score
            if s["mastery_consecutive"] >= s["mastery_target"]:
                record_and_advance(s, correct, spoken, score, True)
                return jsonify({**base, "mastery_mode_done": True, "advance": True, **session_payload(s)})
            return jsonify({**base, "mastery_mode": True, "streak_broken": False, **session_payload(s)})
        s["mastery_consecutive"] = 0
        return jsonify({**base, "mastery_mode": True, "streak_broken": True, **session_payload(s)})

    # Normal practice: first failure counts; passing after failures triggers mastery repetitions.
    if passed:
        target = mastery_target_for(s["failed_attempts"])
        cw = detect_cloze_word(correct)
        s["mastery_score"] = score
        if target > 0:
            s["mastery_target"] = target
        if cw:
            s["cloze_active"] = True
            s["cloze_word"] = cw
            s["cloze_attempts"] = 0
            return jsonify({**base, "cloze_mode": True, **session_payload(s)})
        if target > 0:
            return jsonify({**base, "mastery_mode": True, "first_pass": True, **session_payload(s)})
        record_and_advance(s, correct, spoken, score, True)
        return jsonify({**base, "advance": True, **session_payload(s)})

    s["failed_attempts"] += 1
    if s["failed_attempts"] >= s["max_attempts"]:
        record_and_advance(s, correct, spoken, score, False, skipped=True)
        return jsonify({**base, "skipped": True, "advance": True, **session_payload(s)})
    return jsonify({**base, "attempts_left": s["max_attempts"] - s["failed_attempts"], **session_payload(s)})

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
    score = similarity(data.get("spoken", ""), data.get("correct", ""))
    return jsonify(score=score, passed=score >= threshold, words=word_level(data.get("spoken", ""), data.get("correct", "")))

@app.post("/api/exam-result")
def exam_result():
    data = request.get_json(force=True)
    s = _sessions.get(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    row = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "teacher_id": s["teacher_id"],
        "student_name": s["student_name"], "exercise": s["exercise_name"], "phase": "final_exam",
        "sentence": data.get("sentence", ""), "spoken": data.get("spoken", ""),
        "score": data.get("score", 0), "passed": data.get("passed", False), "attempts": 1, "mastery_reps": 0,
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
    return jsonify(ok=True, teacher=teacher_public(tid))

@app.post("/api/teacher-settings")
def teacher_settings():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    _teacher_state[tid]["threshold"] = max(80, min(100, int(data.get("threshold", _teacher_state[tid]["threshold"]))))
    _teacher_state[tid]["max_attempts"] = max(4, min(7, int(data.get("max_attempts", _teacher_state[tid]["max_attempts"]))))
    save_state()
    return jsonify(ok=True, teacher=teacher_public(tid))

@app.post("/api/catalog")
def api_catalog():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    return jsonify(ok=True, exercises=load_catalog("en"))

@app.post("/api/set-exercise")
def set_exercise():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify(ok=False), 401
    _teacher_state[tid]["exercise_name"] = data.get("name", "תרגול דמו")
    _teacher_state[tid]["csv_url"] = data.get("csv_url", "")
    save_state()
    return jsonify(ok=True, teacher=teacher_public(tid), sentence_count=len(load_sentences_from_csv(data.get("csv_url", ""))))

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
            students.append({
                "name": s["student_name"], "index": s["current"], "total": len(s["sentences"]),
                "done": s["current"] >= len(s["sentences"]), "exercise": s["exercise_name"],
                "threshold": s["threshold"], "max_attempts": s["max_attempts"],
                "failed_attempts": s["failed_attempts"], "mastery_target": s["mastery_target"],
                "mastery_consecutive": s["mastery_consecutive"],
            })
    return jsonify(ok=True, teacher=teacher_public(tid), students=students)

if __name__ == "__main__":
    app.run(debug=True)
