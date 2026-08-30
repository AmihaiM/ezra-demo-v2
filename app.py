import os, sys, json, time, re, csv, io, random, threading, hmac, hashlib, base64
from urllib.parse import urlparse, parse_qs, quote
from difflib import SequenceMatcher
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, Response, g
import requests

# Force stdout/stderr to be line-buffered (flush on every newline) instead of
# whatever gunicorn's sync worker leaves them as by default. Without this,
# print() debug output across the whole app can sit in an internal buffer and
# never show up in Render's log viewer in any reasonable time - it looks
# exactly like "nothing was logged" even though the code ran fine. Bit us
# investigating the Azure pronunciation debug output below; fixing it here
# once, globally, instead of adding flush=True to every individual print().
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# How long fetched Google Sheets content (exercise catalog + parsed sentence
# rows) is cached in memory before being re-fetched. This used to be 3600
# (1 hour), which meant a teacher editing their exercise's sheet - fixing a
# typo, adding/removing sentences - wouldn't see the change reflected for up
# to an hour, with no way to force it sooner (a browser refresh has zero
# effect since this cache lives on the server, not the client). Shortened to
# 5 minutes as a safety net; set_exercise() below also explicitly invalidates
# the cache for a teacher's chosen exercise every time they (re)select it, so
# in practice a teacher can force an immediate refresh just by pressing
# "בחר" again on the exercise they're already using.
CACHE_TTL = 300
STATE_FILE = os.path.join(BASE_DIR, "teacher_state.json")
IL_TZ = ZoneInfo("Asia/Jerusalem")

# --- Shared Claude API infra, used by both AI features: photo -> exercise
# (OCR + sentence selection + translation + topic tagging in one call) and
# AI-generated personalized sentences for a student's weak grammar topics.
# One key, one call site, one rate-limit story instead of duplicating this
# per feature. Not configured (no ANTHROPIC_API_KEY) simply means both
# features are unavailable - call_claude() fails closed with a clear error,
# nothing else in the app depends on this being set.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")
# Soft per-teacher daily cap on AI-feature calls, so a bug (or an
# enthusiastic teacher generating exercise after exercise) can't run up an
# unbounded API bill before there's paying revenue to cover it. In-memory
# only - resets on restart too, which is fine, this is a cost safety net,
# not a security control.
AI_DAILY_LIMIT_PER_TEACHER = int(os.getenv("AI_DAILY_LIMIT_PER_TEACHER", "20"))
_ai_usage = {}

def check_and_bump_ai_quota(tid):
    """Returns True (and counts the call) if this teacher is still under
    today's AI-feature cap; False if they've hit it - callers must check
    this BEFORE spending an API call, not after."""
    day = datetime.now(IL_TZ).strftime("%Y-%m-%d")
    key = (tid, day)
    used = _ai_usage.get(key, 0)
    if used >= AI_DAILY_LIMIT_PER_TEACHER:
        return False
    _ai_usage[key] = used + 1
    return True

def call_claude(messages, system=None, max_tokens=2000, model=None):
    """Shared helper for both AI features - the one place that knows how to
    call the Anthropic Messages API (text or vision), with one timeout and
    error-handling policy. Returns (text, error): text is None and error is
    a short Hebrew message on any failure (missing key, timeout, non-200,
    malformed response), so callers always fail closed with something
    sane to show a teacher instead of a raw exception or a silent hang.
    `messages` follows the standard Anthropic Messages API shape, e.g.
    [{"role": "user", "content": "..."}] for text, or
    [{"role": "user", "content": [{"type": "image", "source": {...}},
                                   {"type": "text", "text": "..."}]}]
    for vision.
    """
    if not ANTHROPIC_API_KEY:
        return None, "AI לא מוגדר בשרת (חסר ANTHROPIC_API_KEY)"
    try:
        payload = {"model": model or ANTHROPIC_MODEL, "max_tokens": max_tokens, "messages": messages}
        if system:
            payload["system"] = system
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if resp.status_code != 200:
            print("CLAUDE API ERROR", resp.status_code, resp.text[:500])
            return None, f"שגיאת AI (קוד {resp.status_code})"
        data = resp.json()
        parts = data.get("content") or []
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        if not text:
            return None, "תשובת AI ריקה"
        return text, None
    except requests.exceptions.Timeout:
        return None, "ה-AI לא הגיב בזמן, נסה שוב"
    except Exception as e:
        print("CLAUDE API CALL FAILED", e)
        return None, "שגיאת AI"

# --- Azure Speech (Pronunciation Assessment) infra ---
# SHADOW MODE ONLY as of this writing: /api/pronunciation-assess below is
# called by the front-end in PARALLEL with the existing browser
# SpeechRecognition-based flow, purely to collect real per-word pronunciation
# scores for evaluation. It does NOT feed into /api/answer's scoring, the
# mastery gauge, cloze, or the exam - failing or being unset here has zero
# effect on a student's actual progress. This is deliberate: cutting the
# whole scoring engine over in one step would be a large, hard-to-verify
# change; running the real API in parallel first lets us validate quality/
# latency/format-compatibility across real devices before anything depends
# on it. Not configured (no AZURE_SPEECH_KEY) simply disables the endpoint.
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "eastus")

# Content-Types MediaRecorder can realistically hand us, mapped to what
# Azure's short-audio REST endpoint accepts directly (no server-side
# transcoding needed for either of these - see the code comment on
# /api/pronunciation-assess for the Safari/iOS gap this does NOT cover yet).
_AZURE_AUDIO_CONTENT_TYPES = {
    "audio/webm": "audio/webm; codecs=opus",
    "audio/webm;codecs=opus": "audio/webm; codecs=opus",
    "audio/ogg": "audio/ogg; codecs=opus",
    "audio/ogg;codecs=opus": "audio/ogg; codecs=opus",
    "audio/wav": "audio/wav; codecs=audio/pcm; samplerate=16000",
}

_MAX_AUDIO_DATA_URL_LEN = 4_000_000  # a few seconds of spoken audio, base64-inflated

def _parse_audio_data_url(data_url):
    """Split a data:audio/...;base64,... URL into (azure_content_type, raw_bytes),
    or raise ValueError with a message safe to log/return. Mirrors
    _parse_image_data_url's shape below for the teacher photo feature."""
    data_url = (data_url or "").strip()
    if not data_url.startswith("data:"):
        raise ValueError("invalid audio format")
    if len(data_url) > _MAX_AUDIO_DATA_URL_LEN:
        raise ValueError("audio too long")
    m = re.match(r"^data:([^;,]+(?:;codecs=[^;,]+)?);base64,(.+)$", data_url, re.DOTALL)
    if not m:
        raise ValueError("invalid audio format")
    media_type, b64 = m.group(1), m.group(2)
    azure_content_type = _AZURE_AUDIO_CONTENT_TYPES.get(media_type.replace(" ", ""))
    if not azure_content_type:
        raise ValueError(f"unsupported audio type: {media_type}")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise ValueError("could not decode audio")
    return azure_content_type, raw

def call_azure_pronunciation(raw_audio, azure_content_type, reference_text, language="en-US"):
    """Calls Azure's short-audio Pronunciation Assessment REST API. Returns
    (result_dict, error): result_dict is None and error is a short message
    on any failure (missing key, timeout, non-200, malformed response) -
    same fail-closed shape as call_claude() above."""
    if not AZURE_SPEECH_KEY:
        return None, "Azure Speech not configured (missing AZURE_SPEECH_KEY)"
    pron_config = {
        "ReferenceText": reference_text,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "EnableMiscue": True,
        "EnableProsodyAssessment": True,
    }
    pron_header = base64.b64encode(json.dumps(pron_config).encode("utf-8")).decode("utf-8")
    url = f"https://{AZURE_SPEECH_REGION}.stt.speech.microsoft.com/speech/recognition/conversation/cognitiveservices/v1"
    headers = {
        "Accept": "application/json;text/xml",
        "Content-Type": azure_content_type,
        "Ocp-Apim-Subscription-Key": AZURE_SPEECH_KEY,
        "Pronunciation-Assessment": pron_header,
    }
    try:
        # TEMP DEBUG (remove once Azure shadow scoring is verified): the
        # front-end confirmed the captured audio itself sounds clear on
        # playback, but Azure keeps returning recognized_text "." with every
        # reference word marked Omission - i.e. Azure isn't reading the audio
        # as speech at all. That could be a real decode failure OR just us
        # not seeing enough of Azure's own response to tell. Logging the
        # exact bytes we send (length + magic header, to confirm it's a
        # valid, non-truncated container) and Azure's FULL raw JSON (not just
        # our extracted summary, which drops RecognitionStatus/Offset/
        # Duration/NBest confidence) should show which side is at fault.
        # flush=True on every debug print here: gunicorn's sync worker
        # doesn't guarantee stdout is line-buffered, so without this these
        # can sit in Python's internal buffer and never reach Render's log
        # viewer at all (looks identical to "nothing happened" from the log
        # UI, even though the request went through fine).
        print(f"AZURE SPEECH REQUEST: content_type={azure_content_type!r} "
              f"bytes={len(raw_audio)} header_hex={raw_audio[:16].hex()}", flush=True)
        resp = requests.post(
            url, params={"language": language, "format": "detailed"},
            headers=headers, data=raw_audio, timeout=20,
        )
        if resp.status_code != 200:
            print("AZURE SPEECH API ERROR", resp.status_code, resp.text[:500], flush=True)
            return None, f"Azure API error ({resp.status_code})"
        result = resp.json()
        print("AZURE SPEECH RAW RESPONSE:", json.dumps(result)[:2000], flush=True)
        return result, None
    except requests.exceptions.Timeout:
        return None, "Azure API timed out"
    except Exception as e:
        print("AZURE SPEECH API CALL FAILED", e)
        return None, "Azure API call failed"

def extract_azure_pronunciation_summary(result):
    """Flattens Azure's response down to just what the front-end shadow
    panel needs - the scores live directly on the NBest[0] item and each
    word (NOT nested under a "PronunciationAssessment" sub-key, despite
    that being the header's own name - confirmed against a real response
    during the POC, this tripped up the first draft of this code too)."""
    nbest = (result.get("NBest") or [{}])[0]
    return {
        "recognized_text": result.get("DisplayText", ""),
        "accuracy_score": nbest.get("AccuracyScore"),
        "fluency_score": nbest.get("FluencyScore"),
        "prosody_score": nbest.get("ProsodyScore"),
        "completeness_score": nbest.get("CompletenessScore"),
        "pron_score": nbest.get("PronScore"),
        "words": [
            {"word": w.get("Word"), "accuracy_score": w.get("AccuracyScore"), "error_type": w.get("ErrorType")}
            for w in nbest.get("Words", [])
        ],
        # TEMP DEBUG (remove once Azure shadow scoring is verified): surface
        # the fields our summary normally drops, straight into the same JSON
        # the browser console already shows - RecognitionStatus in
        # particular tells us WHY recognized_text is "." (e.g. "Success"
        # with genuinely no speech matched vs. "InitialSilenceTimeout" vs.
        # some decode failure), without needing to fight Render's log viewer
        # at all. debug_nbest_confidence is Azure's own confidence that this
        # NBest alternative is correct - near 0 there would point at a
        # decode/format problem rather than a real pronunciation issue.
        "debug_recognition_status": result.get("RecognitionStatus"),
        "debug_offset": result.get("Offset"),
        "debug_duration": result.get("Duration"),
        "debug_nbest_confidence": nbest.get("Confidence"),
    }

def extract_json_block(text):
    """Best-effort extraction of a JSON array/object from a Claude text
    response - the prompt always asks for JSON only, but models sometimes
    wrap it in a ```json fence or add a stray sentence before/after despite
    that instruction, so this strips down to the outermost [...] or {...}
    before parsing rather than trusting the response to be bare JSON."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start_chars, end_chars = "[{", "]}"
    start = None
    for i, c in enumerate(text):
        if c in start_chars:
            start = i
            break
    if start is None:
        raise ValueError("no JSON found in AI response")
    opener = text[start]
    closer = end_chars[start_chars.index(opener)]
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in AI response")

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
        "photo_url": "",
        # Gmail/Google Workspace address linked for Google Sign-In (see
        # /api/teacher-login-google) - empty by default, meaning this teacher
        # can only log in with the password until they link one (self-serve
        # in teacher.html settings, or set here/in /admin).
        "google_email": os.getenv("BEN_GOOGLE_EMAIL", ""),
    },
    "sara": {
        "name": "שרה", "color": "#be185d", "color_light": "#fce8f3", "voice_gender": "female",
        "results_tab": "Sara", "student_password": os.getenv("SARA_STUDENT_PASSWORD", "class2026"),
        "teacher_password": os.getenv("SARA_TEACHER_PASSWORD", "sara2026"),
        "default_threshold": int(os.getenv("SARA_THRESHOLD", "85")),
        "default_max_attempts": int(os.getenv("SARA_MAX_ATTEMPTS", "5")),
        "photo_url": "",
        "google_email": os.getenv("SARA_GOOGLE_EMAIL", ""),
    },
}
# Google Sign-In (OAuth). This is the app's OAuth 2.0 "Web application"
# Client ID from Google Cloud Console - NOT a secret (it's embedded in the
# frontend page, same as any Google Sign-In button on any website) so it's
# fine to expose via /api/config. Without it set, Google Sign-In is simply
# unavailable and the app falls back to the existing password login - see
# verify_google_id_token() below.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
# Signs/verifies the short-lived teacher session token issued at login (see
# make_teacher_token/verify_teacher_token below). A Google-authenticated
# teacher never has a "password" to resend on every subsequent dashboard
# request the way the password-login flow always has - this token is what
# lets every OTHER teacher-only endpoint (settings, catalog, results, ...)
# accept "I already proved who I am at login" instead of requiring the
# actual password again. Falls back to a fixed dev value so local/dev runs
# still work, but a real deployment should set SECRET_KEY explicitly - see
# admin setup instructions.
APP_SECRET_KEY = os.getenv("SECRET_KEY", "dev-insecure-secret-change-me")
CATALOG_SHEET_ID = os.getenv("CATALOG_SHEET_ID", "134GzKi9KWNCP_avNg5Z7drhHp3Re7RRALrNrDcOeFnk")
RESULTS_SHEET_ID = os.getenv("RESULTS_SHEET_ID", "17a-y_-nL9L85Kl7zL1F1ovTGbQy7q5NlBX24C-a_6JU")
# Each teacher writes results into their own separate spreadsheet file (not just a separate
# tab) so that opening the file directly never exposes the other teacher's data.
RESULTS_SHEET_IDS = {
    "ben": RESULTS_SHEET_ID,
    "sara": os.getenv("SARA_RESULTS_SHEET_ID", "1JGWw_Jf8m3WF2-HS6sEohRmV6v2mmp29b6iSE3rgsOQ"),
}
EZRA_APP_BASE_URL = os.getenv("EZRA_APP_BASE_URL", "https://speakmaster.org")
# Super-admin dashboard (/admin) - one shared password (not per-teacher), lets
# whoever runs the pilot see every teacher/student at a glance and add new
# teachers without a code change + redeploy. Admin-added teachers are stored
# in a "Teachers" tab of the catalog spreadsheet (see load_extra_teachers/
# _upsert_teacher_row below) rather than a local file, because Render's local
# disk is not reliably persisted across redeploys/restarts - the same reason
# every other durable thing in this app (results, catalog, student levels)
# already lives in a Google Sheet instead.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin2026")
ADMIN_SHEET_ID = os.getenv("ADMIN_SHEET_ID", CATALOG_SHEET_ID)
TEACHERS_TAB = "Teachers"

_cache = {}
_sessions = {}
_pending_results = []

# --- Durable session storage (Postgres) ---------------------------------
# Root cause of the recurring "session not found" / "התחלה מחדש" bugs: a
# student's LIVE in-progress state (which sentence, how many attempts,
# mastery streak, review queue...) lived only in the _sessions dict above -
# plain process memory. Every Render redeploy or free-tier spin-down wiped
# it silently. Results already escaped this via Google Sheets; this mirrors
# that same "durable copy outside the process" idea for the live session
# itself. Entirely optional and backward compatible: with no DATABASE_URL
# set, every function below is a no-op and the app behaves exactly as it
# did before (memory-only) - so this can be deployed before the DB exists.
DATABASE_URL = os.getenv("DATABASE_URL", "")

_db_pool = None
_db_pool_init_failed = False
_db_pool_failed_at = 0
_db_pool_lock = threading.Lock()
# A transient hiccup at cold-start (DB provider still waking up, brief DNS
# blip, ...) shouldn't permanently disable pooling for this worker's entire
# lifetime - self-heal by allowing a retry after this cooldown, same "don't
# get stuck failed forever" philosophy as the rest of this app. The one-off
# _db_conn() fallback covers requests in between just fine either way.
_DB_POOL_RETRY_COOLDOWN_SEC = 30

def _get_db_pool():
    """Lazily create ONE connection pool per gunicorn worker process (module-
    level global, so it's created once on first use and reused for the life
    of the process - not per request). Opening a fresh TCP+auth connection
    on every single request that touches a session (which is nearly every
    request, via the after_request persistence hook) was real, avoidable
    overhead at any real concurrency - a pool hands out an already-open
    connection instead. Falls back to returning None (caller then opens a
    plain one-off connection, exactly the old behavior) if psycopg_pool isn't
    installed, so this degrades gracefully rather than breaking DB access
    entirely on an older/un-redeployed environment."""
    global _db_pool, _db_pool_init_failed, _db_pool_failed_at
    if not DATABASE_URL or _db_pool_init_failed:
        return None
    if _db_pool is not None:
        return _db_pool
    if _db_pool_failed_at and time.time() - _db_pool_failed_at < _DB_POOL_RETRY_COOLDOWN_SEC:
        return None
    # Guards against a double-init race if the WSGI server ever uses threaded
    # workers (gthread/gevent) - two requests on two threads could otherwise
    # both see _db_pool as None at the same moment and each construct their
    # own pool. Sync workers (today's likely setup) never hit this race since
    # each process handles one request at a time, but this makes the switch
    # to threaded workers - one of the concurrency changes worth making -
    # safe without a separate follow-up fix.
    with _db_pool_lock:
        if _db_pool is not None:
            return _db_pool
        if _db_pool_init_failed or (_db_pool_failed_at and time.time() - _db_pool_failed_at < _DB_POOL_RETRY_COOLDOWN_SEC):
            return None
        try:
            from psycopg_pool import ConnectionPool
            # min_size=0: do NOT hold an idle connection open when nothing is
            # using the DB. This used to be min_size=1, which meant this
            # worker permanently kept one live Postgres connection open from
            # the moment it first touched the DB - on a serverless Postgres
            # host (e.g. Neon) that never lets the compute endpoint suspend,
            # so "compute time" is billed/quota'd 24/7 regardless of actual
            # traffic. Combined with the 4-second results-flush loop below,
            # this silently burned through a free-tier monthly compute quota
            # in days rather than the whole month. max_size is generous
            # relative to a single worker's request concurrency but still
            # bounded, so a burst of traffic can't open unbounded connections
            # against Postgres's own connection limit. kwargs=connect_timeout
            # mirrors the old one-off psycopg.connect(..., connect_timeout=5)
            # behavior.
            pool = ConnectionPool(
                DATABASE_URL, min_size=0, max_size=10, timeout=5,
                kwargs={"connect_timeout": 5}, open=False,
            )
            # Explicit open() (rather than open=True in the constructor) is
            # the version-safe way to do this across psycopg_pool releases -
            # it also means a slow/unreachable DB at startup raises HERE,
            # inside our own try/except, instead of however the constructor
            # itself would have handled it.
            pool.open(wait=True, timeout=5)
            _db_pool = pool
            return _db_pool
        except ImportError:
            # Missing package, not a transient DB issue - retrying every
            # cooldown window would never help until the next deploy anyway.
            print("DB CONNECTION POOL DISABLED: psycopg_pool not installed (pip install -r requirements.txt) - falling back to one-off connections")
            _db_pool_init_failed = True
            return None
        except Exception as e:
            print("DB POOL INIT FAILED (will retry) ", e)
            _db_pool_failed_at = time.time()
            return None

def _db_conn():
    """Returns a live connection - from the pool when available, otherwise a
    short-lived one-off connection (the original behavior). Callers must
    always release what they get back via _db_release(conn), never conn.close()
    directly, so a pooled connection is returned to the pool instead of being
    torn down."""
    pool = _get_db_pool()
    if pool is not None:
        try:
            return pool.getconn()
        except Exception as e:
            print("DB POOL GETCONN FAILED", e)
            # Fall through to a one-off connection rather than failing the
            # whole request - the pool being momentarily exhausted/unhealthy
            # shouldn't mean session persistence stops working entirely.
    if not DATABASE_URL:
        return None
    try:
        import psycopg
    except ImportError:
        print("DB SESSION PERSISTENCE DISABLED: psycopg not installed (pip install -r requirements.txt)")
        return None
    try:
        return psycopg.connect(DATABASE_URL, connect_timeout=5)
    except Exception as e:
        print("DB CONNECT FAILED", e)
        return None

def _db_release(conn):
    """Pairs with _db_conn() - returns a pooled connection to the pool, or
    closes a one-off connection, whichever _db_conn() actually handed out.
    Every _db_conn() caller must call this in a finally block instead of
    conn.close() directly."""
    if conn is None:
        return
    pool = _db_pool
    if pool is not None:
        try:
            pool.putconn(conn)
            return
        except Exception as e:
            print("DB POOL PUTCONN FAILED", e)
            try:
                conn.close()
            except Exception:
                pass
            return
    try:
        conn.close()
    except Exception:
        pass

def db_init():
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "student_id TEXT PRIMARY KEY, data JSONB NOT NULL, "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            # Durable outbox for results-sheet writes (see write_result/
            # _flush_pending_results below). A finalized sentence used to
            # write straight to Google Sheets inline, inside the same
            # request the student was waiting on - 3-5 sequential Sheets API
            # calls, ~1-3 blocking seconds, on every single finalized
            # sentence. Queuing here instead means the student's request
            # returns immediately once this one fast local INSERT commits;
            # a background thread (any worker process, guarded by a Postgres
            # advisory lock so only one of them actually does it at a time)
            # drains this table in batches on its own schedule.
            cur.execute(
                "CREATE TABLE IF NOT EXISTS pending_results ("
                "id BIGSERIAL PRIMARY KEY, row_data JSONB NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            # Payment scaffold (see /api/grow-webhook + get_subscription_status
            # below) - one row per paying identity (keyed by email, the same
            # stable identifier used everywhere else in the app). NOT YET
            # ENFORCED anywhere: no existing endpoint currently checks this
            # table before granting access. It exists so the mechanism is
            # real and testable once there's an actual Grow account to
            # connect it to - see the big comment on grow_webhook().
            cur.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions ("
                "email TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'inactive', "
                "plan TEXT, grow_transaction_id TEXT, "
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            # AI feature 1: exercises a teacher generated from a photo of a
            # text page (see /api/teacher/photo-to-sentences and
            # /api/teacher/save-ai-exercise). Deliberately NOT stored as a
            # Google Sheet like every other exercise - that would require the
            # sheet to be publicly "anyone with the link" shared (same as
            # every teacher-authored CSV exercise), which is wrong for
            # AI-generated content that hasn't been reviewed by anyone but
            # its own teacher yet. Stored here instead and selected via a
            # "ai://<id>" sentinel in the normal csv_url field - see
            # load_ai_exercise_sentences() and new_session().
            cur.execute(
                "CREATE TABLE IF NOT EXISTS ai_exercises ("
                "id BIGSERIAL PRIMARY KEY, teacher_id TEXT NOT NULL, "
                "name TEXT NOT NULL, sentences JSONB NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
    except Exception as e:
        print("DB INIT FAILED", e)
    finally:
        _db_release(conn)

def get_subscription_status(email):
    """Returns the stored status string ('active', 'inactive', 'canceled',
    ...) for this email, or None if either there's no row for them yet, the
    email is blank, or no DB is configured at all. None is deliberately
    treated the same as "don't know" rather than "not paid" by any future
    caller - payment gating is not wired into anything yet (see
    grow_webhook()), so returning None here should never itself block
    access; it's a read helper waiting for a caller, not an enforcement
    point."""
    email = (email or "").strip().lower()
    if not email:
        return None
    conn = _db_conn()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM subscriptions WHERE email = %s", (email,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print("GET SUBSCRIPTION STATUS FAILED", e)
        return None
    finally:
        _db_release(conn)

def _upsert_subscription(email, status, plan=None, grow_transaction_id=None):
    email = (email or "").strip().lower()
    if not email:
        return False
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO subscriptions (email, status, plan, grow_transaction_id, updated_at) "
                "VALUES (%s, %s, %s, %s, now()) "
                "ON CONFLICT (email) DO UPDATE SET status = EXCLUDED.status, "
                "plan = COALESCE(EXCLUDED.plan, subscriptions.plan), "
                "grow_transaction_id = COALESCE(EXCLUDED.grow_transaction_id, subscriptions.grow_transaction_id), "
                "updated_at = now()",
                (email, status, plan, grow_transaction_id),
            )
        return True
    except Exception as e:
        print("UPSERT SUBSCRIPTION FAILED", e)
        return False
    finally:
        _db_release(conn)

def _session_to_jsonable(s):
    """finalized_indices is a Python set() - JSON has no set type, so store
    it as a sorted list. accuracy_data is keyed by integer sentence index -
    JSON object keys are always strings, so without the explicit str(k) here
    (and the matching int(k) in _session_from_jsonable below) every
    accuracy_data[s["current"]] lookup elsewhere in the code would silently
    stop matching the instant a session reloads from the database."""
    d = dict(s)
    d["finalized_indices"] = sorted(d.get("finalized_indices") or [])
    d["accuracy_data"] = {str(k): v for k, v in (d.get("accuracy_data") or {}).items()}
    return d

def _session_from_jsonable(d):
    d = dict(d)
    d["finalized_indices"] = set(d.get("finalized_indices") or [])
    d["accuracy_data"] = {int(k): v for k, v in (d.get("accuracy_data") or {}).items()}
    return d

def save_session(student_id):
    """Upsert one student's full session dict to Postgres. No-op if no DB is
    configured or the id isn't currently in memory. Never called by hand
    from individual endpoints - see the after_request hook below, which
    persists every session an endpoint actually touched, so nothing can
    forget to save."""
    s = _sessions.get(student_id)
    if s is None:
        return
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (student_id, data, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (student_id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (student_id, json.dumps(_session_to_jsonable(s))),
            )
    except Exception as e:
        print("SAVE SESSION FAILED", student_id, e)
    finally:
        _db_release(conn)

def load_session(student_id):
    """Read one student's session back from Postgres into the in-memory
    cache - the fallback path the moment a redeploy/restart has wiped
    _sessions, instead of that student hitting 'session not found'."""
    conn = _db_conn()
    if not conn:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM sessions WHERE student_id = %s", (student_id,))
            row = cur.fetchone()
            if not row:
                return None
            s = _session_from_jsonable(row[0])
            _sessions[student_id] = s
            return s
    except Exception as e:
        print("LOAD SESSION FAILED", student_id, e)
        return None
    finally:
        _db_release(conn)

def delete_session_row(student_id):
    """Remove one student's row from Postgres entirely (not just from
    memory) - used by the admin/teacher "delete student" action below, and
    by the rename path when a student's email or teacher changes (their
    student_id is the primary key, so a rename is a delete-old +
    insert-under-new-key, never an in-place key update)."""
    conn = _db_conn()
    if not conn:
        return
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE student_id = %s", (student_id,))
    except Exception as e:
        print("DELETE SESSION FAILED", student_id, e)
    finally:
        _db_release(conn)

def _list_all_sessions_from_db():
    """Every persisted session from Postgres, keyed by student_id - the
    complete historical roster. _sessions alone only has whoever has hit
    this process since its last restart, which used to be the ONLY roster
    the admin/teacher student lists could show (so a restart made students
    who hadn't been active since briefly "disappear" from the list, even
    though their progress was never actually lost). Merging this with
    _sessions (see callers) gives a complete AND up-to-the-second roster."""
    conn = _db_conn()
    if not conn:
        return {}
    out = {}
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SELECT student_id, data FROM sessions")
            for student_id, data in cur.fetchall():
                try:
                    out[student_id] = _session_from_jsonable(data)
                except Exception:
                    continue
    except Exception as e:
        print("LIST SESSIONS FAILED", e)
    finally:
        _db_release(conn)
    return out

def get_session(student_id):
    """Drop-in replacement for the old _sessions.get(student_id) - checks
    memory first (identical fast path to before), falls back to the
    database only when missing there. Also marks the id as touched this
    request so _persist_touched_sessions (after_request, below) saves any
    change automatically without every endpoint needing its own save call."""
    if not student_id:
        return None
    s = _sessions.get(student_id)
    if s is None:
        s = load_session(student_id)
    if s is not None:
        try:
            g.touched_sessions.add(student_id)
        except RuntimeError:
            pass  # called outside a request context (shouldn't happen in practice)
    return s

@app.before_request
def _init_touched_sessions():
    g.touched_sessions = set()

@app.after_request
def _persist_touched_sessions(resp):
    for sid in getattr(g, "touched_sessions", ()):
        save_session(sid)
    return resp

db_init()
# --------------------------------------------------------------------------
# Cache of teacher_id -> that teacher's results worksheet gid (see
# get_results_tab_gid below) - avoids one extra Google Sheets API round trip
# on every single teacher login once it's been looked up once.
_results_tab_gid_cache = {}

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
# A0 is a pre-beginner level, deliberately NOT part of CEFR_LEVELS/the
# adaptive placement walk (which still only steps across real CEFR levels -
# see placement_answer). It's reached two ways: (1) a placement test where
# the student fails even the easiest sentence tested (see the final_level
# fallback in placement_answer), or (2) a teacher/admin assigning it by
# hand. ALL_STUDENT_LEVELS is the full set of values a student's saved
# level is allowed to take (used by set_student_level's validation and
# _lookup_student_level_row's sheet-value check) - CEFR_LEVELS itself stays
# untouched so every existing index/order-based calculation (placement
# up/down steps, next_cefr_level, the "never placed, default A1" rule) keeps
# working exactly as before.
ALL_STUDENT_LEVELS = ["A0"] + CEFR_LEVELS
# Placement test (see new_session/placement_answer below): a brand-new
# level-track student is never just dropped in at A1 and left to re-practice
# levels they already know - a short adaptive test finds their real starting
# level first. Starts at B1 (index 2) as a reasonable general anchor, moves
# up a level on a pass / down a level on a fail, and stops as soon as the
# result flips (that flip brackets the student's real level) or after
# PLACEMENT_MAX_STEPS sentences, whichever comes first - fast by design,
# since the whole point is not to waste the student's time.
PLACEMENT_START_IDX = 2
PLACEMENT_MAX_STEPS = 6

LEVEL_NAMES_HE = {
    "A0": "רמה A0 — צעד ראשון באנגלית",
    "A1": "רמה A1 — מתחילים",
    "A2": "רמה A2 — בסיסי",
    "B1": "רמה B1 — בינוני",
    "B2": "רמה B2 — בינוני-גבוה",
    "C1": "רמה C1 — מתקדם",
    "C2": "רמה C2 — שליטה מלאה",
}

# The 7 grammar topics every CEFR level (A1-C2) is organized around, in a
# fixed teaching order - each one repeats at every level (with vocabulary
# scaling up), and pronoun/person (I/you/he/she/it/we/they) rotates WITHIN
# a topic rather than being its own separate topic axis. "general" tags the
# original pre-existing sentences (mixed topics, kept as-is for continuity);
# "vocab" tags A0's single-word/short-phrase items.
GRAMMAR_TOPIC_ORDER = [
    "present_simple_statement", "present_simple_question", "present_continuous",
    "past_simple_statement", "past_simple_question", "future", "imperative",
]
GRAMMAR_TOPIC_NAMES_HE = {
    "present_simple_statement": "הווה פשוט — משפט חיווי",
    "present_simple_question": "הווה פשוט — משפט שאלה",
    "present_continuous": "הווה מתמשך",
    "past_simple_statement": "עבר פשוט — משפט חיווי",
    "past_simple_question": "עבר פשוט — משפט שאלה",
    "future": "עתיד",
    "imperative": "ציווי",
    "general": "כללי",
    "vocab": "אוצר מילים",
}

LEVEL_SENTENCES = {
    "A0": [
        {"en": "Hello", "he": "שלום", "topic": "vocab", "emoji": "👋"},
        {"en": "Thank you", "he": "תודה", "topic": "vocab", "emoji": "🙏"},
        {"en": "Yes", "he": "כן", "topic": "vocab", "emoji": "👍"},
        {"en": "No", "he": "לא", "topic": "vocab", "emoji": "👎"},
        {"en": "Water", "he": "מים", "topic": "vocab", "emoji": "💧"},
        {"en": "Apple", "he": "תפוח", "topic": "vocab", "emoji": "🍎"},
        {"en": "Bread", "he": "לחם", "topic": "vocab", "emoji": "🍞"},
        {"en": "Milk", "he": "חלב", "topic": "vocab", "emoji": "🥛"},
        {"en": "Dog", "he": "כלב", "topic": "vocab", "emoji": "🐶"},
        {"en": "Cat", "he": "חתול", "topic": "vocab", "emoji": "🐱"},
        {"en": "Book", "he": "ספר", "topic": "vocab", "emoji": "📖"},
        {"en": "House", "he": "בית", "topic": "vocab", "emoji": "🏠"},
        {"en": "Car", "he": "מכונית", "topic": "vocab", "emoji": "🚗"},
        {"en": "Sun", "he": "שמש", "topic": "vocab", "emoji": "☀️"},
        {"en": "Big", "he": "גדול", "topic": "vocab", "emoji": "🐘"},
        {"en": "Small", "he": "קטן", "topic": "vocab", "emoji": "🐜"},
        {"en": "Mother", "he": "אמא", "topic": "vocab", "emoji": "👩"},
        {"en": "Father", "he": "אבא", "topic": "vocab", "emoji": "👨"},
        {"en": "One", "he": "אחת", "topic": "vocab", "emoji": "1️⃣"},
        {"en": "Two", "he": "שתיים", "topic": "vocab", "emoji": "2️⃣"},
        {"en": "Three", "he": "שלוש", "topic": "vocab", "emoji": "3️⃣"},
        {"en": "Good morning", "he": "בוקר טוב", "topic": "vocab", "emoji": "🌅"},
        {"en": "I am hungry", "he": "אני רעב", "topic": "vocab", "emoji": "🍽️"},
        {"en": "I am happy", "he": "אני שמח", "topic": "vocab", "emoji": "😊"},
    ],
    "A1": [
        {"en": "I am a student", "he": "אני תלמיד", "topic": "general"},
        {"en": "This is my book", "he": "זה הספר שלי", "topic": "general"},
        {"en": "She has a red car", "he": "יש לה מכונית אדומה", "topic": "general"},
        {"en": "We live in Tel Aviv", "he": "אנחנו גרים בתל אביב", "topic": "general"},
        {"en": "He likes coffee", "he": "הוא אוהב קפה", "topic": "general"},
        {"en": "The cat is on the table", "he": "החתול על השולחן", "topic": "general"},
        {"en": "I am hungry now", "he": "אני רעב עכשיו", "topic": "general"},
        {"en": "My name is David", "he": "קוראים לי דוד", "topic": "general"},
        {"en": "They are my friends", "he": "הם החברים שלי", "topic": "general"},
        {"en": "Can you help me, please", "he": "אתה יכול לעזור לי, בבקשה", "topic": "general"},
        {"en": "What time is it", "he": "מה השעה", "topic": "general"},
        {"en": "I have two brothers", "he": "יש לי שני אחים", "topic": "general"},
        {"en": "The weather is nice today", "he": "מזג האוויר נעים היום", "topic": "general"},
        {"en": "She works in a bank", "he": "היא עובדת בבנק", "topic": "general"},
        {"en": "We eat breakfast at eight", "he": "אנחנו אוכלים ארוחת בוקר בשמונה", "topic": "general"},
        {"en": "I eat breakfast every morning", "he": "אני אוכל ארוחת בוקר כל בוקר", "topic": "present_simple_statement"},
        {"en": "She likes ice cream", "he": "היא אוהבת גלידה", "topic": "present_simple_statement"},
        {"en": "They live in a small house", "he": "הם גרים בבית קטן", "topic": "present_simple_statement"},
        {"en": "Do you like coffee", "he": "האם אתה אוהב קפה", "topic": "present_simple_question"},
        {"en": "Does he play soccer", "he": "האם הוא משחק כדורגל", "topic": "present_simple_question"},
        {"en": "Where do they work", "he": "איפה הם עובדים", "topic": "present_simple_question"},
        {"en": "I am reading a book now", "he": "אני קורא ספר עכשיו", "topic": "present_continuous"},
        {"en": "She is cooking dinner", "he": "היא מבשלת ארוחת ערב", "topic": "present_continuous"},
        {"en": "We are watching a movie", "he": "אנחנו צופים בסרט", "topic": "present_continuous"},
        {"en": "I walked to school yesterday", "he": "הלכתי לבית הספר אתמול", "topic": "past_simple_statement"},
        {"en": "He played tennis last week", "he": "הוא שיחק טניס בשבוע שעבר", "topic": "past_simple_statement"},
        {"en": "They visited their grandmother", "he": "הם ביקרו את סבתא שלהם", "topic": "past_simple_statement"},
        {"en": "Did you see that movie", "he": "האם ראית את הסרט הזה", "topic": "past_simple_question"},
        {"en": "Did she call you yesterday", "he": "האם היא התקשרה אליך אתמול", "topic": "past_simple_question"},
        {"en": "What did they eat for lunch", "he": "מה הם אכלו לארוחת צהריים", "topic": "past_simple_question"},
        {"en": "I will visit my friend tomorrow", "he": "אני אבקר את החבר שלי מחר", "topic": "future"},
        {"en": "She will study English next year", "he": "היא תלמד אנגלית בשנה הבאה", "topic": "future"},
        {"en": "We will travel to Israel soon", "he": "אנחנו ניסע לישראל בקרוב", "topic": "future"},
        {"en": "Please close the door", "he": "בבקשה סגור את הדלת", "topic": "imperative"},
        {"en": "Sit down, please", "he": "שב בבקשה", "topic": "imperative"},
        {"en": "Don't touch that", "he": "אל תיגע בזה", "topic": "imperative"},
    ],
    "A2": [
        {"en": "Yesterday I went to school", "he": "אתמול הלכתי לבית הספר", "topic": "general"},
        {"en": "We will meet at six o'clock", "he": "ניפגש בשעה שש", "topic": "general"},
        {"en": "I bought a new phone last week", "he": "קניתי טלפון חדש בשבוע שעבר", "topic": "general"},
        {"en": "She was very tired after work", "he": "היא הייתה עייפה מאוד אחרי העבודה", "topic": "general"},
        {"en": "Can I have the bill, please", "he": "אפשר לקבל את החשבון, בבקשה", "topic": "general"},
        {"en": "He is going to visit his parents", "he": "הוא הולך לבקר את ההורים שלו", "topic": "general"},
        {"en": "I usually wake up early in the morning", "he": "אני בדרך כלל קם מוקדם בבוקר", "topic": "general"},
        {"en": "They watched a movie last night", "he": "הם צפו בסרט אמש", "topic": "general"},
        {"en": "Do you know where the station is", "he": "אתה יודע איפה התחנה", "topic": "general"},
        {"en": "It was raining all day yesterday", "he": "ירד גשם כל היום אתמול", "topic": "general"},
        {"en": "We are planning a trip to Eilat", "he": "אנחנו מתכננים טיול לאילת", "topic": "general"},
        {"en": "I need to buy some vegetables", "he": "אני צריך לקנות ירקות", "topic": "general"},
        {"en": "My sister is learning to drive", "he": "אחותי לומדת לנהוג", "topic": "general"},
        {"en": "He never eats breakfast", "he": "הוא אף פעם לא אוכל ארוחת בוקר", "topic": "general"},
        {"en": "Please turn off the lights before you leave", "he": "בבקשה כבה את האורות לפני שאתה יוצא", "topic": "general"},
        {"en": "My brother works at a hospital every day", "he": "אחי עובד בבית חולים כל יום", "topic": "present_simple_statement"},
        {"en": "We usually have dinner at seven o'clock", "he": "אנחנו בדרך כלל אוכלים ארוחת ערב בשבע", "topic": "present_simple_statement"},
        {"en": "She often reads books before she sleeps", "he": "היא לעתים קרובות קוראת ספרים לפני שהיא ישנה", "topic": "present_simple_statement"},
        {"en": "How often do you exercise", "he": "כמה פעמים אתה מתאמן", "topic": "present_simple_question"},
        {"en": "What time does the train leave", "he": "באיזו שעה הרכבת יוצאת", "topic": "present_simple_question"},
        {"en": "Do your parents speak English", "he": "האם ההורים שלך מדברים אנגלית", "topic": "present_simple_question"},
        {"en": "He is learning to drive this month", "he": "הוא לומד לנהוג החודש", "topic": "present_continuous"},
        {"en": "The children are playing in the garden", "he": "הילדים משחקים בגינה", "topic": "present_continuous"},
        {"en": "I am waiting for the bus right now", "he": "אני מחכה לאוטובוס כרגע", "topic": "present_continuous"},
        {"en": "We traveled to Eilat last summer", "he": "נסענו לאילת בקיץ שעבר", "topic": "past_simple_statement"},
        {"en": "She finished her homework before dinner", "he": "היא סיימה את שיעורי הבית לפני ארוחת הערב", "topic": "past_simple_statement"},
        {"en": "They bought a new car last month", "he": "הם קנו מכונית חדשה בחודש שעבר", "topic": "past_simple_statement"},
        {"en": "Where did you go on vacation", "he": "לאן נסעת בחופשה", "topic": "past_simple_question"},
        {"en": "Did he finish his project on time", "he": "האם הוא סיים את הפרויקט בזמן", "topic": "past_simple_question"},
        {"en": "How did she learn to swim", "he": "איך היא למדה לשחות", "topic": "past_simple_question"},
        {"en": "I am going to start a new job next week", "he": "אני עומד להתחיל עבודה חדשה בשבוע הבא", "topic": "future"},
        {"en": "They will move to a bigger apartment", "he": "הם יעברו לדירה גדולה יותר", "topic": "future"},
        {"en": "We won't be late if we leave now", "he": "לא נאחר אם נצא עכשיו", "topic": "future"},
        {"en": "Turn left at the next corner", "he": "פנה שמאלה בפינה הבאה", "topic": "imperative"},
        {"en": "Remember to bring your passport", "he": "זכור להביא את הדרכון שלך", "topic": "imperative"},
        {"en": "Never leave the stove on", "he": "לעולם אל תשאיר את הכיריים דלוקים", "topic": "imperative"},
    ],
    "B1": [
        {"en": "I have never been to Paris", "he": "מעולם לא הייתי בפריז", "topic": "general"},
        {"en": "If it rains, we will stay home", "he": "אם ירד גשם, נישאר בבית", "topic": "general"},
        {"en": "She has already finished her homework", "he": "היא כבר סיימה את שיעורי הבית שלה", "topic": "general"},
        {"en": "I think this restaurant is too expensive", "he": "אני חושב שהמסעדה הזאת יקרה מדי", "topic": "general"},
        {"en": "Have you ever tried sushi before", "he": "ניסית פעם סושי", "topic": "general"},
        {"en": "We have been living here for five years", "he": "אנחנו גרים כאן כבר חמש שנים", "topic": "general"},
        {"en": "If I had more time, I would travel more", "he": "אם היה לי יותר זמן, הייתי מטייל יותר", "topic": "general"},
        {"en": "He apologized for being late to the meeting", "he": "הוא התנצל על האיחור לפגישה", "topic": "general"},
        {"en": "I'm not sure if I agree with your opinion", "he": "אני לא בטוח שאני מסכים עם הדעה שלך", "topic": "general"},
        {"en": "They have decided to move to a bigger apartment", "he": "הם החליטו לעבור לדירה גדולה יותר", "topic": "general"},
        {"en": "She would like to become a doctor someday", "he": "היא הייתה רוצה להיות רופאה יום אחד", "topic": "general"},
        {"en": "It seems like the traffic is getting worse", "he": "נראה שהתנועה נהיית גרועה יותר", "topic": "general"},
        {"en": "I should have called you earlier", "he": "הייתי צריך להתקשר אליך קודם", "topic": "general"},
        {"en": "We were supposed to meet an hour ago", "he": "היינו אמורים להיפגש לפני שעה", "topic": "general"},
        {"en": "Although it was expensive, we bought the tickets", "he": "למרות שזה היה יקר, קנינו את הכרטיסים", "topic": "general"},
        {"en": "Most people believe that exercise improves health", "he": "רוב האנשים מאמינים שפעילות גופנית משפרת את הבריאות", "topic": "present_simple_statement"},
        {"en": "The company offers several types of insurance", "he": "החברה מציעה כמה סוגי ביטוח", "topic": "present_simple_statement"},
        {"en": "He usually manages his time very well", "he": "הוא בדרך כלל מנהל את זמנו היטב", "topic": "present_simple_statement"},
        {"en": "What do you think about this new policy", "he": "מה אתה חושב על המדיניות החדשה הזאת", "topic": "present_simple_question"},
        {"en": "Why does she always arrive early", "he": "למה היא תמיד מגיעה מוקדם", "topic": "present_simple_question"},
        {"en": "How much does it cost to rent an apartment here", "he": "כמה עולה לשכור דירה כאן", "topic": "present_simple_question"},
        {"en": "The government is discussing new environmental laws", "he": "הממשלה דנה בחוקים סביבתיים חדשים", "topic": "present_continuous"},
        {"en": "I am currently working on an important project", "he": "אני עובד כרגע על פרויקט חשוב", "topic": "present_continuous"},
        {"en": "Prices are rising because of inflation", "he": "המחירים עולים בגלל האינפלציה", "topic": "present_continuous"},
        {"en": "The team worked hard to finish the project on time", "he": "הצוות עבד קשה כדי לסיים את הפרויקט בזמן", "topic": "past_simple_statement"},
        {"en": "She explained the situation clearly to everyone", "he": "היא הסבירה את המצב בבירור לכולם", "topic": "past_simple_statement"},
        {"en": "They negotiated a better deal with the supplier", "he": "הם ניהלו משא ומתן על עסקה טובה יותר עם הספק", "topic": "past_simple_statement"},
        {"en": "Why did the meeting start late", "he": "למה הישיבה התחילה מאוחר", "topic": "past_simple_question"},
        {"en": "What decision did the committee make", "he": "איזו החלטה קיבלה הוועדה", "topic": "past_simple_question"},
        {"en": "Did the plan work as expected", "he": "האם התוכנית עבדה כמצופה", "topic": "past_simple_question"},
        {"en": "The economy will probably improve next year", "he": "הכלכלה ככל הנראה תשתפר בשנה הבאה", "topic": "future"},
        {"en": "If we plan carefully, we will succeed", "he": "אם נתכנן בקפידה, נצליח", "topic": "future"},
        {"en": "I will let you know as soon as I decide", "he": "אני אודיע לך ברגע שאחליט", "topic": "future"},
        {"en": "Please consider all the options before deciding", "he": "אנא שקול את כל האפשרויות לפני שתחליט", "topic": "imperative"},
        {"en": "Make sure to double-check your work", "he": "הקפד לבדוק שוב את העבודה שלך", "topic": "imperative"},
        {"en": "Don't hesitate to ask for help if needed", "he": "אל תהסס לבקש עזרה אם צריך", "topic": "imperative"},
    ],
    "B2": [
        {"en": "The report was submitted before the deadline", "he": "הדוח הוגש לפני המועד האחרון", "topic": "general"},
        {"en": "She said that she would call me later", "he": "היא אמרה שהיא תתקשר אליי מאוחר יותר", "topic": "general"},
        {"en": "The building is being renovated this month", "he": "הבניין משופץ בחודש הזה", "topic": "general"},
        {"en": "He mentioned that the project had been delayed", "he": "הוא ציין שהפרויקט התעכב", "topic": "general"},
        {"en": "It's not worth arguing about such a small issue", "he": "זה לא שווה להתווכח על עניין כל כך קטן", "topic": "general"},
        {"en": "The decision was made without consulting the team", "he": "ההחלטה התקבלה בלי להתייעץ עם הצוות", "topic": "general"},
        {"en": "I'm afraid I have to disagree with that statement", "he": "אני חושש שאני צריך לחלוק על ההצהרה הזאת", "topic": "general"},
        {"en": "The company was founded over twenty years ago", "he": "החברה נוסדה לפני יותר מעשרים שנה", "topic": "general"},
        {"en": "Despite the challenges, the team met its goals", "he": "למרות האתגרים, הצוות עמד ביעדים שלו", "topic": "general"},
        {"en": "He was accused of breaking the rules", "he": "הוא הואשם בהפרת הכללים", "topic": "general"},
        {"en": "The manager insisted that changes be made immediately", "he": "המנהל התעקש שהשינויים ייעשו מיד", "topic": "general"},
        {"en": "I wish I had studied harder for the exam", "he": "הלוואי שהייתי לומד יותר בשקידה למבחן", "topic": "general"},
        {"en": "The new policy will be implemented next quarter", "he": "המדיניות החדשה תיושם ברבעון הבא", "topic": "general"},
        {"en": "She's been putting off the decision for weeks", "he": "היא דוחה את ההחלטה כבר שבועות", "topic": "general"},
        {"en": "It turned out that the rumor was completely false", "he": "התברר שהשמועה הייתה שקרית לחלוטין", "topic": "general"},
        {"en": "Research shows that sleep affects concentration significantly", "he": "מחקרים מראים ששינה משפיעה משמעותית על ריכוז", "topic": "present_simple_statement"},
        {"en": "The organization relies heavily on volunteer support", "he": "הארגון נשען רבות על תמיכת מתנדבים", "topic": "present_simple_statement"},
        {"en": "Modern technology constantly reshapes how we communicate", "he": "הטכנולוגיה המודרנית מעצבת מחדש כל הזמן את הדרך שבה אנו מתקשרים", "topic": "present_simple_statement"},
        {"en": "What factors influence consumer behavior the most", "he": "אילו גורמים משפיעים הכי הרבה על התנהגות הצרכנים", "topic": "present_simple_question"},
        {"en": "How does the new regulation affect small businesses", "he": "איך התקנה החדשה משפיעה על עסקים קטנים", "topic": "present_simple_question"},
        {"en": "Why do so many people struggle with time management", "he": "למה כל כך הרבה אנשים מתקשים בניהול זמן", "topic": "present_simple_question"},
        {"en": "Scientists are developing new methods to fight climate change", "he": "מדענים מפתחים שיטות חדשות להילחם בשינויי האקלים", "topic": "present_continuous"},
        {"en": "The company is expanding its operations overseas", "he": "החברה מרחיבה את פעילותה בחו\"ל", "topic": "present_continuous"},
        {"en": "More people are choosing to work remotely nowadays", "he": "יותר אנשים בוחרים לעבוד מרחוק בימינו", "topic": "present_continuous"},
        {"en": "The government introduced new measures to reduce unemployment", "he": "הממשלה הנהיגה אמצעים חדשים כדי להפחית את האבטלה", "topic": "past_simple_statement"},
        {"en": "Researchers discovered an unexpected link between diet and mood", "he": "חוקרים גילו קשר בלתי צפוי בין תזונה למצב רוח", "topic": "past_simple_statement"},
        {"en": "The company faced significant challenges during the crisis", "he": "החברה התמודדה עם אתגרים משמעותיים במהלך המשבר", "topic": "past_simple_statement"},
        {"en": "What caused the sudden increase in prices", "he": "מה גרם לעלייה הפתאומית במחירים", "topic": "past_simple_question"},
        {"en": "How did the team overcome such a difficult obstacle", "he": "איך הצוות התגבר על מכשול כה קשה", "topic": "past_simple_question"},
        {"en": "Why did the negotiations fail in the end", "he": "למה המשא ומתן נכשל בסופו של דבר", "topic": "past_simple_question"},
        {"en": "Unless something changes, costs will continue to rise", "he": "אלא אם משהו ישתנה, העלויות ימשיכו לעלות", "topic": "future"},
        {"en": "Experts predict that demand will increase significantly", "he": "מומחים חוזים שהביקוש יגדל משמעותית", "topic": "future"},
        {"en": "The new policy will likely affect thousands of employees", "he": "המדיניות החדשה תשפיע ככל הנראה על אלפי עובדים", "topic": "future"},
        {"en": "Consider the long-term consequences before making a decision", "he": "שקול את ההשלכות ארוכות הטווח לפני קבלת החלטה", "topic": "imperative"},
        {"en": "Avoid making assumptions without sufficient evidence", "he": "הימנע מהנחות ללא ראיות מספקות", "topic": "imperative"},
        {"en": "Always verify your sources before sharing information", "he": "תמיד ודא את המקורות שלך לפני שיתוף מידע", "topic": "imperative"},
    ],
    "C1": [
        {"en": "Rarely have I seen such dedication to a project", "he": "לעיתים רחוקות ראיתי מסירות כזאת לפרויקט", "topic": "general"},
        {"en": "Had I known earlier, I would have acted differently", "he": "אילו ידעתי מוקדם יותר, הייתי פועל אחרת", "topic": "general"},
        {"en": "Not only did she finish first, but she also broke the record", "he": "היא לא רק סיימה ראשונה, אלא גם שברה את השיא", "topic": "general"},
        {"en": "It is essential that every detail be verified beforehand", "he": "חיוני שכל פרט ייבדק מראש", "topic": "general"},
        {"en": "Little did they know how much the decision would cost them", "he": "הם לא ידעו כמה ההחלטה תעלה להם", "topic": "general"},
        {"en": "The committee recommended that the policy be reconsidered", "he": "הוועדה המליצה שהמדיניות תישקל מחדש", "topic": "general"},
        {"en": "Seldom do we encounter such a compelling argument", "he": "לעיתים רחוקות אנו נתקלים בטיעון משכנע כל כך", "topic": "general"},
        {"en": "Were it not for her guidance, the project would have failed", "he": "לולא ההדרכה שלה, הפרויקט היה נכשל", "topic": "general"},
        {"en": "The findings, though preliminary, suggest a clear trend", "he": "הממצאים, אף שהם ראשוניים, מצביעים על מגמה ברורה", "topic": "general"},
        {"en": "He is said to have influenced an entire generation of writers", "he": "אומרים שהוא השפיע על דור שלם של סופרים", "topic": "general"},
        {"en": "Under no circumstances should this document be shared externally", "he": "בשום פנים ואופן אין לשתף את המסמך הזה מחוץ לארגון", "topic": "general"},
        {"en": "So convincing was her argument that no one objected", "he": "הטיעון שלה היה משכנע עד כדי כך שאיש לא התנגד", "topic": "general"},
        {"en": "The proposal warrants further consideration before approval", "he": "ההצעה מצדיקה שיקול נוסף לפני האישור", "topic": "general"},
        {"en": "Given the circumstances, the outcome was hardly surprising", "he": "לאור הנסיבות, התוצאה בקושי הפתיעה", "topic": "general"},
        {"en": "He acted as though nothing unusual had happened", "he": "הוא נהג כאילו לא קרה שום דבר יוצא דופן", "topic": "general"},
        {"en": "Economic instability often undermines public confidence in institutions", "he": "חוסר יציבות כלכלית פוגע לעתים קרובות באמון הציבור במוסדות", "topic": "present_simple_statement"},
        {"en": "The evidence suggests a correlation rather than a direct cause", "he": "הראיות מרמזות על מתאם ולא על סיבה ישירה", "topic": "present_simple_statement"},
        {"en": "Effective leadership requires balancing competing priorities", "he": "מנהיגות אפקטיבית דורשת איזון בין עדיפויות מתחרות", "topic": "present_simple_statement"},
        {"en": "To what extent does government policy shape economic outcomes", "he": "עד כמה מדיניות הממשלה מעצבת תוצאות כלכליות", "topic": "present_simple_question"},
        {"en": "What underlying assumptions does this argument rely on", "he": "על אילו הנחות יסוד מסתמכת הטענה הזו", "topic": "present_simple_question"},
        {"en": "How do cultural differences influence negotiation strategies", "he": "איך הבדלים תרבותיים משפיעים על אסטרטגיות משא ומתן", "topic": "present_simple_question"},
        {"en": "Analysts are reassessing their forecasts in light of new data", "he": "אנליסטים בוחנים מחדש את התחזיות שלהם לאור נתונים חדשים", "topic": "present_continuous"},
        {"en": "The industry is undergoing a fundamental transformation", "he": "התעשייה עוברת שינוי מהותי", "topic": "present_continuous"},
        {"en": "Policymakers are grappling with the implications of automation", "he": "קובעי המדיניות מתמודדים עם ההשלכות של האוטומציה", "topic": "present_continuous"},
        {"en": "The committee ultimately rejected the proposal despite strong support", "he": "הוועדה דחתה בסופו של דבר את ההצעה למרות תמיכה חזקה", "topic": "past_simple_statement"},
        {"en": "The findings contradicted several long-held assumptions", "he": "הממצאים סתרו כמה הנחות ותיקות", "topic": "past_simple_statement"},
        {"en": "The crisis exposed significant weaknesses in the system", "he": "המשבר חשף חולשות משמעותיות במערכת", "topic": "past_simple_statement"},
        {"en": "What implications did the ruling have for future cases", "he": "אילו השלכות היו לפסיקה על מקרים עתידיים", "topic": "past_simple_question"},
        {"en": "How did the researchers account for conflicting variables", "he": "איך החוקרים התייחסו למשתנים סותרים", "topic": "past_simple_question"},
        {"en": "Why did the initiative fail to gain widespread support", "he": "למה היוזמה לא הצליחה לזכות בתמיכה נרחבת", "topic": "past_simple_question"},
        {"en": "Should the trend continue, resources will become increasingly scarce", "he": "אם המגמה תימשך, המשאבים יהפכו למוגבלים יותר ויותר", "topic": "future"},
        {"en": "The reform is expected to have far-reaching consequences", "he": "הרפורמה צפויה להיות בעלת השלכות מרחיקות לכת", "topic": "future"},
        {"en": "Given current projections, demand will likely outpace supply", "he": "לאור התחזיות הנוכחיות, הביקוש כנראה יעלה על ההיצע", "topic": "future"},
        {"en": "Weigh the evidence carefully before drawing conclusions", "he": "שקול את הראיות בקפידה לפני הסקת מסקנות", "topic": "imperative"},
        {"en": "Refrain from oversimplifying a complex issue", "he": "הימנע מפישוט יתר של סוגיה מורכבת", "topic": "imperative"},
        {"en": "Challenge your own assumptions whenever possible", "he": "אתגר את ההנחות שלך בכל הזדמנות", "topic": "imperative"},
    ],
    "C2": [
        {"en": "Notwithstanding the setbacks, the initiative persevered", "he": "חרף הנסיגות, היוזמה התמידה", "topic": "general"},
        {"en": "The ambiguity of the clause warrants further scrutiny", "he": "העמימות של הסעיף מצריכה בחינה נוספת", "topic": "general"},
        {"en": "Her eloquence captivated the entire audience", "he": "הרהיטות שלה ריתקה את כל הקהל", "topic": "general"},
        {"en": "The findings corroborate the initial hypothesis", "he": "הממצאים מאששים את ההשערה הראשונית", "topic": "general"},
        {"en": "He remained impervious to criticism throughout the ordeal", "he": "הוא נותר חסין לביקורת לאורך כל המבחן", "topic": "general"},
        {"en": "The negotiations were fraught with unforeseen complications", "he": "המשא ומתן היה רווי בסיבוכים בלתי צפויים", "topic": "general"},
        {"en": "Such an egregious error cannot go unaddressed", "he": "טעות כה חמורה אינה יכולה להישאר ללא טיפול", "topic": "general"},
        {"en": "The author's prose is renowned for its subtlety and nuance", "he": "הפרוזה של הסופר ידועה בעדינותה וברבדיה", "topic": "general"},
        {"en": "The policy's ramifications are still being assessed", "he": "ההשלכות של המדיניות עדיין נבחנות", "topic": "general"},
        {"en": "Her tenacity in the face of adversity was inspiring", "he": "העקשנות שלה מול קשיים הייתה מעוררת השראה", "topic": "general"},
        {"en": "The evidence, albeit circumstantial, was compelling", "he": "הראיות, אף שהיו נסיבתיות, היו משכנעות", "topic": "general"},
        {"en": "The board's decision was met with unanimous approval", "he": "החלטת הדירקטוריון זכתה לאישור פה אחד", "topic": "general"},
        {"en": "His argument, though cogent, failed to sway the jury", "he": "הטיעון שלו, אף שהיה משכנע, לא הצליח לשכנע את חבר המושבעים", "topic": "general"},
        {"en": "The city's infrastructure is ill-equipped to handle the surge", "he": "התשתית של העיר אינה מצוידת כראוי להתמודד עם הזינוק", "topic": "general"},
        {"en": "It would be remiss of us not to acknowledge her contribution", "he": "זו תהיה רשלנות מצידנו לא להכיר בתרומתה", "topic": "general"},
        {"en": "The nuances of the argument often elude casual observers", "he": "הניואנסים של הטענה חומקים לעתים קרובות ממתבוננים מזדמנים", "topic": "present_simple_statement"},
        {"en": "Systemic inequities perpetuate themselves across generations", "he": "אי-שוויון מערכתי משמר את עצמו לאורך דורות", "topic": "present_simple_statement"},
        {"en": "Rhetorical framing subtly shapes public perception of complex issues", "he": "מסגור רטורי מעצב בעדינות את התפיסה הציבורית של סוגיות מורכבות", "topic": "present_simple_statement"},
        {"en": "What accounts for the persistent disparity despite decades of reform", "he": "מה מסביר את הפער המתמשך למרות עשורים של רפורמה", "topic": "present_simple_question"},
        {"en": "To what degree can correlation be disentangled from causation here", "he": "עד כמה ניתן להפריד כאן בין מתאם לסיבתיות", "topic": "present_simple_question"},
        {"en": "How does the interplay of incentives distort rational decision-making", "he": "איך המשחק ההדדי של תמריצים מעוות קבלת החלטות רציונלית", "topic": "present_simple_question"},
        {"en": "Institutions are quietly recalibrating their strategies amid mounting scrutiny", "he": "מוסדות מכיילים מחדש בשקט את האסטרטגיות שלהם לנוכח ביקורת גוברת", "topic": "present_continuous"},
        {"en": "The discourse is shifting toward a more nuanced understanding of risk", "he": "השיח נע לעבר הבנה מורכבת יותר של סיכון", "topic": "present_continuous"},
        {"en": "Emerging evidence is complicating the prevailing consensus", "he": "ראיות מתהוות מסבכות את הקונצנזוס הרווח", "topic": "present_continuous"},
        {"en": "The reform inadvertently entrenched the very disparities it sought to address", "he": "הרפורמה, בשוגג, ביססה את אותם פערים שביקשה לטפל בהם", "topic": "past_simple_statement"},
        {"en": "Historians long overlooked the significance of this event", "he": "היסטוריונים התעלמו זמן רב מהמשמעות של האירוע הזה", "topic": "past_simple_statement"},
        {"en": "The policy's unintended consequences outweighed its intended benefits", "he": "ההשלכות הבלתי מכוונות של המדיניות עלו על התועלות המכוונות שלה", "topic": "past_simple_statement"},
        {"en": "What prompted such a dramatic reversal in policy", "he": "מה הניע היפוך כה דרמטי במדיניות", "topic": "past_simple_question"},
        {"en": "How did the paradigm shift reshape the entire field", "he": "איך שינוי הפרדיגמה עיצב מחדש את התחום כולו", "topic": "past_simple_question"},
        {"en": "Why did prevailing theories fail to anticipate the collapse", "he": "למה התיאוריות הרווחות לא הצליחו לצפות את הקריסה", "topic": "past_simple_question"},
        {"en": "Absent decisive intervention, the disparity will only widen", "he": "בהיעדר התערבות נחרצת, הפער רק יתרחב", "topic": "future"},
        {"en": "The ramifications will likely reverberate for decades to come", "he": "ההשלכות ככל הנראה יהדהדו במשך עשורים קדימה", "topic": "future"},
        {"en": "Should current trends persist, the consensus will inevitably fracture", "he": "אם המגמות הנוכחיות יימשכו, הקונצנזוס בהכרח יתפורר", "topic": "future"},
        {"en": "Resist the temptation to reduce a nuanced debate to simple slogans", "he": "התנגד לפיתוי לצמצם דיון מורכב לסיסמאות פשוטות", "topic": "imperative"},
        {"en": "Scrutinize the underlying data before accepting any sweeping claim", "he": "בחן בקפידה את הנתונים הבסיסיים לפני קבלת כל טענה גורפת", "topic": "imperative"},
        {"en": "Question the framing as rigorously as the content itself", "he": "הטל ספק במסגור באותה קפדנות כמו בתוכן עצמו", "topic": "imperative"},
    ],
}

# Reverse lookup: English sentence text -> grammar topic, built once from the
# built-in curriculum above. Used by get_weak_topics() to map a student's
# past result rows (which only ever store the sentence TEXT, not its topic -
# the results Google Sheet's column layout is deliberately left untouched,
# see RESULT_HEADERS/the comment on _result_row_to_values) back to the topic
# that sentence was practicing, without adding a new sheet column.
SENTENCE_TOPIC_LOOKUP = {
    sent["en"]: sent.get("topic", "general")
    for level_sentences in LEVEL_SENTENCES.values()
    for sent in level_sentences
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
            # Whether THIS teacher's students see the chat-bubble "Private"
            # skin by default on their very first login (a student's own
            # later toggle on their own device always overrides this - see
            # index.html's privateMode init + startApp()). Off by default so
            # existing teachers' students see no change until the teacher
            # opts in via /api/teacher-settings.
            "default_private_mode": False,
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
        state[tid]["default_private_mode"] = bool(state[tid].get("default_private_mode", False))
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
        "photo_url": t.get("photo_url", ""),
        "default_private_mode": bool(s.get("default_private_mode", False)),
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

def lighten_hex(hex_color, amount=0.82):
    """Blend a HEX color towards white, for deriving a matching light "avatar
    background" tint from a teacher's chosen main color (used when the admin
    picks a color swatch but doesn't separately specify color_light - without
    this every teacher's avatar background defaulted to the same flat
    lavender no matter what color they picked, which looked mismatched)."""
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        r = round(r + (255 - r) * amount)
        g = round(g + (255 - g) * amount)
        b = round(b + (255 - b) * amount)
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return "#ede7ff"

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

def invalidate_sentence_cache(csv_url):
    """Drop the cached parsed rows for one exercise's sheet, so the next
    load_sentences_from_csv(_ex) call re-fetches from Google Sheets instead of
    serving a stale copy. Called whenever a teacher (re)selects an exercise,
    and from the explicit "refresh" endpoint below."""
    csv_url = extract_csv_url(csv_url or "")
    if not csv_url:
        return
    _cache.pop("sentences:" + csv_url, None)

def save_ai_exercise(tid, name, sentences):
    """Persist a teacher-reviewed, AI-generated exercise (see
    /api/teacher/save-ai-exercise) and return its new id, or None on
    failure (no DB configured, or the insert itself failed)."""
    conn = _db_conn()
    if not conn:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_exercises (teacher_id, name, sentences) VALUES (%s, %s, %s) RETURNING id",
                (tid, name, json.dumps(sentences)),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        print("SAVE AI EXERCISE FAILED", tid, e)
        return None
    finally:
        _db_release(conn)

def load_ai_exercise_sentences(ai_id):
    """Mirrors load_sentences_from_csv_ex's (sentences, used_fallback)
    contract exactly, so new_session() can treat an "ai://<id>" csv_url
    exactly like a real one - used_fallback=True (generic demo content) on
    any failure (bad id, no DB, row deleted) rather than raising, so a
    dangling reference never crashes a student's session."""
    try:
        conn = _db_conn()
        if not conn:
            return FALLBACK_SENTENCES[:], True
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT sentences FROM ai_exercises WHERE id = %s", (int(ai_id),))
                row = cur.fetchone()
        finally:
            _db_release(conn)
        if not row or not row[0]:
            return FALLBACK_SENTENCES[:], True
        sentences = row[0] if isinstance(row[0], list) else json.loads(row[0])
        sentences = [s for s in sentences if isinstance(s, dict) and s.get("en")]
        if not sentences:
            return FALLBACK_SENTENCES[:], True
        return sentences, False
    except Exception as e:
        print("LOAD AI EXERCISE FAILED", ai_id, e)
        return FALLBACK_SENTENCES[:], True

_NUM_WORD_TO_DIGIT = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
    "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
    "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
    "hundred": "100", "thousand": "1000",
}

def normalize(text):
    text = text or ""
    # Chrome's speech recognizer frequently auto-formats a spoken number as a
    # digit, and especially formats spoken times like "eight" (o'clock) as
    # "8:00" - the ":00" is redundant for an on-the-hour time, so strip it
    # before the general punctuation strip below turns "8:00" into "800"
    # (which would then fail to match a sentence that spells out "eight").
    text = re.sub(r"\b(\d{1,2}):00\b", r"\1", text)
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()

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

def _num_tolerant_word(w):
    # A single spelled-out number word (e.g. "eight") and its digit form
    # (e.g. "8", already normalized down from a recognizer-formatted "8:00")
    # are the same answer, just formatted differently by the speech
    # recognizer - not a real pronunciation mistake. Only handles single-word
    # numbers (one..ninety, hundred, thousand); multi-word numbers like
    # "twenty five" are intentionally out of scope here since collapsing them
    # would change the word count and break the position-based alignment
    # below - a rarer case than the plain single-number mismatch reported.
    return _NUM_WORD_TO_DIGIT.get(w, w)

def _tolerant_key(w):
    return _s_tolerant_word(_num_tolerant_word(w))

def _s_tolerant_match(a_words, b_words):
    return len(a_words) == len(b_words) and [_tolerant_key(w) for w in a_words] == [_tolerant_key(w) for w in b_words]

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
    # Align using the tolerant keys (trailing-s + number-word/digit - see
    # _tolerant_key) so this breakdown never contradicts similarity()'s score -
    # a sentence that scores 100% because of one of these tolerances must not
    # still show a word marked "wrong" here, which would look like a
    # contradiction to the student.
    sp_key, co_key = [_tolerant_key(w) for w in sp], [_tolerant_key(w) for w in co]
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
    placement_active = False
    placement_sentence = None
    if not csv_url.strip():
        email_clean = (student_email or "").strip().lower()
        if email_clean and not has_saved_student_level(teacher_id, email_clean):
            # Brand new to the level track (no saved level yet, and we have
            # an email to key it on) - run the short adaptive placement test
            # first instead of cold-starting at A1. See placement_answer()
            # for how this concludes and hands off into the real curriculum.
            placement_active = True
            sentences = []
            used_fallback = False
            level_exercise_name = "מבחן רמות"
            placement_sentence = random.choice(LEVEL_SENTENCES[CEFR_LEVELS[PLACEMENT_START_IDX]])
        else:
            # No teacher-selected exercise: fall back to the built-in, per-student
            # CEFR leveled curriculum (starts at their saved level, advances
            # automatically) instead of the old 3-sentence generic demo content.
            sentences, level_track = load_level_track_sentences(teacher_id, student_email)
            used_fallback = False
            level_exercise_name = LEVEL_NAMES_HE.get(level_track, "תרגול דמו")
    elif csv_url.startswith("ai://"):
        # AI-generated exercise (see save_ai_exercise/load_ai_exercise_sentences)
        # - never fetched over HTTP like a real csv_url, so it works
        # regardless of Google Sheets sharing settings.
        sentences, used_fallback = load_ai_exercise_sentences(csv_url[len("ai://"):])
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
                    _persist_teacher_exercise(teacher_id)
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
        # Global practice flow, swept across ALL sentences one stage at a time
        # (didactic "let it breathe" restructure): "preview" (ungraded,
        # listen/read every sentence once, free to go back/forward) -> then
        # "accuracy" (station 1 + Bloom mastery reps, exactly as before, but
        # completing one sentence moves on to the NEXT sentence instead of
        # immediately testing that same sentence's cloze) -> then "cloze"
        # (a second full sweep, cloze-testing every sentence that has one) ->
        # then the review round / final exam, unchanged. "current" is always
        # the index WITHIN the active stage's sweep, reset to 0 when a stage
        # hands off to the next one. Sessions with no loaded sentences skip
        # preview entirely (nothing to page through) so they fall straight
        # into the existing empty-exercise handling.
        "stage": "placement" if placement_active else ("preview" if sentences else "accuracy"),
        "current": 0,
        # Placement test state (see placement_answer below) - all no-ops
        # once placement_active is False, which is the case for every
        # session except a level-track student's very first one.
        "placement_active": placement_active,
        "placement_idx": PLACEMENT_START_IDX,
        "placement_step": 0,
        "placement_history": [],
        "placement_current": placement_sentence,
        # Per-sentence accuracy-stage results (mastery reps/score/attempts),
        # cached here by index once a sentence finishes the accuracy sweep,
        # so the SINGLE result row for that sentence (still just one row per
        # sentence, same as before) can be written later once its cloze
        # sweep is resolved too - even though the two sweeps now happen far
        # apart in time instead of back-to-back for the same sentence.
        "accuracy_data": {},
        # Indices already fully written to results (skipped/capped straight
        # out of the accuracy sweep) - the cloze sweep must skip over these
        # entirely rather than asking for a cloze on a sentence that already
        # has its one-and-only result row.
        "finalized_indices": set(),
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
        # Distinct from "completed" above, which is set the moment the
        # student merely REACHES station 4 (practice/cloze sweep done) -
        # long before they've actually taken the exam. This flag is only set
        # once the client explicitly reports the exam summary screen was
        # shown (see /api/exam-complete), and is what login-resume logic and
        # the "you just finished" message below key off - otherwise a
        # student who skipped sentences in practice and hadn't started/
        # finished the exam yet would get told they "just finished" with a
        # misleading pre-exam average on their very next login.
        "exam_completed": False,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    # Self-persisting on creation (not just relying on the after_request
    # hook) since new_session() assigns _sessions[student_id] directly
    # rather than going through get_session() - this guarantees a brand-new
    # student survives a restart even within their very first request.
    save_session(student_id)


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
    base = (EZRA_APP_BASE_URL or "https://speakmaster.org").rstrip("/")
    return f"{base}/?lang=en&link={quote(csv_url, safe=':/?&=%') }"

def extract_sheet_id(value):
    """Pull the raw spreadsheet ID out of a normal Google Sheets URL pasted
    from the browser address bar (share/edit link) - or, if it doesn't look
    like a URL at all, assume it's already a raw ID and use it as-is."""
    raw = clean_cell(value).strip()
    if not raw:
        return ""
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", raw)
    return m.group(1) if m else raw

TEACHERS_HEADER = [
    "teacher_id", "name", "color", "color_light", "voice_gender",
    "student_password", "teacher_password", "threshold", "max_attempts",
    "results_sheet_id", "created_at", "photo_url", "exercise_name", "csv_url",
    "google_email",
]

def load_extra_teachers():
    """Admin-added (or admin-edited) teachers, stored in a "Teachers" tab of
    the catalog spreadsheet rather than a local file - Render's local disk is
    not reliably persisted across redeploys/restarts, the same reason every
    other durable thing in this app (results, catalog, student levels)
    already lives in a Google Sheet instead. Returns a dict in the same shape
    as the hardcoded TEACHERS dict, meant to be merged on top of it.
    A row here DOES override a hardcoded entry (Dan/Sara) if one exists for
    the same teacher_id - that's intentional: saving an edit via /admin
    writes a row for whichever teacher was edited, hardcoded or not, and that
    saved row is meant to become the new source of truth from then on. A
    hardcoded teacher who has never been edited simply has no row here yet,
    so their env-var-configured defaults keep applying untouched.
    """
    extra = {}
    import gspread
    # Retry transient Google API errors (503 "service unavailable", 429 rate
    # limit) a few times with a short backoff before giving up. This used to
    # have zero retries, and since this whole function only ever ran ONCE at
    # process startup (see _extra_teachers = load_extra_teachers() below), a
    # single transient hiccup at exactly that moment meant every admin-added
    # teacher (anyone beyond the two hardcoded ones) silently vanished from
    # the student login screen for the rest of that worker's lifetime - no
    # error shown to anyone, no automatic recovery short of a redeploy. That
    # is exactly what happened in production (APIError 503 at boot).
    last_err = None
    for attempt in range(3):
        try:
            gc = get_gspread_client()
            sh = gc.open_by_key(ADMIN_SHEET_ID)
            try:
                ws = sh.worksheet(TEACHERS_TAB)
            except gspread.WorksheetNotFound:
                return extra
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    else:
        print("LOAD EXTRA TEACHERS FAILED (all retries exhausted)", last_err)
        return extra
    try:
        for r in ws.get_all_records():
            tid = re.sub(r"[^a-z0-9]", "", clean_cell(r.get("teacher_id", "")).strip().lower())
            if not tid:
                continue
            extra[tid] = {
                "name": clean_cell(r.get("name", "")) or tid,
                "color": clean_cell(r.get("color", "")) or "#4318D1",
                "color_light": clean_cell(r.get("color_light", "")) or "#ede7ff",
                "voice_gender": clean_cell(r.get("voice_gender", "")) or "female",
                "results_tab": tid,
                "student_password": clean_cell(r.get("student_password", "")) or "class2026",
                "teacher_password": clean_cell(r.get("teacher_password", "")) or (tid + "2026"),
                "default_threshold": int(r.get("threshold") or 85),
                "default_max_attempts": int(r.get("max_attempts") or 5),
                "photo_url": clean_cell(r.get("photo_url", "")),
                "google_email": clean_cell(r.get("google_email", "")),
                # Currently-selected exercise, durably saved here (see
                # _upsert_teacher_row) instead of only in teacher_state.json
                # on Render's ephemeral local disk, which is wiped on every
                # redeploy - that's what silently reset every teacher back to
                # the demo exercise after each push.
                "_saved_exercise_name": clean_cell(r.get("exercise_name", "")),
                "_saved_csv_url": clean_cell(r.get("csv_url", "")),
            }
            rsid = clean_cell(r.get("results_sheet_id", "")).strip()
            if rsid:
                RESULTS_SHEET_IDS[tid] = rsid
    except Exception as e:
        print("LOAD EXTRA TEACHERS FAILED", e)
    return extra

def _upsert_teacher_row(tid, entry, results_sheet_id):
    """Create OR update this teacher's row in the "Teachers" sheet tab, so
    both adding a new teacher and editing an existing one (including the two
    hardcoded ones, Dan/Sara) survive the next redeploy/restart (see
    load_extra_teachers above). Creates the tab + header row on first use.
    entry may also carry "exercise_name"/"csv_url" - a teacher's CURRENTLY
    SELECTED exercise used to live only in _teacher_state, saved only to a
    local JSON file (teacher_state.json). Render's local disk does not
    survive a redeploy, so every push silently reset every teacher back to
    the demo exercise - this sheet row is now the durable source of truth
    for that too, same as everything else here."""
    import gspread
    gc = get_gspread_client()
    sh = gc.open_by_key(ADMIN_SHEET_ID)
    try:
        ws = sh.worksheet(TEACHERS_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=TEACHERS_TAB, rows=100, cols=len(TEACHERS_HEADER))
        ws.append_row(TEACHERS_HEADER, value_input_option="USER_ENTERED")
    # Sheets created before exercise_name/csv_url were added to TEACHERS_HEADER
    # only have the older, shorter header row - extend it in place (append
    # any missing columns at the end) rather than assuming every live sheet
    # already matches the current TEACHERS_HEADER exactly.
    header = ws.row_values(1)
    missing = [h for h in TEACHERS_HEADER if h not in header]
    if missing:
        header = header + missing
        last_col = chr(ord("A") + len(header) - 1)
        ws.update(f"A1:{last_col}1", [header], value_input_option="USER_ENTERED")
    values_by_key = {
        "teacher_id": tid, "name": entry.get("name", tid), "color": entry.get("color", "#4318D1"),
        "color_light": entry.get("color_light", "#ede7ff"), "voice_gender": entry.get("voice_gender", "female"),
        "student_password": entry.get("student_password", "class2026"),
        "teacher_password": entry.get("teacher_password", tid + "2026"),
        "threshold": entry.get("default_threshold", 85), "max_attempts": entry.get("default_max_attempts", 5),
        "results_sheet_id": results_sheet_id or "", "created_at": now_str(),
        "photo_url": entry.get("photo_url", ""),
        "google_email": entry.get("google_email", ""),
        # entry (a plain teacher-profile dict: name/color/passwords/...) never
        # actually carries these two - fall back to this server's current
        # in-memory selection for that teacher instead of blanking the cell,
        # so a routine admin profile edit (name/color/password) can never
        # silently wipe out the teacher's already-selected exercise.
        "exercise_name": entry.get("exercise_name") or _teacher_state.get(tid, {}).get("exercise_name", ""),
        "csv_url": entry.get("csv_url") or _teacher_state.get(tid, {}).get("csv_url", ""),
    }
    row_values = [values_by_key.get(h, "") for h in header]
    cell = None
    try:
        cell = ws.find(tid, in_column=1)
    except Exception:
        cell = None
    if cell:
        last_col = chr(ord("A") + len(header) - 1)
        ws.update(f"A{cell.row}:{last_col}{cell.row}", [row_values], value_input_option="USER_ENTERED")
    else:
        ws.append_row(row_values, value_input_option="USER_ENTERED")

def _persist_teacher_exercise(tid):
    """Durably save just this teacher's currently-selected exercise (name +
    csv_url) into their existing row in the Teachers sheet, touching ONLY
    those two cells - name/color/passwords/results_sheet_id/photo are left
    completely untouched, so this can never clobber them even though it's a
    much more frequent write than a full profile edit. Best-effort: must
    never break exercise selection itself if the sheet write fails (the
    in-memory _teacher_state change already happened and still works for the
    rest of this server's lifetime - it just won't survive the next
    redeploy)."""
    try:
        import gspread
        gc = get_gspread_client()
        sh = gc.open_by_key(ADMIN_SHEET_ID)
        try:
            ws = sh.worksheet(TEACHERS_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=TEACHERS_TAB, rows=100, cols=len(TEACHERS_HEADER))
            ws.append_row(TEACHERS_HEADER, value_input_option="USER_ENTERED")
        header = ws.row_values(1)
        missing = [h for h in TEACHERS_HEADER if h not in header]
        if missing:
            header = header + missing
            last_col = chr(ord("A") + len(header) - 1)
            ws.update(f"A1:{last_col}1", [header], value_input_option="USER_ENTERED")
        cell = ws.find(tid, in_column=1)
        if not cell:
            _upsert_teacher_row(tid, dict(TEACHERS.get(tid, {})), RESULTS_SHEET_IDS.get(tid, ""))
            return
        ts = _teacher_state.get(tid, {})
        name_col = header.index("exercise_name") + 1
        url_col = header.index("csv_url") + 1
        ws.update_cell(cell.row, name_col, ts.get("exercise_name", ""))
        ws.update_cell(cell.row, url_col, ts.get("csv_url", ""))
    except Exception as e:
        print("PERSIST TEACHER EXERCISE FAILED", tid, e)

# Merge in any admin-added teachers now that get_gspread_client/load_extra_teachers
# are defined above (this has to run after TEACHERS/_teacher_state's initial
# setup earlier in the file, and after these two functions - hence placed
# here rather than up near the hardcoded Dan/Sara TEACHERS dict).
#
# _persisted_teacher_ids tracks which teacher_ids are actually durable across
# a restart: the two hardcoded ones, plus anything that was just successfully
# read back from the Teachers sheet tab (proof it's really saved there). A
# teacher added via /api/admin-add-teacher only joins this set once its sheet
# write confirms success - if that write silently failed, the admin dashboard
# can flag it as "not saved" instead of the teacher just vanishing, unexplained,
# on the next redeploy (exactly what happened before this was added).
_persisted_teacher_ids = set(TEACHERS.keys())
# results_tab for Dan/Sara points at their existing, already-populated results
# worksheet tabs ("Ben"/"Sara") - a sheet-loaded override must never replace
# that with the lowercase teacher_id (load_extra_teachers' generic default),
# or their score history would silently look empty (wrong tab name). Every
# other field DOES take the sheet's value when an edit was saved for them.
_hardcoded_results_tabs = {tid: t["results_tab"] for tid, t in TEACHERS.items()}

def _apply_extra_teachers(extra):
    """Merges a freshly-loaded extra-teachers dict into the live TEACHERS/
    _teacher_state globals - the same logic that used to run exactly once at
    import time, now shared with the periodic refresh below so a transient
    Google API failure at boot isn't permanent for the rest of that worker's
    life (see load_extra_teachers' own retry-loop comment for the incident
    this fixes). A no-op (extra={}) safely changes nothing - it does NOT
    remove any teacher already in TEACHERS, so a transient failure on a
    LATER refresh can't un-load a teacher that loaded fine earlier."""
    if not extra:
        return
    TEACHERS.update(extra)
    for _tid, _tab in _hardcoded_results_tabs.items():
        if _tid in TEACHERS:
            TEACHERS[_tid]["results_tab"] = _tab
    _persisted_teacher_ids.update(extra.keys())
    for _tid, _t in extra.items():
        if _tid not in _teacher_state:
            _teacher_state[_tid] = {
                "threshold": _t["default_threshold"], "max_attempts": _t["default_max_attempts"],
                "exercise_name": _t.get("_saved_exercise_name") or "תרגול דמו",
                "csv_url": _t.get("_saved_csv_url") or "", "custom_exercises": [],
                "allowed_students": [], "restrict_to_list": False, "silence_timeout_ms": 1200,
            }

_apply_extra_teachers(load_extra_teachers())

_EXTRA_TEACHERS_REFRESH_SEC = 600
_extra_teachers_refresh_started = False
_extra_teachers_refresh_lock = threading.Lock()

def _start_extra_teachers_refresh_thread():
    """Defense in depth on top of load_extra_teachers' own retries: even if
    every retry at boot fails (Google having a genuinely bad few minutes),
    this recovers on its own within _EXTRA_TEACHERS_REFRESH_SEC instead of
    needing someone to notice and trigger a redeploy. Also picks up a
    teacher added/edited via /admin without waiting for the next restart."""
    global _extra_teachers_refresh_started
    with _extra_teachers_refresh_lock:
        if _extra_teachers_refresh_started:
            return
        _extra_teachers_refresh_started = True
        def _loop():
            while True:
                time.sleep(_EXTRA_TEACHERS_REFRESH_SEC)
                try:
                    _apply_extra_teachers(load_extra_teachers())
                except Exception as e:
                    print("EXTRA TEACHERS REFRESH LOOP ERROR", e)
        threading.Thread(target=_loop, name="extra-teachers-refresh", daemon=True).start()

_start_extra_teachers_refresh_thread()

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

def _result_row_to_values(header, row):
    """One result dict -> one flat list of cell values, in the exact column
    order of `header`. Shared by the single-row sync fallback and the
    batched flush writer so there's only one place that knows how a result
    dict maps onto sheet columns. Older sheets may still have a legacy
    lowercase header row (timestamp, teacher, student, ... - from before the
    20-column RESULT_HEADERS design) sitting to the left of the current
    headers, because ensure_results_header only ever APPENDS missing columns
    rather than replacing the row - so only write into columns whose header
    is a real, recognized RESULT_HEADERS label, leaving any legacy column
    blank rather than duplicating values into two side-by-side column sets.
    """
    out = []
    for h in header:
        if h in RESULT_HEADERS:
            key = RESULT_KEY_ALIASES.get(h, h)
            out.append(row.get(key, ""))
        else:
            out.append("")
    return out

def _open_results_worksheet(teacher_id):
    """Open (creating if needed) the results tab for one teacher, and make
    sure its header row is current. Returns (worksheet, header) or raises."""
    import gspread
    sheet_id = RESULTS_SHEET_IDS.get(teacher_id, RESULTS_SHEET_ID)
    sh = get_gspread_client().open_by_key(sheet_id)
    tab = TEACHERS[teacher_id]["results_tab"]
    try:
        ws = sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab, rows=1000, cols=24)
        ws.append_row(RESULT_HEADERS, value_input_option="USER_ENTERED")
    header = ensure_results_header(ws)
    return ws, header

def _write_result_sync(row):
    """Original single-row, fully synchronous path straight to Google
    Sheets - 3-5 sequential API calls, done inline. Kept as the fallback for
    when there's no database configured at all (nothing to queue into) and
    for the rare case the queue insert itself fails - so a result is never
    silently dropped just because Postgres had a bad moment. NOT used for
    the normal case anymore; see write_result()/_flush_pending_results()."""
    try:
        ws, header = _open_results_worksheet(row["teacher_id"])
        out = _result_row_to_values(header, row)
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

def _write_results_batch_to_sheet(teacher_id, rows):
    """Same anchored-to-column-A approach as _write_result_sync, but for
    MANY rows from ONE teacher in a single pass: one header check, one
    get_all_values() read to find the next free row, one ws.update() for the
    whole batch - instead of repeating that whole sequence per row. This is
    what actually cuts both the per-result latency AND the number of Google
    Sheets API calls under load (each finalized sentence used to cost its
    own 3-5 calls; now a whole batch of them shares one)."""
    if not rows:
        return True
    try:
        ws, header = _open_results_worksheet(teacher_id)
        matrix = [_result_row_to_values(header, row) for row in rows]
        next_row = len(ws.get_all_values()) + 1
        last_col = chr(ord("A") + len(header) - 1)
        ws.update(f"A{next_row}:{last_col}{next_row + len(matrix) - 1}", matrix, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print("WRITE RESULTS BATCH FAILED", teacher_id, len(rows), e)
        return False

def _enqueue_pending_result(row):
    """Durably queue one result row in Postgres for the background flush
    thread to pick up - an ordinary local INSERT, fast (single-digit
    milliseconds) compared to the 1-3+ seconds a synchronous Sheets write
    chain could take. Returns True only if the row is safely durable
    somewhere (in the queue) - callers should fall back to a direct
    synchronous write if this returns False, so a DB hiccup never means a
    lost result."""
    conn = _db_conn()
    if not conn:
        return False
    try:
        with conn, conn.cursor() as cur:
            cur.execute("INSERT INTO pending_results (row_data) VALUES (%s)", (json.dumps(row),))
        # Wake the flush loop immediately instead of making it wait for its
        # next timer tick - see _pending_results_event below for why the loop
        # no longer just polls on a short fixed timer.
        _pending_results_event.set()
        return True
    except Exception as e:
        print("ENQUEUE PENDING RESULT FAILED", e)
        return False
    finally:
        _db_release(conn)

def write_result(row):
    # In-memory copy stays exactly as before - it's what my-history/teacher
    # results reads use to show a result that hasn't reached the Sheet yet
    # (see read_results_sheet_rows' callers), and that gap is now the norm
    # for every write for a few seconds (queued, not yet flushed) rather
    # than only on an outright failure - so this still needs to be populated
    # unconditionally, immediately, regardless of how the write below goes.
    _pending_results.append(row)
    svc_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not svc_json:
        print("RESULT STORED IN MEMORY ONLY", row)
        return False
    if _enqueue_pending_result(row):
        return True
    # No DB configured, or the queue insert itself failed - fall back to the
    # old inline synchronous write rather than losing the result.
    return _write_result_sync(row)

# --- Background flush: drains pending_results into Google Sheets in batches,
# on a timer, off the request path entirely. Only one worker PROCESS actually
# does this at any moment (see the pg_advisory_lock below) even if several
# gunicorn workers are running, so multiple processes can never race writing
# the same rows twice. Deliberately uses its OWN one-off connection (not the
# shared pool) for the whole lock+read+write+unlock sequence: a Postgres
# advisory lock is tied to the specific database session that acquired it,
# and returning that connection to a shared pool mid-lock (for some other
# request to pick up) would be exactly the kind of subtle cross-purpose bug
# this design needs to avoid. Closing this connection when done - even on an
# unexpected error - also releases the lock automatically as a safety net,
# on top of the explicit unlock call.
_RESULTS_FLUSH_LOCK_KEY = 918273645
_RESULTS_FLUSH_BATCH_SIZE = 200
# Safety-net poll interval ONLY - not the normal trigger anymore. The loop
# below wakes immediately via _pending_results_event whenever a result is
# actually queued, so in normal operation this number barely matters. It used
# to be the sole trigger at 4 seconds, which meant this loop opened a fresh
# Postgres connection every 4 seconds forever, 24/7, whether or not there was
# anything to flush. On a serverless Postgres host (Neon) that auto-suspends
# its compute after a few minutes of no activity to save "compute time"
# quota, a connection every 4 seconds never let it suspend - so the free
# monthly compute-hour quota was being burned around the clock regardless of
# real student traffic, and ran out days into the month instead of lasting
# the whole month. Now this is just a fallback in case an event is ever
# missed (e.g. a row inserted by another process/restart) - it's set well
# above Neon's default ~5-minute auto-suspend window so idle periods (nights,
# weekends) actually let the DB go to sleep and stop counting against quota.
RESULTS_FLUSH_INTERVAL_SEC = float(os.getenv("RESULTS_FLUSH_INTERVAL_SEC", "900"))
_pending_results_event = threading.Event()

def _flush_pending_results():
    if not DATABASE_URL:
        return
    try:
        import psycopg
    except ImportError:
        return
    conn = None
    got_lock = False
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=5)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_RESULTS_FLUSH_LOCK_KEY,))
            got_lock = cur.fetchone()[0]
        if not got_lock:
            return
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, row_data FROM pending_results ORDER BY id LIMIT %s",
                (_RESULTS_FLUSH_BATCH_SIZE,),
            )
            batch = cur.fetchall()
        if not batch:
            return
        by_teacher = {}
        for pending_id, row_data in batch:
            by_teacher.setdefault(row_data.get("teacher_id"), []).append((pending_id, row_data))
        flushed_ids = []
        for tid, items in by_teacher.items():
            if tid not in TEACHERS:
                # Teacher no longer exists (deleted/renamed) - nothing sane to
                # write it into; drop rather than let it jam the queue forever.
                flushed_ids += [pid for pid, _ in items]
                continue
            ok = _write_results_batch_to_sheet(tid, [r for _, r in items])
            if ok:
                flushed_ids += [pid for pid, _ in items]
            # On failure, leave these rows in the queue untouched - the row
            # data itself lives safely in Postgres either way, so the next
            # tick just retries them along with whatever's arrived since.
        if flushed_ids:
            with conn, conn.cursor() as cur:
                cur.execute("DELETE FROM pending_results WHERE id = ANY(%s)", (flushed_ids,))
    except Exception as e:
        print("FLUSH PENDING RESULTS FAILED", e)
    finally:
        if conn is not None:
            if got_lock:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(%s)", (_RESULTS_FLUSH_LOCK_KEY,))
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass

_results_flush_thread_started = False
_results_flush_thread_lock = threading.Lock()

def _start_results_flush_thread():
    """Starts the periodic flush loop once per process. A no-op with no
    DATABASE_URL configured - in that case write_result() never queues
    anything in the first place (falls straight back to the old synchronous
    write), so there would be nothing for this thread to do anyway."""
    global _results_flush_thread_started
    if not DATABASE_URL:
        return
    with _results_flush_thread_lock:
        if _results_flush_thread_started:
            return
        _results_flush_thread_started = True
        def _loop():
            while True:
                # Blocks here doing NOTHING (no DB connection held) until
                # either _enqueue_pending_result() signals real work, or the
                # safety-net timeout elapses - see RESULTS_FLUSH_INTERVAL_SEC
                # above for why this replaced a dumb fixed-interval poll.
                _pending_results_event.wait(timeout=RESULTS_FLUSH_INTERVAL_SEC)
                _pending_results_event.clear()
                try:
                    _flush_pending_results()
                except Exception as e:
                    print("RESULTS FLUSH LOOP ERROR", e)
        threading.Thread(target=_loop, name="results-flush", daemon=True).start()

# Started once here, at import time - by the point this line runs, db_init()
# (called earlier in the module) has already created the pending_results
# table, and every function _start_results_flush_thread/the loop it starts
# depends on is already defined above.
_start_results_flush_thread()

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

def _lookup_student_level_row(tid, email):
    """Returns the saved CEFR level for this student, or None if no row
    exists yet - i.e. this student has never been placed on the level track
    before (brand new). Shared by get_student_level (which falls back to A1)
    and has_saved_student_level (which needs to tell "never placed" apart
    from "was placed at A1") so there's only one Sheets lookup to maintain.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    key = f"studentlevelrow:{tid}:{email}"
    if key in _cache and time.time() - _cache[key][0] < 300:
        return _cache[key][1]
    found = None
    try:
        ws = _student_levels_ws(tid)
        for row in ws.get_all_values()[1:]:
            if len(row) >= 2 and row[0].strip().lower() == email:
                candidate = row[1].strip().upper()
                if candidate in ALL_STUDENT_LEVELS:
                    found = candidate
                break
    except Exception as e:
        print("GET STUDENT LEVEL FAILED", e)
    _cache[key] = (time.time(), found)
    return found

def get_student_level(tid, email):
    """Look up a student's current CEFR level for this teacher. Defaults to
    the first level (A1) whenever there's no record yet, or on any error -
    a brand-new/never-seen student always starts at the beginning (in
    practice this only happens if the placement test - see
    has_saved_student_level/new_session - was skipped, e.g. no email)."""
    return _lookup_student_level_row(tid, email) or CEFR_LEVELS[0]

def has_saved_student_level(tid, email):
    """True once this student has an actual saved level - either from a
    completed placement test or from auto-advancement. False means this
    student has never touched the level track before and should take the
    short placement test first (see new_session) instead of always cold-
    starting at A1 and wasting time re-practicing levels they already know.
    """
    return _lookup_student_level_row(tid, email) is not None

def set_student_level(tid, email, level):
    """Persist a student's new CEFR level (e.g. after auto-advancement)."""
    email = (email or "").strip().lower()
    if not email or level not in ALL_STUDENT_LEVELS:
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
        _cache[f"studentlevelrow:{tid}:{email}"] = (time.time(), level)
        return True
    except Exception as e:
        print("SET STUDENT LEVEL FAILED", e)
        return False

def _truthy(v):
    """Normalize a value that may be a real Python bool (in-memory result,
    still in _pending_results) or a Sheets cell string ("TRUE"/"FALSE"/"True"
    - gspread round-trips booleans as text) into an actual bool."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes")

def get_weak_topics(tid, email, min_attempts=2):
    """Return this student's grammar topics, weakest-first, based on their
    past pass rate per topic in the built-in leveled curriculum (CSV-imported
    teacher exercises aren't tagged with a topic, so they're simply not part
    of this signal). Topics with fewer than min_attempts recorded attempts
    are left out entirely - not enough signal yet to call a topic "weak"
    rather than just "not practiced much". "vocab" (single-word flashcards,
    A0 only) is excluded too since near-100% pass rates there aren't a
    meaningful difficulty signal the way a grammar pattern is.

    Reads the durable results Google Sheet (same source teacher/my-history
    views use), not the in-memory-only _pending_results, so this reflects a
    student's REAL history even across server restarts - merge_with_pending
    then layers in anything written moments ago that hasn't flushed to the
    sheet yet. That sheet read is relatively slow, so results are cached for
    5 minutes per student (same TTL used elsewhere for Sheets-backed lookups
    like _lookup_student_level_row).

    Returns [] on any error, when there's no email, or when there's simply
    not enough data yet - the caller (load_level_track_sentences) treats []
    as "no adjustment, use the curriculum's normal built-in order," so this
    function failing closed never breaks a session.
    """
    email = (email or "").strip().lower()
    if not email:
        return []
    key = f"weaktopics:{tid}:{email}"
    if key in _cache and time.time() - _cache[key][0] < 300:
        return _cache[key][1]
    weak = []
    try:
        sheet_rows, _ = read_results_sheet_rows(tid)
        rows = merge_with_pending(sheet_rows, tid, email)
        stats = {}
        for r in rows:
            if (r.get("student_email") or "").strip().lower() != email:
                continue
            if r.get("phase") != "practice" or _truthy(r.get("skipped")):
                continue
            # Prefer the topic carried directly on the row (only present for
            # rows still in memory / the Postgres JSONB queue - see the
            # comment on finalize_sentence's row dict); once a row has been
            # flushed to the Sheet that key is silently dropped (not a real
            # column), so fall back to deriving it from the sentence text.
            topic = r.get("topic") or SENTENCE_TOPIC_LOOKUP.get(r.get("sentence") or "")
            if not topic or topic in ("vocab", "general"):
                continue
            attempts, passes = stats.get(topic, (0, 0))
            stats[topic] = (attempts + 1, passes + (1 if _truthy(r.get("passed")) else 0))
        scored = [(topic, passes / attempts) for topic, (attempts, passes) in stats.items() if attempts >= min_attempts]
        scored.sort(key=lambda pair: pair[1])
        weak = [topic for topic, _ in scored]
    except Exception as e:
        print("GET WEAK TOPICS FAILED", tid, email, e)
        weak = []
    _cache[key] = (time.time(), weak)
    return weak

def load_level_track_sentences(tid, email):
    """Return (sentences, level) for the default built-in curriculum, based
    on the student's current saved CEFR level for this teacher. Within that
    level, sentences are reordered (not filtered - every sentence in the
    level still appears exactly once, same as before) so that grammar topics
    this student has struggled with historically (see get_weak_topics) come
    up EARLIER in the sweep than topics they've already shown they're solid
    on. A student with no history yet, or too little history to call
    anything "weak", gets the curriculum's original built-in order,
    unchanged - this only ever re-prioritizes, it never invents or drops
    content.
    """
    level = get_student_level(tid, email)
    sentences = LEVEL_SENTENCES.get(level, LEVEL_SENTENCES[CEFR_LEVELS[0]])[:]
    weak_topics = get_weak_topics(tid, email)
    if weak_topics:
        weak_rank = {topic: i for i, topic in enumerate(weak_topics)}
        # Stable sort: sentences whose topic isn't in weak_rank at all (never
        # attempted, or already solid) keep their original relative order and
        # all sort after every weak topic; ties within the same weak topic
        # also keep their original relative order.
        sentences.sort(key=lambda sent: weak_rank.get(sent.get("topic", ""), len(weak_rank)))
    return sentences, level

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

def advance_within_accuracy_stage(s):
    """A sentence has just been read correctly (station 1, or after Bloom
    mastery reps) during the ACCURACY sweep. Per the didactic restructure,
    this no longer continues straight into that same sentence's cloze check -
    cloze for every sentence is deferred into its own dedicated sweep only
    after ALL sentences have been read correctly once each (see
    advance_stage_if_swept). So: cache what would previously have gone
    straight into the result row (mastery reps/score/attempts so far), then
    simply move on to the next sentence within the accuracy sweep. The
    result row itself is written later by finalize_sentence(), once this
    sentence's cloze step (or lack of one) is resolved - still exactly one
    row per sentence overall, just written later.
    """
    s.setdefault("accuracy_data", {})[s["current"]] = {
        "attempts": max(1, s.get("sentence_attempts", 0)),
        "mastery_target": s.get("mastery_target", 0),
        "mastery_score": s.get("mastery_score", 0),
    }
    s["current"] += 1
    s["failed_attempts"] = 0
    s["sentence_attempts"] = 0
    s["stage2_attempts"] = 0
    s["bonus_attempts"] = 0
    s["mastery_target"] = 0
    s["mastery_consecutive"] = 0
    s["mastery_score"] = 0

def finalize_sentence(s, correct, spoken, score, passed=True, skipped=False, metrics=None):
    """Writes the ONE result row for a sentence's whole journey (accuracy
    sweep + cloze sweep, or a skip out of either) and advances the active
    sweep to the next index. If this sentence already passed through the
    accuracy sweep earlier (the normal case - see advance_within_accuracy_stage
    above), its cached mastery reps/score/attempts are folded in here so the
    row looks exactly like the old single-pass version even though the two
    sweeps can now happen far apart in time. If there's no cached data (the
    sentence was skipped/capped straight out of the accuracy sweep and never
    reached cloze at all), the live session counters are used instead -
    identical to the original pre-restructure behavior for that case.
    """
    fluency = fluency_from_metrics(spoken, score, metrics)
    ad = s.setdefault("accuracy_data", {}).pop(s["current"], None) or {}
    # Carry the sentence's teacher-authored fill-in-the-blank variant (if any)
    # through into the stored result row - this is how it survives into the
    # exam sentence pool later (built from s["results"], not re-fetched from
    # the CSV), so the final exam can show the same blanked prompt too.
    current_obj = s["sentences"][s["current"]] if s["current"] < len(s["sentences"]) else {}
    best_score = max(ad.get("mastery_score", 0), s.get("mastery_score", 0), score or 0)
    row = {
        "timestamp": now_str(),
        "teacher_id": s["teacher_id"], "student_name": s["student_name"],
        "student_email": s.get("student_email", ""), "exercise": s["exercise_name"],
        "phase": "practice", "sentence": correct, "spoken": spoken, "score": score,
        "passed": bool(passed), "skipped": bool(skipped),
        "attempts": ad.get("attempts", max(1, s.get("sentence_attempts", 0))),
        "max_attempts": s.get("max_attempts", ""),
        "mastery_reps": ad.get("mastery_target", s.get("mastery_target", 0)),
        "mastery_status": "mastered" if passed and not skipped else "not_mastered",
        "mastery_score": best_score,
        "cloze_passed": bool(s.get("cloze_passed", False)),
        "completion": current_obj.get("completion", ""),
        # Not a results-sheet column (RESULT_KEY_ALIASES/_result_row_to_values
        # silently drop any key that isn't a known header) - this only ever
        # lives in the JSONB pending_results queue / in-memory _pending_results,
        # where get_weak_topics() reads it back directly instead of having to
        # re-derive it from the sentence text via SENTENCE_TOPIC_LOOKUP.
        "topic": current_obj.get("topic", ""),
        **fluency,
    }
    s["results"].append(row)
    write_result(row)
    s.setdefault("finalized_indices", set()).add(s["current"])
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

def cloze_fields_for(sentence_obj):
    cw = detect_cloze_word(sentence_obj.get("en", ""))
    completion = sentence_obj.get("completion", "")
    return cw, completion

def advance_stage_if_swept(s):
    """Whenever the active sweep (accuracy or cloze) has visited every
    sentence, hand off to the next global stage - called at the top of both
    /api/question and /api/answer so the two can never disagree about which
    stage/sentence is currently active.
    """
    if s["stage"] == "accuracy" and s["current"] >= len(s["sentences"]):
        s["stage"] = "cloze"
        s["current"] = 0
    if s["stage"] == "cloze":
        # Skip straight over any sentence already fully finalized during the
        # accuracy sweep (it was skipped/capped there and already has its one
        # result row - it never gets a second look in cloze).
        finalized = s.setdefault("finalized_indices", set())
        while s["current"] < len(s["sentences"]) and s["current"] in finalized:
            s["current"] += 1
        if s["current"] < len(s["sentences"]) and not s.get("cloze_active"):
            sentence_obj = s["sentences"][s["current"]]
            cw, completion = cloze_fields_for(sentence_obj)
            if not cw and not completion:
                # Nothing to cloze-test in this sentence - it was already
                # fully mastered in the accuracy sweep, so finalize it
                # straight from that cached data and keep unwinding until we
                # land on a sentence that actually needs a cloze attempt (or
                # the sweep ends).
                ad = s.get("accuracy_data", {}).get(s["current"], {})
                # ad is only populated by a genuine accuracy-stage pass
                # (advance_within_accuracy_stage). If it's empty, this index
                # was fast-forwarded past accuracy without ever being
                # attempted (student jumped straight to a later station via
                # the step bar) - record it as skipped, not as a silent
                # fabricated "pass", so it's correctly excluded from the exam
                # pool instead of inflating the score with a 0% "pass".
                attempted = bool(ad)
                finalize_sentence(s, sentence_obj.get("en", ""), "", ad.get("mastery_score", 0), passed=attempted, skipped=not attempted, metrics=None)
                advance_stage_if_swept(s)
                return
            s["cloze_active"] = True
            s["cloze_word"] = cw
            s["cloze_display"] = completion
            s["cloze_attempts"] = 0

@app.route("/")
def home():
    return Response(open(os.path.join(BASE_DIR, "index.html"), "rb").read(), content_type="text/html; charset=utf-8")

@app.route("/teacher")
def teacher():
    return Response(open(os.path.join(BASE_DIR, "teacher.html"), "rb").read(), content_type="text/html; charset=utf-8")

@app.route("/admin")
def admin_page():
    return Response(open(os.path.join(BASE_DIR, "admin.html"), "rb").read(), content_type="text/html; charset=utf-8")

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

@app.route("/favicon-32.png")
def favicon_32():
    return Response(open(os.path.join(BASE_DIR, "favicon-32.png"), "rb").read(), content_type="image/png")

@app.route("/favicon-16.png")
def favicon_16():
    return Response(open(os.path.join(BASE_DIR, "favicon-16.png"), "rb").read(), content_type="image/png")

@app.route("/favicon.ico")
def favicon_ico():
    # Some browsers request /favicon.ico regardless of the <link> tags above -
    # serve the 32px PNG for that path too rather than let it 404.
    return Response(open(os.path.join(BASE_DIR, "favicon-32.png"), "rb").read(), content_type="image/png")

@app.route("/intro.mp4")
def intro_video():
    # Short branded splash clip played once on app load (see index.html) -
    # served with Accept-Ranges so mobile Safari (which requests video with
    # a Range header even for short clips) doesn't choke on it.
    path = os.path.join(BASE_DIR, "intro.mp4")
    data = open(path, "rb").read()
    resp = Response(data, content_type="video/mp4")
    resp.headers["Accept-Ranges"] = "bytes"
    # Short cache lifetime, not the week-long one this used to have: this file
    # gets swapped during active branding iteration, and a long max-age means
    # every browser that already loaded the app keeps serving its OLD cached
    # copy of intro.mp4 for days after a new one is deployed - exactly what
    # happened here (server had the new video; browsers kept playing the old
    # one from cache). 5 minutes is enough to avoid re-downloading it on every
    # single page reload without risking a stale video for very long.
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

@app.get("/api/teachers")
def api_teachers():
    return jsonify({tid: teacher_public(tid) for tid in TEACHERS})

@app.get("/api/config")
def api_config():
    # Public, non-secret runtime config the frontend needs before login -
    # currently just whether Google Sign-In is set up (GOOGLE_CLIENT_ID is
    # itself not a secret, so it's fine to hand back directly). Frontends use
    # this to decide whether to render the "Sign in with Google" button at
    # all, instead of showing a button that would just fail every time on a
    # deployment where nobody has created a Google OAuth Client ID yet.
    return jsonify(google_client_id=GOOGLE_CLIENT_ID)

# --- Payment (Grow) scaffold -------------------------------------------
# Grow (grow.business) is the recommended Israeli payment/clearing provider
# for a future paid tier - it has a documented developer API + webhook
# support suited to gating a custom app (not just a pre-built course page).
# This endpoint + the subscriptions table/helpers above are the receiving
# end of that integration, built now so the shape exists in code - but as
# of this being written there is NO real Grow merchant/developer account
# yet, so three things are explicitly NOT done here and need to happen
# before this goes live:
#   1. Create a Grow account, get real API/webhook credentials, and set
#      GROW_WEBHOOK_SECRET (env var) to the real signing secret Grow issues.
#   2. Replace the signature check below with Grow's actual documented
#      scheme (header name + algorithm) - what's here is a reasonable
#      generic HMAC placeholder, not verified against real Grow docs, since
#      there's no account yet to check against.
#   3. Decide the product shape (per-student subscription vs per-teacher/
#      school license) and only then add an actual access check somewhere
#      real (e.g. in verify_student/_complete_student_login) - right now
#      NOTHING in the app reads this table to block anything, so turning
#      this endpoint on cannot break the current free pilot.
GROW_WEBHOOK_SECRET = os.getenv("GROW_WEBHOOK_SECRET", "")

@app.post("/api/grow-webhook")
def grow_webhook():
    raw = request.get_data()
    data = request.get_json(silent=True) or {}
    if GROW_WEBHOOK_SECRET:
        provided_sig = request.headers.get("X-Grow-Signature", "")
        expected_sig = hmac.new(GROW_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not provided_sig or not hmac.compare_digest(provided_sig, expected_sig):
            return jsonify(ok=False, error="invalid signature"), 401
    # Field names below are best-effort guesses (email/status under a few
    # common aliases) pending real Grow webhook payload docs/examples once
    # an account exists - update these once you have a real test payload.
    email = (data.get("email") or data.get("customer_email") or data.get("payer_email") or "").strip().lower()
    raw_status = (data.get("status") or data.get("event") or "").strip().lower()
    if not email:
        print("GROW WEBHOOK: no email in payload", data)
        return jsonify(ok=False, error="no email in payload"), 400
    active_statuses = {"paid", "active", "success", "completed", "subscription_created", "subscription_renewed"}
    inactive_statuses = {"canceled", "cancelled", "failed", "refunded", "subscription_canceled", "expired"}
    if raw_status in active_statuses:
        status = "active"
    elif raw_status in inactive_statuses:
        status = "inactive"
    else:
        status = raw_status or "unknown"
    saved = _upsert_subscription(
        email, status,
        plan=data.get("plan") or data.get("product"),
        grow_transaction_id=data.get("transaction_id") or data.get("id"),
    )
    print("GROW WEBHOOK", email, status, "saved" if saved else "DB SAVE FAILED (no DB configured?)")
    return jsonify(ok=True)

# Verifies a Google Identity Services ID token entirely server-side against
# Google's public signing keys - no client secret involved, just the OAuth
# Client ID above. Returns the verified claims dict (email, email_verified,
# name, ...) on success, or None if the token is invalid/expired/forged, or
# if Google Sign-In isn't configured at all (GOOGLE_CLIENT_ID unset) - in
# that last case callers should not treat this as "wrong credentials", the
# feature is just not turned on yet for this deployment.
_google_auth_request = None
def verify_google_id_token(token):
    if not GOOGLE_CLIENT_ID or not token:
        return None
    global _google_auth_request
    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_auth_requests
        if _google_auth_request is None:
            _google_auth_request = google_auth_requests.Request()
        return google_id_token.verify_oauth2_token(token, _google_auth_request, GOOGLE_CLIENT_ID)
    except Exception as e:
        print("GOOGLE ID TOKEN VERIFY FAILED", e)
        return None

# Stateless signed session token, issued once at successful teacher login
# (password OR Google) and resent by the client on every later teacher-only
# request instead of the real password. "Stateless" matters here: this app
# has no server-side session store and no sticky-session guarantee across
# gunicorn worker processes, so a token that only lived in one worker's
# memory would randomly 401 depending which worker handled the next
# request. Signing tid+expiry with HMAC instead means ANY worker can verify
# ANY token on its own, with no shared state needed - same trick as a JWT,
# just hand-rolled to avoid a new dependency for one field.
def make_teacher_token(tid, ttl_seconds=60 * 60 * 24 * 30):
    exp = int(time.time()) + ttl_seconds
    payload = f"{tid}:{exp}"
    sig = hmac.new(APP_SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode()

def verify_teacher_token(token):
    try:
        tid, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(":", 2)
        expected_sig = hmac.new(APP_SECRET_KEY.encode(), f"{tid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected_sig) or int(exp) < int(time.time()) or tid not in TEACHERS:
            return None
        return tid
    except Exception:
        return None

def _teacher_auth_ok(tid, data):
    """Shared auth check for every teacher-only endpoint EXCEPT the login
    endpoints themselves: accepts either the real teacher_password (the
    original/always-available path) OR a valid session token from a prior
    login (issued by _complete_teacher_login - see make_teacher_token
    above), which is the only credential a Google-authenticated teacher
    ever has. Replaces the old inline
    `tid not in TEACHERS or password != TEACHERS[tid]["teacher_password"]`
    check that every one of these endpoints used to duplicate."""
    if tid not in TEACHERS:
        return False
    password = data.get("password", "")
    if password and password == TEACHERS[tid]["teacher_password"]:
        return True
    token = data.get("token", "")
    if token and verify_teacher_token(token) == tid:
        return True
    return False

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
    return _complete_student_login(tid, name, email)

@app.post("/api/verify-student-google")
def verify_student_google():
    # Google Sign-In path for students: instead of typing name+email+the
    # shared class password, the student picks their Google account and we
    # trust Google's own verification of who they are - the email comes
    # straight from the verified token, never from a client-supplied field,
    # so it can't be spoofed the way a typed email could be. Everything
    # after identity is established (roster restriction, session
    # resume/reset, response shape) is identical to the password path -
    # see _complete_student_login(), shared by both.
    data = request.get_json(force=True)
    tid = data.get("teacher_id")
    credential = data.get("credential", "")
    if tid not in TEACHERS:
        return jsonify(ok=False, error="bad request"), 400
    ginfo = verify_google_id_token(credential)
    if not ginfo:
        return jsonify(ok=False, error="ההתחברות עם Google לא הוגדרה או שהאימות נכשל. נסה שוב או התחבר עם סיסמה."), 401
    if not ginfo.get("email_verified"):
        return jsonify(ok=False, error="חשבון ה-Google הזה אינו מאומת."), 401
    email = (ginfo.get("email") or "").strip().lower()
    name = (data.get("name") or ginfo.get("name") or email.split("@")[0]).strip()
    if not email or not name:
        return jsonify(ok=False, error="bad request"), 400
    return _complete_student_login(tid, name, email)

def _complete_student_login(tid, name, email):
    # Everything after a student's identity is established (by password OR
    # by verified Google token) - roster gate, resume-vs-restart detection,
    # response shape - is identical, so both /api/verify-student and
    # /api/verify-student-google funnel into this single implementation
    # rather than duplicating it.
    ts = _teacher_state[tid]
    if ts.get("restrict_to_list"):
        allowed = {n.casefold() for n in ts.get("allowed_students", [])}
        if name.casefold() not in allowed:
            return jsonify(ok=False, error="השם שלך אינו ברשימת התלמידים המורשים. פנה למורה שלך."), 403
    # Stable, deterministic session id (teacher + email) instead of a fresh
    # timestamped id on every login. This is what actually lets a student
    # close the tab/app and come back later to find themselves exactly where
    # they left off, instead of restarting the whole exercise from sentence 1
    # every time - previously EVERY login minted a brand-new id, so there was
    # never anything to resume. (Surviving an actual SERVER RESTART/redeploy
    # is a separate, bigger limitation - session state still lives only in
    # memory, not in a database - see the /api/answer 404 handling comment.)
    safe_email = re.sub(r"[^a-z0-9]", "_", email)
    sid = f"{tid}_{safe_email}"
    existing = get_session(sid)
    # Gate resuming on exam_completed, NOT completed - "completed" is set the
    # moment the student merely REACHES station 4 (practice/cloze sweep
    # done), well before they've actually taken the exam. Gating on it here
    # meant a student who skipped a few sentences in practice and hadn't
    # even started/finished the exam yet would, on their very next login, be
    # silently reset into a brand-new attempt - reported bug: "I didn't
    # really finish, but I got told I did and landed back on the same
    # level". exam_completed is only set once the client explicitly reports
    # the exam summary screen was shown (see /api/exam-complete) - until
    # then this is still the same in-progress attempt.
    same_unfinished_exercise = bool(
        existing and existing.get("csv_url", "") == ts.get("csv_url", "") and not existing.get("exam_completed")
    )
    # Only worth asking "continue or restart?" if they actually got past the
    # very start - never got past the first ungraded preview sentence means
    # there's nothing meaningful to resume, so just carry on silently.
    resumable = same_unfinished_exercise and not (existing.get("stage") == "preview" and existing.get("current", 0) == 0)
    # A session marked exam_completed silently gets replaced by a brand-new
    # new_session() below. If that happens right after the student actually
    # finished, they land back on station 1 of a fresh attempt with zero
    # explanation, which reads as "why did it just go back to the same
    # exercise". Capture what they just finished here so the client can show
    # a one-time, honest explanation instead of looking like silent data
    # loss. Prefer the actual exam results for the average shown - not the
    # pre-exam practice-phase results - since that's what the student
    # thinks of as "what I just did".
    just_completed = None
    if existing and existing.get("exam_completed"):
        prev_results = existing.get("exam_results") or existing.get("results", [])
        prev_avg = int(sum(r.get("score", 0) for r in prev_results) / len(prev_results)) if prev_results else None
        just_completed = {"exercise": existing.get("exercise_name", ""), "avg_score": prev_avg}
    if same_unfinished_exercise:
        # Same student, same teacher, same exercise, not finished yet -
        # resume in place by default rather than wiping their progress (the
        # frontend still asks the student to confirm before actually
        # continuing into it - see "resumed"/"resume_progress" below - but
        # the session itself is preserved either way until/unless they
        # explicitly choose "start over" via /api/restart-exercise).
        # Just refresh the display name (in case spelling/casing changed)
        # and the timestamp used for the teacher's live-dashboard sort order.
        existing["student_name"] = name
        existing["updated_at"] = int(time.time())
    else:
        # No session yet, the exercise changed under them, or they already
        # finished this one before - start fresh, same as always.
        new_session(sid, tid, name, student_email=email)
    resp = {
        "ok": True, "student_id": sid, "teacher": teacher_public(tid), "exercise": _sessions[sid]["exercise_name"],
        # Echoed back explicitly (not just implied by what the client sent)
        # so the Google Sign-In path - where the client may not have had a
        # typed name/email to begin with - can pick up exactly what was
        # actually used to log in, straight from the one place that
        # resolved it (the verified token, for name/email; see
        # /api/verify-student-google).
        "student_name": name, "student_email": email,
    }
    if just_completed:
        resp["just_completed"] = just_completed
    if resumable:
        sess = _sessions[sid]
        resp["resumed"] = True
        if sess.get("placement_active"):
            resp["resume_progress"] = {
                "index": sess.get("placement_step", 0), "total": PLACEMENT_MAX_STEPS, "stage": "placement",
            }
        else:
            resp["resume_progress"] = {
                "index": sess.get("current", 0), "total": len(sess.get("sentences", [])),
                "stage": sess.get("stage", "accuracy"),
            }
    return jsonify(resp)

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

def delete_student_rows_from_sheet(tid, email):
    """Best-effort erasure of a student's historical result rows from their
    teacher's results sheet, for the "delete student" admin/teacher action
    (GDPR-style request). Scans the 'Student Email' column and removes every
    matching row. This is deliberately separate from - and can fail
    independently of - deleting the student's live session from Postgres:
    the caller must report both outcomes rather than assuming one implies
    the other, since a sheet permission issue must never look like a
    successful full erasure when it wasn't."""
    email = (email or "").strip().lower()
    if not email:
        return False, "no email given"
    svc_json = os.getenv("GOOGLE_CREDENTIALS_JSON") or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not svc_json:
        return False, "Google Sheets not configured"
    if tid not in TEACHERS:
        return False, "unknown teacher"
    try:
        import gspread
        sheet_id = RESULTS_SHEET_IDS.get(tid, RESULTS_SHEET_ID)
        sh = get_gspread_client().open_by_key(sheet_id)
        tab = TEACHERS[tid]["results_tab"]
        ws = sh.worksheet(tab)
        values = ws.get_all_values()
        if not values:
            return True, None
        header = values[0]
        try:
            email_col = header.index("Student Email")
        except ValueError:
            return False, "'Student Email' column not found in sheet"
        # 1-indexed sheet row numbers, +1 to skip the header row. Delete from
        # the bottom up so earlier deletions never shift the row numbers of
        # matches still queued to be removed.
        match_rows = [
            i + 2 for i, r in enumerate(values[1:])
            if email_col < len(r) and r[email_col].strip().lower() == email
        ]
        for row_num in reversed(match_rows):
            ws.delete_rows(row_num)
        return True, None
    except Exception as e:
        print("SHEET SCRUB FAILED", tid, email, e)
        return False, str(e)

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

@app.post("/api/restart-exercise")
def restart_exercise():
    """The student was asked "continue where you left off, or start over?"
    (see /api/verify-student's "resumed" flag) and chose to start over -
    wipe the resumed session's progress and rebuild it fresh, same as a
    brand-new login would, but without needing a new session id."""
    data = request.get_json(force=True)
    s = get_session(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    new_session(s["student_id"], s["teacher_id"], s["student_name"], student_email=s.get("student_email", ""))
    return jsonify(ok=True)

@app.get("/api/question")
def question():
    s = get_session(request.args.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    if s.get("placement_active"):
        # Short adaptive placement test, entirely separate from the normal
        # preview/accuracy/cloze stage machine below - see new_session() and
        # placement_answer(). The SAME sentence is returned on repeated polls
        # until it's actually answered (placement_current only changes inside
        # placement_answer), so a page refresh mid-test never loses the
        # question the student is looking at.
        q = s["placement_current"] or {"he": "", "en": ""}
        return jsonify({
            "done": False, "stage": "placement", "he": q.get("he", ""), "en": q.get("en", ""),
            "index": s.get("placement_step", 0), "total": PLACEMENT_MAX_STEPS,
            "exercise": s["exercise_name"], "voice_gender": s["voice_gender"],
        })
    if s["stage"] == "preview":
        # Ungraded exposure sweep - no mic, no score, just the sentence text
        # and audio, with free back/forward navigation (see /api/preview-nav).
        # Lets the student's ear/eye settle on the whole set before the first
        # graded attempt, instead of cold-opening straight into a recording.
        q = s["sentences"][s["current"]]
        return jsonify({
            "done": False, "stage": "preview", "he": q["he"], "en": q["en"], "emoji": q.get("emoji", ""),
            "index": s["current"], "total": len(s["sentences"]),
            "exercise": s["exercise_name"], "voice_gender": s["voice_gender"],
            "can_go_back": s["current"] > 0,
            "content_mismatch": s.get("content_mismatch", False),
        })
    advance_stage_if_swept(s)
    if s["current"] >= len(s["sentences"]):
        # Main pass is done. Before the final exam, give one extra single-attempt
        # round for any sentence that had to be skipped after 5 failed tries.
        if not s.get("in_review") and s.get("review_queue"):
            s["in_review"] = True
            s["review_index"] = 0
        if s.get("in_review") and s["review_index"] < len(s["review_queue"]):
            q = s["review_queue"][s["review_index"]]
            return jsonify({
                "done": False, "he": q["he"], "en": q["en"], "emoji": q.get("emoji", ""),
                "index": s["review_index"], "total": len(s["review_queue"]),
                "threshold": s["threshold"], "max_attempts": 1, "voice_gender": s["voice_gender"],
                "exercise": s["exercise_name"], "review_round": True, **session_payload(s)
            })
        s["completed"] = True
        s["stage"] = "done"
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
        "done": False, "stage": s["stage"], "he": q["he"], "en": q["en"], "emoji": q.get("emoji", ""), "index": s["current"], "total": len(s["sentences"]),
        "threshold": s["threshold"], "max_attempts": s["max_attempts"], "voice_gender": s["voice_gender"],
        "exercise": s["exercise_name"], "review_round": False,
        "content_mismatch": s.get("content_mismatch", False), **session_payload(s)
    })

@app.post("/api/preview-nav")
def preview_nav():
    """Navigation for the ungraded preview sweep only - forward or back a
    sentence, no scoring involved. Once "next" is pressed past the last
    sentence, the session moves on into the graded accuracy stage. Silently
    a no-op if the student has already left the preview stage (e.g. a stale
    button press after already moving on)."""
    data = request.get_json(force=True)
    s = get_session(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    if s["stage"] != "preview":
        return jsonify(ok=True)
    if data.get("direction") == "back":
        s["current"] = max(0, s["current"] - 1)
    else:
        s["current"] += 1
        if s["current"] >= len(s["sentences"]):
            s["stage"] = "accuracy"
            s["current"] = 0
    return jsonify(ok=True)

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
    s = get_session(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    pc = s.get("cap_pending")
    if not pc:
        return jsonify(error="no pending decision"), 400
    s["cap_pending"] = None
    s["bonus_attempts"] = 0
    finalize_sentence(s, pc["correct"], pc["spoken"], pc["score"], False, skipped=True, metrics=pc.get("metrics"))
    if pc.get("sentence_obj") and not s.get("in_review"):
        s["review_queue"].append(pc["sentence_obj"])
    return jsonify(ok=True)

@app.post("/api/cap-retry")
def cap_retry():
    """Student chose "another round" instead of moving on - grant a small
    batch of bonus attempts on the SAME sentence/station instead of forcing an
    advance, for cases where they feel close to a correct answer."""
    data = request.get_json(force=True)
    s = get_session(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    if not s.get("cap_pending"):
        return jsonify(error="no pending decision"), 400
    s["cap_pending"] = None
    s["bonus_attempts"] = int(s.get("bonus_attempts", 0)) + 3
    return jsonify(ok=True, **session_payload(s))

def placement_answer(s, spoken, metrics):
    """Score one placement-test attempt and either move to the next level to
    test, or - once the result flips (bracketing the student's real level)
    or PLACEMENT_MAX_STEPS is reached - finish the test, save the level, and
    hand off straight into a normal session at that level. Uses the exact
    same recording/scoring pipeline as regular practice (best_score_for_spoken,
    word_level, 100%-match pass/fail) - only the level-selection logic around
    it is new."""
    q = s.get("placement_current") or {"he": "", "en": ""}
    _, correct, score = best_score_for_spoken(spoken, q)
    passed = score >= 100
    words = word_level(spoken, correct)
    level_tested = CEFR_LEVELS[s["placement_idx"]]
    s["placement_history"].append({"level": level_tested, "passed": passed})
    s["placement_step"] = int(s.get("placement_step", 0)) + 1
    base = {
        "correct": correct, "spoken": spoken, "score": score, "passed": passed,
        "words": words, "advance": False, "station": "placement",
        "index": s["placement_step"], "total": PLACEMENT_MAX_STEPS,
    }
    history = s["placement_history"]
    # The first time a pass and a fail have BOTH been seen, the boundary is
    # bracketed - that's enough to place the student without grinding
    # through every level. Otherwise keep going up to the step cap.
    converged = len(history) >= 2 and history[-1]["passed"] != history[-2]["passed"]
    finished = converged or s["placement_step"] >= PLACEMENT_MAX_STEPS

    if not finished:
        if passed:
            s["placement_idx"] = min(s["placement_idx"] + 1, len(CEFR_LEVELS) - 1)
        else:
            s["placement_idx"] = max(s["placement_idx"] - 1, 0)
        next_level = CEFR_LEVELS[s["placement_idx"]]
        s["placement_current"] = random.choice(LEVEL_SENTENCES[next_level])
        return jsonify({**base, "placement_done": False})

    # Final level = the highest level the student actually passed. A student
    # who never passed even a single sentence tested - including the easiest
    # one, A1 - almost certainly isn't ready for the CEFR curriculum at all,
    # so they're placed at A0 (short vocabulary/phrases with emoji support)
    # instead of being floored at A1 and immediately struggling.
    passed_indices = [CEFR_LEVELS.index(h["level"]) for h in history if h["passed"]]
    final_level = CEFR_LEVELS[max(passed_indices)] if passed_indices else "A0"
    email = s.get("student_email", "")
    set_student_level(s["teacher_id"], email, final_level)
    real_sentences, _ = load_level_track_sentences(s["teacher_id"], email)
    s["placement_active"] = False
    s["placement_current"] = None
    s["level_track"] = final_level
    s["exercise_name"] = LEVEL_NAMES_HE.get(final_level, "תרגול דמו")
    s["sentences"] = real_sentences
    s["stage"] = "preview" if real_sentences else "accuracy"
    s["current"] = 0
    return jsonify({
        **base, "placement_done": True, "placed_level": final_level,
        "placed_level_name": LEVEL_NAMES_HE.get(final_level, final_level),
        # Full per-level pass/fail breakdown so the frontend can show a
        # results summary (not just the final number) before the student
        # confirms they're ready to start practicing at that level.
        "history": [{"level": h["level"], "level_name": LEVEL_NAMES_HE.get(h["level"], h["level"]),
                     "passed": h["passed"]} for h in history],
    })

@app.post("/api/pronunciation-assess")
def pronunciation_assess():
    """SHADOW MODE (see the comment on call_azure_pronunciation above): sends
    a real recording to Azure's Pronunciation Assessment API and returns real
    per-word accuracy/fluency scores. Called by the front-end alongside the
    normal /api/answer flow, purely for evaluation - the result here is NOT
    stored, NOT written to the results sheet, and has NO effect on a
    student's score, mastery progress, or pass/fail. Requires a valid,
    already-started student session (same as /api/answer) so this can't be
    hit by an anonymous caller to run up the Azure bill."""
    data = request.get_json(force=True)
    sid = data.get("student", "")
    s = get_session(sid)
    if not s:
        return jsonify(ok=False, error="session not found"), 404
    if not AZURE_SPEECH_KEY:
        return jsonify(ok=False, error="Azure Speech not configured"), 501
    reference_text = (data.get("reference_text") or "").strip()
    if not reference_text:
        return jsonify(ok=False, error="missing reference_text"), 400
    try:
        azure_content_type, raw_audio = _parse_audio_data_url(data.get("audio_data_url", ""))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    result, err = call_azure_pronunciation(raw_audio, azure_content_type, reference_text)
    if err:
        return jsonify(ok=False, error=err), 502
    return jsonify(ok=True, **extract_azure_pronunciation_summary(result))

@app.post("/api/answer")
def answer():
    data = request.get_json(force=True)
    sid, spoken = data.get("student", ""), data.get("answer", "")
    metrics = data.get("metrics") or {}
    s = get_session(sid)
    if not s:
        # score=None (not 0) is deliberate: the front-end treats a missing/non-numeric
        # score as "couldn't check the answer" rather than a real failed attempt.
        # This happens when the server restarted (redeploy or free-tier spin-down)
        # and lost this student's in-memory session - it is NOT a real 0%.
        return jsonify(error="session not found", score=None, passed=False, words=[], advance=False), 404
    if s.get("placement_active"):
        # Bypasses the normal accuracy/mastery/cloze machinery entirely - see
        # new_session()/placement_answer(). s["sentences"] is empty during
        # placement, so this MUST be handled before the "current >= len
        # (sentences)" early-return below, which would otherwise misread an
        # empty sentence list as "session already done".
        return placement_answer(s, spoken, metrics)
    if not s.get("in_review"):
        advance_stage_if_swept(s)
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
            "topic": review_sentence.get("topic", ""),
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
    # app would silently skip straight to the next sentence.
    #
    # Didactic restructure: stations no longer run back-to-back for the SAME
    # sentence. s["stage"] says which global sweep is active - "accuracy"
    # (station 1 + Bloom mastery reps below) or "cloze" (station 3, entered
    # only via the dedicated cloze sweep in advance_stage_if_swept, never
    # inline here anymore). Every sentence passes through the accuracy sweep
    # first; only once EVERY sentence has been read correctly does the cloze
    # sweep begin, sentence by sentence again from the top.

    # Station 3: Cloze check - only reachable once s["stage"]=="cloze" has set
    # s["cloze_active"] for the current sentence (see advance_stage_if_swept).
    if s["stage"] == "cloze" and s["cloze_active"]:
        if passed:
            s["cloze_passed"] = True
            finalize_sentence(s, correct, spoken, score, True, metrics=metrics)
            return jsonify({**base, "station": "cloze", "cloze_done": True, "advance": True, **session_payload(s)})

        s["cloze_attempts"] += 1
        if s["cloze_attempts"] >= s["max_attempts"] + s.get("bonus_attempts", 0):
            return cap_choice(s, correct, spoken, score, {**base, "station": "cloze", "cloze_failed": True}, metrics=metrics, sentence_obj=sentence_obj, attempts_used=s["cloze_attempts"])
        return jsonify({**base, "station": "cloze", "cloze_mode": True, **session_payload(s)})

    # Station 2: Bloom practice/mastery, within the accuracy sweep.
    if s["mastery_target"] > 0:
        s["stage2_attempts"] = int(s.get("stage2_attempts", 0)) + 1
        stage2_cap_reached = s["stage2_attempts"] >= s["max_attempts"] + s.get("bonus_attempts", 0)
        if passed:
            s["mastery_consecutive"] += 1
            s["mastery_score"] = max(s.get("mastery_score", 0), score)
            if s["mastery_consecutive"] >= s["mastery_target"]:
                # Practice mastery is complete for THIS sentence. Per the
                # restructure, move on to the NEXT sentence within the
                # accuracy sweep instead of testing this one's cloze right
                # away - cloze for every sentence happens later, in its own
                # dedicated sweep (see advance_stage_if_swept).
                advance_within_accuracy_stage(s)
                return jsonify({**base, "station": "practice", "mastery_mode_done": True, "advance": True, **session_payload(s)})
            return jsonify({**base, "station": "practice", "mastery_mode": True, "streak_broken": False, **session_payload(s)})
        # A failed repetition mid-way through Bloom reinforcement does NOT reset
        # progress back to zero - it simply isn't counted as one of the required
        # successes. The student still just needs (target - consecutive) more
        # correct repetitions. Only running out of station 2's own attempt budget
        # ends the loop (handled below).
        if stage2_cap_reached:
            return cap_choice(s, correct, spoken, score, {**base, "station": "practice"}, metrics=metrics, sentence_obj=sentence_obj, attempts_used=s["stage2_attempts"])
        return jsonify({**base, "station": "practice", "mastery_mode": True, "streak_broken": True, **session_payload(s)})

    # Station 1: normal practice / initial read. A passing first read enters
    # Bloom repetition mode if there were prior failures, otherwise the
    # sentence's accuracy portion is already done - move on to the next
    # sentence within the accuracy sweep (see comment above).
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

        advance_within_accuracy_stage(s)
        return jsonify({**base, "station": "practice", "advance": True, **session_payload(s)})

    s["failed_attempts"] += 1
    if cap_reached:
        return cap_choice(s, correct, spoken, score, base, metrics=metrics, sentence_obj=sentence_obj, attempts_used=s["sentence_attempts"])
    return jsonify({**base, **session_payload(s)})

@app.post("/api/skip")
def skip():
    data = request.get_json(force=True)
    s = get_session(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    if s["stage"] == "preview":
        # Nothing to "give up on" in the ungraded preview - use the
        # back/forward buttons (/api/preview-nav) there instead.
        return jsonify(ok=True)
    advance_stage_if_swept(s)
    if s["current"] < len(s["sentences"]):
        correct = s["sentences"][s["current"]].get("en", "")
        finalize_sentence(s, correct, "", 0, False, skipped=True)
    return jsonify(ok=True)

_STAGE_TO_STATION = {"preview": 1, "accuracy": 2, "cloze": 3, "done": 4}

def _blank_display_for(sentence_obj):
    """Same blanking logic real station 3 uses (cloze_fields_for +
    detect_cloze_word), just pre-rendered into a display string here instead
    of being driven live by cloze_active/cloze_word session state. Needed so
    the free-practice picker can show a genuinely blanked prompt for ANY
    sentence with a detectable cloze word - not only the minority that have a
    teacher-authored "completion" override - otherwise station 3's free
    practice silently degrades into looking identical to station 2's."""
    cw, completion = cloze_fields_for(sentence_obj)
    if completion:
        return completion
    if not cw:
        return ""
    words = sentence_obj.get("en", "").split()
    return " ".join("______" if re.sub(r"[^a-z0-9]", "", w.lower()) == cw else w for w in words)

@app.get("/api/exercise-sentences")
def exercise_sentences():
    """Full sentence list for the current exercise - used by the bottom
    step-bar's "browse and freely re-practice" picker when a student taps an
    earlier, already-completed station. Practicing from here always goes
    through the existing ungraded practice-repeat flow, never touches scored
    results. cloze_display is the pre-blanked prompt for station 3's variant
    of that free practice (empty string if this sentence has no detectable
    blank, same as real cloze - it just never gets tested there either)."""
    s = get_session(request.args.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    return jsonify(sentences=[
        {"he": q.get("he", ""), "en": q.get("en", ""), "emoji": q.get("emoji", ""), "completion": q.get("completion", ""), "cloze_display": _blank_display_for(q)}
        for q in s.get("sentences", [])
    ])

@app.post("/api/jump-station")
def jump_station():
    """Student-driven station jump (bottom step bar) - free forward or
    backward navigation between the 4 stations at any moment, per explicit
    request. Semantics, chosen deliberately to reuse existing, already-safe
    mechanics rather than rewriting scoring:
    - Forward jumps (skip ahead) fast-forward past the intervening station(s)
      WITHOUT calling finalize_sentence for the sentences skipped over - they
      simply never went through that station, so whatever station they DO
      still reach (e.g. cloze, station 3) scores them purely on what they
      actually say there. This mirrors today's single-sentence skip: skipped
      content is excluded from the exam pool, never counted as a zero.
    - Backward to station 1 (preview) is a true free re-visit - preview is
      always ungraded, so there is nothing to protect.
    - Backward to station 2/3 after they're already finished is intentionally
      NOT a real state rewind (that would risk writing duplicate/conflicting
      result rows for the same sentence). Instead it hands back mode
      "free_practice" so the client opens the existing ungraded
      practice-repeat flow over that station's sentences - real progress and
      scores are never touched.
    """
    data = request.get_json(force=True)
    s = get_session(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    try:
        target = int(data.get("target"))
    except (TypeError, ValueError):
        target = 0
    if target not in (1, 2, 3, 4):
        return jsonify(ok=False, error="bad target"), 400
    advance_stage_if_swept(s)
    stage_num = _STAGE_TO_STATION.get(s["stage"], 2)
    if target == stage_num:
        return jsonify(ok=True, mode="same", station=stage_num)
    if target < stage_num:
        if target == 1:
            s["stage"] = "preview"
            s["current"] = 0
            return jsonify(ok=True, mode="preview")
        return jsonify(ok=True, mode="free_practice", station=target)
    # target > stage_num: skip forward past the intervening station(s).
    if s["stage"] == "preview":
        s["stage"] = "accuracy"
        s["current"] = 0
    if target >= 3 and s["stage"] == "accuracy":
        s["current"] = len(s["sentences"])
        advance_stage_if_swept(s)
    if target >= 4 and s["stage"] == "cloze":
        s["current"] = len(s["sentences"])
        # Jumping straight to the exam is a deliberate "test me on everything"
        # request, not a per-sentence skip - mark the session done/complete
        # immediately (mirrors what /api/question's own done-detection would
        # do once it's next called) so teacher-facing live status and
        # resume-login logic stay consistent even though the client builds
        # the exam directly from the full sentence list instead of going
        # through /api/question's (mostly-empty/skipped) results pool.
        s["stage"] = "done"
        s["completed"] = True
    return jsonify(ok=True, mode="skipped_forward", station=target)

@app.post("/api/score-only")
def score_only():
    data = request.get_json(force=True)
    tid = data.get("teacher_id", "ben")
    threshold = _teacher_state.get(tid, {}).get("threshold", 85)
    spoken = data.get("spoken", "")
    correct = data.get("correct", "")
    score = similarity(spoken, correct)
    return jsonify(score=score, passed=score >= threshold, words=word_level(spoken, correct), debug_expected=correct)

@app.post("/api/practice-repeat")
def practice_repeat():
    """Voluntary, ungraded re-attempt of a sentence the student already
    passed - lets them go back and say it again for their own benefit
    ("I finished it, but I want another rep") without touching the session's
    stage/index bookkeeping, the results sheet, or the mastery/exam pool.
    Pure score-and-forget, using the same 100%-to-pass bar stations 1-3 use
    for a real pass, so the feedback stays consistent with what "passing"
    means everywhere else in the app."""
    data = request.get_json(force=True)
    spoken = data.get("answer", "")
    correct = (data.get("correct") or "").strip()
    if not correct:
        return jsonify(score=0, passed=False, words=[], error="missing sentence"), 400
    score = similarity(spoken, correct)
    return jsonify(score=score, passed=score >= 100, words=word_level(spoken, correct))

@app.post("/api/exam-result")
def exam_result():
    data = request.get_json(force=True)
    s = get_session(data.get("student", ""))
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
        "topic": SENTENCE_TOPIC_LOOKUP.get(data.get("sentence", ""), ""),
        **fluency,
    }
    s["exam_results"].append(row)
    write_result(row)
    return jsonify(ok=True)

@app.post("/api/exam-complete")
def exam_complete():
    """Explicit signal that the student has actually finished the exam (the
    client calls this from showExamSummary()) - see the exam_completed field
    set in new_session() for why this must be tracked separately from
    "completed" (reaching station 4), and how login-resume logic depends on
    it."""
    data = request.get_json(force=True)
    s = get_session(data.get("student", ""))
    if not s:
        return jsonify(error="session not found"), 404
    s["exam_completed"] = True
    s["updated_at"] = int(time.time())
    return jsonify(ok=True)

def get_results_tab_gid(tid, sheet_id):
    """Best-effort lookup of the specific worksheet (tab) gid holding this
    teacher's results, so the "open results sheet" link in teacher.html can
    deep-link straight to their tab instead of whatever tab the spreadsheet
    happens to open on by default (gid=0 / last-viewed). Several teachers can
    share one results spreadsheet with one tab each (see results_tab in
    TEACHERS/load_extra_teachers) - without the gid, a teacher clicking the
    link could land on a DIFFERENT teacher's tab (or an empty default tab)
    and reasonably conclude their results never made it to the sheet, even
    though they did - this was reported as exactly that: results visible in
    the dashboard but "missing" from the sheet."""
    if tid in _results_tab_gid_cache:
        return _results_tab_gid_cache[tid]
    try:
        import gspread
        tab = TEACHERS[tid]["results_tab"]
        sh = get_gspread_client().open_by_key(sheet_id)
        ws = sh.worksheet(tab)
        _results_tab_gid_cache[tid] = ws.id
        return ws.id
    except Exception as e:
        print("GET RESULTS TAB GID FAILED", tid, e)
        return None

@app.post("/api/teacher-login")
def teacher_login():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    return _complete_teacher_login(tid)

@app.post("/api/teacher-login-google")
def teacher_login_google():
    # Google Sign-In path for teachers: a teacher's account is linked to a
    # specific Gmail/Google Workspace address via the "google_email" field
    # (set in /admin - see TEACHERS schema and admin.html). If the verified
    # token's email doesn't match any teacher's linked address, there is
    # nothing to log into - most likely nobody has linked that teacher's
    # account yet, which is a setup step for the admin, not a wrong password.
    data = request.get_json(force=True)
    credential = data.get("credential", "")
    ginfo = verify_google_id_token(credential)
    if not ginfo:
        return jsonify(ok=False, error="ההתחברות עם Google לא הוגדרה או שהאימות נכשל. נסה שוב או התחבר עם סיסמה."), 401
    if not ginfo.get("email_verified"):
        return jsonify(ok=False, error="חשבון ה-Google הזה אינו מאומת."), 401
    email = (ginfo.get("email") or "").strip().lower()
    tid = next((t for t, cfg in TEACHERS.items() if (cfg.get("google_email") or "").strip().lower() == email), None)
    if not tid:
        return jsonify(ok=False, error="לא נמצא חשבון מורה המקושר לכתובת ה-Gmail הזו. יש לקשר אותה בעמוד הניהול (/admin) ואז לנסות שוב."), 403
    return _complete_teacher_login(tid)

def _complete_teacher_login(tid):
    s = _teacher_state[tid]
    sheet_id = RESULTS_SHEET_IDS.get(tid, RESULTS_SHEET_ID)
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    gid = get_results_tab_gid(tid, sheet_id)
    if gid is not None:
        sheet_url += f"#gid={gid}"
    return jsonify(
        ok=True, teacher=teacher_public(tid),
        allowed_students=s.get("allowed_students", []),
        restrict_to_list=bool(s.get("restrict_to_list", False)),
        results_sheet_url=sheet_url,
        google_email=TEACHERS[tid].get("google_email", ""),
        # Issued on every successful login (password or Google) so the
        # client never has to resend the real password on subsequent
        # requests - it can send this token instead. See _teacher_auth_ok.
        token=make_teacher_token(tid),
    )

@app.post("/api/teacher-allowed-students")
def teacher_allowed_students():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
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
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    _teacher_state[tid]["threshold"] = max(80, min(100, int(data.get("threshold", _teacher_state[tid]["threshold"]))))
    _teacher_state[tid]["max_attempts"] = max(4, min(7, int(data.get("max_attempts", _teacher_state[tid]["max_attempts"]))))
    if "silence_timeout_ms" in data:
        _teacher_state[tid]["silence_timeout_ms"] = max(400, min(3000, int(data.get("silence_timeout_ms", 1200))))
    if "default_private_mode" in data:
        _teacher_state[tid]["default_private_mode"] = bool(data.get("default_private_mode"))
    sheet_warning = None
    if "google_email" in data:
        # Self-service Google Sign-In linking: the teacher already proved
        # they know the teacher_password to reach this endpoint, so letting
        # them link/unlink their own Gmail here (rather than only via
        # /admin) is the same trust level as changing any other setting.
        # Written through to TEACHERS + the Teachers sheet (see
        # _upsert_teacher_row) so it survives a redeploy, same as the
        # admin-driven path.
        google_email = (data.get("google_email") or "").strip().lower()
        if google_email and not EMAIL_RE.match(google_email):
            return jsonify(ok=False, error="כתובת ה-Gmail שהוזנה אינה תקינה"), 400
        entry = dict(TEACHERS[tid])
        entry["google_email"] = google_email
        TEACHERS[tid] = entry
        try:
            _upsert_teacher_row(tid, entry, RESULTS_SHEET_IDS.get(tid, ""))
            _persisted_teacher_ids.add(tid)
        except Exception as e:
            print("SAVE GOOGLE EMAIL FAILED", e)
            sheet_warning = "הקישור פעיל כרגע, אך השמירה לגיליון נכשלה - ייתכן שהוא ייעלם אחרי ריסטארט הבא."
    save_state()
    resp = {"ok": True, "teacher": teacher_public(tid)}
    if sheet_warning:
        resp["sheet_warning"] = sheet_warning
    return jsonify(resp)

@app.post("/api/catalog")
def api_catalog():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
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
    if not _teacher_auth_ok(tid, data):
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
                "לא הצלחתי לכתוב ל-Google Sheet הראשי. ודא ש-GOOGLE_CREDENTIALS_JSON מוגדר "
                "ושה-Service Account קיבל הרשאת Editor לגיליון התרגילים."
            ),
            details=str(e),
        ), 500

    # Select the newly-added/existing sheet exercise for this teacher immediately.
    _teacher_state[tid]["exercise_name"] = item["name"]
    _teacher_state[tid]["csv_url"] = item["csv_url"]
    save_state()
    _persist_teacher_exercise(tid)
    return jsonify(ok=True, exercise=item, sentence_count=len(sentences), teacher=teacher_public(tid))

_MAX_PHOTO_DATA_URL_LEN = 7_000_000  # ~5MB raw image, base64-inflated

def _parse_image_data_url(data_url):
    """Split a data:image/...;base64,... URL into (media_type, base64_data),
    or raise ValueError with a Hebrew message safe to show a teacher."""
    data_url = (data_url or "").strip()
    if not data_url.startswith("data:"):
        raise ValueError("פורמט תמונה לא תקין")
    if len(data_url) > _MAX_PHOTO_DATA_URL_LEN:
        raise ValueError("התמונה גדולה מדי - נסה תמונה קטנה/דחוסה יותר")
    m = re.match(r"^data:([^;]+);base64,(.+)$", data_url, re.DOTALL)
    if not m:
        raise ValueError("פורמט תמונה לא תקין")
    media_type, b64 = m.group(1), m.group(2)
    if media_type not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
        raise ValueError("סוג קובץ לא נתמך - צלם/י PNG, JPEG או WEBP")
    return media_type, b64

_PHOTO_TOPIC_LIST = ", ".join(GRAMMAR_TOPIC_ORDER + ["general", "vocab"])

@app.post("/api/teacher/photo-to-sentences")
def photo_to_sentences():
    """Feature 1 step 1/2: teacher uploads a photo of a text page, Claude
    vision extracts up to 10 standalone spoken-practice sentences with
    Hebrew translations, a topic tag, and a CEFR level guess. Returns a DRAFT
    only - nothing is saved or delivered to any student yet. The teacher
    reviews/edits the draft client-side and calls save-ai-exercise below to
    actually activate it."""
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    if not ANTHROPIC_API_KEY:
        return jsonify(ok=False, error="פיצ'ר ה-AI לא מוגדר בשרת כרגע."), 501
    if not check_and_bump_ai_quota(tid):
        return jsonify(ok=False, error=f"הגעת למכסת ה-AI היומית ({AI_DAILY_LIMIT_PER_TEACHER} הפקות). נסה/י שוב מחר."), 429
    try:
        media_type, b64 = _parse_image_data_url(data.get("image_data_url", ""))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400

    prompt = (
        "This is a photo of a page of English text. Extract up to 10 clear, "
        "grammatically complete, standalone English sentences from it that "
        "would work well for a student to practice speaking aloud (each "
        "sentence should make sense on its own, without needing the rest of "
        "the page for context - skip fragments, headings, and anything that "
        "doesn't read as a full sentence). For each sentence, provide a "
        "natural Hebrew translation, a rough CEFR level (A1, A2, B1, B2, C1, "
        "or C2), and a topic tag chosen from EXACTLY this list: "
        f"{_PHOTO_TOPIC_LIST}. Respond with ONLY a JSON array, no other "
        "text, no markdown fence, in this exact shape: "
        '[{"en": "...", "he": "...", "level": "A2", "topic": "..."}, ...]. '
        "If the photo doesn't contain readable English text, respond with []."
    )
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
            {"type": "text", "text": prompt},
        ],
    }]
    text, err = call_claude(messages, max_tokens=2000)
    if err:
        return jsonify(ok=False, error=err), 502
    try:
        items = extract_json_block(text)
    except Exception as e:
        print("PHOTO-TO-SENTENCES PARSE FAILED", e, text[:500] if text else None)
        return jsonify(ok=False, error="לא הצלחתי לפרש את תשובת ה-AI. נסה/י שוב עם תמונה ברורה יותר."), 502
    if not isinstance(items, list):
        return jsonify(ok=False, error="תשובת AI לא תקינה"), 502

    valid_topics = set(GRAMMAR_TOPIC_ORDER) | {"general", "vocab"}
    cleaned = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        en = clean_cell(str(item.get("en", "")))
        he = clean_cell(str(item.get("he", "")))
        if not en or not he or not looks_english(en):
            continue
        level = str(item.get("level", "")).strip().upper()
        if level not in ALL_STUDENT_LEVELS:
            level = ""
        topic = str(item.get("topic", "")).strip()
        if topic not in valid_topics:
            topic = "general"
        cleaned.append({"en": en, "he": he, "level": level, "topic": topic})
    if not cleaned:
        return jsonify(ok=False, error="לא נמצאו משפטים ברורים בתמונה. נסה/י תמונה חדה יותר, עם תאורה טובה."), 200
    return jsonify(ok=True, sentences=cleaned)

@app.post("/api/teacher/save-ai-exercise")
def save_ai_exercise_endpoint():
    """Feature 1 step 2/2: persist the teacher-reviewed (possibly edited)
    sentence list from photo-to-sentences above, and immediately select it
    as this teacher's active exercise - same "select immediately" behavior
    as add-exercise for a normal CSV exercise."""
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    name = clean_cell(data.get("name", "")) or "תרגיל מתמונה"
    raw_sentences = data.get("sentences") or []
    if not isinstance(raw_sentences, list):
        return jsonify(ok=False, error="פורמט משפטים לא תקין"), 400
    sentences = []
    for item in raw_sentences[:15]:
        if not isinstance(item, dict):
            continue
        en = clean_cell(str(item.get("en", "")))
        he = clean_cell(str(item.get("he", "")))
        if not en:
            continue
        sentences.append({"en": en, "he": he or en, "topic": item.get("topic") or "general"})
    if not sentences:
        return jsonify(ok=False, error="אין משפטים תקינים לשמירה"), 400

    ai_id = save_ai_exercise(tid, name, sentences)
    if ai_id is None:
        return jsonify(ok=False, error="שמירה נכשלה - ודא שמסד הנתונים מוגדר (DATABASE_URL)."), 500

    csv_url = f"ai://{ai_id}"
    _teacher_state[tid]["exercise_name"] = name
    _teacher_state[tid]["csv_url"] = csv_url
    save_state()
    _persist_teacher_exercise(tid)
    return jsonify(ok=True, ai_exercise_id=ai_id, sentence_count=len(sentences), teacher=teacher_public(tid))

@app.post("/api/teacher/generate-topic-booster")
def generate_topic_booster():
    """AI feature 2, stage B: generate NEW sentences targeting one grammar
    topic at one CEFR level, once the built-in curriculum bank for that
    topic/level has been exhausted for a given student (or a teacher just
    wants extra targeted drill material). Reuses the exact same shared
    infra as photo-to-sentences (call_claude, quota, JSON extraction) and
    returns a draft in the exact same shape, so the teacher.html review/edit
    UI built for feature 1 (aiPhotoDraft/renderAiPhotoDraft/saveAiPhotoExercise)
    works for this unchanged - saving still goes through
    /api/teacher/save-ai-exercise. Nothing here is ever injected into a
    student's LIVE session automatically; a teacher always reviews and
    explicitly saves+selects it first, same safety model as feature 1.
    """
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    if not ANTHROPIC_API_KEY:
        return jsonify(ok=False, error="פיצ'ר ה-AI לא מוגדר בשרת כרגע."), 501
    if not check_and_bump_ai_quota(tid):
        return jsonify(ok=False, error=f"הגעת למכסת ה-AI היומית ({AI_DAILY_LIMIT_PER_TEACHER} הפקות). נסה/י שוב מחר."), 429

    level = str(data.get("level", "")).strip().upper()
    if level not in CEFR_LEVELS:
        return jsonify(ok=False, error="רמת CEFR לא תקינה"), 400
    topic = str(data.get("topic", "")).strip()
    student_email = (data.get("student_email") or "").strip().lower()
    if topic == "auto":
        if not student_email:
            return jsonify(ok=False, error="כדי לבחור נושא אוטומטית צריך גם את האימייל של התלמיד/ה"), 400
        weak = get_weak_topics(tid, student_email)
        if not weak:
            return jsonify(ok=False, error="אין עדיין מספיק נתוני תרגול לתלמיד/ה הזו כדי לזהות נושא חלש."), 200
        topic = weak[0]
    if topic not in GRAMMAR_TOPIC_ORDER:
        return jsonify(ok=False, error="נושא דקדוקי לא תקין"), 400

    # Few-shot examples straight from the real curriculum, so style/difficulty
    # matches what the student already sees elsewhere - prefer this exact
    # level, fall back to neighboring levels if this level+topic combo is thin.
    examples = [s for s in LEVEL_SENTENCES.get(level, []) if s.get("topic") == topic]
    if len(examples) < 3:
        for other_level in CEFR_LEVELS:
            if other_level == level:
                continue
            examples += [s for s in LEVEL_SENTENCES.get(other_level, []) if s.get("topic") == topic]
            if len(examples) >= 3:
                break
    example_lines = "\n".join(f'- "{s["en"]}"' for s in examples[:5])
    topic_he = GRAMMAR_TOPIC_NAMES_HE.get(topic, topic)

    prompt = (
        f"Generate 8 NEW English sentences for spoken-practice drilling, all "
        f"at CEFR level {level}, all specifically drilling this grammar "
        f'pattern: "{topic}" ({topic_he}). Do not reuse any of these existing '
        f"example sentences (for style/difficulty reference only):\n{example_lines}\n\n"
        "Each sentence must be natural to say aloud, grammatically complete, "
        "and clearly exercise the target grammar pattern. Provide a natural "
        "Hebrew translation for each. Respond with ONLY a JSON array, no "
        "other text, no markdown fence, in this exact shape: "
        '[{"en": "...", "he": "..."}, ...]'
    )
    text, err = call_claude([{"role": "user", "content": prompt}], max_tokens=2000)
    if err:
        return jsonify(ok=False, error=err), 502
    try:
        items = extract_json_block(text)
    except Exception as e:
        print("TOPIC BOOSTER PARSE FAILED", e, text[:500] if text else None)
        return jsonify(ok=False, error="לא הצלחתי לפרש את תשובת ה-AI. נסה/י שוב."), 502
    if not isinstance(items, list):
        return jsonify(ok=False, error="תשובת AI לא תקינה"), 502

    cleaned = []
    for item in items[:10]:
        if not isinstance(item, dict):
            continue
        en = clean_cell(str(item.get("en", "")))
        he = clean_cell(str(item.get("he", "")))
        if not en or not he or not looks_english(en):
            continue
        cleaned.append({"en": en, "he": he, "level": level, "topic": topic})
    if not cleaned:
        return jsonify(ok=False, error="ה-AI לא החזיר משפטים תקינים. נסה/י שוב."), 200
    return jsonify(ok=True, sentences=cleaned, topic=topic, topic_he=topic_he, level=level)

@app.post("/api/set-exercise")
def set_exercise():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    csv_url = extract_csv_url(data.get("csv_url", ""))
    _teacher_state[tid]["exercise_name"] = clean_cell(data.get("name", "תרגול דמו")) or "תרגול דמו"
    _teacher_state[tid]["csv_url"] = csv_url
    save_state()
    _persist_teacher_exercise(tid)
    # Always force a fresh pull from the sheet on (re)selection - a teacher
    # pressing "בחר" on the exercise they're already using is a natural,
    # expected way to say "I just edited the sheet, load the latest version",
    # not just a redundant no-op.
    invalidate_sentence_cache(csv_url)
    return jsonify(ok=True, teacher=teacher_public(tid), sentence_count=len(load_sentences_from_csv(csv_url)))

@app.post("/api/refresh-exercise")
def refresh_exercise():
    """Explicit "reload the content from the sheet now" action for a teacher,
    without needing to re-pick the exercise from the catalog list - handy
    right after editing the Google Sheet mid-lesson. New students starting
    after this call get the fresh content immediately; students already
    mid-exercise keep the sentence set they started with (each session
    captured its own copy at creation time), so this never disrupts someone
    already partway through."""
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    csv_url = _teacher_state[tid].get("csv_url", "")
    if not csv_url.strip():
        return jsonify(ok=True, sentence_count=0, note="no_exercise_selected")
    invalidate_sentence_cache(csv_url)
    sentences = load_sentences_from_csv(csv_url)
    return jsonify(ok=True, sentence_count=len(sentences))

@app.post("/api/teacher-results")
def teacher_results():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    # Read from the Google Sheet (durable) instead of only _pending_results
    # (wiped on every server restart/redeploy) - same persistence model as
    # the student-facing "My Results" view, so the teacher's Results tab no
    # longer silently loses history whenever Render spins the dyno down.
    sheet_rows, debug = read_results_sheet_rows(tid)
    rows = merge_with_pending(sheet_rows, tid)
    return jsonify(ok=True, rows=rows[-200:], debug=debug)

def _session_phase_label(s):
    stage = s.get("stage", "accuracy")
    if stage == "done":
        return "סיים"
    if s.get("in_review"):
        return "סבב חזרה"
    if stage == "preview":
        return "חשיפה"
    if stage == "cloze":
        return "קלוז"
    return "Mastery" if s.get("mastery_target", 0) > 0 else "אימון"

@app.post("/api/teacher-students")
def teacher_students():
    data = request.get_json(force=True)
    tid, password = data.get("teacher_id", ""), data.get("password", "")
    if not _teacher_auth_ok(tid, data):
        return jsonify(ok=False), 401
    # Merge the durable Postgres roster with in-memory _sessions rather than
    # reading _sessions alone - otherwise any student who hasn't made a
    # request since the last restart would appear to have vanished from
    # their own teacher's roster, even though their progress was never
    # actually lost (see the DB-persistence work above).
    combined = {**_list_all_sessions_from_db(), **_sessions}
    students = []
    for sid, s in combined.items():
        if s["teacher_id"] == tid:
            phase = _session_phase_label(s)
            done = phase == "סיים"
            students.append({
                "student_id": sid,
                "name": s["student_name"], "email": s.get("student_email", ""),
                "index": s["current"], "total": len(s["sentences"]),
                "done": done, "exercise": s["exercise_name"],
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

def _is_admin(data):
    return bool(ADMIN_PASSWORD) and data.get("password") == ADMIN_PASSWORD

def _admin_or_teacher_auth(data):
    """Returns ('admin', None) for a valid admin password, ('teacher', tid)
    for a valid teacher_id+password, or None if neither checks out. Shared
    by the student-management endpoints below (view/edit/delete), which the
    admin dashboard uses across every teacher and each teacher's own
    dashboard uses scoped to just themselves."""
    if _is_admin(data):
        return ("admin", None)
    tid = data.get("teacher_id", "")
    if _teacher_auth_ok(tid, data):
        return ("teacher", tid)
    return None

@app.post("/api/admin-login")
def admin_login():
    data = request.get_json(force=True)
    if not _is_admin(data):
        return jsonify(ok=False, error="סיסמה שגויה"), 401
    return jsonify(ok=True)

@app.post("/api/admin-teachers")
def admin_teachers():
    """Overview row per teacher for the admin dashboard - name, current
    exercise, and a live count of students currently mid-session, pulled
    from the same in-memory _sessions the per-teacher dashboard uses."""
    data = request.get_json(force=True)
    if not _is_admin(data):
        return jsonify(ok=False), 401
    out = []
    for tid, t in TEACHERS.items():
        sessions_for_tid = [s for s in _sessions.values() if s["teacher_id"] == tid]
        out.append({
            "teacher_id": tid, "name": t["name"], "color": t["color"],
            "voice_gender": t["voice_gender"], "photo_url": t.get("photo_url", ""),
            "exercise_name": _teacher_state.get(tid, {}).get("exercise_name", ""),
            "active_students": sum(1 for s in sessions_for_tid if _session_phase_label(s) != "סיים"),
            "completed_students": sum(1 for s in sessions_for_tid if _session_phase_label(s) == "סיים"),
            "has_results_sheet": tid in RESULTS_SHEET_IDS,
            "persisted": tid in _persisted_teacher_ids,
        })
    out.sort(key=lambda x: x["name"])
    return jsonify(ok=True, teachers=out)

@app.post("/api/admin-students")
def admin_students():
    """Every active/completed student session across ALL teachers, for the
    admin's cross-teacher view (the per-teacher dashboard at /teacher only
    ever sees its own teacher_id's students)."""
    data = request.get_json(force=True)
    if not _is_admin(data):
        return jsonify(ok=False), 401
    combined = {**_list_all_sessions_from_db(), **_sessions}
    students = []
    for sid, s in combined.items():
        students.append({
            "student_id": sid,
            "teacher_id": s["teacher_id"], "teacher_name": TEACHERS.get(s["teacher_id"], {}).get("name", s["teacher_id"]),
            "name": s["student_name"], "email": s.get("student_email", ""),
            "index": s["current"], "total": len(s["sentences"]),
            "exercise": s["exercise_name"], "phase": _session_phase_label(s),
            "created_at": s.get("created_at"), "updated_at": s.get("updated_at"),
        })
    students.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return jsonify(ok=True, students=students)

@app.post("/api/manage-student-update")
def manage_student_update():
    """Edit a student's display name, email, or (admin only) which teacher
    they belong to. Name-only edits update the record in place. Changing
    the email or teacher_id changes the student's primary key
    (student_id = teacher_id + email), so those are handled as a rename:
    copy the session under the new id, drop the old one, in both the
    in-memory cache and Postgres. Refuses to silently overwrite an existing
    student if the computed new id already belongs to someone else."""
    data = request.get_json(force=True)
    auth = _admin_or_teacher_auth(data)
    if not auth:
        return jsonify(ok=False, error="לא מורשה"), 401
    role, scope_tid = auth
    old_sid = data.get("student_id", "")
    s = get_session(old_sid)
    if not s:
        return jsonify(ok=False, error="תלמיד לא נמצא"), 404
    if role == "teacher" and s["teacher_id"] != scope_tid:
        return jsonify(ok=False, error="אין הרשאה לתלמיד/ה של מורה אחר/ת"), 403
    new_teacher_id = (data.get("teacher_id_new") or s["teacher_id"]).strip()
    if new_teacher_id != s["teacher_id"]:
        if role == "teacher":
            return jsonify(ok=False, error="העברת תלמיד/ה למורה אחר/ת דורשת הרשאת admin"), 403
        if new_teacher_id not in TEACHERS:
            return jsonify(ok=False, error="מורה יעד לא קיים"), 400
    new_email = (data.get("email") or s.get("student_email") or "").strip().lower()
    new_name = data.get("name")
    if new_name is not None and new_name.strip():
        s["student_name"] = new_name.strip()
    safe_email = re.sub(r"[^a-z0-9]", "_", new_email)
    new_sid = f"{new_teacher_id}_{safe_email}"
    if new_sid == old_sid:
        s["updated_at"] = int(time.time())
        save_session(old_sid)
        return jsonify(ok=True, student_id=old_sid)
    if new_sid in _sessions or load_session(new_sid):
        return jsonify(ok=False, error="כבר קיים תלמיד/ה עם האימייל/מורה האלה - לא ניתן למזג אוטומטית"), 409
    s["student_id"] = new_sid
    s["teacher_id"] = new_teacher_id
    s["student_email"] = new_email
    if new_teacher_id in TEACHERS:
        s["voice_gender"] = TEACHERS[new_teacher_id]["voice_gender"]
    s["updated_at"] = int(time.time())
    _sessions[new_sid] = s
    _sessions.pop(old_sid, None)
    save_session(new_sid)
    delete_session_row(old_sid)
    return jsonify(ok=True, student_id=new_sid)

@app.post("/api/manage-student-delete")
def manage_student_delete():
    """Delete a student's live session (Postgres + memory). If
    scrub_sheet is true, also best-effort deletes their historical result
    rows from the teacher's Google Sheet - reported separately from the
    session deletion since one can succeed while the other fails, and the
    caller must know exactly which happened rather than assuming."""
    data = request.get_json(force=True)
    auth = _admin_or_teacher_auth(data)
    if not auth:
        return jsonify(ok=False, error="לא מורשה"), 401
    role, scope_tid = auth
    sid = data.get("student_id", "")
    s = get_session(sid)
    if not s:
        return jsonify(ok=False, error="תלמיד לא נמצא"), 404
    if role == "teacher" and s["teacher_id"] != scope_tid:
        return jsonify(ok=False, error="אין הרשאה לתלמיד/ה של מורה אחר/ת"), 403
    tid, email = s["teacher_id"], s.get("student_email", "")
    _sessions.pop(sid, None)
    delete_session_row(sid)
    sheet_scrub_ok, sheet_scrub_error = (None, None)
    if data.get("scrub_sheet"):
        sheet_scrub_ok, sheet_scrub_error = delete_student_rows_from_sheet(tid, email)
    return jsonify(ok=True, sheet_scrub_ok=sheet_scrub_ok, sheet_scrub_error=sheet_scrub_error)

@app.post("/api/admin-add-teacher")
def admin_add_teacher():
    """Add a new teacher at runtime (no code change/redeploy needed) - takes
    effect immediately in-memory AND is written to the "Teachers" sheet tab
    so it's still there after the next restart (see load_extra_teachers)."""
    data = request.get_json(force=True)
    if not _is_admin(data):
        return jsonify(ok=False), 401
    tid = re.sub(r"[^a-z0-9]", "", (data.get("teacher_id") or "").strip().lower())
    name = clean_cell(data.get("name", "")).strip()
    if not tid or not name:
        return jsonify(ok=False, error="נדרש מזהה (אותיות/ספרות באנגלית) ושם"), 400
    if tid in TEACHERS:
        return jsonify(ok=False, error="המזהה הזה כבר קיים - בחר מזהה אחר"), 400
    gender = data.get("voice_gender") if data.get("voice_gender") in ("male", "female") else "female"
    student_password = (data.get("student_password") or "").strip() or "class2026"
    teacher_password = (data.get("teacher_password") or "").strip() or (tid + "2026")
    color = clean_cell(data.get("color", "")) or "#4318D1"
    # No separate "light" shade collected from the admin form (just the one
    # swatch/HEX field) - derive a matching pastel tint from the chosen main
    # color instead of always falling back to the same flat lavender for
    # every teacher regardless of what they picked.
    color_light = clean_cell(data.get("color_light", "")) or lighten_hex(color)
    try:
        threshold = max(80, min(100, int(data.get("threshold") or 85)))
    except (TypeError, ValueError):
        threshold = 85
    try:
        max_attempts = max(4, min(7, int(data.get("max_attempts") or 5)))
    except (TypeError, ValueError):
        max_attempts = 5
    results_sheet_id = extract_sheet_id(data.get("results_sheet_url") or "")
    # A small compressed image, already resized+encoded to a data: URI by the
    # browser before it ever reaches here (see admin.html) - or a plain
    # external image link if the admin pastes one instead. Storing this
    # directly in the Teachers sheet cell (rather than accepting a raw file
    # upload to save on Render's disk) keeps it consistent with everything
    # else durable in this app, and Render's local disk isn't reliably
    # persisted across restarts anyway.
    photo_url = (data.get("photo_url") or "").strip()
    if len(photo_url) > 45000:
        return jsonify(ok=False, error="התמונה גדולה מדי לשמירה - נסה תמונה קטנה/דחוסה יותר"), 400
    google_email = (data.get("google_email") or "").strip().lower()
    if google_email and not EMAIL_RE.match(google_email):
        return jsonify(ok=False, error="כתובת ה-Gmail שהוזנה אינה תקינה"), 400

    entry = {
        "name": name, "color": color, "color_light": color_light, "voice_gender": gender,
        "results_tab": tid, "student_password": student_password, "teacher_password": teacher_password,
        "default_threshold": threshold, "default_max_attempts": max_attempts, "photo_url": photo_url,
        "google_email": google_email,
    }
    TEACHERS[tid] = entry
    if results_sheet_id:
        RESULTS_SHEET_IDS[tid] = results_sheet_id
    _teacher_state[tid] = {
        "threshold": threshold, "max_attempts": max_attempts, "exercise_name": "תרגול דמו",
        "csv_url": "", "custom_exercises": [], "allowed_students": [], "restrict_to_list": False,
        "silence_timeout_ms": 1200,
    }
    save_state()
    sheet_warning = None
    try:
        _upsert_teacher_row(tid, entry, results_sheet_id)
        _persisted_teacher_ids.add(tid)
    except Exception as e:
        # The teacher is already usable in-memory (login works right now) even
        # if this write fails - just warn the admin that it may not survive
        # the next redeploy until they retry or fix the Sheets connection.
        # Deliberately NOT added to _persisted_teacher_ids here, so the admin
        # dashboard's teacher list can flag this one as unsaved until a retry
        # succeeds - this is exactly the gap that let a teacher disappear on
        # restart with no warning before this check existed.
        print("APPEND TEACHER ROW FAILED", e)
        sheet_warning = "המורה נוסף/ה ופעיל/ה כרגע, אך השמירה לגיליון נכשלה - ייתכן שהמורה ייעלם/תיעלם אחרי ריסטארט הבא. בדקו את חיבור ה-Google Sheets ונסו שוב."
    return jsonify(
        ok=True, teacher_id=tid, teacher_password=teacher_password, student_password=student_password,
        results_sheet_configured=bool(results_sheet_id), warning=sheet_warning,
    )

@app.post("/api/admin-retry-persist-teacher")
def admin_retry_persist_teacher():
    """Retry saving an already-added (in-memory) teacher to the Teachers
    sheet tab, for when the first save failed (see admin_add_teacher's
    sheet_warning) - lets the admin fix a Sheets connection issue and re-save
    without re-entering the teacher's details or risking a duplicate-id error
    from re-submitting the add form."""
    data = request.get_json(force=True)
    if not _is_admin(data):
        return jsonify(ok=False), 401
    tid = (data.get("teacher_id") or "").strip().lower()
    if tid not in TEACHERS:
        return jsonify(ok=False, error="מורה לא נמצא/ה"), 404
    if tid in _persisted_teacher_ids:
        return jsonify(ok=True, already_persisted=True)
    try:
        _upsert_teacher_row(tid, TEACHERS[tid], RESULTS_SHEET_IDS.get(tid, ""))
        _persisted_teacher_ids.add(tid)
        return jsonify(ok=True)
    except Exception as e:
        print("RETRY APPEND TEACHER ROW FAILED", e)
        return jsonify(ok=False, error="השמירה נכשלה שוב - בדקו את חיבור ה-Google Sheets (הרשאות שיתוף, credentials)."), 500

@app.post("/api/admin-teacher-detail")
def admin_teacher_detail():
    """Full editable snapshot of one teacher (including current password
    values, unlike /api/admin-teachers' list view) - used to pre-fill the
    admin dashboard's edit form so the admin can see what's already set
    instead of retyping everything from scratch."""
    data = request.get_json(force=True)
    if not _is_admin(data):
        return jsonify(ok=False), 401
    tid = (data.get("teacher_id") or "").strip().lower()
    if tid not in TEACHERS:
        return jsonify(ok=False, error="מורה לא נמצא/ה"), 404
    t = TEACHERS[tid]
    return jsonify(ok=True, teacher={
        "teacher_id": tid, "name": t.get("name", tid), "color": t.get("color", "#4318D1"),
        "voice_gender": t.get("voice_gender", "female"),
        "student_password": t.get("student_password", ""), "teacher_password": t.get("teacher_password", ""),
        "photo_url": t.get("photo_url", ""),
        "google_email": t.get("google_email", ""),
        "results_sheet_id": RESULTS_SHEET_IDS.get(tid, ""),
    })

@app.post("/api/admin-update-teacher")
def admin_update_teacher():
    """Edit an existing teacher's details (any teacher - including the two
    hardcoded ones, Dan/Sara). Every field is optional here: only fields
    actually present/non-empty in the request overwrite the current value,
    so the admin doesn't have to re-supply everything (e.g. re-type both
    passwords) just to change one field like the color. Takes effect
    immediately in-memory, and is written to the Teachers sheet tab the same
    way a newly added teacher is - see _upsert_teacher_row."""
    data = request.get_json(force=True)
    if not _is_admin(data):
        return jsonify(ok=False), 401
    tid = (data.get("teacher_id") or "").strip().lower()
    if tid not in TEACHERS:
        return jsonify(ok=False, error="מורה לא נמצא/ה"), 404
    entry = dict(TEACHERS[tid])
    if clean_cell(data.get("name", "")).strip():
        entry["name"] = clean_cell(data["name"]).strip()
    if data.get("voice_gender") in ("male", "female"):
        entry["voice_gender"] = data["voice_gender"]
    color = clean_cell(data.get("color", "")).strip()
    if color:
        entry["color"] = color
        entry["color_light"] = lighten_hex(color)
    if (data.get("student_password") or "").strip():
        entry["student_password"] = data["student_password"].strip()
    if (data.get("teacher_password") or "").strip():
        entry["teacher_password"] = data["teacher_password"].strip()
    if data.get("threshold"):
        try:
            entry["default_threshold"] = max(80, min(100, int(data["threshold"])))
        except (TypeError, ValueError):
            pass
    if data.get("max_attempts"):
        try:
            entry["default_max_attempts"] = max(4, min(7, int(data["max_attempts"])))
        except (TypeError, ValueError):
            pass
    photo_url = data.get("photo_url")
    if photo_url is not None:
        photo_url = photo_url.strip()
        if len(photo_url) > 45000:
            return jsonify(ok=False, error="התמונה גדולה מדי לשמירה - נסה תמונה קטנה/דחוסה יותר"), 400
        entry["photo_url"] = photo_url
    google_email = data.get("google_email")
    if google_email is not None:
        google_email = google_email.strip().lower()
        if google_email and not EMAIL_RE.match(google_email):
            return jsonify(ok=False, error="כתובת ה-Gmail שהוזנה אינה תקינה"), 400
        entry["google_email"] = google_email
    results_sheet_id = RESULTS_SHEET_IDS.get(tid, "")
    if (data.get("results_sheet_url") or "").strip():
        results_sheet_id = extract_sheet_id(data["results_sheet_url"])
        RESULTS_SHEET_IDS[tid] = results_sheet_id

    TEACHERS[tid] = entry
    if tid in _teacher_state:
        _teacher_state[tid]["threshold"] = entry.get("default_threshold", _teacher_state[tid]["threshold"])
        _teacher_state[tid]["max_attempts"] = entry.get("default_max_attempts", _teacher_state[tid]["max_attempts"])
    save_state()
    sheet_warning = None
    try:
        _upsert_teacher_row(tid, entry, results_sheet_id)
        _persisted_teacher_ids.add(tid)
    except Exception as e:
        print("UPDATE TEACHER ROW FAILED", e)
        sheet_warning = "העדכון פעיל כרגע, אך השמירה לגיליון נכשלה - ייתכן שהשינויים ייעלמו אחרי ריסטארט הבא. בדקו את חיבור ה-Google Sheets ונסו שוב (או השתמשו ב'נסה שוב' בטבלת המורים)."
        # Bug this fixes: if this teacher was ALREADY marked persisted from an
        # earlier successful save (e.g. when first added), a later failed
        # edit - such as adding a photo - used to leave them marked
        # "persisted" anyway, since this set only ever grew. The admin
        # dashboard's "✅ נשמר לצמיתות" badge would then lie: it showed green
        # even though the LATEST change (the photo) never reached the sheet,
        # so it silently reverted on the next restart with no warning ever
        # shown again. Un-marking it here makes the badge flip back to
        # "⚠️ לא נשמר" so the retry button actually appears.
        _persisted_teacher_ids.discard(tid)
    return jsonify(ok=True, warning=sheet_warning)

if __name__ == "__main__":
    app.run(debug=True)
