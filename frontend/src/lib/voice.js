// Microphone in, speech out: the browser half of app/voice.py. Audio lives here for echo cancellation:
// the console's voice must play through this AudioContext to be in the cancellation reference, or a reply
// that says the wake phrase summons itself. Frames leave only for localhost and only while armed.
// Design: DESIGN.md → Voice.
import { timer, withTimeout } from "./timers.js";

const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/voice`;

const SAMPLE_RATE = 16000;
const FRAME = 480;                  // 30ms, what the backend's VAD wants
const PLAY_IDLE_MS = 900;           // how long a gap between sentences may be

// Audio thread: 128-sample quanta into 30ms signed-16-bit PCM frames. A blob URL, since a worklet loads from a URL.
const WORKLET_SRC = `
class MicFrames extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(${FRAME}); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === ${FRAME}) {
        const pcm = new Int16Array(${FRAME});
        for (let j = 0; j < ${FRAME}; j++) {
          const s = Math.max(-1, Math.min(1, this.buf[j]));
          pcm[j] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        this.port.postMessage(pcm.buffer, [pcm.buffer]);
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("mic-frames", MicFrames);
`;

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

export function createVoice({ onEvent }) {
  let ctx = null;             // one AudioContext: mic in and speech out share it
  let stream = null;
  let node = null;
  let ws = null;
  let starting = null;

  let wake = false;           // wake spotting armed (mic stays open)
  let ptt = false;            // talk button held
  const queue = [];           // pending speech, in the order it was written
  let source = null;          // what's playing right now
  // `source` can't be the busy flag: it is set inside decodeAudioData's callback, so a sentence arriving
  // between shift() and start() would fork a second playback chain (Kokoro makes bursts common).
  let decoding = false;       // a chunk is between shift() and start()
  let generation = 0;         // bumped to strand decodes that stopSpeech outran
  let playing = false;
  const idleTimer = timer();  // grace period before the mouth counts as shut

  const emit = (ev) => { try { onEvent(ev); } catch { /* never break the audio path */ } };

  // Frames go out only when something listens: the difference between mic available and mic transmitting.
  const armed = () => wake || ptt || playing;

  // Speech out needs only an output context: turning it on must not light the recording indicator.
  async function ensureCtx() {
    if (!ctx) {
      ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      await ctx.resume();
    }
    return ctx;
  }

  async function start() {
    if (ws && ws.readyState === WebSocket.OPEN && node) return;
    if (starting) return starting;
    starting = (async () => {
      // Raced against a clock: a WKWebView with no capture-permission delegate never settles this promise.
      stream = await withTimeout(
        navigator.mediaDevices.getUserMedia({
          audio: {
            echoCancellation: true,   // the console's own voice, removed
            noiseSuppression: true,
            autoGainControl: true,
            channelCount: 1,
          },
        }),
        10000, "the request was never answered");
      await ensureCtx();
      const url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: "application/javascript" }));
      await ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);

      node = new AudioWorkletNode(ctx, "mic-frames", { numberOfOutputs: 0 });
      node.port.onmessage = (e) => {
        if (armed() && ws && ws.readyState === WebSocket.OPEN) ws.send(e.data);
      };
      ctx.createMediaStreamSource(stream).connect(node);

      await new Promise((resolve, reject) => {
        ws = new WebSocket(WS_URL);
        ws.binaryType = "arraybuffer";
        ws.onopen = () => { ws.send(JSON.stringify({ type: "mode", mode: wake ? "wake" : "off" })); resolve(); };
        ws.onerror = () => reject(new Error("voice socket failed"));
        ws.onclose = () => emit({ type: "closed" });
        ws.onmessage = (e) => emit(JSON.parse(e.data));
      });
    })().finally(() => { starting = null; });
    return starting;
  }

  function stop() {
    idleTimer.clear();
    playing = false;
    generation++;             // a decode outstanding across a teardown is dead
    decoding = false;
    ws?.close();
    node?.disconnect();
    stream?.getTracks().forEach((t) => t.stop());
    ctx?.close();
    ws = node = stream = ctx = null;
    emit({ type: "listening", on: false });
  }

  // Shut the mic only when nothing else wants it (push-to-talk ending must not cut a reply you may talk over).
  function releaseIfIdle() {
    if (!wake && !ptt && !playing) stop();
  }

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  async function setWake(on) {
    wake = on;
    if (on) await start();
    send({ type: "mode", mode: on ? "wake" : "off" });
    if (!on) releaseIfIdle();
  }

  async function pttDown() {
    ptt = true;
    await start();
    if (!ptt) return;           // released before the mic was ready
    send({ type: "ptt", down: true });
  }

  function pttUp() {
    if (!ptt) return;
    ptt = false;
    send({ type: "ptt", down: false });
  }

  // Tells the backend a wake hit is an interruption. Going quiet is delayed (PLAY_IDLE_MS): sentences arrive
  // with gaps, and flapping tore the audio context down mid-reply. `now` is for a real stop.
  function setPlaying(on, now = false) {
    idleTimer.clear();
    if (on) {
      if (playing) return;
      playing = true;
    } else if (!playing) {
      return;
    } else if (!now) {
      idleTimer.start(() => setPlaying(false, true), PLAY_IDLE_MS);
      return;
    } else {
      playing = false;
    }
    send({ type: "speaking", on: playing });
    emit({ type: "speaking", on: playing });
    if (!playing) releaseIfIdle();
  }

  function playNext() {
    if (decoding) return;       // the chain is already moving; don't fork it
    const wav = queue.shift();
    if (!wav) { source = null; setPlaying(false); return; }
    decoding = true;
    const gen = generation;
    const done = (fn) => (arg) => {
      // A decode in flight across stopSpeech belongs to a finished reply: don't play it.
      if (gen !== generation) return;
      decoding = false;
      if (ctx) fn(arg);
    };
    ctx.decodeAudioData(wav.slice(0).buffer, done((buf) => {
      source = ctx.createBufferSource();
      source.buffer = buf;
      source.connect(ctx.destination);
      source.onended = () => { if (source) playNext(); };
      source.start();
    }), done(() => playNext()));
  }

  // Per sentence as the reply streams; the queue keeps order (async decode would reorder short ones).
  async function enqueueSpeech(b64) {
    // Barge-in needs the mic open; with wake off, speech out is output only.
    if (wake) await start(); else await ensureCtx();
    queue.push(b64ToBytes(b64));
    setPlaying(true);
    if (!source && !decoding) playNext();
  }

  function stopSpeech() {
    queue.length = 0;
    generation++;
    decoding = false;
    if (source) {
      const s = source;
      source = null;              // so onended doesn't chain into the next one
      s.onended = null;
      try { s.stop(); } catch { /* already ended */ }
    }
    setPlaying(false, true);
  }

  // Sound for silent work (a four-second provider call is dead air in the room).
  async function earcon() {
    await ensureCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const t = ctx.currentTime;
    osc.frequency.setValueAtTime(660, t);
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.06, t + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t);
    osc.stop(t + 0.13);
  }

  return { start, stop, setWake, pttDown, pttUp, enqueueSpeech, stopSpeech, earcon,
           isOpen: () => !!ws };
}
