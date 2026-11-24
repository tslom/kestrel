"""Voice for the console: ears (parakeet-mlx) and mouth (Kokoro, `say` fallback), both local.

Audio stays local; the model only sees the transcript, along the path a typed
message takes. Nothing is stored. Three stages guard the 600M-parameter ASR
model: Silero VAD -> openWakeWord -> parakeet, so nothing becomes text until
the console is summoned. Design: DESIGN.md → Voice.

Wire protocol (JSON + binary over WS /ws/voice):
  client -> server:
    binary frames: 16kHz mono signed 16-bit PCM, 480 samples (30ms) each
    {"type": "mode", "mode": "off"|"wake"}   -- arm/disarm wake spotting
    {"type": "ptt", "down": true|false}      -- push-to-talk button
    {"type": "speaking", "on": true|false}   -- the console is talking now
  server -> client:
    {"type": "ready", "wake_word": "hey kestrel"}   -- the phrase, spelled to be read
    {"type": "wake"}                         -- summoned; listening for a turn
    {"type": "listening", "on": bool, "source": "ptt"|"wake"}
    {"type": "barge_in"}                     -- summoned while it was talking
    {"type": "transcript", "text": "...", "source": ..., "send": bool}
    {"type": "status", "text": "..."}

A `ptt` transcript goes to the composer, a `wake` one is sent, `barge_in`
interrupts. The wake word is the only thing that opens the mic, mid-reply too.
"""

import asyncio
import re
import shutil
import subprocess
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from .config import (VOICE_MAX_UTTERANCE_S,
                     VOICE_MIN_SPEECH_MS, VOICE_SETTLED_SPEECH_MS,
                     VOICE_SILENCE_MS, VOICE_SILENCE_PATIENT_MS, VOICE_STT_MODEL,
                     VOICE_TTS_ENGINE, VOICE_TTS_KOKORO_MODEL,
                     VOICE_TTS_KOKORO_SPEED, VOICE_TTS_KOKORO_VOICE,
                     VOICE_TTS_RATE, VOICE_TTS_VOICE, VOICE_TTS_VOICE_PREFS,
                     VOICE_VAD_THRESHOLD, VOICE_WAKE_THRESHOLD,
                     VOICE_WAKE_TIMEOUT_S, VOICE_WAKE_WORD)

router = APIRouter()

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 480          # 30ms — silero's frame, and the client's
FRAME_MS = 1000 * FRAME_SAMPLES // SAMPLE_RATE
WAKE_CHUNK = 1280            # 80ms — what openWakeWord wants at a time


# ---------- which phrase summons it ----------
# VOICE_WAKE_WORD is a bundled name or a .onnx path; the phrase is the file's stem.

def wake_label(spec: str = VOICE_WAKE_WORD) -> str:
    """What to call the wake word out loud — for the UI, not for loading."""
    stem = Path(spec).stem if spec.endswith((".onnx", ".tflite")) else spec
    return stem.replace("_", " ")


def wake_ready(spec: str = VOICE_WAKE_WORD) -> bool:
    """Whether that spec will actually load (a missing trained model would otherwise
    fail at Listener construction); the status route checks first."""
    import importlib.util
    if importlib.util.find_spec("openwakeword") is None:
        return False
    if spec.endswith((".onnx", ".tflite")):
        return Path(spec).is_file()
    import openwakeword
    return spec in openwakeword.MODELS


# Prepended to push-to-talk captures only (the button lands after speech begins);
# wake capture skips it, since the audio before the trigger is the wake word.
PREROLL_FRAMES = 33          # ~1s

# One synthesized chunk; a paragraph handed over whole can't be interrupted.
SPEAK_MAX_CHARS = 600


# ---------- speech in (parakeet-mlx) ----------

_stt_model: Any = None

# One lock for every MLX graph evaluation, transcription and synthesis alike.
_mlx_lock = asyncio.Lock()


def _load_stt() -> Any:
    """Load the ASR model once, on first use, not at import (~2.5GB). Callers hold _mlx_lock."""
    global _stt_model
    if _stt_model is None:
        from parakeet_mlx import from_pretrained
        _stt_model = from_pretrained(VOICE_STT_MODEL)
    return _stt_model


def _transcribe_sync(pcm: np.ndarray) -> str:
    """float32 mono @16k -> text. Blocking; call it in a thread."""
    import mlx.core as mx
    from parakeet_mlx.audio import get_logmel

    model = _load_stt()
    # float32 required: get_logmel reuses the input dtype, and bfloat16 breaks the matmul.
    mel = get_logmel(mx.array(pcm.astype(np.float32)), model.preprocessor_config)
    results = model.generate(mel)
    return results[0].text.strip() if results else ""


async def transcribe(pcm: np.ndarray) -> str:
    """Transcribe one utterance, serialized on _mlx_lock (also covers the model load)."""
    if pcm.size < SAMPLE_RATE // 10:
        return ""
    async with _mlx_lock:
        return await asyncio.to_thread(_transcribe_sync, pcm)


# ---------- the gate (Silero VAD -> openWakeWord) ----------

class Listener:
    """Per-connection audio state machine. Sync (cheap enough for the event loop);
    `feed()` returns events rather than sending them, so it is testable without a socket."""

    def __init__(self, wake_word: str = VOICE_WAKE_WORD, barge_in: bool = True):
        from openwakeword.model import Model
        from openwakeword.vad import VAD

        self.vad = VAD()
        # onnx: no tflite-runtime wheel for Apple silicon.
        self.wake_model = Model(wakeword_models=[wake_word],
                                inference_framework="onnx")
        self.wake_word = wake_word
        # Off = deaf while the console talks.
        self.barge_in = barge_in

        self.mode = "off"           # "off" | "wake"
        self.speaking = False       # the console is talking through the speakers

        self.capturing = False
        self.source = ""            # what started this capture
        self.auto_end = True        # push-to-talk ends on the button, not on silence
        self.chunks: list[np.ndarray] = []
        self.captured_ms = 0
        self.speech_ms = 0
        self.silence_ms = 0

        self.preroll: deque[np.ndarray] = deque(maxlen=PREROLL_FRAMES)
        self.wake_buf = np.zeros(0, dtype=np.int16)

    # -- capture control (push-to-talk drives these directly) --

    def start_capture(self, source: str, *, preroll: bool = False,
                      auto_end: bool = True) -> None:
        self.capturing = True
        self.source = source
        self.auto_end = auto_end
        self.chunks = list(self.preroll) if preroll else []
        self.captured_ms = FRAME_MS * len(self.chunks)
        self.speech_ms = 0
        self.silence_ms = 0

    def silence_needed(self) -> int:
        """Pause length that ends the turn: PATIENT until VOICE_SETTLED_SPEECH_MS of
        *accumulated speech* (not elapsed time), then the short VOICE_SILENCE_MS.
        Push-to-talk ends on the button and never comes here."""
        return (VOICE_SILENCE_MS if self.speech_ms >= VOICE_SETTLED_SPEECH_MS
                else VOICE_SILENCE_PATIENT_MS)

    def _finish(self, reason: str) -> list[dict]:
        """End a capture; under VOICE_MIN_SPEECH_MS of speech the audio is dropped, not transcribed."""
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)
        speech_ms, source = self.speech_ms, self.source
        self.capturing = False
        self.chunks = []
        self.source = ""
        # Clear the spent phrase so it can't re-trigger next frame.
        self.wake_model.reset()
        self.wake_buf = np.zeros(0, dtype=np.int16)
        events: list[dict] = [{"type": "listening", "on": False}]
        if speech_ms >= VOICE_MIN_SPEECH_MS and audio.size:
            events.append({"type": "utterance", "pcm": audio,
                           "source": source, "reason": reason})
        elif reason != "cancel":
            events.append({"type": "status", "text": "Didn't catch anything."})
        return events

    def stop_capture(self) -> list[dict]:
        return self._finish("stopped") if self.capturing else []

    def cancel(self) -> list[dict]:
        return self._finish("cancel") if self.capturing else []

    # -- the frame path --

    def feed(self, frame: np.ndarray) -> list[dict]:
        """One 30ms int16 frame in, zero or more events out."""
        if frame.size != FRAME_SAMPLES:
            return []
        events: list[dict] = []
        f32 = frame.astype(np.float32) / 32768.0
        self.preroll.append(f32)

        if not self.capturing and self.mode == "off":
            return events

        speech = float(self.vad.predict(frame)) >= VOICE_VAD_THRESHOLD

        if self.capturing:
            self.chunks.append(f32)
            self.captured_ms += FRAME_MS
            if speech:
                self.speech_ms += FRAME_MS
                self.silence_ms = 0
            else:
                self.silence_ms += FRAME_MS
            if self.captured_ms >= VOICE_MAX_UTTERANCE_S * 1000:
                return events + self._finish("max")
            if self.auto_end:
                if self.speech_ms and self.silence_ms >= self.silence_needed():
                    return events + self._finish("silence")
                # Summoned into an empty room: give up.
                if not self.speech_ms and self.captured_ms >= VOICE_WAKE_TIMEOUT_S * 1000:
                    return events + self._finish("cancel")
            return events

        if self.speaking and not self.barge_in:
            return events

        # Wake spotting is the only way in, mid-reply too; echo cancellation (lib/voice.js) stops self-summoning.
        self.wake_buf = np.concatenate([self.wake_buf, frame])
        while self.wake_buf.size >= WAKE_CHUNK:
            chunk, self.wake_buf = self.wake_buf[:WAKE_CHUNK], self.wake_buf[WAKE_CHUNK:]
            scores = self.wake_model.predict(chunk)
            if max(scores.values(), default=0.0) >= VOICE_WAKE_THRESHOLD:
                self.wake_model.reset()
                self.wake_buf = np.zeros(0, dtype=np.int16)
                self.start_capture("wake")
                # Mid-reply: stop the mouth first (same event as the Stop button).
                barge = [{"type": "barge_in"}] if self.speaking else []
                return events + barge + [
                    {"type": "wake"},
                    {"type": "listening", "on": True, "source": "wake"}]
        return events


# ---------- speech out (macOS `say`) ----------

_FENCE = re.compile(r"```")
_SENTENCE_END = re.compile(r'(?<=[.!?])["\'”’)\]]*(\s|$)')

# Lines that are for the eyes only (tables, rules).
_UNSPEAKABLE_LINE = re.compile(r"^\s*(\||[-*_]{3,}\s*$)")

_INLINE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),            # images: nothing to say
    # citation/page markers are for the eye (read aloud: hex)
    (re.compile(r"\[(?:cite|page):[^\]\n]*\]"), ""),
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),        # links: the words, not the URL
    (re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]"), r"\2"),  # wikilink with an alias
    (re.compile(r"\[\[([^\]]+)\]\]"), r"\1"),
    (re.compile(r"`([^`]*)`"), r"\1"),
    (re.compile(r"\*\*([^*]+)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)"), r"\1"),
    (re.compile(r"~~([^~]+)~~"), r"\1"),
    (re.compile(r"https?://\S+"), "the link"),
    (re.compile(r"[←-⯿️\U0001f000-\U0001faff]"), ""),  # arrows, emoji
]


# ---------- speech normalization ----------
# Pronunciation fixes, not rewrites: each rule swaps a token for how it is read
# aloud (17% of 1094 real sentences hit a defect; misaki has no phoneme for `:`
# or `~`). Rationale and the verified-good list: DESIGN.md → Voice.

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}
_ORDINALS = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth",
             9: "ninth", 12: "twelfth", 20: "twentieth", 30: "thirtieth"}

_TIMEZONES = {
    "ET": "Eastern", "EST": "Eastern", "EDT": "Eastern",
    "CT": "Central", "CST": "Central", "CDT": "Central",
    "MT": "Mountain", "MST": "Mountain", "MDT": "Mountain",
    "PT": "Pacific", "PST": "Pacific", "PDT": "Pacific",
}
_WEEKDAYS = {
    "Mon": "Monday", "Tue": "Tuesday", "Tues": "Tuesday",
    "Wed": "Wednesday", "Thu": "Thursday", "Thur": "Thursday",
    "Thurs": "Thursday", "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday",
}
# Each is mispronounced as written ("Jul" -> ʤˈʌl); May needs no entry.
_MONTHS = {
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
    "Jun": "June", "Jul": "July", "Aug": "August", "Sep": "September",
    "Sept": "September", "Oct": "October", "Nov": "November",
    "Dec": "December",
}
# Units the lexicon spells out as initialisms ("T S P"); `tbsp` is already fine.
_UNITS = {
    "tsp": "teaspoon", "hr": "hour", "hrs": "hours",
    "min": "minute", "mins": "minutes",
}
# The leading group keeps a stuck-on count ("24hrs" -> "24 hours") and tells plural from "1 hr".
_UNIT = re.compile(r"(?<![A-Za-z])(\d+)?\s*(" + "|".join(
    sorted(_UNITS, key=len, reverse=True)) + r")(?![A-Za-z])")


def _spoken_unit(m: "re.Match[str]") -> str:
    count, word = m.group(1), _UNITS[m.group(2)]
    if count is None:
        return word
    if count != "1" and not word.endswith("s"):
        word += "s"
    return f"{count} {word}"
# Own rule: "w/Sam" has no letter boundary to anchor on.
_WITH = re.compile(r"(?<![A-Za-z])w/\s*(?=[A-Za-z])")
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December"]

# Capitalised only: lowercase `sun`/`sat`/`mar` are ordinary words.
_ABBREV = re.compile(r"\b(" + "|".join(
    sorted((*_WEEKDAYS, *_MONTHS), key=len, reverse=True)) + r")\b\.?")
_TZ = re.compile(r"\b(" + "|".join(_TIMEZONES) + r")\b")
# Real IANA regions only: any Capitalised/Capitalised pair would turn "Pull/Legs" into "Legs".
_TZ_ID = re.compile(r"\b(?:America|Europe|Asia|Africa|Australia|Pacific"
                    r"|Atlantic|Indian|Antarctica)/([A-Z][A-Za-z_]+)\b")
_ISO_DATE = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
# Whitespace stays inside the optional meridiem group, or "5:30 to 9" comes out "thirtyto".
_CLOCK = re.compile(r"\b(\d{1,2}):([0-5]\d)(?:\s*([ap])\.?m\.?\b)?",
                    re.IGNORECASE)
# Tight range dashes only; a spaced em dash is prose.
_RANGE_DASH = re.compile(r"(?<=[0-9m])[–—](?=[0-9]|noon\b|midnight\b)")
# Other tight dashes get spaced: misaki emits nothing between two alphanumerics.
_TIGHT_DASH = re.compile(r"(?<=[0-9A-Za-z])[–—](?=[0-9A-Za-z])")
_APPROX = re.compile(r"~(?=\d)")


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (" " + _ONES[ones] if ones else "")


def _ordinal_words(n: int) -> str:
    if n in _ORDINALS:
        return _ORDINALS[n]
    if n < 20:
        return _ONES[n] + "th"
    tens, ones = divmod(n, 10)
    return _TENS[tens] + " " + _ordinal_words(ones)


def _spoken_clock(m: "re.Match[str]") -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23:
        return m.group(0)           # not a time; a score or a ratio
    meridiem = (m.group(3) or "").lower() or ("p" if hour >= 12 else "")
    hour12 = hour % 12 or 12
    if minute == 0 and hour12 == 12 and meridiem:
        return "noon" if meridiem == "p" else "midnight"
    if minute == 0:
        said = _num_words(hour12) + " o'clock"
    elif minute < 10:
        said = _num_words(hour12) + " oh " + _ONES[minute]
    else:
        said = _num_words(hour12) + " " + _num_words(minute)
    if not m.group(3) and hour < 13:
        return said                 # bare "9:30" — don't invent a half of day
    if meridiem == "a":
        return said + " in the morning"
    return said + (" in the afternoon" if hour12 == 12 or hour12 < 6
                   else " in the evening")


def _normalize_speech(text: str) -> str:
    """Written forms in, spoken forms out. Rule order matters (DESIGN.md → Voice)."""
    text = _TZ_ID.sub(lambda m: m.group(1).replace("_", " "), text)
    text = _ISO_DATE.sub(
        lambda m: (f"{_MONTH_NAMES[int(m.group(2))]} "
                   f"{_ordinal_words(int(m.group(3)))}, {m.group(1)}"), text)
    # Before the dashes: "9am-~2" is a tight dash only once the tilde is a word.
    text = _APPROX.sub("about ", text)
    text = _RANGE_DASH.sub(" to ", text)
    text = _TIGHT_DASH.sub(" — ", text)
    text = _CLOCK.sub(_spoken_clock, text)
    text = _TZ.sub(lambda m: _TIMEZONES[m.group(1)], text)
    text = _ABBREV.sub(
        lambda m: _WEEKDAYS.get(m.group(1)) or _MONTHS[m.group(1)], text)
    text = _WITH.sub("with ", text)
    return _UNIT.sub(_spoken_unit, text)


def speakable(text: str) -> str:
    """Markdown in, speakable prose out ("" if none): drops visual-only text, then normalizes pronunciation."""
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        # Drop the fenced contents too, not just the ``` lines.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or _UNSPEAKABLE_LINE.match(line):
            continue
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)          # heading marks
        line = re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", line)     # bullets
        line = re.sub(r"^\s*>\s?", "", line)                   # quote marks
        for pattern, repl in _INLINE_RULES:
            line = pattern.sub(repl, line)
        line = line.strip()
        if line:
            out.append(line)
    return _normalize_speech(re.sub(r"\s+", " ", " ".join(out)).strip())


def take_sentences(buf: str, *, flush: bool = False) -> tuple[list[str], str]:
    """Split streamed reply text into (chunks, remainder). A sentence needs its terminator and
    the whitespace after; an unclosed fence holds everything after it. `flush` takes the rest."""
    chunks: list[str] = []
    if not flush:
        fences = [m.start() for m in _FENCE.finditer(buf)]
        if len(fences) % 2:                 # mid-fence: nothing past it is final
            head, buf = buf[:fences[-1]], buf[fences[-1]:]
        else:
            head, buf = buf, ""
    else:
        head, buf = buf, ""

    while head:
        m = _SENTENCE_END.search(head)
        if m and m.end() <= SPEAK_MAX_CHARS:
            chunks.append(head[:m.end()].strip())
            head = head[m.end():]
            continue
        if len(head) > SPEAK_MAX_CHARS:     # a sentence that never ends
            cut = head.rfind(" ", 0, SPEAK_MAX_CHARS)
            if cut <= 0:
                cut = SPEAK_MAX_CHARS
            chunks.append(head[:cut].strip())
            head = head[cut:]
            continue
        break

    if flush and head.strip():
        chunks.append(head.strip())
        head = ""
    return [c for c in chunks if c], head + buf


# `say -v '?'` line: name (may hold spaces/brackets), locale (not always xx_XX), `#`, sample.
_VOICE_LINE = re.compile(r"^(.*?)\s+([a-z]{2,3}(?:[-_][A-Za-z0-9]+)+)\s+#")

# A downloaded Premium/Enhanced build is listed beside the compact one.
_VARIANT_RANK = {"premium": 0, "enhanced": 1}

_tts_voice: str | None = None


def installed_voices() -> list[tuple[str, str]]:
    """(name, locale) for every voice `say` will accept, as it lists them."""
    if not shutil.which("say"):
        return []
    try:
        out = subprocess.run(["say", "-v", "?"], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    return [(m.group(1).strip(), m.group(2))
            for line in out.stdout.splitlines()
            if (m := _VOICE_LINE.match(line))]


def _best_variant(name: str, installed: list[tuple[str, str]]) -> str:
    """The best installed spelling of one voice name, or "": English first, then Premium > Enhanced > bare."""
    def rank(n: str) -> tuple[int, int]:
        return _VARIANT_RANK.get(n[len(name):].strip(" ()").lower(), 2), len(n)

    matches = [(n, loc) for n, loc in installed
               if n == name or n.startswith(name + " (")]
    english = [n for n, loc in matches if loc.startswith("en")]
    # Pinning a non-English voice by name is allowed; it just isn't preferred.
    return min(english or [n for n, _ in matches], key=rank, default="")


def kokoro_ready() -> bool:
    """Whether Kokoro can run, without loading it: needs both mlx_audio and misaki (G2P)."""
    import importlib.util
    return all(importlib.util.find_spec(m) is not None
               for m in ("mlx_audio", "misaki"))


def tts_voice() -> str:
    """The installed voice name to hand `say`, or "" for the system default. Cached: a new voice needs a restart.

    Resolved against the installed list because `say -v <absent>` exits 0 with a silent substitute.
    """
    global _tts_voice
    if _tts_voice is None:
        want = VOICE_TTS_VOICE.strip()
        installed = installed_voices()
        if not installed:                  # no `say`, or its output changed
            _tts_voice = "" if want.lower() == "auto" else want
        else:
            prefs = VOICE_TTS_VOICE_PREFS if want.lower() == "auto" else [want]
            _tts_voice = next(
                (v for v in (_best_variant(p, installed) for p in prefs) if v), "")
    return _tts_voice


# ---------- speech out: Kokoro-82M (mlx-audio, on Metal) ----------

_tts_model: Any = None

# The voice prefix sets the G2P language (b British, a American); derived so they can't drift.
_KOKORO_LANG = {"b": "b", "a": "a"}


def _kokoro_lang(voice: str) -> str:
    return _KOKORO_LANG.get(voice[:1], "a")


def _synthesize_kokoro_sync(text: str, voice: str) -> tuple[Any, int]:
    """Text -> (float32 samples, sample rate). Callers hold _mlx_lock."""
    global _tts_model
    if _tts_model is None:
        from mlx_audio.tts.utils import load_model
        _tts_model = load_model(VOICE_TTS_KOKORO_MODEL)
    segments = list(_tts_model.generate(
        text=text, voice=voice, speed=VOICE_TTS_KOKORO_SPEED,
        lang_code=_kokoro_lang(voice)))
    if not segments:
        raise RuntimeError("kokoro returned no audio")
    audio = np.concatenate([np.asarray(s.audio) for s in segments])
    return audio, segments[0].sample_rate


async def _synthesize_kokoro(text: str, voice: str) -> bytes | None:
    """One chunk -> WAV bytes, or None to fall back to `say`. Shares _mlx_lock; one chunk is ~0.25s."""
    import io

    import soundfile as sf
    try:
        async with _mlx_lock:
            audio, rate = await asyncio.to_thread(
                _synthesize_kokoro_sync, text, voice)
    except Exception:
        # Every failure is the same to the caller: fall back to `say`.
        return None
    buf = io.BytesIO()
    sf.write(buf, audio, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


_warm_task: asyncio.Task | None = None


def warm() -> None:
    """Pre-warm Kokoro in the background (first call ~2.4s, then ~0.15s). Fire-and-forget; never raises."""
    global _warm_task
    if (VOICE_TTS_ENGINE != "kokoro" or not kokoro_ready()
            or _tts_model is not None
            or (_warm_task is not None and not _warm_task.done())):
        return
    _warm_task = asyncio.create_task(
        _synthesize_kokoro("Ready.", VOICE_TTS_KOKORO_VOICE))


# ---------- speech out: the dispatch ----------

async def synthesize(text: str, voice: str | None = None,
                     engine: str | None = None) -> bytes | None:
    """One chunk of text -> WAV bytes (for decodeAudioData) via the configured engine; `voice`/`engine` override it for auditioning."""
    text = text.strip()
    if not text:
        return None
    if (engine or VOICE_TTS_ENGINE) == "kokoro":
        wav = await _synthesize_kokoro(text, voice or VOICE_TTS_KOKORO_VOICE)
        if wav:
            return wav
        if engine == "kokoro":     # asked for by name: don't silently swap
            return None
        voice = None               # a Kokoro voice name means nothing to `say`
    return await _synthesize_say(text, voice)


async def _synthesize_say(text: str, voice: str | None = None) -> bytes | None:
    """One chunk -> WAV bytes via macOS `say`, the no-model floor. Text goes via a file: a leading dash would parse as a flag."""
    text = text.strip()
    if not text or not shutil.which("say"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "say.txt", Path(tmp) / "say.wav"
        src.write_text(text[:SPEAK_MAX_CHARS], encoding="utf-8")
        cmd = ["say", "-f", str(src), "-o", str(dst),
               "--file-format=WAVE", "--data-format=LEI16@22050"]
        if (v := voice if voice is not None else tts_voice()):
            cmd[1:1] = ["-v", v]
        if VOICE_TTS_RATE:
            cmd[1:1] = ["-r", str(VOICE_TTS_RATE)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (asyncio.TimeoutError, OSError):
            return None
        if proc.returncode != 0 or not dst.exists():
            return None
        return dst.read_bytes()


# ---------- routes ----------

@router.get("/api/voice/status")
async def voice_status() -> dict:
    """What the browser needs to decide whether to offer a microphone."""
    import importlib.util
    return {
        "stt": importlib.util.find_spec("parakeet_mlx") is not None,
        "wake": wake_ready(),
        "tts": bool(shutil.which("say")) or kokoro_ready(),
        "tts_engine": VOICE_TTS_ENGINE if kokoro_ready() else "say",
        # The live engine names its own voice (no shared namespace).
        "tts_voice": (VOICE_TTS_KOKORO_VOICE
                      if VOICE_TTS_ENGINE == "kokoro" and kokoro_ready()
                      else tts_voice()),   # "" = whatever the system default is
        "wake_word": wake_label(),
        "stt_loaded": _stt_model is not None,
        "sample_rate": SAMPLE_RATE,
        "frame_samples": FRAME_SAMPLES,
    }


@router.post("/api/voice/speak")
async def voice_speak(body: dict) -> Response:
    """Speak a line of text. The console streams its own speech over the
    websocket; this is for one-off lines (and for checking the mouth works
    without saying anything to the model)."""
    # An explicit `voice`/`engine` bypasses the configured pair for this line
    # only, which is how you audition candidates without a restart apiece.
    picked, engine = body.get("voice"), body.get("engine")
    wav = await synthesize(speakable(str(body.get("text", ""))),
                           str(picked) if picked else None,
                           str(engine) if engine else None)
    if not wav:
        return JSONResponse({"spoken": False}, status_code=204)
    return Response(content=wav, media_type="audio/wav")


@router.websocket("/ws/voice")
async def voice_ws(ws: WebSocket) -> None:
    """Microphone in, events out. Audio only — the transcript leaves here on
    its way to the console, and the frames themselves are dropped."""
    await ws.accept()
    try:
        listener = Listener()
    except Exception as exc:   # models missing or unloadable
        await ws.send_json({"type": "status", "text": f"Voice unavailable: {exc}"})
        await ws.close()
        return

    lock = asyncio.Lock()      # transcription runs while frames keep arriving
    pending: set[asyncio.Task] = set()

    async def handle(events: list[dict]) -> None:
        for ev in events:
            if ev["type"] != "utterance":
                await ws.send_json(ev)
                continue
            # Transcription takes about a second; frames must keep being
            # read, or the socket's buffer becomes the recording.
            task = asyncio.create_task(finish(ev))
            pending.add(task)
            task.add_done_callback(pending.discard)

    async def finish(ev: dict) -> None:
        async with lock:
            await ws.send_json({"type": "status", "text": "Transcribing…"})
            try:
                text = await transcribe(ev["pcm"])
            except Exception as exc:
                await ws.send_json({"type": "status", "text": f"Transcription failed: {exc}"})
                return
        if not text:
            await ws.send_json({"type": "status", "text": "Didn't catch anything."})
            return
        # Push-to-talk lands in the composer to be read first; a summoned turn
        # was spoken *at* the console, so it goes.
        await ws.send_json({"type": "transcript", "text": text,
                            "source": ev["source"],
                            "send": ev["source"] == "wake"})

    await ws.send_json({"type": "ready", "wake_word": wake_label(listener.wake_word)})
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                frame = np.frombuffer(data, dtype="<i2")
                for i in range(0, frame.size - FRAME_SAMPLES + 1, FRAME_SAMPLES):
                    await handle(listener.feed(frame[i:i + FRAME_SAMPLES]))
                continue
            text = msg.get("text")
            if not text:
                continue
            try:
                import json
                payload = json.loads(text)
            except ValueError:
                continue
            kind = payload.get("type")
            if kind == "mode":
                mode = payload.get("mode")
                if mode in ("off", "wake"):
                    listener.mode = mode
                    if mode == "off" and listener.capturing and listener.source != "ptt":
                        await handle(listener.cancel())
            elif kind == "ptt":
                if payload.get("down"):
                    listener.start_capture("ptt", preroll=True, auto_end=False)
                    await ws.send_json({"type": "listening", "on": True, "source": "ptt"})
                else:
                    await handle(listener.stop_capture())
            elif kind == "speaking":
                listener.speaking = bool(payload.get("on"))
    except WebSocketDisconnect:
        pass
    finally:
        for task in list(pending):
            task.cancel()
