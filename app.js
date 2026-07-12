// =====================================================
// EZRA APP — CLEAN MONOLITH VERSION
// =====================================================

// =====================
// STATE (single source of truth)
// =====================
const state = {
  teacherData: {},
  currentTeacher: null,
  studentName: "",
  studentId: "",
  currentQ: null,
  waiting: false,
  processing: false,

  examMode: false,
  examIndex: 0,
  examSentences: [],
  examResults: [],
  practiceResults: [],
  questionMap: {},

  voiceGender: "female"
};

// =====================
// CONSTANTS
// =====================
const TEACHER_EMOJIS = {
  ben: "👨‍🏫",
  sara: "👩‍🏫"
};

// =====================================================
// INIT
// =====================================================
document.addEventListener("DOMContentLoaded", init);

function init() {
  bindUI();
  loadTeachers();
}

// =====================
// UI BINDINGS
// =====================
function bindUI() {
  document.getElementById("tc-ben").addEventListener("click", () => selectTeacher("ben"));
  document.getElementById("tc-sara").addEventListener("click", () => selectTeacher("sara"));

  document.getElementById("login-name").addEventListener("keydown", e => {
    if (e.key === "Enter") document.getElementById("login-pass").focus();
  });

  document.getElementById("login-pass").addEventListener("keydown", e => {
    if (e.key === "Enter") submitLogin();
  });
}

// =====================================================
// TEACHERS
// =====================================================
function loadTeachers() {
  fetch("/api/teachers")
    .then(r => r.json())
    .then(data => {
      state.teacherData = data;
      updateTeacherCards(data);
    })
    .catch(console.warn);
}

function updateTeacherCards(data) {
  Object.keys(data).forEach(id => {
    const t = data[id];
    const card = document.getElementById("tc-" + id);
    if (!card) return;

    const avatar = card.querySelector(".tc-avatar");
    const nameEl = card.querySelector(".tc-name");

    if (avatar) {
      avatar.style.background = t.color_light;
      avatar.style.borderColor = t.color;
    }
    if (nameEl) nameEl.textContent = t.name;
  });
}

// =====================================================
// TEACHER SELECTION
// =====================================================
function selectTeacher(id) {
  state.currentTeacher = id;

  const t = state.teacherData[id] || {};
  const color = t.color || "#4318D1";
  const colorLight = t.color_light || "#e8f0fe";

  document.documentElement.style.setProperty("--tc", color);
  document.documentElement.style.setProperty("--tc-light", colorLight);

  document.getElementById("login-avatar-emoji").textContent =
    TEACHER_EMOJIS[id] || "👨‍🏫";

  document.getElementById("login-avatar").style.background = color;
  document.getElementById("login-teacher-name").textContent = t.name || id;

  document.getElementById("login-overlay").classList.add("visible");
}

// =====================================================
// AUTH
// =====================================================
function cancelLogin() {
  document.getElementById("login-overlay").classList.remove("visible");
  state.currentTeacher = null;
}

function submitLogin() {
  const name = document.getElementById("login-name").value.trim();
  const pass = document.getElementById("login-pass").value;

  if (!name) return;

  const errBox = document.getElementById("login-error");
  errBox.style.display = "none";

  fetch("/api/verify-student", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      teacher_id: state.currentTeacher,
      name,
      password: pass
    })
  })
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {
      state.studentName = name;
      state.studentId = d.student_id;
      startApp();
    })
    .catch(() => {
      errBox.style.display = "block";
      document.getElementById("login-pass").value = "";
    });
}

// =====================================================
// APP START
// =====================================================
function startApp() {
  const t = state.teacherData[state.currentTeacher] || {};

  state.voiceGender = t.voice_gender || "female";

  document.getElementById("h-teacher-name").textContent = t.name;
  document.getElementById("h-student-tag").textContent = "👤 " + state.studentName;
  document.getElementById("avatar-emoji").textContent =
    TEACHER_EMOJIS[state.currentTeacher];

  document.getElementById("login-overlay").classList.remove("visible");
  document.getElementById("select-screen").style.display = "none";
  document.getElementById("app-screen").classList.add("visible");

  loadQuestion();
}

// =====================================================
// QUESTION FLOW
// =====================================================
function loadQuestion() {
  resetUI();

  fetch("/api/question?student=" + state.studentId)
    .then(r => r.json())
    .then(q => {
      if (q.done) {
        state.practiceResults = q.results || [];
        startExamMode(state.practiceResults);
        return;
      }

      state.currentQ = q;
      if (q.he) state.questionMap[q.en] = q.he;

      document.getElementById("hebrew").textContent = q.he;
      document.getElementById("english-ref").textContent = q.en;
      document.getElementById("prog-label").textContent =
        `משפט ${q.index + 1} מתוך ${q.total}`;

      document.getElementById("prog-fill").style.width =
        Math.round((q.index / q.total) * 100) + "%";

      setTimeout(playSentence, 400);
    })
    .catch(e => {
      document.getElementById("hebrew").textContent = "שגיאה: " + e.message;
    });
}

// =====================================================
// RESET UI (safe cleanup)
// =====================================================
function resetUI() {
  state.processing = false;

  const sb = document.getElementById("spoken-box");
  sb.classList.remove("visible");
  sb.textContent = "";

  document.getElementById("score-row").style.display = "none";
  document.getElementById("result-banner").className = "result-banner";

  document.getElementById("english-ref").style.opacity = "0";
}

// =====================================================
// SPEECH OUTPUT
// =====================================================
function playSentence() {
  if (!state.currentQ) return;

  speechSynthesis.cancel();

  const u = new SpeechSynthesisUtterance(state.currentQ.en);
  u.lang = "en-US";
  u.rate = 0.88;

  const voices = speechSynthesis.getVoices().filter(v => v.lang.startsWith("en"));
  if (voices.length) u.voice = voices[0];

  speechSynthesis.speak(u);
}

// =====================================================
// RESULT HANDLER (safe minimal version kept extensible)
// =====================================================
function showResult(result) {
  const score = result.score || 0;

  const color =
    score >= 85 ? "#4CAF50" :
    score >= 60 ? "#FF9800" :
    "#ef5350";

  const scoreRow = document.getElementById("score-row");
  scoreRow.style.display = "flex";

  document.getElementById("score-fill").style.width = score + "%";
  document.getElementById("score-fill").style.background = color;
  document.getElementById("score-num").textContent = score + "%";
}

// =====================================================
// PLACEHOLDERS (kept for compatibility)
// =====================================================
function skipSentence() {}
function revealEnglish() {}
function resetSession() { location.reload(); }

// expose globals
window.selectTeacher = selectTeacher;
window.submitLogin = submitLogin;
window.cancelLogin = cancelLogin;
window.playSentence = playSentence;
window.showResult = showResult;
window.skipSentence = skipSentence;
window.revealEnglish = revealEnglish;
window.resetSession = resetSession;