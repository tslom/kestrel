"""Voice for the console — ears and mouth, both local.

Claude supplies neither end: content blocks are text, image, document,
thinking and the tool pair, so there is no audio in and no speech out to
switch on. Both transducers are ours, and both run here rather than in the
model: parakeet-mlx turns microphone audio into a transcript, macOS `say`
turns the reply into sound. **Audio local, reasoning remote** — the model
only ever sees the text, along exactly the path a typed message already
takes.

This does not reopen the attachment refusal. `providers/attachments.py`
answers a dropped audio file with "the model cannot listen to it" and that
stays right: nobody is present to catch a hallucinated transcript of a file.
A live microphone is the other case — the user is in the room and the
transcript lands in the composer to be read before it sends.

Nothing here is stored. Frames are transcribed and dropped, synthesized WAVs
end with the response that carried them; app.db gains no table.

Three stages guard the expensive one, so the 600M-parameter model is not
running all day:

    Silero VAD (~1MB ONNX, CPU)  ->  openWakeWord  ->  parakeet
    is this speech at all?           was that my name?  what did they say?

"Always listening" is then true of about a megabyte of ONNX, and *nothing
becomes text* until the console is summoned — which is the property that
makes an open microphone livable in a room with other people in it.

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

The client owns what happens next: a `ptt` transcript is dropped into the
composer to be read, a `wake` one is sent (`send: true`), and `barge_in`
triggers the interrupt the console already has.

The wake word is the *only* thing that opens the microphone, including while
the console is mid-reply — say it again and the speech stops and a new turn
starts. Nothing else can: not the room, and not the reply's own audio.
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
#
# openWakeWord ships six phrases and nothing else, so any name of our own is a
# model we trained (see docs/wake-word.md). Both arrive here the same way:
# VOICE_WAKE_WORD is either one of the bundled names or a path to a .onnx, and
# `Model(wakeword_models=[...])` takes either. The phrase itself is the file's
# stem, which is why the trained model is named for what you say to it.

def wake_label(spec: str = VOICE_WAKE_WORD) -> str:
    """What to call the wake word out loud — for the UI, not for loading."""
    stem = Path(spec).stem if spec.endswith((".onnx", ".tflite")) else spec
    return stem.replace("_", " ")


def wake_ready(spec: str = VOICE_WAKE_WORD) -> bool:
    """Whether that spec will actually load. A trained model that hasn't been
    copied over yet is the likely case, and it fails at Listener construction —
    i.e. when the socket opens, far from the config that caused it. The status
    route checks first so the client just doesn't offer the button."""
    import importlib.util
    if importlib.util.find_spec("openwakeword") is None:
        return False
    if spec.endswith((".onnx", ".tflite")):
        return Path(spec).is_file()
    import openwakeword
    return spec in openwakeword.MODELS


# Push-to-talk starts *after* the trigger, by which point some of the sentence
# is already gone — the button is pressed as you begin talking, not before it.
# A ring buffer of what was just heard is prepended so the utterance starts
# where the user did; a second of it, because leading room noise costs parakeet
# nothing. Wake capture deliberately skips it: there the audio just before
# the trigger is the wake word itself, which nobody wants in their transcript.
PREROLL_FRAMES = 33          # ~1s

# One synthesized chunk. `say` is fast, but a paragraph handed over whole is
# a paragraph you cannot interrupt for its duration.
SPEAK_MAX_CHARS = 600


# ---------- speech in (parakeet-mlx) ----------

_stt_model: Any = None

# One lock for every MLX graph evaluation in this module, transcription and
# synthesis alike. They are two models on one GPU and MLX evaluation is not
# something to run concurrently with itself; sharing the lock is what stops
# the console synthesising a reply into the middle of transcribing the
# turn that interrupted it.
_mlx_lock = asyncio.Lock()


def _load_stt() -> Any:
    """Load the ASR model once, on first use.

    Not at import: it is ~2.5GB on disk and a few seconds to load, and a
    backend that nobody talks to should pay neither. Callers hold _mlx_lock.
    """
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
    # float32 is required, not preferred: get_logmel views the FFT's complex
    # output back through the *input's* dtype to get magnitudes, so handing it
    # bfloat16 silently doubles the bin count and the filterbank matmul fails.
    mel = get_logmel(mx.array(pcm.astype(np.float32)), model.preprocessor_config)
    results = model.generate(mel)
    return results[0].text.strip() if results else ""


async def transcribe(pcm: np.ndarray) -> str:
    """Transcribe one utterance.

    Serialized on _mlx_lock, shared with synthesis: MLX evaluation is not
    something to run concurrently with itself, and there is one microphone
    anyway. The lock also covers the first-call model load, so two quick
    utterances can't both start one.
    """
    if pcm.size < SAMPLE_RATE // 10:
        return ""
    async with _mlx_lock:
        return await asyncio.to_thread(_transcribe_sync, pcm)


# ---------- the gate (Silero VAD -> openWakeWord) ----------

class Listener:
    """Per-connection audio state machine. Sync, and cheap enough to stay on
    the event loop: silero on a 30ms frame and openWakeWord on an 80ms chunk
    are together a low single-digit percentage of one core, against the cost
    of shuttling every frame through a thread.

    `feed()` returns events rather than sending them, so the state machine is
    testable without a socket — the transitions are the part with bugs in
    them, not the JSON.
    """

    def __init__(self, wake_word: str = VOICE_WAKE_WORD, barge_in: bool = True):
        from openwakeword.model import Model
        from openwakeword.vad import VAD

        self.vad = VAD()
        # onnx, not the default tflite: there is no tflite-runtime wheel for
        # Apple silicon, and the ONNX models ship in the same download.
        self.wake_model = Model(wakeword_models=[wake_word],
                                inference_framework="onnx")
        self.wake_word = wake_word
        # Whether the wake word may interrupt a reply in progress. Off makes
        # the console deaf for as long as it is talking.
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
        """How long a pause has to run before the turn counts as over.

        A single threshold has to serve two pauses that look identical to a
        VAD and mean opposite things: the one after you've finished, and the
        one in the middle of "so, um... the Ellis thread". Tuning one number
        trades cutting people off against making the console feel slow, and
        there is no setting that wins both.

        So it depends on how far into the utterance you are. Early on — still
        assembling the sentence — it waits VOICE_SILENCE_PATIENT_MS. Once
        VOICE_SETTLED_SPEECH_MS of actual speech has accumulated, a pause is
        much more likely to be a finished thought, and it drops to the short
        VOICE_SILENCE_MS so replies stay snappy where snappiness is free.

        Note this reads *accumulated speech*, not elapsed time: a long hunt
        for a word never advances it, which is exactly the case that used to
        get cut off. Push-to-talk doesn't come through here at all — it ends
        on the button, which remains the tool for composing out loud.
        """
        return (VOICE_SILENCE_MS if self.speech_ms >= VOICE_SETTLED_SPEECH_MS
                else VOICE_SILENCE_PATIENT_MS)

    def _finish(self, reason: str) -> list[dict]:
        """End a capture. Too little actual speech and the audio is dropped
        rather than transcribed: the model is a language model with a decoder,
        so handed a cough it will return a word, and a word that was never
        said is worse than silence."""
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, dtype=np.float32)
        speech_ms, source = self.speech_ms, self.source
        self.capturing = False
        self.chunks = []
        self.source = ""
        # Whatever fired the wake model is spent; clearing its buffer stops
        # the same phrase re-triggering on the next frame.
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
                # Summoned into an empty room: give up rather than sit there
                # recording until the length cap.
                if not self.speech_ms and self.captured_ms >= VOICE_WAKE_TIMEOUT_S * 1000:
                    return events + self._finish("cancel")
            return events

        if self.speaking and not self.barge_in:
            # Deaf while it talks: the reply has to finish before anything can
            # summon it again.
            return events

        # Wake spotting — the only thing that opens the microphone, and it runs
        # while the console is talking too. An earlier version listened for
        # *speech* during a reply so you could talk over it without saying the
        # phrase; what that actually did was open the mic at the end of every
        # answer, because a reply is 900ms of "still speaking" grace over a room
        # that is rarely silent. Requiring the phrase costs a word and makes the
        # trigger unambiguous.
        #
        # The hazard this creates is the synthesizer reading the wake word out
        # loud and summoning itself, which is why the mic is captured with the
        # browser's echo cancellation on: what arrives here already has the
        # console's own voice subtracted (see frontend/src/lib/voice.js). On
        # speakers loud enough to defeat that, VOICE_WAKE_THRESHOLD is the knob.
        self.wake_buf = np.concatenate([self.wake_buf, frame])
        while self.wake_buf.size >= WAKE_CHUNK:
            chunk, self.wake_buf = self.wake_buf[:WAKE_CHUNK], self.wake_buf[WAKE_CHUNK:]
            scores = self.wake_model.predict(chunk)
            if max(scores.values(), default=0.0) >= VOICE_WAKE_THRESHOLD:
                self.wake_model.reset()
                self.wake_buf = np.zeros(0, dtype=np.int16)
                self.start_capture("wake")
                # Summoned mid-reply: stop the mouth before opening the ear.
                # Same event the Stop button raises, so the SDK's bookkeeping
                # for a turn cut off mid-sentence is unchanged.
                barge = [{"type": "barge_in"}] if self.speaking else []
                return events + barge + [
                    {"type": "wake"},
                    {"type": "listening", "on": True, "source": "wake"}]
        return events


# ---------- speech out (macOS `say`) ----------

_FENCE = re.compile(r"```")
_SENTENCE_END = re.compile(r'(?<=[.!?])["\'”’)\]]*(\s|$)')

# Lines that are for the eyes only. A table read aloud is a stream of numbers
# with no structure left in it.
_UNSPEAKABLE_LINE = re.compile(r"^\s*(\||[-*_]{3,}\s*$)")

_INLINE_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),            # images: nothing to say
    # Citations and page embeds are markers for the eye ([cite:d3f9a1#p12
    # "…"], [page:…]); read aloud they were a string of hex.
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
#
# Stripping markdown is not enough: what's left is still written English, and
# a synthesizer reads written English badly in ways that are specific and
# countable. Measured over 1094 sentences from real console sessions, 17% hit
# at least one of the defects below, and the worst of them are not mere
# awkwardness — misaki (Kokoro's G2P) has no phoneme for `:`, `~` or a tight
# en dash, so "5:30-9:00" reaches the voice with three holes punched in it.
#
# These rules are pronunciation fixes, deliberately not rewrites. Each one
# replaces a token with how that same token is read out loud, so the invariant
# is weaker than speakable()'s "only ever drops" but still holds: nothing is
# said that isn't a reading of what's on screen. Anything a listener would
# have to be *told* — that a 3pm and a 5pm collide, that a total is surprising
# — is the writer's job, and VOICE_APPEND asks for it in console_prompt.py.
#
# Verified-good and therefore absent: `$45`, `40%`, `&`, `@`, `1st`, `RSVP`,
# and the spaced em dash all already phonemize correctly. Touching them would
# be churn.

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
# Every one of these is mispronounced rather than spelled out — "Jul" comes
# back ʤˈʌl, "Apr" comes back ˈapɹə. May needs no entry: it is already whole.
_MONTHS = {
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
    "Jun": "June", "Jul": "July", "Aug": "August", "Sep": "September",
    "Sept": "September", "Oct": "October", "Nov": "November",
    "Dec": "December",
}
# Units the lexicon reads as initialisms: `tsp` becomes "T S P". Only the ones
# that are actually wrong — `tbsp` already comes back "tablespoon".
_UNITS = {
    "tsp": "teaspoon", "hr": "hour", "hrs": "hours",
    "min": "minute", "mins": "minutes",
}
# The leading group carries any "24" stuck to the front, so "24hrs" comes out
# "24 hours" rather than "24hours", and tells the plural apart from "1 hr".
_UNIT = re.compile(r"(?<![A-Za-z])(\d+)?\s*(" + "|".join(
    sorted(_UNITS, key=len, reverse=True)) + r")(?![A-Za-z])")


def _spoken_unit(m: "re.Match[str]") -> str:
    count, word = m.group(1), _UNITS[m.group(2)]
    if count is None:
        return word
    if count != "1" and not word.endswith("s"):
        word += "s"
    return f"{count} {word}"
# Its own rule, because the slash means "w/Sam" has no letter boundary after
# it to anchor on, and unqualified it would read "double-u".
_WITH = re.compile(r"(?<![A-Za-z])w/\s*(?=[A-Za-z])")
_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November",
                "December"]

# Capitalised only: `Sun`/`Sat`/`Mar` lowercase are ordinary words, and the
# schedule text these come from always capitalises them.
_ABBREV = re.compile(r"\b(" + "|".join(
    sorted((*_WEEKDAYS, *_MONTHS), key=len, reverse=True)) + r")\b\.?")
_TZ = re.compile(r"\b(" + "|".join(_TIMEZONES) + r")\b")
# Anchored on the real IANA regions. Matching any Capitalised/Capitalised pair
# and keeping the right half looks equivalent and is not: it turns the workout
# split "Pull/Legs" into "Legs" and the colourway "Brown/White" into "White",
# silently deleting a word the screen still shows.
_TZ_ID = re.compile(r"\b(?:America|Europe|Asia|Africa|Australia|Pacific"
                    r"|Atlantic|Indian|Antarctica)/([A-Z][A-Za-z_]+)\b")
_ISO_DATE = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b")
# The whitespace lives *inside* the optional meridiem group. Outside it, a
# bare "5:30 to 9" loses the space it matched and comes out "five thirtyto".
_CLOCK = re.compile(r"\b(\d{1,2}):([0-5]\d)(?:\s*([ap])\.?m\.?\b)?",
                    re.IGNORECASE)
# A dash spanning a range is the word "to". Only tight ones: a spaced em dash
# is prose punctuation and already reads as the pause it is.
_RANGE_DASH = re.compile(r"(?<=[0-9m])[–—](?=[0-9]|noon\b|midnight\b)")
# Every other tight dash gets spaced rather than interpreted. Between two
# alphanumerics misaki emits nothing at all, but with room around it the same
# character is a clean pause — so this buys the fix without having to guess
# whether "PHYS—120" meant a range.
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
    """Written forms in, spoken forms out.

    Order matters twice: the range dash is resolved while both sides are
    still digits (afterwards "5:30-9:00" has words either side and no longer
    looks like a range), and the clock rule runs before the timezone one so
    it can consume the meridiem that sits between them.
    """
    text = _TZ_ID.sub(lambda m: m.group(1).replace("_", " "), text)
    text = _ISO_DATE.sub(
        lambda m: (f"{_MONTH_NAMES[int(m.group(2))]} "
                   f"{_ordinal_words(int(m.group(3)))}, {m.group(1)}"), text)
    # Before the dashes: "9am-~2" only looks like a tight dash once the tilde
    # has become a word, and resolving it after would leave the dash unfixed.
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
    """Markdown in, something worth hearing out — or "" if there's nothing.

    Markdown does not speak. `say` reads `**` as "asterisk asterisk", an emoji
    by its CLDR name, and a table row as its pipes. The reply is on screen in
    full either way, so the rule here is to drop what only works visually and
    speak the prose that's left.

    Then `_normalize_speech` fixes how the survivors are pronounced. That is
    the one place this function is not purely subtractive — it substitutes,
    never adds — because dropping is no help against "3:30", which is prose
    the synthesizer simply cannot say.
    """
    out: list[str] = []
    in_fence = False
    for line in text.splitlines():
        # The fence markers are not the problem; what's between them is. Only
        # dropping the ``` lines themselves left `uvicorn app.main:app` being
        # read out as a sentence.
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
    """Split a growing stream of reply text into speakable chunks.

    Called on every text delta, so it has to be honest about what is still
    arriving: a sentence is only handed over once its terminator *and* the
    whitespace after it exist, and an unclosed code fence holds everything
    from the fence onward until it closes. `flush` (end of turn) gives up
    both scruples and takes what's left.
    """
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


# Which voice `say` uses. Named here rather than left to the system default
# because the default is what makes people turn the speaker off.
#
# `say -v '?'` lists one voice per line: a name, the locale, then a `#` and a
# sample sentence. The name can hold spaces and its own brackets — "Sandy
# (English (UK))" — so the locale is the anchor and the name is whatever
# precedes it. The locale is not always `xx_XX`: Majed is `ar_001` and Fiona
# is `en-scotland`, so match the shape loosely rather than exclude voices for
# having an unusual one.
_VOICE_LINE = re.compile(r"^(.*?)\s+([a-z]{2,3}(?:[-_][A-Za-z0-9]+)+)\s+#")

# Apple ships each voice compact and puts the good build behind a download.
# Both spellings then appear in the list and they do not sound alike.
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
    """The best-sounding installed spelling of one voice name, or "".

    Two things make this more than a lookup. Downloading Serena leaves both
    `Serena` and `Serena (Premium)` in the list, and the bare one is the
    compact build — the voice you wanted to get away from. And a bare first
    name is not unique across locales: the novelty voices ship in a dozen, so
    plain matching hands "Sandy" back as `Sandy (German (Germany))`, which
    reads English prose with a German pronunciation model. English wins
    outright; among English, the better build wins.
    """
    def rank(n: str) -> tuple[int, int]:
        return _VARIANT_RANK.get(n[len(name):].strip(" ()").lower(), 2), len(n)

    matches = [(n, loc) for n, loc in installed
               if n == name or n.startswith(name + " (")]
    english = [n for n, loc in matches if loc.startswith("en")]
    # Pinning a non-English voice by name is allowed; it just isn't preferred.
    return min(english or [n for n, _ in matches], key=rank, default="")


def kokoro_ready() -> bool:
    """Whether the Kokoro path can run at all, checked without loading it.

    Both halves matter and they arrive separately: mlx-audio is the model
    runner, misaki is the G2P it calls to turn text into phonemes, and
    installing the first without the second gets you an engine that imports
    cleanly and then fails on every sentence.
    """
    import importlib.util
    return all(importlib.util.find_spec(m) is not None
               for m in ("mlx_audio", "misaki"))


def tts_voice() -> str:
    """The voice to hand `say`, resolved once and cached.

    A name in VOICE_TTS_VOICE pins it; "auto" walks VOICE_TTS_VOICE_PREFS and
    takes the first one installed. Either way the answer is a name that is
    actually present, or "" for the system default.

    Checking first matters because `say -v` does not fail on a voice that
    isn't installed — it exits 0 having quietly substituted another one.
    (Not even the no-flag default: `say -v Catherine` and `say -v Zzzznotreal`
    render byte-identical output, and both differ from bare `say`.) So the
    symptom of naming an absent voice is not an error or silence, it's the
    wrong voice with nothing anywhere saying so — which is exactly how a Siri
    voice fails, since those are named in System Settings but invisible here.
    `/api/voice/status` reports what this resolved to for the same reason.

    Cached because it shells out: a voice downloaded mid-session needs a
    backend restart, which downloading one already does.
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

# Kokoro's voice names carry their accent in the prefix, and the G2P has to
# agree with it: `b` is British, `a` American. Deriving it here rather than
# configuring it separately means the two can't drift, and a British voice
# read through the American front-end is not an error anyone would go looking
# for — it just sounds slightly wrong forever.
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
    """One chunk -> WAV bytes, or None to let the caller fall back to `say`.

    Serialized on the same lock as transcription: both are MLX graph
    evaluation on the one GPU, and the ASR side already decided that isn't
    something to run concurrently with itself. The grain is one chunk, so a
    barge-in waiting on this waits ~0.25s, not a whole reply.
    """
    import io

    import soundfile as sf
    try:
        async with _mlx_lock:
            audio, rate = await asyncio.to_thread(
                _synthesize_kokoro_sync, text, voice)
    except Exception:
        # Missing model, missing misaki, a voice name that doesn't exist —
        # all the same to the caller, which has a synthesiser that always
        # works sitting right there.
        return None
    buf = io.BytesIO()
    sf.write(buf, audio, rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


_warm_task: asyncio.Task | None = None


def warm() -> None:
    """Build the synthesis pipeline in the background, if it isn't already.

    Kokoro's first call costs ~2.4s and every one after it ~0.15s: the model
    load is trivial, but the G2P pipeline is constructed lazily and the first
    graph evaluation is where Metal compiles the kernels. Paying that on the
    opening sentence of a reply is the one place it's actually audible, so it
    gets paid when a session says it wants speech instead.

    Fire-and-forget by design — it must not delay the session start it hangs
    off, and _synthesize_kokoro already swallows every way it can fail.
    """
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
    """One chunk of text -> WAV bytes, through the configured synthesiser.

    `voice` and `engine` override the configured pair, which is for
    auditioning: picking a voice means hearing several say the same line, and
    restarting the backend between candidates is not that.

    Whatever the engine, the contract is a WAV the browser can hand straight
    to `decodeAudioData` — the playback queue, the barge-in coupling and the
    speaking flag all sit downstream of this function and none of them know
    or care which model produced the bytes.
    """
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
    """One chunk of text -> WAV bytes, via macOS `say`.

    Free, offline, always installed, and the floor the rest of speech-out
    stands on: it is what runs when Kokoro can't load, so it is deliberately
    the one path here with no model, no download and nothing to go wrong.

    The text goes through a file rather than argv: `say` takes its string as
    positional arguments, so a reply that happens to start with a dash would
    be read as a flag.
    """
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
        # Whichever engine is live names its own voice: a Kokoro voice and a
        # `say` voice share no namespace, so reporting both would be noise.
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
