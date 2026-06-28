"""
EZRA Demo v2 — Multi-teacher Flask backend
"""
import os, json, time, re, csv, io, random
from flask import Flask, request, jsonify, Response
from difflib import SequenceMatcher
import requests

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Runtime teacher settings (updated via dashboard, reset on restart) ──────
_teacher_runtime = {}   # teacher_id -> {threshold, max_attempts}

# ── Teacher configurations ────────────────────────────────────────────────
TEACHERS = {
    "ben": {
        "name": "בן",
        "color": "#1a56db",
        "color_light": "#e8f0fe",
        "voice_gender": "male",
        "sentences_sheet_id": os.environ.get(
            "BEN_SENTENCES_SHEET",
            "134GzKi9KWNCP_avNg5Z7drhHp3Re7RRALrNrDcOeFnk"),
        "results_sheet_id": os.environ.get(
            "BEN_RESULTS_SHEET",
            "17a-y_-nL9L85Kl7zL1F1ovTGbQy7q5NlBX24C-a_6JU"),
        "results_tab": "Ben",
        "student_password": os.environ.get("BEN_STUDENT_PASSWORD", ""),
        "teacher_password": os.environ.get("BEN_TEACHER_PASSWORD", "ben2026"),
        "default_threshold": int(os.environ.get("BEN_THRESHOLD", "85")),
        "default_max_attempts": int(os.environ.get("BEN_MAX_ATTEMPTS", "5")),
    },
    "sara": {
        "name": "שרה",
        "color": "#be185d",
        "color_light": "#fce8f3",
        "voice_gender": "female",
        "sentences_sheet_id": os.environ.get(
            "SARA_SENTENCES_SHEET",
            "134GzKi9KWNCP_avNg5Z7drhHp3Re7RRALrNrDcOeFnk"),
        "results_sheet_id": os.environ.get(
            "SARA_RESULTS_SHEET",
            "17a-y_-nL9L85Kl7zL1F1ovTGbQy7q5NlBX24C-a_6JU"),
        "results_tab": "Sara",
        "student_password": os.environ.get("SARA_STUDENT_PASSWORD", ""),
        "teacher_password": os.environ.get("SARA_TEACHER_PASSWORD", "sara2026"),
        "default_threshold": int(os.environ.get("SARA_THRESHOLD", "85")),
        "default_max_attempts": int(os.environ.get("SARA_MAX_ATTEMPTS", "5")),
    },
}

def t_threshold(tid):
    return _teacher_runtime.get(tid, {}).get(
        "threshold", TEACHERS[tid]["default_threshold"])

def t_max_attempts(tid):
    return _teacher_runtime.get(tid, {}).get(
        "max_attempts", TEACHERS[tid]["default_max_attempts"])

def t_exercise(tid):
    """Return (exercise_name, csv_url) for teacher's currently selected exercise."""
    return _teacher_runtime.get(tid, {}).get("exercise", (None, None))

# ── Sentence catalog + loading ────────────────────────────────────────────
_sentences_cache = {}   # csv_url -> (timestamp, [sentences])
CACHE_TTL = 3600        # 1 hour

# Catalog sheet ID (the master list of exercises)
CATALOG_SHEET_ID = os.environ.get(
    "CATALOG_SHEET_ID",
    "134GzKi9KWNCP_avNg5Z7drhHp3Re7RRALrNrDcOeFnk")

FALLBACK_SENTENCES = [
    {"he": "אני אוהב ללמוד אנגלית", "en": "I love learning English"},
    {"he": "היום יום יפה", "en": "Today is a beautiful day"},
    {"he": "אני רוצה לדבר אנגלית בשטף", "en": "I want to speak English fluently"},
    {"he": "המורה עוזרת לי להתקדם", "en": "The teacher helps me improve"},
    {"he": "אני מתרגל כל יום", "en": "I practice every day"},
]

def load_sentences_from_csv_url(csv_url):
    """
    Load sentences from a published CSV URL.
    Format: column A = English, column B = Hebrew (NO header row).
    """
    csv_url = csv_url.strip()
    now = time.time()
    cached = _sentences_cache.get(csv_url)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    try:
        r = requests.get(csv_url, timeout=15)
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        sentences = []
        for row in reader:
            if len(row) < 2:
                continue
            en = row[0].strip()
            he = row[1].strip()
            # Skip empty rows or rows that look like headers
            if not en or not he:
                continue
            if en.lower() in ("english", "en", "sentence"):
                continue
            if len(en.split()) < 2:
                continue
            sentences.append({"en": en, "he": he})

        if sentences:
            _sentences_cache[csv_url] = (now, sentences)
            return sentences

    except Exception as e:
        print(f"[sheets] load_sentences error: {e}")

    return FALLBACK_SENTENCES

def load_catalog(lang_filter="en"):
    """
    Load exercise catalog from the master sheet.
    Returns list of {name, url, csv_url} dicts filtered by lang.
    """
    cache_key = f"catalog_{lang_filter}"
    now = time.time()
    cached = _sentences_cache.get(cache_key)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]

    sheet_url = (
        f"https://docs.google.com/spreadsheets/d/{CATALOG_SHEET_ID}"
        f"/gviz/tq?tqx=out:csv&sheet=Sheet1"
    )
    try:
        r = requests.get(sheet_url, timeout=15)
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        exercises = []
        for row in reader:
            if len(row) < 3:
                continue
            name    = row[0].strip().strip('"')
            app_url = row[2].strip().strip('"')
            if not app_url or "ezra" not in app_url:
                continue
            # Extract lang and link params
            import urllib.parse as urlparse
            try:
                parsed = urlparse.urlparse(app_url)
                params = urlparse.parse_qs(parsed.query)
                lang     = params.get("lang", [""])[0]
                csv_link = params.get("link", [""])[0].strip()
            except Exception:
                continue

            if lang_filter and lang != lang_filter:
                continue
            if not csv_link:
                continue
            if not name:
                continue

            exercises.append({
                "name":    name,
                "url":     app_url,
                "csv_url": csv_link,
                "lang":    lang,
            })

        _sentences_cache[cache_key] = (now, exercises)
        return exercises

    except Exception as e:
        print(f"[catalog] load error: {e}")
        return []

def get_teacher_sentences(tid):
    """Get sentences for a teacher based on their selected exercise."""
    _, csv_url = t_exercise(tid)
    if csv_url:
        return load_sentences_from_csv_url(csv_url)
    return FALLBACK_SENTENCES

# ── Student sessions ──────────────────────────────────────────────────────
# key: student_id
# value: {teacher_id, queue, index, attempts, mastery_consecutive,
#         mastery_target, cloze_word, cloze_attempts, results, done}
_sessions = {}

MASTERY_PHASES = [("normal", 0), ("cloze", 0), ("mastery", 3)]

COMMON_VERBS = {
    "be","is","are","was","were","been","being",
    "have","has","had","having",
    "do","does","did","done","doing",
    "go","goes","went","gone","going",
    "say","says","said","saying",
    "get","gets","got","gotten","getting",
    "make","makes","made","making",
    "know","knows","knew","known","knowing",
    "think","thinks","thought","thinking",
    "take","takes","took","taken","taking",
    "see","sees","saw","seen","seeing",
    "come","comes","came","coming",
    "want","wants","wanted","wanting",
    "look","looks","looked","looking",
    "use","uses","used","using",
    "find","finds","found","finding",
    "give","gives","gave","given","giving",
    "tell","tells","told","telling",
    "work","works","worked","working",
    "call","calls","called","calling",
    "try","tries","tried","trying",
    "ask","asks","asked","asking",
    "need","needs","needed","needing",
    "feel","feels","felt","feeling",
    "become","becomes","became","becoming",
    "leave","leaves","left","leaving",
    "put","puts","putting",
    "mean","means","meant","meaning",
    "keep","keeps","kept","keeping",
    "let","lets","letting",
    "begin","begins","began","begun","beginning",
    "show","shows","showed","shown","showing",
    "hear","hears","heard","hearing",
    "play","plays","played","playing",
    "run","runs","ran","running",
    "move","moves","moved","moving",
    "live","lives","lived","living",
    "believe","believes","believed","believing",
    "hold","holds","held","holding",
    "bring","brings","brought","bringing",
    "happen","happens","happened","happening",
    "write","writes","wrote","written","writing",
    "provide","provides","provided","providing",
    "sit","sits","sat","sitting",
    "stand","stands","stood","standing",
    "lose","loses","lost","losing",
    "pay","pays","paid","paying",
    "meet","meets","met","meeting",
    "include","includes","included","including",
    "continue","continues","continued","continuing",
    "set","sets","setting",
    "learn","learns","learned","learning",
    "change","changes","changed","changing",
    "lead","leads","led","leading",
    "understand","understands","understood","understanding",
    "watch","watches","watched","watching",
    "follow","follows","followed","following",
    "stop","stops","stopped","stopping",
    "speak","speaks","spoke","spoken","speaking",
    "read","reads","reading",
    "spend","spends","spent","spending",
    "grow","grows","grew","grown","growing",
    "open","opens","opened","opening",
    "walk","walks","walked","walking",
    "win","wins","won","winning",
    "offer","offers","offered","offering",
    "remember","remembers","remembered","remembering",
    "love","loves","loved","loving",
    "consider","considers","considered","considering",
    "appear","appears","appeared","appearing",
    "buy","buys","bought","buying",
    "wait","waits","waited","waiting",
    "serve","serves","served","serving",
    "die","dies","died","dying",
    "send","sends","sent","sending",
    "expect","expects","expected","expecting",
    "build","builds","built","building",
    "stay","stays","stayed","staying",
    "fall","falls","fell","fallen","falling",
    "cut","cuts","cutting",
    "reach","reaches","reached","reaching",
    "kill","kills","killed","killing",
    "remain","remains","remained","remaining",
    "suggest","suggests","suggested","suggesting",
    "raise","raises","raised","raising",
    "pass","passes","passed","passing",
    "sell","sells","sold","selling",
    "require","requires","required","requiring",
    "report","reports","reported","reporting",
    "decide","decides","decided","deciding",
    "pull","pulls","pulled","pulling",
    "help","helps","helped","helping",
    "start","starts","started","starting",
    "study","studies","studied","studying",
    "eat","eats","ate","eaten","eating",
    "drink","drinks","drank","drunk","drinking",
    "sleep","sleeps","slept","sleeping",
    "drive","drives","drove","driven","driving",
    "swim","swims","swam","swum","swimming",
    "fly","flies","flew","flown","flying",
    "sing","sings","sang","sung","singing",
    "dance","dances","danced","dancing",
    "smile","smiles","smiled","smiling",
    "laugh","laughs","laughed","laughing",
    "cry","cries","cried","crying",
    "travel","travels","traveled","traveling",
    "visit","visits","visited","visiting",
    "enjoy","enjoys","enjoyed","enjoying",
    "choose","chooses","chose","chosen","choosing",
    "create","creates","created","creating",
    "share","shares","shared","sharing",
}

def normalize(s):
    return re.sub(r"[^a-z ]", "", s.lower()).strip()

def detect_cloze_word(sentence):
    words = normalize(sentence).split()
    if len(words) < 3:
        return None

    def is_verb(w):
        if w in COMMON_VERBS:
            return True
        if w.endswith("ed") and (w[:-2] in COMMON_VERBS or w[:-1] in COMMON_VERBS):
            return True
        if w.endswith("ing") and w[:-3] in COMMON_VERBS:
            return True
        if w.endswith("s") and w[:-1] in COMMON_VERBS:
            return True
        return False

    for w in words[1:]:
        if is_verb(w):
            return w

    candidates = [(len(w), w) for w in words[1:-1] if len(w) > 4]
    if candidates:
        return sorted(candidates, reverse=True)[0][1]
    return None

# ── Scoring ────────────────────────────────────────────────────────────────
PASS_THRESHOLD = 85  # global fallback

def similarity(a, b):
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0
    base = round(SequenceMatcher(None, a, b).ratio() * 100)
    # Bonus: all words present
    wa, wb = set(a.split()), set(b.split())
    coverage = len(wa & wb) / max(len(wb), 1)
    bonus = round(coverage * 10)
    return min(100, base + bonus)

def word_level(spoken, correct):
    sw = normalize(spoken).split()
    cw = normalize(correct).split()
    sm = SequenceMatcher(None, sw, cw)
    result = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for w in cw[j1:j2]:
                result.append({"word": w, "status": "correct"})
        elif tag in ("replace", "delete"):
            for w in cw[j1:j2]:
                result.append({"word": w, "status": "wrong"})
        elif tag == "insert":
            for w in cw[j1:j2]:
                result.append({"word": w, "status": "missing"})
    return result

# ── Session helpers ────────────────────────────────────────────────────────
MASTERY_TARGET = 3

def new_session(student_id, teacher_id):
    sentences = list(get_teacher_sentences(teacher_id))
    random.shuffle(sentences)
    _sessions[student_id] = {
        "teacher_id":           teacher_id,
        "queue":                sentences,
        "index":                0,
        "attempts":             0,
        "phase":                "normal",   # normal | cloze | mastery
        "mastery_consecutive":  0,
        "mastery_target":       MASTERY_TARGET,
        "cloze_word":           None,
        "cloze_attempts":       0,
        "results":              [],
        "done":                 False,
    }

def get_session(student_id):
    return _sessions.get(student_id)

# ── Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    with open(os.path.join(BASE_DIR, "index.html"), "rb") as f:
        content = f.read()
    return Response(content, content_type="text/html; charset=utf-8")

@app.route("/teacher")
def teacher_page():
    with open(os.path.join(BASE_DIR, "teacher.html"), "rb") as f:
        content = f.read()
    return Response(content, content_type="text/html; charset=utf-8")

@app.route("/api/teachers", methods=["GET"])
def api_teachers():
    return jsonify({
        tid: {
            "name":         t["name"],
            "color":        t["color"],
            "color_light":  t["color_light"],
            "voice_gender": t.get("voice_gender", "female"),
            "threshold":    t_threshold(tid),
            "max_attempts": t_max_attempts(tid),
        }
        for tid, t in TEACHERS.items()
    })

@app.route("/api/verify-student", methods=["POST"])
def verify_student():
    data     = request.get_json(force=True)
    teacher_id = data.get("teacher_id", "")
    name     = data.get("name", "").strip()
    password = data.get("password", "")

    if teacher_id not in TEACHERS:
        return jsonify({"ok": False, "error": "unknown teacher"}), 400

    expected = TEACHERS[teacher_id]["student_password"]
    if expected and password != expected:
        return jsonify({"ok": False}), 401

    student_id = f"{teacher_id}_{name}_{int(time.time())}"
    new_session(student_id, teacher_id)
    return jsonify({"ok": True, "student_id": student_id})

@app.route("/api/question", methods=["GET"])
def question():
    student_id = request.args.get("student", "")
    sess = get_session(student_id)
    if not sess:
        return jsonify({"error": "session not found"}), 404

    tid = sess["teacher_id"]
    queue = sess["queue"]
    idx   = sess["index"]

    if idx >= len(queue):
        results = sess["results"]
        return jsonify({"done": True, "results": results})

    q = queue[idx]
    phase = sess["phase"]

    resp = {
        "he":    q["he"],
        "en":    q["en"],
        "index": idx,
        "total": len(queue),
        "phase": phase,
        "mastery_target":      sess["mastery_target"] if phase == "mastery" else 0,
        "mastery_consecutive": sess["mastery_consecutive"] if phase == "mastery" else 0,
        "cloze_word":          sess["cloze_word"] if phase == "cloze" else None,
    }
    return jsonify(resp)

@app.route("/api/answer", methods=["POST"])
def answer():
    data       = request.get_json(force=True)
    student_id = data.get("student", "")
    spoken     = data.get("answer", "")
    sess       = get_session(student_id)
    if not sess:
        return jsonify({"error": "session not found"}), 404

    tid   = sess["teacher_id"]
    threshold    = t_threshold(tid)
    max_attempts = t_max_attempts(tid)
    queue = sess["queue"]
    idx   = sess["index"]
    q     = queue[idx]
    phase = sess["phase"]

    score  = similarity(spoken, q["en"])
    passed = score >= threshold
    words  = word_level(spoken, q["en"])

    # ── CLOZE phase ────────────────────────────────────────────────────────
    if phase == "cloze":
        sess["cloze_attempts"] += 1
        cloze_word = sess["cloze_word"]

        # Check if cloze word present in spoken
        cloze_ok = cloze_word and (
            cloze_word in normalize(spoken).split()
        )
        overall_ok = passed and cloze_ok

        if overall_ok:
            # Advance to mastery
            sess["phase"] = "mastery"
            sess["mastery_consecutive"] = 0
            sess["mastery_target"] = MASTERY_TARGET
            return jsonify({
                "score": score, "passed": True, "words": words,
                "cloze_mode": True, "cloze_done": True,
                "cloze_word": cloze_word,
                "mastery_mode": True,
                "mastery_target": MASTERY_TARGET,
                "mastery_consecutive": 0,
                "advance": False,
            })
        else:
            # Failed cloze attempt
            if sess["cloze_attempts"] >= max_attempts:
                # Give up on cloze, go to mastery anyway
                sess["phase"] = "mastery"
                sess["mastery_consecutive"] = 0
                sess["mastery_target"] = MASTERY_TARGET
                return jsonify({
                    "score": score, "passed": False, "words": words,
                    "cloze_mode": True, "cloze_done": True,
                    "cloze_word": cloze_word,
                    "cloze_attempts_left": 0,
                    "mastery_mode": True,
                    "mastery_target": MASTERY_TARGET,
                    "mastery_consecutive": 0,
                    "advance": False,
                })
            return jsonify({
                "score": score, "passed": False, "words": words,
                "cloze_mode": True, "cloze_done": False,
                "cloze_word": cloze_word,
                "cloze_attempts_left": max_attempts - sess["cloze_attempts"],
            })

    # ── MASTERY phase ──────────────────────────────────────────────────────
    if phase == "mastery":
        target = sess["mastery_target"]
        if passed:
            sess["mastery_consecutive"] += 1
        else:
            broken = sess["mastery_consecutive"] > 0
            sess["mastery_consecutive"] = 0
            return jsonify({
                "score": score, "passed": False, "words": words,
                "mastery_mode": True,
                "mastery_target": target,
                "mastery_consecutive": 0,
                "streak_broken": broken,
                "advance": False,
            })

        consecutive = sess["mastery_consecutive"]
        if consecutive >= target:
            # Mastery achieved — advance
            sess["results"].append({
                "sentence": q["en"],
                "score":    score,
                "passed":   True,
                "skipped":  False,
            })
            sess["index"]               += 1
            sess["attempts"]             = 0
            sess["phase"]                = "normal"
            sess["mastery_consecutive"]  = 0
            sess["cloze_word"]           = None
            sess["cloze_attempts"]       = 0
            _write_result(tid, student_id, q, score, True)
            return jsonify({
                "score": score, "passed": True, "words": words,
                "mastery_mode": True,
                "mastery_target": target,
                "mastery_consecutive": consecutive,
                "advance": True,
            })

        return jsonify({
            "score": score, "passed": True, "words": words,
            "mastery_mode": True,
            "mastery_target": target,
            "mastery_consecutive": consecutive,
            "advance": False,
        })

    # ── NORMAL phase ───────────────────────────────────────────────────────
    sess["attempts"] += 1

    if passed:
        # Enter cloze phase
        cloze_word = detect_cloze_word(q["en"])
        if cloze_word:
            sess["phase"] = "cloze"
            sess["cloze_word"] = cloze_word
            sess["cloze_attempts"] = 0
            return jsonify({
                "score": score, "passed": True, "words": words,
                "cloze_mode": None,
                "cloze_word": cloze_word,
                "first_pass": True,
            })
        else:
            # No cloze word — go straight to mastery
            sess["phase"] = "mastery"
            sess["mastery_consecutive"] = 0
            sess["mastery_target"] = MASTERY_TARGET
            return jsonify({
                "score": score, "passed": True, "words": words,
                "first_pass": True,
                "mastery_mode": True,
                "mastery_target": MASTERY_TARGET,
                "mastery_consecutive": 0,
                "advance": False,
            })

    # Failed normal attempt
    if sess["attempts"] >= max_attempts:
        # Skip sentence
        sess["results"].append({
            "sentence": q["en"],
            "score":    score,
            "passed":   False,
            "skipped":  True,
        })
        sess["index"]    += 1
        sess["attempts"]  = 0
        sess["phase"]     = "normal"
        _write_result(tid, student_id, q, score, False)
        return jsonify({
            "score": score, "passed": False, "words": words,
            "skipped": True,
        })

    return jsonify({
        "score":    score,
        "passed":   False,
        "words":    words,
        "threshold": threshold,
        "advance":  False,
    })

@app.route("/api/skip", methods=["POST"])
def skip():
    data       = request.get_json(force=True)
    student_id = data.get("student", "")
    sess = get_session(student_id)
    if not sess:
        return jsonify({"error": "session not found"}), 404

    tid = sess["teacher_id"]
    queue = sess["queue"]
    idx   = sess["index"]

    if idx < len(queue):
        q = queue[idx]
        sess["results"].append({
            "sentence": q["en"],
            "score":    0,
            "passed":   False,
            "skipped":  True,
        })
        _write_result(tid, student_id, q, 0, False)

    sess["index"]    += 1
    sess["attempts"]  = 0
    sess["phase"]     = "normal"
    sess["cloze_word"] = None
    sess["cloze_attempts"] = 0
    sess["mastery_consecutive"] = 0
    return jsonify({"ok": True})

@app.route("/api/score-only", methods=["POST"])
def score_only():
    data    = request.get_json(force=True)
    spoken  = data.get("spoken", "")
    correct = data.get("correct", "")
    tid     = data.get("teacher_id", "ben")
    threshold = t_threshold(tid) if tid in TEACHERS else PASS_THRESHOLD
    score   = similarity(spoken, correct)
    passed  = score >= threshold
    words   = word_level(spoken, correct)
    return jsonify(score=score, passed=passed, words=words, threshold=threshold)

@app.route("/api/reset", methods=["POST"])
def reset():
    data       = request.get_json(force=True)
    student_id = data.get("student", "")
    teacher_id = data.get("teacher_id", "ben")
    if student_id in _sessions:
        del _sessions[student_id]
    new_session(student_id, teacher_id)
    return jsonify({"ok": True})

# ── Teacher dashboard API ──────────────────────────────────────────────────

@app.route("/api/teacher-login", methods=["POST"])
def teacher_login():
    data     = request.get_json(force=True)
    tid      = data.get("teacher_id", "")
    password = data.get("password", "")
    if tid not in TEACHERS:
        return jsonify({"ok": False}), 400
    if password != TEACHERS[tid]["teacher_password"]:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "teacher": {
        "name":         TEACHERS[tid]["name"],
        "color":        TEACHERS[tid]["color"],
        "color_light":  TEACHERS[tid]["color_light"],
        "threshold":    t_threshold(tid),
        "max_attempts": t_max_attempts(tid),
    }})

@app.route("/api/teacher-settings", methods=["POST"])
def teacher_settings():
    data     = request.get_json(force=True)
    tid      = data.get("teacher_id", "")
    password = data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify({"ok": False}), 401

    rt = _teacher_runtime.setdefault(tid, {})
    if "threshold" in data:
        rt["threshold"] = max(80, min(100, int(data["threshold"])))
    if "max_attempts" in data:
        rt["max_attempts"] = max(4, min(7, int(data["max_attempts"])))

    return jsonify({"ok": True, "threshold": t_threshold(tid), "max_attempts": t_max_attempts(tid)})

@app.route("/api/teacher-results", methods=["POST"])
def teacher_results():
    data     = request.get_json(force=True)
    tid      = data.get("teacher_id", "")
    password = data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify({"ok": False}), 401

    # Fetch from Google Sheets
    sheet_id = TEACHERS[tid]["results_sheet_id"]
    tab      = TEACHERS[tid]["results_tab"]
    url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/gviz/tq?tqx=out:csv&sheet={tab}"
    )
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        reader = csv.DictReader(io.StringIO(r.text))
        rows = [dict(row) for row in reader]
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:
        return jsonify({"ok": True, "rows": [], "note": str(e)})

@app.route("/api/reload-sentences", methods=["POST"])
def reload_sentences():
    data     = request.get_json(force=True)
    tid      = data.get("teacher_id", "")
    password = data.get("password", "")
    if tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]:
        return jsonify({"ok": False}), 401
    _, csv_url