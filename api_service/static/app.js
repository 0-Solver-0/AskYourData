// AskYourData — interface : question écrite ou dictée → /api/v1/ask → réponse tirée de la base.
// Aucun chiffre n'est calculé ici : la page affiche les lignes renvoyées par la base.
(() => {
  "use strict";

  const ACCOUNT_ID = 1;
  const MAX_RECORDING_MS = 60000;

  const $ = (sel, root = document) => root.querySelector(sel);
  const form = $("#ask-form");
  const input = $("#question");
  const sendBtn = $("#send");
  const micBtn = $("#mic");
  const voiceState = $("#voice-state");
  const answers = $("#answers");
  const template = $("#answer-template");

  // Étapes du pipeline, dans l'ordre (clés de timings_ms)
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
  const human = (name) => {
    const t = String(name).replace(/_/g, " ").trim();
    return t.charAt(0).toUpperCase() + t.slice(1);
  };
  const isNumber = (v) => typeof v === "number" && Number.isFinite(v);
  const fmtNumber = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  const fmtCell = (v) => (v === null || v === undefined ? "—" : isNumber(v) ? fmtNumber.format(v) : typeof v === "boolean" ? (v ? "Yes" : "No") : String(v));
  const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.max(0, Math.round(ms))} ms`);
  const fmtUsd = (v) => `$${(v || 0).toFixed(4)}`;

  // ── Source et modèles ───────────────────────────────────────────────────────
  fetch("/api/v1/health").then((r) => r.json()).then((h) => {
    const db = h.execution && h.execution.db;
    const prep = h.llm && h.llm.prep && h.llm.prep.primary && h.llm.prep.primary.model;
    const sql = h.llm && h.llm.sql && h.llm.sql.primary && h.llm.sql.primary.model;
    const parts = [];
    if (db) parts.push(el("span", {}, "Odoo database ", el("strong", { text: db })));
    if (prep && sql) parts.push(el("span", {}, " with ", el("strong", { text: short(prep) }), " and ", el("strong", { text: short(sql) })));
    $("#source").replaceChildren(...(parts.length ? parts : [document.createTextNode("Database not reachable")]));
    if (h.execution && h.execution.status !== "ok") {
      $("#source").append(document.createTextNode(" (read-only access not configured)"));
    }
  }).catch(() => { $("#source").textContent = "Service not reachable"; });

  function short(model) {
    return String(model).split("/").pop();
  }

  // ── Question écrite ─────────────────────────────────────────────────────────
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (q) ask(q, { origin: "Typed" });
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  document.querySelectorAll(".examples button").forEach((b) =>
    b.addEventListener("click", () => { input.value = b.textContent; ask(b.textContent, { origin: "Example" }); }));

  function setBusy(state) {
    busy = state;
    sendBtn.disabled = state;
    micBtn.disabled = state && !recorder;
  }

  async function ask(question, { origin, voice } = {}) {
    if (busy) return;
    setBusy(true);
    const card = template.content.firstElementChild.cloneNode(true);
    $(".answer-question", card).textContent = question;
    $(".answer-origin", card).textContent = voice ? `Spoken, ${fmtMs(voice.audio_ms)} of audio` : origin || "";
    answers.prepend(card);
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });

    const started = performance.now();
    const workingText = $(".working-text", card);
    const workingTime = $(".working-time", card);
    const timer = setInterval(() => {
      const ms = performance.now() - started;
      workingTime.textContent = fmtMs(ms);
      for (const [at, text] of WORKING_HINTS) if (ms >= at) workingText.textContent = text;
    }, 200);

    let data;
    try {
      const resp = await fetch("/api/v1/ask", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, account_id: ACCOUNT_ID }),
      });
      data = await resp.json();
      if (!resp.ok && !data.status) data = { status: "error", reason: data.error || `HTTP ${resp.status}` };
    } catch (err) {
      data = { status: "error", reason: "The service did not answer. Check that the API is running, then ask again." };
    } finally {
      clearInterval(timer);
      setBusy(false);
    }
    render(card, data, voice);
  }

  // ── Rendu d'une réponse ─────────────────────────────────────────────────────
  const STATUS_TEXT = {
    cannot_answer: "This data is not in the database",
    refused: "This request cannot be run",
    clarification_needed: "The question needs to be more precise",
    missing: "Some data is missing",
    error: "No reliable answer",
  };

  function render(card, data, voice) {
    const body = $(".answer-body", card);
    body.replaceChildren();
    if (data.assumption) {
      body.append(el("p", { class: "assumption" }, el("strong", { text: "Assumed: " }), data.assumption));
    }
    if (data.status === "ok" && data.answer) {
      body.append(el("p", { class: "summary", text: data.answer.summary }));
      body.append(resultTable(data.answer));
      body.append(resultTools(data.answer, $(".answer-question", card).textContent));
    } else {
      const title = STATUS_TEXT[data.status] || "No answer";
      body.append(el("p", { class: "notice" }, el("strong", { text: title }),
        data.reason || "The system could not build a query it can trust, so it gives no figure."));
    }
    renderTrace(card, data, voice);
    renderHow(card, data);
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

  function resultTools(answer, question) {
    const count = `${answer.row_count} ${answer.row_count === 1 ? "row" : "rows"}${answer.truncated ? ", first rows only" : ""}`;
    const download = el("button", { type: "button", class: "link-button", text: "Download CSV" });
    download.addEventListener("click", () => {
      const esc = (v) => (v === null || v === undefined ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
      const csv = [answer.columns, ...answer.rows].map((r) => r.map(esc).join(",")).join("\n");
      const a = el("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })),
        download: `${question.slice(0, 60).replace(/[^\w]+/g, "_")}.csv` });
      a.click();
      URL.revokeObjectURL(a.href);
    });
    return el("div", { class: "result-tools" }, el("span", { text: count }), download);
  }

  function renderTrace(card, data, voice) {
    const timings = Object.assign({}, data.timings_ms || {});
    if (voice) timings.transcribe = voice.elapsed_ms;
    const steps = STEPS.filter(([key]) => timings[key] > 0).map(([key, label, color]) => ({ key, label, color, ms: timings[key] }));
    if (!steps.length) return;
    const total = steps.reduce((s, x) => s + x.ms, 0);
    const trace = $(".trace", card);
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
      ["Tokens", `${(tokens(usage.prep) + tokens(usage.sql)).toLocaleString("en-US")}`,
        usage.sql ? `${(usage.sql.tokens_cached || 0).toLocaleString("en-US")} from cache` : null],
      ["Cost", fmtUsd(cost), null],
      ["Models", [usage.prep && short(usage.prep.model), usage.sql && short(usage.sql.model)].filter(Boolean).join(" + ") || "—", null],
    ];
    $(".trace-totals", trace).replaceChildren(...totals.map(([k, v, note]) =>
      el("div", {}, el("dt", { text: k }),
        el("dd", { class: k === "Models" ? "models" : "" }, v, note ? el("small", { text: ` ${note}` }) : null))));
    trace.hidden = false;
  }

  function renderHow(card, data) {
    const blocks = [];
    const sr = data.structured_request;
    if (sr) {
      const pieces = [];
      for (const [k, v] of Object.entries(sr)) {
        if (!v || (Array.isArray(v) && !v.length) || ["status", "reason"].includes(k)) continue;
        for (const item of Array.isArray(v) ? v : [v]) pieces.push(el("span", {}, el("em", { text: human(k) }), String(item)));
      }
      if (pieces.length) blocks.push(el("div", {}, el("h3", { text: "How the question was understood" }), el("div", { class: "pieces" }, ...pieces)));
    }
    const choice = data.subject_choice;
    if (choice && choice.table) {
      const how = { judge: "chosen by the judge (Muse Spark)", header: "the document behind its lines",
        forced: "chosen by the judge (Muse Spark)", vectors: "closest match" }[choice.by] || choice.by;
      const text = `${choice.table}: ${how}` + (choice.reason ? ` — ${choice.reason}` : "")
        + (choice.by === "judge" && choice.vectors && choice.vectors !== choice.table ? ` (closest match was ${choice.vectors})` : "");
      blocks.push(el("div", {}, el("h3", { text: "Main table" }), el("p", { text })));
    }
    const sql = data.executed_sql || data.sql;
    if (sql) blocks.push(el("div", {}, el("h3", { text: data.executed_sql ? "Query that ran (read-only)" : "Query written, not run" }), el("pre", { class: "sql", text: sql })));
    if (data.tables_used && data.tables_used.length) {
      blocks.push(el("div", {}, el("h3", { text: "Tables sent to the model" }), el("p", { text: data.tables_used.join(", ") })));
    }
    const refused = (data.checks || []).filter((c) => c.error);
    if (refused.length) {
      blocks.push(el("div", {}, el("h3", { text: "Corrections" }),
        ...refused.map((c) => el("p", { text: c.error }))));
    }
    if (data.missing_report && data.missing_report.length) {
      blocks.push(el("div", {}, el("h3", { text: "Data the model asked for" }),
        ...data.missing_report.map((r) => el("p", { text: `${r.text}: ${r.found ? [r.table, r.column].filter(Boolean).join(".") : "not found"}` }))));
    }
    if (!blocks.length) return;
    const how = $(".how", card);
    $(".how-body", how).replaceChildren(...blocks);
    how.hidden = false;
  }

  // ── Question dictée ─────────────────────────────────────────────────────────
  let recorder = null;
  let chunks = [];
  let stopTimer = null;
  let recordStart = 0;
  let tick = null;

  function showVoice(text, isError = false) {
    voiceState.hidden = !text;
    voiceState.textContent = text || "";
    voiceState.classList.toggle("error", isError);
  }

  micBtn.addEventListener("click", () => (recorder ? stopRecording() : startRecording()));

  async function startRecording() {
    if (busy) return;
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
    recordStart = performance.now();
    micBtn.setAttribute("aria-pressed", "true");
    $("#mic-label").textContent = "Stop and ask";
    sendBtn.disabled = true;
    tick = setInterval(() => showVoice(`Listening ${fmtMs(performance.now() - recordStart)}. Click the microphone again when you are done.`), 200);
    stopTimer = setTimeout(stopRecording, MAX_RECORDING_MS);
  }

  function stopRecording() {
    if (!recorder) return;
    clearTimeout(stopTimer);
    clearInterval(tick);
    micBtn.setAttribute("aria-pressed", "false");
    $("#mic-label").textContent = "Start speaking";
    const r = recorder;
    recorder = null;
    r.stop();
  }

  async function handleRecording() {
    setBusy(true);
    showVoice("Turning your voice into text");
    let result;
    try {
      const wav = await toWav16k(new Blob(chunks, { type: chunks[0] ? chunks[0].type : "audio/webm" }));
      const fd = new FormData();
      fd.append("audio", wav, "question.wav");
      const resp = await fetch("/api/v1/transcribe", { method: "POST", body: fd });
      result = await resp.json();
      if (!resp.ok) throw new Error(result.error || `HTTP ${resp.status}`);
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
    input.value = result.text;
    showVoice(`Heard: “${result.text}”`);
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
