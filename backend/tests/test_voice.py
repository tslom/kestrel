"""Voice: the listener state machine and the markdown-to-speech pass, with no models loaded."""

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
    """Tables are skipped; the prose is what gets spoken."""
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
# Expectations are what misaki (production G2P config) needs handed to it; corpus of 1094 real sentences.

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
    """Capitalised/Capitalised pairs keep both words."""
    assert voice.speakable("Pull/Legs day, Brown/White colorway.") == (
        "Pull/Legs day, Brown/White colorway.")


def test_normalization_is_idempotent():
    """Rules don't re-fire on their own output (tilde becomes a word before the dash rules)."""
    once = voice.speakable("Class 9am–~2 today, Thu Jul 30 at 3:30 PM ET.")
    assert voice._normalize_speech(once) == once


# ---------- take_sentences(): cutting the delta stream ----------

def test_sentence_is_held_until_its_terminator_arrives():
    """A half sentence is held back, not spoken."""
    chunks, rest = voice.take_sentences("Your first class is at 9")
    assert chunks == [] and rest == "Your first class is at 9"
    chunks, rest = voice.take_sentences(rest + ":30. Then ")
    assert chunks == ["Your first class is at 9:30."] and rest == "Then "


def test_open_code_fence_holds_everything_after_it():
    """Lines inside an unclosed fence are not spoken."""
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
    """A custom wake model's phrase comes from its file stem."""
    model = tmp_path / "hey_kestrel.onnx"
    model.write_bytes(b"")
    assert voice.wake_label(str(model)) == "hey kestrel"
    assert voice.wake_label("hey_jarvis") == "hey jarvis"


def test_a_wake_model_that_isnt_there_yet_is_not_offered(tmp_path):
    """The status route reports a missing wake model before Listener's constructor fails on it."""
    assert not voice.wake_ready(str(tmp_path / "hey_kestrel.onnx"))
    assert not voice.wake_ready("hey_gandalf")
    assert voice.wake_ready("hey_jarvis")


# ---------- which voice comes back out ----------

# One `say -v '?'` line per voice, including odd locale shapes and bracketed names.
SAY_LIST = """\
Karen               en_AU    # Hello! My name is Karen.
Sandy (German (Germany)) de_DE    # Hallo! Ich heiße Sandy.
Sandy (English (UK)) en_GB    # Hello! My name is Sandy.
Majed               ar_001   # مرحبًا! اسمي ماجد.
Fiona               en-scotland # Hello! My name is Fiona.
"""


@pytest.fixture
def say_list(monkeypatch):
    """Stand in for `say -v '?'` so the result doesn't depend on installed voices."""
    def fake_run(cmd, **kw):
        return type("P", (), {"stdout": listing[0]})()

    listing = [SAY_LIST]
    monkeypatch.setattr(voice.shutil, "which", lambda _: "/usr/bin/say")
    monkeypatch.setattr(voice.subprocess, "run", fake_run)
    monkeypatch.setattr(voice, "_tts_voice", None)
    return listing


def test_every_voice_is_parsed_including_the_odd_locales(say_list):
    """The locale shape is matched loosely (`ar_001`, `en-scotland`)."""
    assert voice.installed_voices() == [
        ("Karen", "en_AU"), ("Sandy (German (Germany))", "de_DE"),
        ("Sandy (English (UK))", "en_GB"), ("Majed", "ar_001"),
        ("Fiona", "en-scotland")]


def test_the_premium_build_wins_over_the_compact_one(say_list):
    """A downloaded Premium/Enhanced build wins over the bare compact name."""
    say_list[0] += ("Serena              en_GB    # Hello!\n"
                    "Serena (Premium)    en_GB    # Hello!\n")
    assert voice.tts_voice() == "Serena (Premium)"


def test_it_falls_through_to_a_voice_that_is_actually_installed(say_list, monkeypatch):
    """A voice with no installed build is not offered (`say` would exit 0 on the default)."""
    monkeypatch.setattr(voice, "VOICE_TTS_VOICE_PREFS", ["Serena", "Karen"])
    assert voice.tts_voice() == "Karen"


def test_an_absent_voice_becomes_the_system_default_not_a_broken_command(say_list, monkeypatch):
    monkeypatch.setattr(voice, "VOICE_TTS_VOICE_PREFS", ["Serena", "Stephanie"])
    assert voice.tts_voice() == ""


def test_a_bare_first_name_does_not_pick_up_another_locale(say_list, monkeypatch):
    """A multi-language voice name resolves to its English build."""
    monkeypatch.setattr(voice, "VOICE_TTS_VOICE", "Sandy")
    assert voice.tts_voice() == "Sandy (English (UK))"


# ---------- which engine renders it ----------

@pytest.fixture
def engines(monkeypatch):
    """Record which synthesiser each call reached."""
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
    """A missing model, misaki or bad voice name falls back to `say`, never fails the stream."""
    assert asyncio.run(voice.synthesize("hi", "zz_nope")) == b"RIFFsay"
    assert engines == [("kokoro", "zz_nope"), ("say", None)]


def test_the_fallback_does_not_hand_say_a_kokoro_voice_name(engines):
    """A Kokoro voice override is dropped on the `say` fallback, not passed through."""
    asyncio.run(voice.synthesize("hi", "zz_nope"))
    assert ("say", None) in engines


def test_asking_for_an_engine_by_name_is_not_quietly_swapped(engines):
    """An explicit engine override never silently answers with the other engine."""
    assert asyncio.run(voice.synthesize("hi", "zz_nope", "kokoro")) is None
    assert engines == [("kokoro", "zz_nope")]


def test_the_configured_voice_is_used_when_none_is_given(engines):
    assert asyncio.run(voice.synthesize("hi")) == b"RIFFkokoro"
    assert engines == [("kokoro", "bf_emma")]


def test_say_can_be_selected_outright(engines):
    assert asyncio.run(voice.synthesize("hi", "Karen", "say")) == b"RIFFsay"
    assert engines == [("say", "Karen")]


def test_the_g2p_follows_the_voices_accent(engines):
    """The G2P dialect follows the voice prefix (`bf_` British, `af_` American)."""
    assert voice._kokoro_lang("bf_emma") == "b"
    assert voice._kokoro_lang("bm_george") == "b"
    assert voice._kokoro_lang("af_heart") == "a"


# ---------- Listener: the state machine ----------

class FakeVAD:
    """Speech is loud, silence is quiet (stands in for the ONNX VAD)."""

    def predict(self, frame, frame_size=480):
        return 1.0 if np.abs(frame).max() > 1000 else 0.0


class FakeWake:
    """Fires on a frame at a distinctive amplitude."""

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
    """With the mic idle, room speech produces no capture and never reaches ASR."""
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
    """A pause that would end a settled utterance leaves an unsettled one running."""
    listener.mode = "wake"
    frames(listener, 8, 30000)                       # summoned
    frames(listener, 10, 5000)                       # "so, um..."
    assert types(frames(listener, ms(voice.VOICE_SILENCE_MS), 0)) == []
    assert listener.capturing                        # still yours
    frames(listener, 20, 5000)                       # "...the Ellis thread"
    assert listener.capturing


def test_the_patient_window_is_not_infinite(listener):
    """An unsettled turn still ends on the longer threshold."""
    listener.mode = "wake"
    frames(listener, 8, 30000)
    frames(listener, 10, 5000)
    events = frames(listener, ms(voice.VOICE_SILENCE_PATIENT_MS), 0)
    assert types(events) == ["listening", "utterance"]


def test_a_settled_utterance_ends_on_the_short_threshold(listener):
    """A settled utterance ends on the short pause, not the patient window."""
    listener.mode = "wake"
    frames(listener, 8, 30000)
    frames(listener, ms(voice.VOICE_SETTLED_SPEECH_MS), 5000)
    assert listener.silence_needed() == voice.VOICE_SILENCE_MS
    events = frames(listener, ms(voice.VOICE_SILENCE_MS), 0)
    assert types(events) == ["listening", "utterance"]


def test_patience_tracks_speech_not_elapsed_time(listener):
    """Hesitation frames do not count toward settling."""
    listener.mode = "wake"
    frames(listener, 8, 30000)
    frames(listener, 10, 5000)                       # 300ms of speech
    frames(listener, 40, 0)                          # 1.2s of hesitation
    assert listener.silence_needed() == voice.VOICE_SILENCE_PATIENT_MS


def test_the_wake_word_itself_stays_out_of_the_transcript(listener):
    """Capture after a wake starts empty, not from the pre-roll (which holds the wake phrase)."""
    listener.mode = "wake"
    frames(listener, 40, 5000)                      # fills the pre-roll ring
    frames(listener, 3, 30000)                      # ...then the wake word
    assert listener.capturing and listener.chunks == []


def test_a_cough_is_dropped_rather_than_transcribed(listener):
    """A burst under the min-speech floor is discarded, not sent to ASR."""
    listener.start_capture("ptt", auto_end=False)
    frames(listener, 3, 9000)                       # ~90ms, under the floor
    events = listener.stop_capture()
    assert types(events) == ["listening", "status"]


def test_the_wake_word_interrupts_a_reply_and_starts_a_turn(listener):
    """The wake phrase mid-playback cuts playback and opens an ordinary summoned turn."""
    listener.mode, listener.speaking = "wake", True
    frames(listener, 20, 5000)                      # TTS playing, room talking
    events = frames(listener, 8, 30000)             # "hey jarvis"
    assert types(events) == ["barge_in", "wake", "listening"]
    assert listener.capturing and listener.source == "wake"


def test_speech_during_a_reply_does_not_open_the_microphone(listener):
    """Bare speech during playback does not barge in."""
    listener.mode, listener.speaking = "wake", True
    assert types(frames(listener, 60, 5000)) == []  # 1.8s of talking, no phrase
    assert not listener.capturing


def test_the_reply_ending_leaves_the_mic_shut(listener):
    """The console going quiet arms nothing; the next turn waits for the phrase."""
    listener.mode, listener.speaking = "wake", True
    frames(listener, 20, 5000)
    listener.speaking = False
    assert types(frames(listener, 30, 5000)) == []
    assert not listener.capturing
    assert types(frames(listener, 8, 30000)) == ["wake", "listening"]


def test_barge_in_off_makes_the_console_deaf_while_it_talks(listener):
    """With interrupts off, not even the wake word is spotted until the reply ends."""
    listener.mode, listener.speaking, listener.barge_in = "wake", True, False
    assert types(frames(listener, 16, 30000)) == []
    assert not listener.capturing
    listener.speaking = False
    assert types(frames(listener, 8, 30000)) == ["wake", "listening"]


def test_push_to_talk_ignores_silence_and_ends_on_the_button(listener):
    """A pause while the button is held is not an end."""
    listener.start_capture("ptt", auto_end=False)
    frames(listener, 10, 5000)
    frames(listener, 60, 0)                         # 1.8s of thinking
    assert listener.capturing
    events = listener.stop_capture()
    assert types(events) == ["listening", "utterance"]
    assert events[1]["source"] == "ptt"


def test_summoned_into_an_empty_room_gives_up(listener):
    """A wake with no speech stops recording instead of running to the length cap."""
    listener.mode = "wake"
    frames(listener, 3, 30000)
    events = frames(listener, int(voice.VOICE_WAKE_TIMEOUT_S * 1000 / voice.FRAME_MS) + 1, 0)
    assert types(events) == ["listening"]           # no utterance
    assert not listener.capturing


def test_capture_stops_at_the_length_cap(listener):
    """A stuck-open mic does not hand ASR a recording of the room."""
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
    """Speech starts off the delta stream, not the finished turn."""
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
    """Sentences still queued when the turn is cut off are never sent to the model."""
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
    """A turn that is all table produces no audio."""
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
    """The wait for the speech backlog is interruptible."""
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
    """Concise-reply mode is fixed at session start with the voice flag."""
    assert "read aloud" not in console_prompt.render_append(voice=False)
    assert "read aloud" in console_prompt.render_append(voice=True)
