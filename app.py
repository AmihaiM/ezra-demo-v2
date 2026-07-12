import os, json, time, re, csv, io
from urllib.parse import urlparse, parse_qs, quote
from difflib import SequenceMatcher
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, Response
import requests

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_TTL = 3600
STATE_FILE = os.path.join(BASE_DIR, "teacher_state.json")
IL_TZ = ZoneInfo("Asia/Jerusalem")

def now_str():
    # Render's server clock runs in UTC. Every result timestamp shown to
    # teachers/students must be in Israel local time (including DST), not
    # raw server time - previously time.strftime() used the server's local
    # (UTC) clock directly, so timestamps in the results table were off by
    # 2-3 hours from the actual wall-clock time in Israel.
    return datetime.now(IL_TZ).strftime("%Y-%m-%d %H:%M:%S")

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

# --- Built-in default CEFR-leveled curriculum -------------------------------
# Used only when a teacher has NOT selected a specific exercise (csv_url is
# empty). Every new/default student starts at A1 and advances automatically
# (see get_student_level/set_student_level + the auto-advance check in
# /api/question) as they demonstrate strong, consistent mastery.
CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

LEVEL_NAMES_HE = {
    "A1": "רמה A1 — מתחילים",
    "A2": "רמה A2 — בסיסי",
    "B1": "רמה B1 — בינוני",
    "B2": "רמה B2 — בינוני-גבוה",
    "C1": "רמה C1 — מתקדם",
    "C2": "רמה C2 — שליטה מלאה",
}

LEVEL_SENTENCES = {
    "A1": [
        {"en": "I am a student", "he": "אני תלמיד"},
        {"en": "This is my book", "he": "זה הספר שלי"},
        {"en": "She has a red car", "he": "יש לה מכונית אדומה"},
        {"en": "We live in Tel Aviv", "he": "אנחנו גרים בתל אביב"},
        {"en": "He likes coffee", "he": "הוא אוהב קפה"},
        {"en": "The cat is on the table", "he": "החתול על השולחן"},
        {"en": "I am hungry now", "he": "אני רעב עכשיו"},
        {"en": "My name is David", "he": "קוראים לי דוד"},
        {"en": "They are my friends", "he": "הם החברים שלי"},
        {"en": "Can you help me, please", "he": "אתה יכול לעזור לי, בבקשה"},
        {"en": "What time is it", "he": "מה השעה"},
        {"en": "I have two brothers", "he": "יש לי שני אחים"},
        {"en": "The weather is nice today", "he": "מזג האוויר נעים היום"},
        {"en": "She works in a bank", "he": "היא עובדת בבנק"},
        {"en": "We eat breakfast at eight", "he": "אנחנו אוכלים ארוחת בוקר בשמונה"},
    ],
    "A2": [
        {"en": "Yesterday I went to school", "he": "אתמול הלכתי לבית הספר"},
        {"en": "We will meet at six o'clock", "he": "ניפגש בשעה שש"},
        {"en": "I bought a new phone last week", "he": "קניתי טלפון חדש בשבוע שעבר"},
        {"en": "She was very tired after work", "he": "היא הייתה עייפה מאוד אחרי העבודה"},
        {"en": "Can I have the bill, please", "he": "אפשר לקבל את החשבון, בבקשה"},
        {"en": "He is going to visit his parents", "he": "הוא הולך לבקר את ההורים שלו"},
        {"en": "I usually wake up early in the morning", "he": "אני בדרך כלל קם מוקדם בבוקר"},
        {"en": "They watched a movie last night", "he": "הם צפו בסרט אמש"},
        {"en": "Do you know where the station is", "he": "אתה יודע איפה התחנה"},
        {"en": "It was raining all day yesterday", "he": "ירד גשם כל היום אתמול"},
        {"en": "We are planning a trip to Eilat", "he": "אנחנו מתכננים טיול לאילת"},
        {"en": "I need to buy some vegetables", "he": "אני צריך לקנות ירקות"},
        {"en": "My sister is learning to drive", "he": "אחותי לומדת לנהוג"},
        {"en": "He never eats breakfast", "he": "הוא אף פעם לא אוכל ארוחת בוקר"},
        {"en": "Please turn off the lights before you leave", "he": "בבקשה כבה את האורות לפני שאתה יוצא"},
    ],
    "B1": [
        {"en": "I have never been to Paris", "he": "מעולם לא הייתי בפריז"},
        {"en": "If it rains, we will stay home", "he": "אם ירד גשם, נישאר בבית"},
        {"en": "She has already finished her homework", "he": "היא כבר סיימה את שיעורי הבית שלה"},
        {"en": "I think this restaurant is too expensive", "he": "אני חושב שהמסעדה הזאת יקרה מדי"},
        {"en": "Have you ever tried sushi before", "he": "ניסית פעם סושי"},
        {"en": "We have been living here for five years", "he": "אנחנו גרים כאן כבר חמש שנים"},
        {"en": "If I had more time, I would travel more", "he": "אם היה לי יותר זמן, הייתי מטייל יותר"},
        {"en": "He apologized for being late to the meeting", "he": "הוא התנצל על האיחור לפגישה"},
        {"en": "I'm not sure if I agree with your opinion", "he": "אני לא בטוח שאני מסכים עם הדעה שלך"},
        {"en": "They have decided to move to a bigger apartment", "he": "הם החליטו לעבור לדירה גדולה יותר"},
        {"en": "She would like to become a doctor someday", "he": "היא הייתה רוצה להיות רופאה יום אחד"},
        {"en": "It seems like the traffic is getting worse", "he": "נראה שהתנועה נהיית גרועה יותר"},
        {"en": "I should have called you earlier", "he": "הייתי צריך להתקשר אליך קודם"},
        {"en": "We were supposed to meet an hour ago", "he": "היינו אמורים להיפגש לפני שעה"},
        {"en": "Although it was expensive, we bought the tickets", "he": "למרות שזה היה יקר, קנינו את הכרטיסים"},
    ],
    "B2": [
        {"en": "The report was submitted before the deadline", "he": "הדוח הוגש לפני המועד האחרון"},
        {"en": "She said that she would call me later", "he": "היא אמרה שהיא תתקשר אליי מאוחר יותר"},
        {"en": "The building is being renovated this month", "he": "הבניין משופץ בחודש הזה"},
        {"en": "He mentioned that the project had been delayed", "he": "הוא ציין שהפרויקט התעכב"},
        {"en": "It's not worth arguing about such a small issue", "he": "זה לא שווה להתווכח על עניין כל כך קטן"},
        {"en": "The decision was made without consulting the team", "he": "ההחלטה התקבלה בלי להתייעץ עם הצוות"},
        {"en": "I'm afraid I have to disagree with that statement", "he": "אני חושש שאני צריך לחלוק על ההצהרה הזאת"},
        {"en": "The company was founded over twenty years ago", "he": "החברה נוסדה לפני יותר מעשרים שנה"},
        {"en": "Despite the challenges, the team met its goals", "he": "למרות האתגרים, הצוות עמד ביעדים שלו"},
        {"en": "He was accused of breaking the rules", "he": "הוא הואשם בהפרת הכללים"},
        {"en": "The manager insisted that changes be made immediately", "he": "המנהל התעקש שהשינויים ייעשו מיד"},
        {"en": "I wish I had studied harder for the exam", "he": "הלוואי שהייתי לומד יותר בשקידה למבחן"},
        {"en": "The new policy will be implemented next quarter", "he": "המדיניות החדשה תיושם ברבעון הבא"},
        {"en": "She's been putting off the decision for weeks", "he": "היא דוחה את ההחלטה כבר שבועות"},
        {"en": "It turned out that the rumor was completely false", "he": "התברר שהשמועה הייתה שקרית לחלוטין"},
    ],
    "C1": [
        {"en": "Rarely have I seen such dedication to a project", "he": "לעיתים רחוקות ראיתי מסירות כזאת לפרויקט"},
        {"en": "Had I known earlier, I would have acted differently", "he": "אילו ידעתי מוקדם יותר, הייתי פועל אחרת"},
        {"en": "Not only did she finish first, but she also broke the record", "he": "היא לא רק סיימה ראשונה, אלא גם שברה את השיא"},
        {"en": "It is essential that every detail be verified beforehand", "he": "חיוני שכל פרט ייבדק מראש"},
        {"en": "Little did they know how much the decision would cost them", "he": "הם לא ידעו כמה ההחלטה תעלה להם"},
        {"en": "The committee recommended that the policy be reconsidered", "he": "הוועדה המליצה שהמדיניות תישקל מחדש"},
        {"en": "Seldom do we encounter such a compelling argument", "he": "לעיתים רחוקות אנו נתקלים בטיעון משכנע כל כך"},
        {"en": "Were it not for her guidance, the project would have failed", "he": "לולא ההדרכה שלה, הפרויקט היה נכשל"},
        {"en": "The findings, though preliminary, suggest a clear trend", "he": "הממצאים, אף שהם ראשוניים, מצביעים על מגמה ברורה"},
        {"en": "He is said to have influenced an entire generation of writers", "he": "אומרים שהוא השפיע על דור שלם של סופרים"},
        {"en": "Under no circumstances should this document be shared externally", "he": "בשום פנים ואופן אין לשתף את המסמך הזה מחוץ לארגון"},
        {"en": "So convincing was her argument that no one objected", "he": "הטיעון שלה היה משכנע עד כדי כך שאיש לא התנגד"},
        {"en": "The proposal warrants further consideration before approval", "he": "ההצעה מצדיקה שיקול נוסף לפני האישור"},
        {"en": "Given the circumstances, the outcome was hardly surprising", "he": "לאור הנסיבות, התוצאה בקושי הפתיעה"},
        {"en": "He acted as though nothing unusual had happened", "he": "הוא נהג כאילו לא קרה שום דבר יוצא דופן"},
    ],
    "C2": [
        {"en": "Notwithstanding the setbacks, the initiative persevered", "he": "חרף הנסיגות, היוזמה התמידה"},
        {"en": "The ambiguity of the clause warrants further scrutiny", "he": "העמימות של הסעיף מצריכה בחינה נוספת"},
        {"en": "Her eloquence captivated the entire audience", "he": "הרהיטות שלה ריתקה את כל הקהל"},
        {"en": "The findings corroborate the initial hypothesis", "he": "הממצאים מאששים את ההשערה הראשונית"},
        {"en": "He remained impervious to criticism throughout the ordeal", "he": "הוא נותר חסין לביקורת לאורך כל המבחן"},
        {"en": "The negotiations were fraught with unforeseen complications", "he": "המשא ומתן היה רווי בסיבוכים בלתי צפויים"},
        {"en": "Such an egregious error cannot go unaddressed", "he": "טעות כה חמורה אינה יכולה להישאר ללא טיפול"},
        {"en": "The author's prose is renowned for its subtlety and nuance", "he": "הפרוזה של הסופר ידועה בעדינותה וברבדיה"},
        {"en": "The policy's ramifications are still being assessed", "he": "ההשלכות של המדיניות עדיין נבחנות"},
        {"en": "Her tenacity in the face of adversity was inspiring", "he": "העקשנות שלה מול קשיים הייתה מעוררת השראה"},
        {"en": "The evidence, albeit circumstantial, was compelling", "he": "הראיות, אף שהיו נסיבתיות, היו משכנעות"},
        {"en": "The board's decision was met with unanimous approval", "he": "החלטת הדירקטוריון זכתה לאישור פה אחד"},
        {"en": "His argument, though cogent, failed to sway the jury", "he": "הטיעון שלו, אף שהיה משכנע, לא הצליח לשכנע את חבר המושבעים"},
        {"en": "The city's infrastructure is ill-equipped to handle the surge", "he": "התשתית של העיר אינה מצוידת כראוי להתמודד עם הזינוק"},
        {"en": "It would be remiss of us not to acknowledge her contribution", "he": "זו תהיה רשלנות מצידנו לא להכיר בתרומתה"},
    ],
}

def next_cefr_level(level):
    """Return the next CEFR level, or the same level if already at the top."""
    try:
        i = CEFR_LEVELS.index(level)
    except ValueError:
        return CEFR_LEVELS[0]
    return CEFR_LEVELS[i + 1] if i + 1 < len(CEFR_LEVELS) else level

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

    def is_blanked_variant(c):
        if "___" in c:
            return True
        # Also treat a near-duplicate of the English sentence (just missing a
        # word or two) as a blanked variant rather than a genuine translation.
        return looks_english(c) and c != en and similarity(c, en) >= 70

    # Some exercises encode the SPECIFIC grammar point they're testing as a
    # second English column with an explicit blank (e.g. "I like ____ blue
    # T-shirt..." to drill articles a/an/the) - this is not noise, it's the
    # exercise author's own intended fill-in-the-blank, often targeting short
    # function words (articles, prepositions) that the app's own generic
    # detect_cloze_word() heuristic would never pick (it only looks for verbs
    # or long words). Capture it as "completion" so station 1's prompt, the
    # cloze station, and the final exam can all use the real thing instead of
    # silently discarding it.
    completion = ""
    for i, c in enumerate(cells):
        if i != en_i and "___" in c:
            completion = c
            break

    # Prefer a Hebrew cell different from English. If none, use another descriptive cell as prompt.
    he = ""
    other_he = [x for x in hebrew_candidates if x[0] != en_i]
    if other_he:
        _, he, _ = max(other_he, key=lambda x: x[2])
    else:
        # No real Hebrew translation in this row. Never fall back to the plain
        # English answer itself (that would spoil it) - prefer the teacher's
        # own blanked variant when one exists, since it shows the sentence
        # structure without giving away the tested word; otherwise fall back
        # to the English sentence as a last resort (existing behavior).
        others = [c for i, c in enumerate(cells) if i != en_i and not is_blanked_variant(c)]
        he = others[0] if others else (completion or en)

    # Guard: never let Hebrew prompt become the answer to score against.
    if not looks_english(en) or len(normalize(en).split()) < 1:
        return None
    return {"en": en, "he": he, "completion": completion}

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

def _s_tolerant_word(w):
    # "phone's" and "phones" are homophones - Chrome's speech recognition can
    # (and does) transcribe either spelling for the exact same pronunciation,
    # and apostrophes are already stripped by normalize() above, so "phone's"
    # already becomes "phones" there. What normalize() can't fix is a genuine
    # word-count mismatch: a source sentence reading "my phone light" (missing
    # the possessive marker) vs a spoken/transcribed "my phone's light" -
    # these are not a real pronunciation mistake, just an ASR/content spelling
    # technicality, so a single trailing "s" is ignored for comparison purposes.
    # This is a real, deliberate tradeoff: it also means a genuine singular vs
    # plural slip (e.g. "cat" vs "cats") will no longer be flagged either -
    # acceptable here since this is a spoken-fluency app, not a written-grammar
    # quiz, and ASR cannot reliably distinguish this class of homophone anyway.
    return w[:-1] if len(w) > 2 and w.endswith("s") else w

def _s_tolerant_match(a_words, b_words):
    return len(a_words) == len(b_words) and [_s_tolerant_word(w) for w in a_words] == [_s_tolerant_word(w) for w in b_words]

def similarity(spoken, correct):
    a, b = normalize(spoken), normalize(correct)
    if not a or not b:
        return 0
    if a == b:
        return 100
    if _s_tolerant_match(a.split(), b.split()):
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
    # Align using the s-tolerant keys (see _s_tolerant_word) so this breakdown
    # never contradicts similarity()'s score - a sentence that scores 100%
    # because of the trailing-s tolerance must not still show a word marked
    # "wrong" here, which would look like a contradiction to the student.
    sp_key, co_key = [_s_tolerant_word(w) for w in sp], [_s_tolerant_word(w) for w in co]
    result = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, sp_key, co_key).get_opcodes():
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
    # bonus_attempts is granted on-demand via /api/cap-retry when a student
    # chooses "another round" instead of being auto-advanced after running out
    # of attempts - it raises the effective cap for the CURRENT sentence only
    # and is reset back to 0 the moment that sentence is actually recorded.
    cap = s.get("max_attempts", 5) + s.get("bonus_attempts", 0)
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
        "cloze_display": s.get("cloze_display", ""),
        "cloze_attempts_left": max(0, cap - s.get("cloze_attempts", 0)),
        "attempts_used": used,
        "attempts_left": left,
        "mastery_score": s.get("mastery_score", 0),
    }

def new_session(student_id, teacher_id, student_name, student_email=""):
    ts = _teacher_state[teacher_id]
    csv_url = ts.get("csv_url", "")
    level_track = None
    level_exercise_name = None
    if not csv_url.strip():
        # No teacher-selected exercise: fall back to the built-in, per-student
        # CEFR leveled curriculum (starts at A1, advances automatically) instead
        # of the old 3-sentence generic demo content.
        sentences, level_track = load_level_track_sentences(teacher_id, student_email)
        used_fallback = False
        level_exercise_name = LEVEL_NAMES_HE.get(level_track, "תרגול דמו")
    else:
        sentences, used_fallback = load_sentences_from_csv_ex(csv_url)
    # content_mismatch=True means a real exercise was selected (csv_url is set)
    # but its sheet could not be loaded, so generic demo sentences were used
    # instead - the session/exercise NAME still says the real exercise, so the
    # teacher dashboard and student view must both flag this clearly instead of
    # silently mislabeling demo content as the real exercise.
    content_mismatch = used_fallback and bool(csv_url.strip())
    if content_mismatch:
        # Self-heal instead of just warning forever: the saved link can go
        # stale (e.g. it was computed and saved by a buggy older version of
        # extract_csv_url before that bug was fixed - exactly what happened
        # to "Lesson 14 Advanced Motke"). Rather than requiring the teacher to
        # manually re-select the exercise to force a recompute, look it up by
        # name in the live catalog (which always recomputes csv_url fresh) and
        # retry once. If that works, permanently overwrite the saved link too,
        # so the warning never has to appear again for anyone.
        exercise_name = ts.get("exercise_name", "")
        for item in load_catalog("en"):
            if item.get("name") == exercise_name and item.get("csv_url") and item.get("csv_url") != csv_url:
                retry_sentences, retry_fallback = load_sentences_from_csv_ex(item["csv_url"])
                if not retry_fallback:
                    csv_url = item["csv_url"]
                    sentences, used_fallback = retry_sentences, retry_fallback
                    content_mismatch = False
                    ts["csv_url"] = csv_url
                    save_state()
                break
    _sessions[student_id] = {
        "student_id": student_id,
        "teacher_id": teacher_id,
        "student_name": student_name,
        "student_email": (student_email or "").strip().lower(),
        "threshold": int(ts["threshold"]),
        "max_attempts": int(ts["max_attempts"]),
        "voice_gender": TEACHERS[teacher_id]["voice_gender"],
        "exercise_name": level_exercise_name or ts.get("exercise_name", "תרגול דמו"),
        "csv_url": csv_url,
        "content_mismatch": content_mismatch,
        "level_track": level_track,
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
        "cloze_display": "",
        "cloze_attempts": 0,
        "cloze_passed": False,
        "bonus_attempts": 0,
        "cap_pending": None,
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
    "Time", "Teacher", "Student", "Student Email", "Exercise", "Phase", "Sentence", "Spoken",
    "Score", "Passed", "Skipped", "Attempts", "Max Attempts",
    "Mastery Repetitions", "Mastery Status", "Mastery Score", "Cloze Passed",
    "Recording Duration MS", "Silence MS", "Words Per Minute", "Fluency Status",
    "STT Confidence"
]

RESULT_KEY_ALIASES = {
    "Time": "timestamp", "Teacher": "teacher_id", "Student": "student_name",
    "Student Email": "student_email",
    "Exercise": "exercise", "Phase": "phase", "Sentence": "sentence", "Spoken": "spoken",
    "Score": "score", "Passed": "passed", "Skipped": "skipped", "Attempts": "attempts",
    "Max Attempts": "max_attempts", "Mastery Repetitions": "mastery_reps",
    "Mastery Status": "mastery_status", "Mastery Score": "mastery_score",
    "Cloze Passed": "cloze_passed",
    "Recording Duration MS": "recording_duration_ms",
    "Silence MS": "silence_ms",
    "Words Per Minute": "words_per_minute",
    "Fluency Status": "fluency_status",
    "STT Confidence": "stt_confidence",
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
        # IMPORTANT: do not use ws.append_row() here. It relies on Google Sheets'
        # own auto-detected "used range" of the ENTIRE tab to decide where the
        # next row goes - which silently breaks if a teacher adds any other
        # content anywhere else on the same tab (e.g. a legend/notes table off
        # to the right) that extends further DOWN than the results table itself.
        # That happened for real: a two-column legend added past column U, going
        # down further than the data, made every subsequent append_row() land
        # new rows starting at the legend's column instead of column A - exactly
        # the "new rows are written in the old columns" bug a teacher hit.
        # Writing to an explicit A<row> range anchors every write to column A
        # regardless of what else exists elsewhere on the tab.
        next_row = len(ws.get_all_values()) + 1
        ws.update(f"A{next_row}", [out], value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print("WRITE RESULT FAILED", e)
        return False

STUDENT_LEVELS_TAB = "StudentLevels"
STUDENT_LEVELS_HEADER = ["Student Email", "Current Level", "Updated At"]

def _student_levels_ws(tid):
    sheet_id = RESULTS_SHEET_IDS.get(tid, RESULTS_SHEET_ID)
    sh = get_gspread_client().open_by_key(sheet_id)
    import gspread
    try:
        ws = sh.worksheet(STUDENT_LEVELS_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=STUDENT_LEVELS_TAB, rows=1000, cols=4)
        ws.append_row(STUDENT_LEVELS_HEADER, value_input_option="USER_ENTERED")
    return ws

def get_student_level(tid, email):
    """Look up a student's current CEFR level for this teacher. Defaults to
    the first level (A1) whenever there's no record yet, or on any error -
    a brand-new/never-seen student always starts at the beginning.
    """
    email = (email or "").strip().lower()
    if not email:
        return CEFR_LEVELS[0]
    key = f"studentlevel:{tid}:{email}"
    if key in _cache and time.time() - _cache[key][0] < 300:
        return _cache[key][1]
    level = CEFR_LEVELS[0]
    try:
        ws = _student_levels_ws(tid)
        for row in ws.get_all_values()[1:]:
            if len(row) >= 2 and row[0].strip().lower() == email:
                candidate = row[1].strip().upper()
                if candidate in CEFR_LEVELS:
                    level = candidate
                break
    except Exception as e:
        print("GET STUDENT LEVEL FAILED", e)
    _cache[key] = (time.time(), level)
    return level

def set_student_level(tid, email, level):
    """Persist a student's new CEFR level (e.g. after auto-advancement)."""
    email = (email or "").strip().lower()
    if not email or level not in CEFR_LEVELS:
        return False
    try:
        ws = _student_levels_ws(tid)
        values = ws.get_all_values()
        row_idx = None
        for i, row in enumerate(values[1:], start=2):
            if len(row) >= 1 and row[0].strip().lower() == email:
                row_idx = i
                break
        if row_idx:
            ws.update(f"A{row_idx}", [[email, level, now_str()]], value_input_option="USER_ENTERED")
        else:
            ws.append_row([email, level, now_str()], value_input_option="USER_ENTERED")
        _cache[f"studentlevel:{tid}:{email}"] = (time.time(), level)
        return True
    except Exception as e:
        print("SET STUDENT LEVEL FAILED", e)
        return False

def load_level_track_sentences(tid, email):
    """Return (sentences, level) for the default built-in curriculum, based
    on the student's current saved CEFR level for this teacher.
    """
    level = get_student_level(tid, email)
    sentences = LEVEL_SENTENCES.get(level, LEVEL_SENTENCES[CEFR_LEVELS[0]])
    return sentences[:], level

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
    # STT confidence instrumentation (documentation-only for now, per explicit
    # decision): the Web Speech API exposes a per-result confidence score that
    # the app never read before. The concern this answers is real - the
    # browser's recognition engine can silently "correct" a mispronunciation
    # toward the expected sentence before our own scoring ever sees the text,
    # letting a student pass without actually being understood correctly. But
    # Chrome's confidence values are independently reported as unreliable/flat
    # in many cases, so this is captured and surfaced to the teacher (a new
    # results-sheet column) WITHOUT touching pass/fail scoring - only once the
    # data shows this signal is actually meaningful in practice should it ever
    # be used to adjust scoring.
    stt_confidence = metrics.get("stt_confidence")
    try:
        stt_confidence = round(float(stt_confidence), 2) if stt_confidence is not None else ""
    except Exception:
        stt_confidence = ""
    return {
        "recording_duration_ms": duration_ms,
        "silence_ms": silence_ms,
        "words_per_minute": wpm,
        "fluency_status": status,
        "stt_confidence": stt_confidence,
    }

def record_and_advance(s, correct, spoken, score, passed=True, skipped=False, metrics=None):
    fluency = fluency_from_metrics(spoken, score, metrics)
    # Carry the sentence's teacher-authored fill-in-the-blank variant (if any)
    # through into the stored result row - this is how it survives into the
    # exam sentence pool later (built from s["results"], not re-fetched from
    # the CSV), so the final exam can show the same blanked prompt too.
    current_obj = s["sentences"][s["current"]] if s["current"] < len(s["sentences"]) else {}
    row = {
        "timestamp": now_str(),
        "teacher_id": s["teacher_id"], "student_name": s["student_name"],
        "student_email": s.get("student_email", ""), "exercise": s["exercise_name"],
        "phase": "practice", "sentence": correct, "spoken": spoken, "score": score,
        "passed": bool(passed), "skipped": bool(skipped),
        "attempts": max(1, s.get("sentence_attempts", 0)),
        "max_attempts": s.get("max_attempts", ""),
        "mastery_reps": s.get("mastery_target", 0),
        "mastery_status": "mastered" if passed and not skipped else "not_mastered",
        "mastery_score": s.get("mastery_score", score),
        "cloze_passed": bool(s.get("cloze_passed", False)),
        "completion": current_obj.get("completion", ""),
        **fluency,
    }
    s["results"].append(row)
    write_result(row)
    s["current"] += 1
    s["failed_attempts"] = 0
    s["sentence_attempts"] = 0
    s["stage2_attempts"] = 0
    # Any bonus attempts granted via /api/cap-retry only ever apply to the
    # sentence they were granted for - never let them silently carry over and
    # inflate the cap for every sentence for the rest of the session.
    s["bonus_attempts"] = 0
    s["last_mastery_target"] = s.get("mastery_target", 0)
    s["mastery_target"] = 0
    s["mastery_consecutive"] = 0
    s["mastery_score"] = 0
    s["cloze_active"] = False
    s["cloze_word"] = None
    s["cloze_display"] = ""
    s["cloze_attempts"] = 0
    s["cloze_passed"] = False

@app.route("/")
def home():
    return Response(open(os.path.join(BASE_DIR, "index.html"), "rb").read(), content_type="text/html; charset=utf-8")

@app.route("/teacher")
def teacher():
    return Response(open(os.path.join(BASE_DIR, "teacher.html"), "rb").read(), content_type="text/html; charset=utf-8")

@app.route("/manifest.json")
def manifest():
    return Response(open(os.path.join(BASE_DIR, "manifest.json"), "rb").read(), content_type="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    resp = Response(open(os.path.join(BASE_DIR, "sw.js"), "rb").read(), content_type="application/javascript")
    # Without this header a service worker served from a subpath would only
    # ever be allowed to control that subpath - it needs to control "/" (the
    # student app) even though it's not served from inside a static/ folder.
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/icon-192.png")
def icon_192():
    return Response(open(os.path.join(BASE_DIR, "icon-192.png"), "rb").read(), content_type="image/png")

@app.route("/icon-512.png")
def icon_512():
    return Response(open(os.path.join(BASE_DIR, "icon-512.png"), "rb").read(), content_type="image/png")

@app.get("/api/teachers")
def api_teachers():
    return jsonify({tid: teacher_public(tid) for tid in TEACHERS})

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

@app.post("/api/verify-student")
def verify_student():
    data = request.get_json(force=True)
    tid, name, password = data.get("teacher_id"), data.get("name", "").strip(), data.get("password", "")
    email = (data.get("email") or "").strip().lower()
    if tid not in TEACHERS or not name:
        return jsonify(ok=False, error="bad request"), 400
    # Email is required (not just recommended): it is the stable identifier used
    # to give each student a real, persistent history across logins/devices in
    # "My Results" (/api/my-history) - a freeform name alone collides too easily
    # (two students both named "עמיחי") and a fresh in-memory session ID is
    # generated on every login, so name+session-id gives no real continuity.
    if not email or not EMAIL_RE.match(email):
        return jsonify(ok=False, error="נדרש אימייל תקין כדי להתחבר."), 400
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
    new_session(sid, tid, name, student_email=email)
    return jsonify(ok=True, student_id=sid, teacher=teacher_public(tid), exercise=_sessions[sid]["exercise_name"])

def read_results_sheet_rows(tid):
    """Read every row for a teacher's results sheet/tab back into dicts keyed
    by the same field names used elsewhere (timestamp, student_name, score...).
    This is the shared building block behind both "My Results" (filtered by
    student email) and the teacher's own Results tab (unfiltered) - both need
    the SAME durability: the Google Sheet is the only store that survives a
    Render restart/redeploy, unlike _pending_results which lives only in this
    process's memory and is wiped every time the free-tier dyno spins down or
    a new deploy goes out. Returns (rows, debug) where debug explains exactly
    what happened if rows comes back empty (sheet not configured, worksheet
    missing, a fetch/auth error, etc.) so that can be surfaced to a caller
    instead of silently looking like "there is no data"."""
    rows = []
    svc_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    debug = {
        "sheet_configured": bool(svc_json),
        "sheet_rows_scanned": 0,
        "sheet_error": None,
    }
    if not svc_json:
        return rows, debug
    try:
        import gspread
        sheet_id = RESULTS_SHEET_IDS.get(tid, RESULTS_SHEET_ID)
        sh = get_gspread_client().open_by_key(sheet_id)
        tab = TEACHERS[tid]["results_tab"]
        ws = sh.worksheet(tab)
        values = ws.get_all_values()
        if values:
            header = values[0]
            debug["sheet_rows_scanned"] = len(values) - 1
            for r in values[1:]:
                rows.append({
                    RESULT_KEY_ALIASES.get(h, h): (r[i] if i < len(r) else "")
                    for i, h in enumerate(header)
                })
    except gspread.WorksheetNotFound:
        debug["sheet_error"] = f"worksheet '{TEACHERS[tid]['results_tab']}' not found"
    except Exception as e:
        print("RESULTS SHEET READ FAILED", tid, e)
        debug["sheet_error"] = str(e)
    return rows, debug

def merge_with_pending(sheet_rows, tid, email=None):
    """Supplement sheet_rows with anything still only in _pending_results
    (written moments ago, or from a local run with no sheet configured at
    all), de-duplicated against what the sheet already returned."""
    seen_keys = {(r.get("timestamp"), r.get("sentence"), r.get("phase"), r.get("student_name")) for r in sheet_rows}
    merged = list(sheet_rows)
    for r in _pending_results:
        if r.get("teacher_id") != tid:
            continue
        if email is not None and (r.get("student_email") or "").strip().lower() != email:
            continue
        k = (r.get("timestamp"), r.get("sentence"), r.get("phase"), r.get("student_name"))
        if k in seen_keys:
            continue
        seen_keys.add(k)
        merged.append(r)
    merged.sort(key=lambda r: r.get("timestamp") or "")
    return merged

@app.post("/api/my-history")
def my_history():
    """A student's full practice history, looked up by email - independent of
    any single in-memory session (which is discarded on every server restart
    and re-created fresh on every login)."""
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    email = (data.get("email") or "").strip().lower()
    if tid not in TEACHERS:
        return jsonify(ok=False, error="bad request"), 400
    if not email or not EMAIL_RE.match(email):
        return jsonify(ok=False, error="נדרש אימייל תקין."), 400
    expected = TEACHERS[tid]["student_password"]
    if expected and password != expected:
        return jsonify(ok=False, error="wrong password"), 401

    sheet_rows, debug = read_results_sheet_rows(tid)
    debug["email_column_found"] = bool(sheet_rows and "student_email" in sheet_rows[0])
    sheet_rows = [r for r in sheet_rows if (r.get("student_email") or "").strip().lower() == email]
    rows = merge_with_pending(sheet_rows, tid, email=email)

    total = len(rows)
    exam_rows = [r for r in rows if r.get("phase") == "final_exam"]
    exam_avg = int(sum(float(r.get("score") or 0) for r in exam_rows) / len(exam_rows)) if exam_rows else None
    exercises = sorted({r.get("exercise") for r in rows if r.get("exercise")})
    return jsonify(ok=True, rows=rows, total=total, exam_avg=exam_avg, exercises=exercises, debug=debug)

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
        # Auto-advancement: only applies to sessions that used the built-in
        # default CEFR track (never touches teacher-chosen exercises). A
        # student levels up only on strong, clean performance across the
        # whole practice pass - high average score, essentially no sentences
        # that needed multiple retries, and nothing left unresolved in the
        # review queue - so a lucky single sentence can't trigger it.
        leveled_up = False
        new_level = None
        if s.get("level_track") and total:
            avg_attempts = sum(r.get("attempts", 1) or 1 for r in s["results"]) / total
            strong = avg >= 90 and avg_attempts <= 1.6 and not s.get("review_queue")
            if strong:
                nxt = next_cefr_level(s["level_track"])
                if nxt != s["level_track"]:
                    if set_student_level(s["teacher_id"], s.get("student_email", ""), nxt):
                        leveled_up, new_level = True, nxt
        return jsonify(
            done=True, results=s["results"], avg_score=avg, total=total, exercise=s["exercise_name"],
            level_track=s.get("level_track"), leveled_up=leveled_up, new_level=new_level,
        )
    q = s["sentences"][s["current"]]
    return jsonify({
        "done": False, "he": q["he"], "en": q["en"], "index": s["current"], "total": len(s["sentences"]),
        "threshold": s["threshold"], "max_attempts": s["max_attempts"], "voice_gender": s["voice_gender"],
        "exercise": s["exercise_name"], "review_round": False,
        "content_mismatch": s.get("content_mismatch", False), **session_payload(s)
    })

def cap_choice(s, correct, spoken, score, base, metrics=None, sentence_obj=None, attempts_used=None):
    """Hard safety valve: no sentence auto-loops forever. But instead of forcibly
    skipping to the next sentence the instant max_attempts is hit (the old
    cap_and_advance behavior), PAUSE here and let the student choose: move on
    now, or ask for a few bonus attempts and keep trying the same sentence.
    This directly answers reports that slow readers/speakers were being
    silently auto-advanced away before they felt ready or felt close to a
    correct answer. Nothing is recorded/advanced until the student actually
    picks one of the two options via /api/cap-continue or /api/cap-retry.
    """
    s["cap_pending"] = {
        "correct": correct, "spoken": spoken, "score": score,
        "metrics": metrics or {}, "sentence_obj": dict(sentence_obj) if sentence_obj else None,
    }
    payload = {
        **base,
        "passed": False,
        "skipped": False,
        "advance": False,
        "cap_reached": True,
        "cap_choice": True,
        "message": "השתמשת בכל הניסיונות למשפט הזה. אפשר להמשיך למשפט הבא, או לבקש עוד סיבוב.",
        **session_payload(s),
    }
    if attempts_used is not None:
        payload["attempts_used"] = attempts_used
        payload["attempts_left"] = 0
    return jsonify(payload)

@app.post("/api/cap-continue")
def cap_continue():
    """Student chose to move on after exhausting attempts on this sentence -
    equivalent to what used to happen automatically. Records the last attempt
    as a skip/fail, queues the sentence for the pre-exam review round, and
    advances to the next sentence."""
    data = request.get_json(force=True)
    s = _sessions.get(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    pc = s.get("cap_pending")
    if not pc:
        return jsonify(error="no pending decision"), 400
    s["cap_pending"] = None
    s["bonus_attempts"] = 0
    record_and_advance(s, pc["correct"], pc["spoken"], pc["score"], False, skipped=True, metrics=pc.get("metrics"))
    if pc.get("sentence_obj") and not s.get("in_review"):
        s["review_queue"].append(pc["sentence_obj"])
    return jsonify(ok=True)

@app.post("/api/cap-retry")
def cap_retry():
    """Student chose "another round" instead of moving on - grant a small
    batch of bonus attempts on the SAME sentence/station instead of forcing an
    advance, for cases where they feel close to a correct answer."""
    data = request.get_json(force=True)
    s = _sessions.get(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    if not s.get("cap_pending"):
        return jsonify(error="no pending decision"), 400
    s["cap_pending"] = None
    s["bonus_attempts"] = int(s.get("bonus_attempts", 0)) + 3
    return jsonify(ok=True, **session_payload(s))

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
            "timestamp": now_str(), "teacher_id": s["teacher_id"],
            "student_name": s["student_name"], "student_email": s.get("student_email", ""),
            "exercise": s["exercise_name"],
            "phase": "review_retry", "sentence": r_correct, "spoken": spoken, "score": r_score,
            "passed": bool(r_passed), "skipped": False, "attempts": 1, "max_attempts": 1,
            "mastery_reps": 0, "mastery_status": "review_pass" if r_passed else "needs_review",
            "mastery_score": r_score, "cloze_passed": "review",
            "completion": review_sentence.get("completion", ""),
            **fluency,
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
        # Include any bonus attempts granted via /api/cap-retry so the
        # attempts-used/max-attempts display students see (e.g. "6 of 8")
        # never looks contradictory after they've asked for another round.
        "max_attempts": s["max_attempts"] + s.get("bonus_attempts", 0),
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
        if s["cloze_attempts"] >= s["max_attempts"] + s.get("bonus_attempts", 0):
            return cap_choice(s, correct, spoken, score, {**base, "station": "cloze", "cloze_failed": True}, metrics=metrics, sentence_obj=sentence_obj, attempts_used=s["cloze_attempts"])
        return jsonify({**base, "station": "cloze", "cloze_mode": True, **session_payload(s)})

    # Station 2: Bloom practice/mastery. The cloze station is NOT shown yet.
    if s["mastery_target"] > 0:
        s["stage2_attempts"] = int(s.get("stage2_attempts", 0)) + 1
        stage2_cap_reached = s["stage2_attempts"] >= s["max_attempts"] + s.get("bonus_attempts", 0)
        if passed:
            s["mastery_consecutive"] += 1
            s["mastery_score"] = max(s.get("mastery_score", 0), score)
            if s["mastery_consecutive"] >= s["mastery_target"]:
                # Practice mastery is complete. Now move to the separate cloze station -
                # ALWAYS, regardless of how many station-2 attempts that took.
                cw = detect_cloze_word(correct)
                completion = sentence_obj.get("completion", "")
                s["mastery_target"] = 0
                s["mastery_consecutive"] = 0
                s["stage2_attempts"] = 0
                if not cw and not completion:
                    record_and_advance(s, correct, spoken, s["mastery_score"] or score, True, metrics=metrics)
                    return jsonify({**base, "station": "practice", "mastery_mode_done": True, "advance": True, **session_payload(s)})
                s["cloze_active"] = True
                s["cloze_word"] = cw
                s["cloze_display"] = completion
                s["cloze_attempts"] = 0
                return jsonify({**base, "station": "practice", "mastery_mode_done": True, "cloze_mode": True, **session_payload(s)})
            return jsonify({**base, "station": "practice", "mastery_mode": True, "streak_broken": False, **session_payload(s)})
        # A failed repetition mid-way through Bloom reinforcement does NOT reset
        # progress back to zero - it simply isn't counted as one of the required
        # successes. The student still just needs (target - consecutive) more
        # correct repetitions. Only running out of station 2's own attempt budget
        # ends the loop (handled below) - this never borrows from station 1 or 3.
        if stage2_cap_reached:
            return cap_choice(s, correct, spoken, score, {**base, "station": "practice"}, metrics=metrics, sentence_obj=sentence_obj, attempts_used=s["stage2_attempts"])
        return jsonify({**base, "station": "practice", "mastery_mode": True, "streak_broken": True, **session_payload(s)})

    # Station 1: normal practice / initial read. A passing first read enters either
    # Bloom repetition mode (if there were failures) or the separate cloze check.
    s["sentence_attempts"] = int(s.get("sentence_attempts", 0)) + 1
    cap_reached = s["sentence_attempts"] >= s["max_attempts"] + s.get("bonus_attempts", 0)
    if passed:
        target = mastery_target_for(s["failed_attempts"])
        s["mastery_score"] = score
        if target > 0:
            s["mastery_target"] = target
            s["mastery_consecutive"] = 1
            s["stage2_attempts"] = 0
            return jsonify({**base, "station": "practice", "mastery_mode": True, "first_pass": True, **session_payload(s)})

        cw = detect_cloze_word(correct)
        completion = sentence_obj.get("completion", "")
        if cw or completion:
            s["cloze_active"] = True
            s["cloze_word"] = cw
            s["cloze_display"] = completion
            s["cloze_attempts"] = 0
            return jsonify({**base, "station": "practice", "cloze_mode": True, **session_payload(s)})

        record_and_advance(s, correct, spoken, score, True, metrics=metrics)
        return jsonify({**base, "station": "practice", "advance": True, **session_payload(s)})

    s["failed_attempts"] += 1
    if cap_reached:
        return cap_choice(s, correct, spoken, score, base, metrics=metrics, sentence_obj=sentence_obj, attempts_used=s["sentence_attempts"])
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
        "timestamp": now_str(), "teacher_id": s["teacher_id"],
        "student_name": s["student_name"], "student_email": s.get("student_email", ""),
        "exercise": s["exercise_name"], "phase": "final_exam",
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
    # Read from the Google Sheet (durable) instead of only _pending_results
    # (wiped on every server restart/redeploy) - same persistence model as
    # the student-facing "My Results" view, so the teacher's Results tab no
    # longer silently loses history whenever Render spins the dyno down.
    sheet_rows, debug = read_results_sheet_rows(tid)
    rows = merge_with_pending(sheet_rows, tid)
    return jsonify(ok=True, rows=rows[-200:], debug=debug)

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
