// Garde du micro, commune aux deux interfaces : une question dictée s'arrête toute seule.
//   - silence après la parole (VOICE_SILENCE_MS)  → arrêt et envoi ;
//   - durée maximale (VOICE_MAX_SECONDS)          → arrêt et envoi ;
//   - aucun son au début (VOICE_NO_SPEECH_MS)     → arrêt sans envoi (rien n'est transcrit ni facturé).
// Les trois limites viennent de .env, via /api/v1/health (voice.recording). Le volume est mesuré dans le
// navigateur (Web Audio), sans modèle.
window.AYDVoiceGuard = (() => {
  "use strict";

  // Secours si /health ne répond pas (mêmes valeurs que .env.example)
  let limits = { max_seconds: 20, silence_ms: 2000, no_speech_ms: 5000 };
  // Volume (RMS, 0 à 1) au-dessus duquel on considère que la personne parle
  const SPEECH_LEVEL = 0.02;
  const STEP_MS = 100;

  fetch("/api/v1/health").then((r) => r.json()).then((h) => {
    const rec = h && h.voice && h.voice.recording;
    if (rec) limits = { max_seconds: rec.max_seconds, silence_ms: rec.silence_ms, no_speech_ms: rec.no_speech_ms };
  }).catch(() => { /* secours ci-dessus */ });

  // onStop(reason) : "silence" | "max" | "no_speech" ; onTick(elapsedMs, limits, heard)
  function watch(stream, { onStop, onTick }) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) {
      const t = setTimeout(() => onStop("max"), limits.max_seconds * 1000);
      return { stop: () => clearTimeout(t) };
    }
    const ctx = new AudioCtx();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const started = performance.now();
    let heard = false;
    let lastVoice = started;
    let done = false;

    const timer = setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
      const now = performance.now();
      if (Math.sqrt(sum / samples.length) > SPEECH_LEVEL) { heard = true; lastVoice = now; }
      const elapsed = now - started;
      if (onTick) onTick(elapsed, limits, heard);
      let reason = null;
      if (!heard && elapsed >= limits.no_speech_ms) reason = "no_speech";
      else if (heard && now - lastVoice >= limits.silence_ms) reason = "silence";
      else if (elapsed >= limits.max_seconds * 1000) reason = "max";
      if (reason) { stop(); onStop(reason); }
    }, STEP_MS);

    function stop() {
      if (done) return;
      done = true;
      clearInterval(timer);
      ctx.close().catch(() => {});
    }
    return { stop };
  }

  return { watch, limits: () => limits };
})();
