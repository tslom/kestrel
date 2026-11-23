// Microphone in, speech out — the browser half of app/voice.py.
//
// The audio lives in the browser for one reason beyond convenience: echo
// cancellation. The mic hears the speakers, and the wake word is spotted while
// the console is talking (that is how you interrupt it), so without
// cancellation a reply that reads the phrase aloud summons itself. getUserMedia's
// echoCancellation is the OS feature (macOS Voice Processing I/O) rather than
// a model, and it cancels what *the browser* is playing. So the console's own
// voice is played back through this same AudioContext instead of by the
// backend: rendering it here is what puts it in the reference signal. On
// headphones none of this matters and it costs nothing.
//
// Frames leave the machine's process only for localhost, and only while the
// mic is armed: with wake spotting off, nothing is transmitted unless you are
// holding the talk button or the console is mid-sentence (barge-in).
import { timer, withTimeout } from "./timers.js";

const WS_URL = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/voice`;

const SAMPLE_RATE = 16000;
const FRAME = 480;                  // 30ms, what the backend's VAD wants
const PLAY_IDLE_MS = 900;           // how long a gap between sentences may be

// Runs on the audio thread: buffers the 128-sample render quanta into 30ms
// frames and converts to the signed 16-bit PCM the backend reads. Shipped as
// a blob URL because a worklet must be loaded from a URL, and inlining it
// keeps the audio path in one file instead of a build-config special case.
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
  // `source` cannot double as the "busy" flag, because it is assigned inside
  // decodeAudioData's callback: between shifting a chunk off the queue and the
  // buffer actually starting, nothing is playing and nothing is marked
  // pending. A sentence arriving in that window would start a second, parallel
  // chain — two of them talking over each other, and then whichever lost the
  // race to assign `source` left an `onended` that chained into nothing, so
  // the reply stopped halfway. Kokoro is what made it common: synthesizing
  // several times faster than audio plays means sentences arrive in bursts,
  // and the window is exactly where they land.
  let decoding = false;       // a chunk is between shift() and start()
  let generation = 0;         // bumped to strand decodes that stopSpeech outran
  let playing = false;
  const idleTimer = timer();  // grace period before the mouth counts as shut

  const emit = (ev) => { try { onEvent(ev); } catch { /* never break the audio path */ } };

  // Frames are sent only when something is actually listening for them. With
  // wake off this is the difference between "the mic is available" and "the
  // mic is transmitting", which is the whole reason an open mic is livable.
  const armed = () => wake || ptt || playing;

  // Speech out needs an output context and nothing else. Keeping this separate
  // from start() is what lets "speak" be on with the mic shut: turning on audio
  // out must not light the browser's recording indicator.
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
      // Raced against a clock because "no answer" is a real outcome here, not
      // a hypothetical: inside a WKWebView whose UI delegate doesn't handle
      // capture permission, WebKit drops the request and this promise never
      // settles either way. Denial is easy to report; silence is what leaves
      // the mic button lit forever with nothing to show.
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

  // Closing the mic the moment push-to-talk ends would also close it in the
  // middle of a reply you might want to talk over, so it only shuts down when
  // nothing else still wants it.
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

  // The backend uses this to know a wake hit is an interruption rather than a
  // fresh summons — it keeps spotting the phrase either way, and echo
  // cancellation is what stops a synthesizer that reads the wake word aloud
  // from summoning the console.
  //
  // Going quiet is delayed because sentences arrive with gaps in them: the
  // next one is still being synthesized when the last finishes playing. With
  // the mic off, flapping this flag in those gaps tore the audio context down
  // between two halves of the same reply. `now` is for a real stop.
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
      // A decode that was in flight when stopSpeech ran belongs to a reply
      // that is over. Without the generation check it would start playing
      // *after* the stop, which is the same bug seen from the other side.
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

  // Called per sentence as the reply streams, so the queue is what keeps them
  // in order — decode is async and would otherwise reorder short sentences.
  async function enqueueSpeech(b64) {
    // Barge-in needs the mic open, so a reply spoken while wake is armed keeps
    // the whole path up; with the mic off, speech out is output only.
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

  // Something is happening that has no sound of its own: a four-second
  // provider call is a visible chip on screen and dead air in the room.
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
