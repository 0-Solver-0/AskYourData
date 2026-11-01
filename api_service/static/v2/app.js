// AskYourData — version conversation : question écrite ou dictée → /api/v1/ask → réponse tirée de la base,
// lue à voix haute par le navigateur (synthèse vocale du système, aucun modèle tiers).
// Aucun chiffre n'est calculé ni écrit ici : la page affiche et lit le résumé et les lignes renvoyés par l'API.
(() => {
  "use strict";

  const ACCOUNT_ID = 1;
  const SPEAK_KEY = "askyourdata.speak";

  const $ = (sel, root = document) => root.querySelector(sel);
  const form = $("#ask-form");
  const input = $("#question");
  const sendBtn = $("#send");
  const micBtn = $("#mic");
  const speakBtn = $("#speak");
  const moreBtn = $("#more");
  const examples = $("#examples");
  const voiceState = $("#voice-state");
  const thread = $("#thread");
  const template = $("#turn-template");

  const STEPS = [
    ["transcribe", "Voice to text", "--step-6"],
    ["structuring", "Understand", "--step-1"],
    ["schema", "Find tables", "--step-2"],
    ["judge", "Pick main table", "--step-6"],
    ["schema_2", "Find tables again", "--step-2"],
    ["generation", "Write SQL", "--step-3"],
    ["extend", "Find missing data", "--step-4"],
    ["generation_2", "Rewrite SQL", "--step-3"],
    ["generation_3", "Fix SQL", "--step-3"],
    ["generation_4", "Fix SQL", "--step-3"],
    ["validation", "Check", "--step-5"],
    ["execution", "Run", "--step-5"],
  ];
  const WORKING_HINTS = [
    [0, "Reading your question"],
    [2500, "Finding the tables that hold the answer"],
    [4500, "Writing the query"],
    [9000, "Checking and running the query"],
    [14000, "Still working: the query needed more data"],
  ];
  const STATUS_TEXT = {
    cannot_answer: "This data is not in the database",
    refused: "This request cannot be run",
    clarification_needed: "The question needs to be more precise",
    missing: "Some data is missing",
    error: "No reliable answer",
  };

  let busy = false;

  // ── Utilitaires ─────────────────────────────────────────────────────────────
  const el = (tag, attrs = {}, ...children) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v);
    }
    for (const c of children) if (c != null) node.append(c);
    return node;
  };
  const svg = (path) => {
    const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    s.setAttribute("viewBox", "0 0 24 24");
    s.setAttribute("aria-hidden", "true");
    const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", path);
    s.append(p);
    return s;
  };
  const ICON_SPEAK = "M4 9v6h4l5 4V5L8 9H4Zm12.5 3a4.5 4.5 0 0 0-2.5-4v8a4.5 4.5 0 0 0 2.5-4Z";
  const ICON_STOP = "M7 7h10v10H7z";
  const ICON_DOWNLOAD = "M12 16 7 11l1.4-1.4 2.6 2.6V4h2v8.2l2.6-2.6L17 11l-5 5Zm-7 4v-2h14v2H5Z";
  const human = (name) => {
    const t = String(name).replace(/_/g, " ").trim();
    return t.charAt(0).toUpperCase() + t.slice(1);
  };
  const isNumber = (v) => typeof v === "number" && Number.isFinite(v);
  const fmtNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  const fmtCell = (v) => (v === null || v === undefined ? "—" : isNumber(v) ? fmtNumber.format(v) : typeof v === "boolean" ? (v ? "Yes" : "No") : String(v));
  const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.max(0, Math.round(ms))} ms`);
  const fmtUsd = (v) => `$${(v || 0).toFixed(4)}`;
  const short = (model) => String(model).split("/").pop();
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* navigation privée */ } },
  };

  // ── Source et modèles ───────────────────────────────────────────────────────
  fetch("/api/v1/health").then((r) => r.json()).then((h) => {
    const db = h.execution && h.execution.db;
    const prep = (h.models && h.models.prep) || (h.llm && h.llm.prep && h.llm.prep.primary && h.llm.prep.primary.model);
    const sql = (h.models && h.models.sql) || (h.llm && h.llm.sql && h.llm.sql.primary && h.llm.sql.primary.model);
    const transcribe = (h.models && h.models.transcribe) || (h.voice && h.voice.model);
    const embedder = (h.models && h.models.embedder) ||
      (h.services && h.services.schema && h.services.schema.embedder && h.services.schema.embedder.model);
    if (h.public) $("#classic-link").hidden = true;      // test public : une seule interface
    const parts = [];
    if (db) parts.push(el("span", {}, "Odoo ", el("strong", { text: db })));
    // Les modèles Meta de la chaîne : compréhension, SQL, voix, recherche des tables
    const models = [prep, sql, transcribe, embedder].filter(Boolean).map(short);
    models.forEach((m, i) => parts.push(el("span", {}, parts.length ? (i ? " + " : " · ") : "", el("strong", { text: m }))));
    $("#source").replaceChildren(...(parts.length ? parts : [document.createTextNode("Database not reachable")]));
  }).catch(() => { $("#source").textContent = "Service not reachable"; });

  // ── Clé d'accès (test public) ───────────────────────────────────────────────
  // Hors mode public, l'API répond { required: false } et rien ne change.
  const KEY_STORE = "askyourdata.key";
  const keyGate = $("#key-gate");
  const keyInput = $("#access-key");
  const keyState = $("#key-state");
  const remainingEl = $("#remaining");
  let accessKey = store.get(KEY_STORE) || "";

  const withKey = (headers = {}) => (accessKey ? { ...headers, "X-Access-Key": accessKey } : headers);

  function showRemaining(info) {
    if (!info || info.remaining == null) return;
    remainingEl.hidden = false;
    remainingEl.textContent = `${info.remaining} ${info.remaining === 1 ? "question" : "questions"} left`;
  }

  function needKey(message) {
    document.body.classList.add("needs-key");
    keyGate.hidden = false;
    keyState.textContent = message || "";
    if (!matchMedia("(pointer: coarse)").matches) keyInput.focus();
  }

  async function checkKey() {
    let resp, info;
    try {
      resp = await fetch("/api/v1/access/me", { headers: withKey() });
      if (resp.status === 404) return true;               // API sans gestion des clés : rien à demander
      info = await resp.json();
    } catch {
      needKey("The service is not reachable. Please try again later.");
      return false;
    }
    if (resp.ok && info.required === false) return true;
    if (resp.ok) {
      store.set(KEY_STORE, accessKey);
      document.body.classList.remove("needs-key");
      keyGate.hidden = true;
      showRemaining(info);
      return true;
    }
    if (info.code === "invalid_key" || info.code === "revoked") { accessKey = ""; store.set(KEY_STORE, ""); }
    needKey(accessKey || info.code !== "invalid_key" ? info.error : "");
    return false;
  }

  keyGate.addEventListener("submit", async (e) => {
    e.preventDefault();
    accessKey = keyInput.value.trim();
    keyInput.value = "";
    if (!(await checkKey()) && !keyState.textContent) keyState.textContent = "This access key is not valid.";
  });
  checkKey();

  // Refus d'accès renvoyé par l'API pendant l'usage : clé révoquée, quota atteint, budget atteint…
  function handleAccessRefusal(data) {
    if (!data || !data.code) return;
    if (data.code === "invalid_key" || data.code === "revoked") { accessKey = ""; store.set(KEY_STORE, ""); needKey(data.error); }
    if (data.code === "quota" || data.code === "budget") showRemaining({ remaining: 0 });
  }

  // ── Lecture à voix haute (synthèse vocale du navigateur) ────────────────────
  const tts = "speechSynthesis" in window ? window.speechSynthesis : null;
  let speaking = null;            // bouton « Listen » de la réponse en cours de lecture

  function englishVoice() {
    const voices = tts ? tts.getVoices() : [];
    return voices.find((v) => /^en(-|_)US/i.test(v.lang) && v.localService) ||
      voices.find((v) => /^en(-|_)US/i.test(v.lang)) || voices.find((v) => /^en/i.test(v.lang)) || null;
  }

  // Ce qui est lu : le résumé de la base (avec l'hypothèse retenue), ou ce qui manque pour répondre
  function speechText(data, voice) {
    // Question dictée : on redit d'abord ce qui a été compris, pour que l'erreur s'entende tout de suite
    const heard = voice && data.rewritten ? `Understood: ${data.rewritten} ` : "";
    if (data.status === "ok" && data.answer) {
      return heard + (data.assumption ? `Assuming: ${data.assumption} ` : "") + data.answer.summary;
    }
    const title = STATUS_TEXT[data.status] || "No answer";
    const notSaid = (data.missing || []).filter((m) => !(data.reason || "").includes(m));
    const missing = notSaid.length ? ` Missing: ${notSaid.join(", ")}.` : "";
    return `${heard}${title}. ${data.reason || "I could not build a query I can trust, so I give no figure."}${missing}`;
  }

  function setListenButton(btn, on) {
    if (!btn) return;
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.replaceChildren(svg(on ? ICON_STOP : ICON_SPEAK), document.createTextNode(on ? "Stop" : "Listen"));
  }

  function speak(text, btn) {
    if (!tts || !text) return;
    stopSpeaking();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US";
    const voice = englishVoice();
    if (voice) u.voice = voice;
    u.rate = 1.02;
    u.onend = u.onerror = () => { if (speaking === btn) { setListenButton(btn, false); speaking = null; } };
    speaking = btn;
    setListenButton(btn, true);
    tts.speak(u);
  }

  function stopSpeaking() {
    if (!tts) return;
    tts.cancel();
    if (speaking) setListenButton(speaking, false);
    speaking = null;
  }

  const autoSpeak = () => speakBtn.getAttribute("aria-pressed") === "true";
  if (!tts) {
    speakBtn.hidden = true;
  } else {
    speakBtn.setAttribute("aria-pressed", store.get(SPEAK_KEY) === "on" ? "true" : "false");
    speakBtn.addEventListener("click", () => {
      const on = !autoSpeak();
      speakBtn.setAttribute("aria-pressed", on ? "true" : "false");
      speakBtn.title = on ? "Answers are read aloud" : "Read answers aloud";
      store.set(SPEAK_KEY, on ? "on" : "off");
      if (!on) stopSpeaking();
    });
    tts.getVoices();                       // charge la liste des voix (asynchrone sur Chrome)
  }

  // ── Saisie ──────────────────────────────────────────────────────────────────
  const fitInput = () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
    sendBtn.disabled = busy || !input.value.trim();
  };
  input.addEventListener("input", fitInput);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (q) ask(q, { origin: "Typed" });
  });

  moreBtn.addEventListener("click", () => {
    const open = !examples.classList.contains("open");
    examples.classList.toggle("open", open);
    moreBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  examples.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    examples.classList.remove("open");
    moreBtn.setAttribute("aria-expanded", "false");
    ask(b.textContent, { origin: "Example" });
  }));

  function setBusy(state) {
    busy = state;
    micBtn.disabled = state && !recorder;
    fitInput();
  }

  // ── Question → réponse ──────────────────────────────────────────────────────
  async function ask(question, { origin, voice } = {}) {
    if (busy) return;
    stopSpeaking();
    document.body.classList.remove("is-empty");
    input.value = "";
    setBusy(true);

    const turn = template.content.firstElementChild.cloneNode(true);
    $(".you-text", turn).textContent = question;
    $(".you-origin", turn).textContent = voice ? `Spoken · ${fmtMs(voice.audio_ms)} of audio` : origin || "";
    thread.append(turn);
    turn.scrollIntoView({ behavior: "smooth", block: "start" });

    const started = performance.now();
    const workingText = $(".working-text", turn);
    const workingTime = $(".working-time", turn);
    const timer = setInterval(() => {
      const ms = performance.now() - started;
      workingTime.textContent = fmtMs(ms);
      for (const [at, text] of WORKING_HINTS) if (ms >= at) workingText.textContent = text;
    }, 200);

    let data;
    try {
      const resp = await fetch("/api/v1/ask", {
        method: "POST", headers: withKey({ "Content-Type": "application/json" }),
        body: JSON.stringify({ question, account_id: ACCOUNT_ID }),
      });
      data = await resp.json();
      if (!resp.ok && data.code) {
        handleAccessRefusal(data);
        data = { status: "refused", reason: data.error };
      } else if (!resp.ok && !data.status) {
        data = { status: "error", reason: data.error || `HTTP ${resp.status}` };
      }
      showRemaining(data.access);
    } catch {
      data = { status: "error", reason: "The service did not answer. Check that the API is running, then ask again." };
    } finally {
      clearInterval(timer);
      setBusy(false);
    }
    const listen = render(turn, data, voice);
    $(".reply", turn).scrollIntoView({ behavior: "smooth", block: "start" });
    // Lu automatiquement si la question a été dictée, ou si la lecture est activée
    if (listen && (voice || autoSpeak())) speak(speechText(data, voice), listen);
    if (!matchMedia("(pointer: coarse)").matches) input.focus();
  }

  function render(turn, data, voice) {
    const body = $(".reply-body", turn);
    body.replaceChildren();
    if (data.rewritten) {
      body.append(el("p", { class: "understood" }, el("strong", { text: "Understood as: " }), data.rewritten));
    }
    if (data.assumption) {
      body.append(el("p", { class: "assumption" }, el("strong", { text: "Assumed: " }), data.assumption));
    }
    if (data.status === "ok" && data.answer) {
      body.append(el("p", { class: "summary", text: data.answer.summary }));
      if (data.answer.columns && data.answer.columns.length) body.append(resultTable(data.answer));
    } else {
      const title = STATUS_TEXT[data.status] || "No answer";
      const notice = el("div", { class: "notice" }, el("strong", { text: title }),
        el("span", { text: data.reason || "The system could not build a query it can trust, so it gives no figure." }));
      const notSaid = (data.missing || []).filter((m) => !(data.reason || "").includes(m));
      if (notSaid.length) notice.append(el("ul", {}, ...notSaid.map((m) => el("li", { text: m }))));
      body.append(notice);
    }
    const listen = renderTools(turn, data, voice);
    renderTrace(turn, data, voice);
    renderHow(turn, data);
    return listen;
  }

  function resultTable(answer) {
    const numeric = answer.columns.map((_, i) => answer.rows.length > 0 &&
      answer.rows.every((r) => r[i] === null || isNumber(r[i])) && answer.rows.some((r) => isNumber(r[i])));
    const thead = el("thead", {}, el("tr", {}, ...answer.columns.map((c, i) =>
      el("th", { class: numeric[i] ? "num" : "", scope: "col", text: human(c) }))));
    const rows = answer.rows.length
      ? answer.rows.map((r) => el("tr", {}, ...r.map((v, i) =>
        el("td", { class: (numeric[i] ? "num" : "") + (v === null ? " empty" : ""), text: fmtCell(v) }))))
      : [el("tr", {}, el("td", { class: "empty", colspan: String(answer.columns.length || 1), text: "No rows" }))];
    return el("div", { class: "table-wrap", tabindex: "0" }, el("table", {}, thead, el("tbody", {}, ...rows)));
  }

  function renderTools(turn, data, voice) {
    const tools = $(".reply-tools", turn);
    const items = [];
    let listen = null;
    if (tts) {
      listen = el("button", { type: "button", class: "chip", "aria-pressed": "false" });
      setListenButton(listen, false);
      listen.addEventListener("click", () => (speaking === listen ? stopSpeaking() : speak(speechText(data, voice), listen)));
      items.push(listen);
    }
    const answer = data.status === "ok" && data.answer;
    if (answer && answer.rows.length) {
      const download = el("button", { type: "button", class: "chip" }, svg(ICON_DOWNLOAD), "CSV");
      download.addEventListener("click", () => {
        const esc = (v) => (v === null || v === undefined ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
        const csv = [answer.columns, ...answer.rows].map((r) => r.map(esc).join(",")).join("\n");
        const a = el("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })),
          download: `${$(".you-text", turn).textContent.slice(0, 60).replace(/[^\w]+/g, "_")}.csv` });
        a.click();
        URL.revokeObjectURL(a.href);
      });
      items.push(download);
    }
    if (answer) {
      items.push(el("span", { class: "count", text:
        `${answer.row_count} ${answer.row_count === 1 ? "row" : "rows"}${answer.truncated ? ", first rows only" : ""}` }));
    }
    if (items.length) { tools.replaceChildren(...items); tools.hidden = false; }
    return listen;
  }

  function renderTrace(turn, data, voice) {
    const timings = Object.assign({}, data.timings_ms || {});
    if (voice) timings.transcribe = voice.elapsed_ms;
    const steps = STEPS.filter(([key]) => timings[key] > 0).map(([key, label, color]) => ({ label, color, ms: timings[key] }));
    const trace = $(".trace", turn);
    if (!steps.length) { trace.hidden = true; return; }
    const total = steps.reduce((s, x) => s + x.ms, 0);
    const bar = $(".trace-bar", trace);
    bar.setAttribute("aria-label", `Time per step: ${steps.map((s) => `${s.label} ${fmtMs(s.ms)}`).join(", ")}`);
    bar.replaceChildren(...steps.map((s) => el("span", {
      style: `width:${(100 * s.ms / total).toFixed(2)}%;background:var(${s.color})`, title: `${s.label}: ${fmtMs(s.ms)}`,
    })));
    $(".trace-steps", trace).replaceChildren(...steps.map((s) =>
      el("li", {}, el("i", { style: `background:var(${s.color})` }), `${s.label} `, el("b", { text: fmtMs(s.ms) }))));
    const usage = data.usage || {};
    const tokens = (u) => (u ? (u.tokens_in || 0) + (u.tokens_out || 0) : 0);
    const cost = (data.cost_usd || 0) + (voice ? voice.cost_usd || 0 : 0);
    const totals = [
      ["Total time", fmtMs(((data.timings_ms && data.timings_ms.total) || 0) + (voice ? voice.elapsed_ms : 0)), null],
      ["Tokens", (tokens(usage.prep) + tokens(usage.judge) + tokens(usage.sql)).toLocaleString("en-US"),
        usage.sql ? `${(usage.sql.tokens_cached || 0).toLocaleString("en-US")} cached` : null],
      ["Cost", fmtUsd(cost), null],
      ["Models", [usage.prep && short(usage.prep.model), usage.sql && short(usage.sql.model)].filter(Boolean).join(" + ") || "—", null],
    ];
    $(".trace-totals", trace).replaceChildren(...totals.map(([k, v, note]) =>
      el("div", {}, el("dt", { text: k }),
        el("dd", { class: k === "Models" ? "models" : "" }, v, note ? el("small", { text: ` ${note}` }) : null))));
  }

  function renderHow(turn, data) {
    const blocks = [];
    const sr = data.structured_request;
    if (sr) {
      const pieces = [];
      for (const [k, v] of Object.entries(sr)) {
        if (!v || (Array.isArray(v) && !v.length) || ["status", "reason"].includes(k)) continue;
        if (k === "entities") {
          for (const [piece, owner] of Object.entries(v)) pieces.push(el("span", {}, el("em", { text: "entity" }), `${piece} → ${owner}`));
          continue;
        }
        for (const item of Array.isArray(v) ? v : [v]) pieces.push(el("span", {}, el("em", { text: human(k) }), String(item)));
      }
      if (pieces.length) blocks.push(el("div", {}, el("h3", { text: "How the question was understood" }), el("div", { class: "pieces" }, ...pieces)));
    }
    const choice = data.subject_choice;
    if (choice && choice.table) {
      const how = { judge: "chosen by the judge (Muse Spark)", header: "the document behind its lines",
        forced: "chosen by the judge (Muse Spark)", vectors: "closest match" }[choice.by] || choice.by;
      blocks.push(el("div", {}, el("h3", { text: "Main table" }),
        el("p", { text: `${choice.table}: ${how}` + (choice.reason ? ` — ${choice.reason}` : "") })));
    }
    const sql = data.executed_sql || data.sql;
    if (sql) blocks.push(el("div", {}, el("h3", { text: data.executed_sql ? "Query that ran (read-only)" : "Query written, not run" }), el("pre", { class: "sql", text: sql })));
    if (data.tables_used && data.tables_used.length) {
      blocks.push(el("div", {}, el("h3", { text: "Tables sent to the model" }), el("p", { text: data.tables_used.join(", ") })));
    }
    const refused = (data.checks || []).filter((c) => c.error);
    if (refused.length) blocks.push(el("div", {}, el("h3", { text: "Corrections" }), ...refused.map((c) => el("p", { text: c.error }))));
    if (data.missing_report && data.missing_report.length) {
      blocks.push(el("div", {}, el("h3", { text: "Data the model asked for" }),
        ...data.missing_report.map((r) => el("p", { text: `${r.text}: ${r.found ? [r.table, r.column].filter(Boolean).join(".") : "not found"}` }))));
    }
    const how = $(".how", turn);
    $(".how-body", how).replaceChildren(...blocks);
    how.hidden = !blocks.length && $(".trace", turn).hidden;
  }

  // ── Question dictée ─────────────────────────────────────────────────────────
  let recorder = null;
  let chunks = [];
  let guard = null;
  let discard = false;

  function showVoice(text, isError = false) {
    voiceState.hidden = !text;
    voiceState.textContent = text || "";
    voiceState.classList.toggle("error", isError);
  }

  micBtn.addEventListener("click", () => (recorder ? stopRecording(false) : startRecording()));

  async function startRecording() {
    if (busy) return;
    stopSpeaking();
    if (!navigator.mediaDevices || !window.MediaRecorder) {
      showVoice("Voice input needs a recent browser and a page opened on localhost or https.", true);
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch {
      showVoice("Microphone access was refused. Allow it in the browser, then try again.", true);
      return;
    }
    chunks = [];
    recorder = new MediaRecorder(stream);
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = () => { stream.getTracks().forEach((t) => t.stop()); handleRecording(); };
    recorder.start();
    micBtn.setAttribute("aria-pressed", "true");
    $("#mic-label").textContent = "Stop and ask";
    sendBtn.disabled = true;
    discard = false;
    // Arrêt automatique : silence après la parole, durée maximale, ou aucun son (sans envoi) — limites de .env
    guard = window.AYDVoiceGuard.watch(stream, {
      onTick: (ms, lim, heard) => showVoice(heard
        ? `Listening ${fmtMs(ms)} · sends after ${lim.silence_ms / 1000} s of silence (max ${lim.max_seconds} s)`
        : `Listening ${fmtMs(ms)} · speak now`),
      onStop: (reason) => stopRecording(reason === "no_speech"),
    });
  }

  function stopRecording(cancel = false) {
    if (!recorder) return;
    if (guard) { guard.stop(); guard = null; }
    discard = cancel === true;
    micBtn.setAttribute("aria-pressed", "false");
    $("#mic-label").textContent = "Start speaking";
    const r = recorder;
    recorder = null;
    r.stop();
  }

  async function handleRecording() {
    if (discard) {
      showVoice("Nothing was heard, so nothing was sent. Tap the microphone and speak.", true);
      return;
    }
    setBusy(true);
    showVoice("Turning your voice into text");
    let result;
    try {
      const wav = await toWav16k(new Blob(chunks, { type: chunks[0] ? chunks[0].type : "audio/webm" }));
      const fd = new FormData();
      fd.append("audio", wav, "question.wav");
      const resp = await fetch("/api/v1/transcribe", { method: "POST", body: fd, headers: withKey() });
      result = await resp.json();
      if (!resp.ok) { handleAccessRefusal(result); throw new Error(result.error || `HTTP ${resp.status}`); }
    } catch (err) {
      setBusy(false);
      showVoice(`Voice could not be turned into text: ${err.message}`, true);
      return;
    }
    setBusy(false);
    if (!result.text) {
      showVoice("Nothing was heard. Speak a little closer to the microphone and try again.", true);
      return;
    }
    showVoice("");
    ask(result.text, { voice: result });
  }

  // Enregistrement du navigateur (webm, ogg…) → WAV mono 16 bits 16 kHz, format attendu par Muse Voice Transcribe
  async function toWav16k(blob) {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    ctx.close();
    const rate = 16000;
    const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
    const src = offline.createBufferSource();
    src.buffer = decoded;
    src.connect(offline.destination);
    src.start();
    const samples = (await offline.startRendering()).getChannelData(0);
    const buffer = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(buffer);
    const text = (off, s) => { for (let i = 0; i < s.length; i++) view.setUint8(off + i, s.charCodeAt(i)); };
    text(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true); text(8, "WAVE");
    text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    text(36, "data"); view.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buffer], { type: "audio/wav" });
  }
})();
