"""Voice: the gate in front of the ASR model, and the text that reaches `say`.

The parts with bugs in them are the state machine (when does a turn start,
when does it end, what gets thrown away) and the markdown-to-speech pass —
not the models, which are exercised for real by the round-trip in
test_voice_roundtrip.py-style manual checks. Everything here runs without
loading 2.5GB of parakeet.
"""

import asyncio

import numpy as np
import pytest

from app import console, console_prompt, voice


# ---------- speakable(): what reaches the synthesizer ----------

def test_markdown_marks_are_not_read_aloud():
    """`say` reads ** as "asterisk asterisk" and a URL character by character."""
    out = voice.speakable("**Two** tasks are [overdue](https://x.test/a) — `fix` them")
    assert out == "Two tasks are overdue — fix them"


def test_tables_and_code_are_skipped_entirely():
    """A table read aloud is a stream of numbers with the structure gone. It's
    on screen in full either way, so the prose is what gets spoken."""
    text = ("Here's the week.\n"
            "| day | steps |\n| --- | --- |\n| Mon | 8000 |\n"
            "```python\nprint('hi')\n```\n"
            "Thursday is the light one.")
    assert voice.speakable(text) == "Here's the week. Thursday is the light one."


def test_headings_bullets_and_wikilinks_lose_their_marks():
    out = voice.speakable("## Today\n- see [[06-Generated/plan|the plan]]\n1. then rest")
    assert out == "Today see the plan then rest"


def test_nothing_speakable_is_empty_not_noise():
    """A turn that is only a table must produce silence, not a spoken pipe."""
    assert voice.speakable("| a | b |\n| - | - |") == ""


# ---------- _normalize_speech(): how the survivors are pronounced ----------
#
# Measured against 1094 sentences pulled from real console sessions. The
# expectations here are what misaki (Kokoro's G2P, with the espeak fallback
# production configures) actually needs to be handed — "3:30" reaches it as
# a bare colon it has no phoneme for, and "2:00" comes back "two zero zero".

@pytest.mark.parametrize("written, spoken", [
    ("It's at 3:30 PM ET.", "It's at three thirty in the afternoon Eastern."),
    ("Lunch is at 2:00.", "Lunch is at two o'clock."),        # not "two zero zero"
    ("Free 9:05 to 9:45.", "Free nine oh five to nine forty five."),
    ("12:00 pm and 12:00 am.", "noon and midnight."),
    ("Tonight 5:30–9:00 PM.", "Tonight five thirty to nine o'clock in the evening."),
    ("Thu Jul 30.", "Thursday July 30."),                     # both are unsayable raw
    ("Due 2026-08-12.", "Due August twelfth, 2026."),
    ("Roughly ~2 hours, 24hrs of slack.", "Roughly about 2 hours, 24 hours of slack."),
    ("Add 2 tsp, then 1 hr.", "Add 2 teaspoons, then 1 hour."),
    ("Meeting w/Sam.", "Meeting with Sam."),
    ("Set to America/Los_Angeles.", "Set to Los Angeles."),
])
def test_written_forms_are_spoken_as_they_are_read(written, spoken):
    assert voice.speakable(written) == spoken


@pytest.mark.parametrize("text", [
    "It cost $45 and 40% is left.",          # already correct; touching it is churn
    "Check-In at the RSVP desk.",            # ASCII hyphen, and an initialism
    "Two things — the dentist, and the flight.",   # spaced em dash is a pause
    "CSCI 103 and BBQ at USC.",
    "The sun was out; I sat down in March.",  # lowercase sun/sat are not weekdays
    "Score was 10:3 and the ratio 21:9.",     # a colon is not always a clock
])
def test_what_already_speaks_correctly_is_left_alone(text):
    assert voice.speakable(text) == text


def test_a_slash_pair_is_not_a_timezone():
    """Keeping the right half of any Capitalised/Capitalised pair looks like
    the same rule and silently deletes a word the screen still shows."""
    assert voice.speakable("Pull/Legs day, Brown/White colorway.") == (
        "Pull/Legs day, Brown/White colorway.")


def test_normalization_is_idempotent():
    """Rules that re-fire on their own output are rules that misfire: the
    tilde has to become a word before the dash rules can see past it."""
    once = voice.speakable("Class 9am–~2 today, Thu Jul 30 at 3:30 PM ET.")
    assert voice._normalize_speech(once) == once


# ---------- take_sentences(): cutting the delta stream ----------

def test_sentence_is_held_until_its_terminator_arrives():
    """Deltas arrive mid-word; speaking a half sentence is worse than waiting."""
    chunks, rest = voice.take_sentences("Your first class is at 9")
    assert chunks == [] and rest == "Your first class is at 9"
    chunks, rest = voice.take_sentences(rest + ":30. Then ")
    assert chunks == ["Your first class is at 9:30."] and rest == "Then "


def test_open_code_fence_holds_everything_after_it():
    """Mid-fence there is no closing ``` yet, so the lines inside it look like
    ordinary prose — speaking them is how a shell command gets read aloud."""
    chunks, rest = voice.take_sentences("Run this. ```sh\nuvicorn app.main:app\n")
    assert chunks == ["Run this."]
    assert rest.startswith("```sh")
    chunks, rest = voice.take_sentences(rest + "```\nThat's it.", flush=True)
    assert voice.speakable(" ".join(chunks)) == "That's it."


def test_flush_takes_the_last_sentence_without_punctuation():
    chunks, rest = voice.take_sentences("done", flush=True)
    assert chunks == ["done"] and rest == ""


def test_a_sentence_that_never_ends_is_cut_at_a_word():
    long = "word " * 400
    chunks, _ = voice.take_sentences(long)
    assert chunks and len(chunks[0]) <= voice.SPEAK_MAX_CHARS
    assert not chunks[0].endswith("wor")     # cut on whitespace, not mid-word


# ---------- which phrase summons it ----------

def test_a_trained_wake_word_is_named_by_its_file(tmp_path):
    """openWakeWord ships six phrases, so our own name is a model we trained
    and configured as a path. The UI still has to be able to say the phrase
    out loud, and the file's stem is the only place it's written down."""
    model = tmp_path / "hey_kestrel.onnx"
    model.write_bytes(b"")
    assert voice.wake_label(str(model)) == "hey kestrel"
    assert voice.wake_label("hey_jarvis") == "hey jarvis"


def test_a_wake_model_that_isnt_there_yet_is_not_offered(tmp_path):
    """Configured before the training run finishes is the likely case, and it
    fails inside Listener's constructor — i.e. when the socket opens, far from
    the config line that caused it. The status route checks first."""
    assert not voice.wake_ready(str(tmp_path / "hey_kestrel.onnx"))
    assert not voice.wake_ready("hey_gandalf")
    assert voice.wake_ready("hey_jarvis")


# ---------- which voice comes back out ----------

# One line of `say -v '?'` per voice, including the two shapes that don't fit
# the obvious `xx_XX` locale pattern and the bracketed names that do parse.
SAY_LIST = """\
Karen               en_AU    # Hello! My name is Karen.
Sandy (German (Germany)) de_DE    # Hallo! Ich heiße Sandy.
Sandy (English (UK)) en_GB    # Hello! My name is Sandy.
Majed               ar_001   # مرحبًا! اسمي ماجد.
Fiona               en-scotland # Hello! My name is Fiona.
"""


@pytest.fixture
def say_list(monkeypatch):
    """Stand in for `say -v '?'` without depending on what this Mac has
    installed — which is the whole variable under test."""
    def fake_run(cmd, **kw):
        return type("P", (), {"stdout": listing[0]})()

    listing = [SAY_LIST]
    monkeypatch.setattr(voice.shutil, "which", lambda _: "/usr/bin/say")
    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    monkeypatch.setattr(voice, "_tts_voice", None)
    return listing


def test_every_voice_is_parsed_including_the_odd_locales(say_list):
    """A voice dropped by the parser is a voice that silently can't be chosen,
    so the locale shape is matched loosely — `ar_001` and `en-scotland` are
    both real and neither is `xx_XX`."""
    assert voice.installed_voices() == [
        ("Karen", "en_AU"), ("Sandy (German (Germany))", "de_DE"),
        ("Sandy (English (UK))", "en_GB"), ("Majed", "ar_001"),
        ("Fiona", "en-scotland")]


def test_the_premium_build_wins_over_the_compact_one(say_list):
    """macOS ships every voice compact and hides the good build behind a
    download. Both spellings then show up and they don't sound alike — picking
    the bare name would quietly hand back the voice you downloaded to escape."""
    say_list[0] += ("Serena              en_GB    # Hello!\n"
                    "Serena (Premium)    en_GB    # Hello!\n")
    assert voice.tts_voice() == "Serena (Premium)"


def test_it_falls_through_to_a_voice_that_is_actually_installed(say_list, monkeypatch):
    """Before the Premium download there is no Serena at all. `say -v Serena`
    would not complain — it exits 0 having used the system default, so the
    symptom of getting this wrong is the annoying voice, silently."""
    monkeypatch.setattr(voice, "VOICE_TTS_VOICE_PREFS", ["Serena", "Karen"])
    assert voice.tts_voice() == "Karen"


def test_an_absent_voice_becomes_the_system_default_not_a_broken_command(say_list, monkeypatch):
    monkeypatch.setattr(voice, "VOICE_TTS_VOICE_PREFS", ["Serena", "Stephanie"])
    assert voice.tts_voice() == ""


def test_a_bare_first_name_does_not_pick_up_another_locale(say_list, monkeypatch):
    """The novelty voices ship in a dozen languages under one name, and `say`
    lists German Sandy first. Reading English prose with her would be a
    pronunciation model away from anything intelligible."""
    monkeypatch.setattr(voice, "VOICE_TTS_VOICE", "Sandy")
    assert voice.tts_voice() == "Sandy (English (UK))"


# ---------- which engine renders it ----------

@pytest.fixture
def engines(monkeypatch):
    """Record which synthesiser each call reached, without running either."""
    calls = []

    async def fake_kokoro(text, voice):
        calls.append(("kokoro", voice))
        return None if voice.startswith("zz") else b"RIFFkokoro"

    async def fake_say(text, voice=None):
        calls.append(("say", voice))
        return b"RIFFsay"

    monkeypatch.setattr(voice, "_synthesize_kokoro", fake_kokoro)
    monkeypatch.setattr(voice, "_synthesize_say", fake_say)
    monkeypatch.setattr(voice, "VOICE_TTS_ENGINE", "kokoro")
    monkeypatch.setattr(voice, "VOICE_TTS_KOKORO_VOICE", "bf_emma")
    return calls


def test_a_failed_kokoro_falls_back_to_say(engines):
    """Speech-out degrading is annoying; speech-out taking the reply stream
    down with it is not acceptable. A missing model, a missing misaki and a
    bad voice name all land here."""
    assert asyncio.run(voice.synthesize("hi", "zz_nope")) == b"RIFFsay"
    assert engines == [("kokoro", "zz_nope"), ("say", None)]


def test_the_fallback_does_not_hand_say_a_kokoro_voice_name(engines):
    """`bf_emma` means nothing to `say`, which would silently substitute some
    other voice rather than refuse — so the override is dropped on the way
    down, not passed through."""
    asyncio.run(voice.synthesize("hi", "zz_nope"))
    assert ("say", None) in engines


def test_asking_for_an_engine_by_name_is_not_quietly_swapped(engines):
    """Auditioning is the whole point of the override: silently answering
    with the other engine would make every comparison a lie."""
    assert asyncio.run(voice.synthesize("hi", "zz_nope", "kokoro")) is None
    assert engines == [("kokoro", "zz_nope")]


def test_the_configured_voice_is_used_when_none_is_given(engines):
    assert asyncio.run(voice.synthesize("hi")) == b"RIFFkokoro"
    assert engines == [("kokoro", "bf_emma")]


def test_say_can_be_selected_outright(engines):
    assert asyncio.run(voice.synthesize("hi", "Karen", "say")) == b"RIFFsay"
    assert engines == [("say", "Karen")]


def test_the_g2p_follows_the_voices_accent(engines):
    """Kokoro's `bf_` prefix is British and `af_` American, and the G2P has to
    agree — a British voice read through the American front-end is a bug that
    only ever presents as sounding slightly off."""
    assert voice._kokoro_lang("bf_emma") == "b"
    assert voice._kokoro_lang("bm_george") == "b"
    assert voice._kokoro_lang("af_heart") == "a"


# ---------- Listener: the state machine ----------

class FakeVAD:
    """Speech is loud, silence is quiet — the real model's answer for the
    synthetic frames below, without the 1MB of ONNX."""

    def predict(self, frame, frame_size=480):
        return 1.0 if np.abs(frame).max() > 1000 else 0.0


class FakeWake:
    """Fires on a frame marked with a distinctive amplitude, so tests can say
    'the wake word happened here' without synthesizing speech."""

    def __init__(self):
        self.reset_calls = 0

    def predict(self, chunk):
        return {"hey_jarvis": 1.0 if np.abs(chunk).max() > 20000 else 0.0}

    def reset(self):
        self.reset_calls += 1


@pytest.fixture
def listener(monkeypatch):
    monkeypatch.setattr(voice, "Listener", voice.Listener)
    obj = voice.Listener.__new__(voice.Listener)
    obj.vad, obj.wake_model = FakeVAD(), FakeWake()
    obj.wake_word, obj.barge_in = "hey_jarvis", True
    obj.mode, obj.speaking = "off", False
    obj.capturing, obj.source, obj.auto_end = False, "", True
    obj.chunks, obj.captured_ms, obj.speech_ms, obj.silence_ms = [], 0, 0, 0
    from collections import deque
    obj.preroll = deque(maxlen=voice.PREROLL_FRAMES)
    obj.wake_buf = np.zeros(0, dtype=np.int16)
    return obj


def frames(listener, n, level):
    """Feed n frames at one amplitude; return every event they produced."""
    out = []
    for _ in range(n):
        frame = np.full(voice.FRAME_SAMPLES, level, dtype=np.int16)
        out.extend(listener.feed(frame))
    return out


def types(events):
    return [e["type"] for e in events]


def test_nothing_is_captured_until_the_console_is_summoned(listener):
    """The whole point of the three-stage gate: with the mic idle, speech in
    the room produces no capture and never reaches the ASR model."""
    listener.mode = "wake"
    assert types(frames(listener, 50, 5000)) == []
    assert not listener.capturing


def ms(n):
    """Frames needed to cover n milliseconds of audio."""
    return n // voice.FRAME_MS + 1


def test_wake_word_starts_a_turn_and_silence_ends_it(listener):
    listener.mode = "wake"
    assert types(frames(listener, 8, 30000)) == ["wake", "listening"]
    frames(listener, ms(voice.VOICE_SETTLED_SPEECH_MS), 5000)   # the command
    events = frames(listener, ms(voice.VOICE_SILENCE_MS), 0)
    assert types(events) == ["listening", "utterance"]
    assert events[1]["source"] == "wake"


def test_thinking_mid_sentence_does_not_end_the_turn(listener):
    """The complaint this exists for: pausing to find a word, early in a
    sentence, used to get you cut off mid-thought. A pause that would end a
    settled utterance has to leave an unsettled one running."""
    listener.mode = "wake"
    frames(listener, 8, 30000)                       # summoned
    frames(listener, 10, 5000)                       # "so, um..."
    assert types(frames(listener, ms(voice.VOICE_SILENCE_MS), 0)) == []
    assert listener.capturing                        # still yours
    frames(listener, 20, 5000)                       # "...the Ellis thread"
    assert listener.capturing


def test_the_patient_window_is_not_infinite(listener):
    """Patience is a longer threshold, not the absence of one — a turn nobody
    finishes still has to end by itself."""
    listener.mode = "wake"
    frames(listener, 8, 30000)
    frames(listener, 10, 5000)
    events = frames(listener, ms(voice.VOICE_SILENCE_PATIENT_MS), 0)
    assert types(events) == ["listening", "utterance"]


def test_a_settled_utterance_ends_on_the_short_threshold(listener):
    """The other half of the trade: once you've said enough that a pause
    probably means you're finished, waiting the patient window would just
    make every reply feel slow."""
    listener.mode = "wake"
    frames(listener, 8, 30000)
    frames(listener, ms(voice.VOICE_SETTLED_SPEECH_MS), 5000)
    assert listener.silence_needed() == voice.VOICE_SILENCE_MS
    events = frames(listener, ms(voice.VOICE_SILENCE_MS), 0)
    assert types(events) == ["listening", "utterance"]


def test_patience_tracks_speech_not_elapsed_time(listener):
    """A long hunt for a word must not count towards settling, or the feature
    defeats itself: the longer you hesitate the sooner you'd be cut off."""
    listener.mode = "wake"
    frames(listener, 8, 30000)
    frames(listener, 10, 5000)                       # 300ms of speech
    frames(listener, 40, 0)                          # 1.2s of hesitation
    assert listener.silence_needed() == voice.VOICE_SILENCE_PATIENT_MS


def test_the_wake_word_itself_stays_out_of_the_transcript(listener):
    """Capture after a wake starts empty, not from the pre-roll: the audio
    just before the trigger is the wake phrase, and nobody wants to read it
    back in their own message."""
    listener.mode = "wake"
    frames(listener, 40, 5000)                      # fills the pre-roll ring
    frames(listener, 3, 30000)                      # ...then the wake word
    assert listener.capturing and listener.chunks == []


def test_a_cough_is_dropped_rather_than_transcribed(listener):
    """Parakeet is a language model with a decoder: hand it a noise burst and
    it returns a word, and a word nobody said is worse than silence."""
    listener.start_capture("ptt", auto_end=False)
    frames(listener, 3, 9000)                       # ~90ms, under the floor
    events = listener.stop_capture()
    assert types(events) == ["listening", "status"]


def test_the_wake_word_interrupts_a_reply_and_starts_a_turn(listener):
    """Barging in is saying the phrase again: the playback is cut off and the
    capture that opens is an ordinary summoned turn, so it sends itself."""
    listener.mode, listener.speaking = "wake", True
    frames(listener, 20, 5000)                      # TTS playing, room talking
    events = frames(listener, 8, 30000)             # "hey jarvis"
    assert types(events) == ["barge_in", "wake", "listening"]
    assert listener.capturing and listener.source == "wake"


def test_speech_during_a_reply_does_not_open_the_microphone(listener):
    """The bug this replaced: bare speech used to trigger barge-in, so the mic
    re-opened at the end of every answer — a reply carries 900ms of
    still-speaking grace, and a room is rarely quiet for it."""
    listener.mode, listener.speaking = "wake", True
    assert types(frames(listener, 60, 5000)) == []  # 1.8s of talking, no phrase
    assert not listener.capturing


def test_the_reply_ending_leaves_the_mic_shut(listener):
    """Nothing is armed by the console going quiet; the next turn waits for the
    phrase exactly as the first one did."""
    listener.mode, listener.speaking = "wake", True
    frames(listener, 20, 5000)
    listener.speaking = False
    assert types(frames(listener, 30, 5000)) == []
    assert not listener.capturing
    assert types(frames(listener, 8, 30000)) == ["wake", "listening"]


def test_barge_in_off_makes_the_console_deaf_while_it_talks(listener):
    """The stricter setting for a room where echo cancellation loses: with
    interrupts off, not even the wake word is spotted until the reply ends."""
    listener.mode, listener.speaking, listener.barge_in = "wake", True, False
    assert types(frames(listener, 16, 30000)) == []
    assert not listener.capturing
    listener.speaking = False
    assert types(frames(listener, 8, 30000)) == ["wake", "listening"]


def test_push_to_talk_ignores_silence_and_ends_on_the_button(listener):
    """A pause while you think is not the end of the sentence when you're
    holding the button down."""
    listener.start_capture("ptt", auto_end=False)
    frames(listener, 10, 5000)
    frames(listener, 60, 0)                         # 1.8s of thinking
    assert listener.capturing
    events = listener.stop_capture()
    assert types(events) == ["listening", "utterance"]
    assert events[1]["source"] == "ptt"


def test_summoned_into_an_empty_room_gives_up(listener):
    """Wake fired on a false positive: stop recording rather than sit there
    until the length cap."""
    listener.mode = "wake"
    frames(listener, 3, 30000)
    events = frames(listener, int(voice.VOICE_WAKE_TIMEOUT_S * 1000 / voice.FRAME_MS) + 1, 0)
    assert types(events) == ["listening"]           # no utterance
    assert not listener.capturing


def test_capture_stops_at_the_length_cap(listener):
    """A stuck-open mic must not hand the model a recording of the room."""
    listener.start_capture("ptt", auto_end=False)
    events = frames(listener, int(voice.VOICE_MAX_UTTERANCE_S * 1000 / voice.FRAME_MS) + 2, 5000)
    assert "utterance" in types(events)
    assert not listener.capturing


# ---------- SpeechPump: the console's mouth ----------

class FakeWS:
    headers: dict[str, str] = {}

    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


def test_the_reply_is_spoken_sentence_by_sentence_in_order(monkeypatch):
    """Off the delta stream, not the finished turn — that's what puts the
    first words in the air about a second in."""
    async def fake_say(text):
        await asyncio.sleep(0.01 if "first" in text else 0)   # slow one first
        return f"WAV:{text}".encode()
    monkeypatch.setattr(voice, "synthesize", fake_say)

    async def run():
        ws = FakeWS()
        pump = console.SpeechPump(ws)
        for delta in ["The first ", "one is at 9. ", "The **second** is at 11. "]:
            await pump.feed(delta)
        await pump.flush()
        pump.close()
        return ws.sent

    sent = asyncio.run(run())
    assert [m["type"] for m in sent] == ["speech", "speech"]
    assert [m["text"] for m in sent] == ["The first one is at 9.", "The second is at 11."]


def test_an_interrupted_turn_stops_talking(monkeypatch):
    """The model must believe it only said what was actually heard, so the
    sentences still queued when the turn is cut off are never sent."""
    async def slow_say(text):
        await asyncio.sleep(5)
        return b"WAV"
    monkeypatch.setattr(voice, "synthesize", slow_say)

    async def run():
        ws = FakeWS()
        pump = console.SpeechPump(ws)
        await pump.feed("One. Two. Three. ")
        pump.close()                       # what stream_response does on interrupt
        await asyncio.sleep(0.05)
        return ws.sent

    assert asyncio.run(run()) == []


def test_a_reply_with_nothing_speakable_is_silent(monkeypatch):
    """A turn that is all table produces no audio at all, rather than `say`
    reading its pipes."""
    calls = []
    async def fake_say(text):
        calls.append(text)
        return b"WAV"
    monkeypatch.setattr(voice, "synthesize", fake_say)

    async def run():
        pump = console.SpeechPump(FakeWS())
        await pump.feed("| day | steps |\n| --- | --- |\n")
        await pump.flush()
        pump.close()

    asyncio.run(run())
    assert calls == []


def test_the_mouth_can_be_stopped_after_the_turn_has_ended(monkeypatch):
    """The model's turn ends when the result lands, but the reply keeps being
    spoken for as long as the backlog takes to synthesize — and a long answer
    is talked over in exactly that window. So the wait for the backlog has to
    be interruptible; when it wasn't, barge-in worked only on short replies."""
    async def slow_say(text):
        await asyncio.sleep(0.05)
        return b"WAV"
    monkeypatch.setattr(voice, "synthesize", slow_say)

    async def run():
        ws = FakeWS()
        pump = console.SpeechPump(ws)
        await pump.feed("One. Two. Three. Four. Five. Six. ")
        flushing = asyncio.create_task(pump.flush())
        await asyncio.sleep(0.06)             # a sentence or so gets out
        pump.close()                          # what the interrupt branch does
        await asyncio.wait_for(flushing, 1)   # ...and the turn is not left hanging
        return ws.sent

    sent = asyncio.run(run())
    assert 0 < len(sent) < 6


def test_the_speakable_rule_rides_only_with_voice_on():
    """It is a real behaviour change — 'lead with one plain sentence' is not
    what you want from a typed answer — so it is fixed at session start
    alongside the voice flag rather than always on."""
    assert "read aloud" not in console_prompt.render_append(voice=False)
    assert "read aloud" in console_prompt.render_append(voice=True)
