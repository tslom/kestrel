# Training "hey kestrel"

openWakeWord ships six phrases and no more: `alexa`, `hey_jarvis`,
`hey_mycroft`, `hey_rhasspy`, `timer`, `weather`. Any name of our own is a model
we train. This is that recipe.

The app is already built for the result: `VOICE_WAKE_WORD` takes either a
bundled name or a path to a `.onnx`, and the phrase shown in the UI is the
file's **stem** — so the model is named for what you say to it, and
`hey_kestrel.onnx` needs no second place to write the name down.

> Caveat worth stating plainly: the procedure below is upstream's, and it has
> not been run here. Sample counts and timings are theirs. What *has* been
> verified in this repo is the config-key list (read out of the installed
> `openwakeword/train.py`, v0.6.0) and the install-side behaviour.

## What you're actually training

Not a speech model. The heavy lifting is done by a frozen embedding model that
ships with openWakeWord; you're fitting a small classifier on top of it. That's
why this is an hour on a borrowed GPU rather than a research project, and why
none of it needs recordings of your voice:

- **Positives** are synthetic — piper TTS says "hey kestrel" a few tens of
  thousands of times across many voices, speeds and pitches, then gets mixed
  with room impulse responses and background noise so it isn't studio-clean.
- **Negatives** are precomputed features over thousands of hours of speech and
  noise, published by the project. You download features, not audio.

The consequence worth knowing: the model learns the phrase as *TTS voices* say
it. If your own pronunciation is unusual, that's where the misses come from —
not from too few samples.

## Where to run it

**Colab, `notebooks/automatic_model_training.ipynb` from the openwakeword
repo.** It wires up piper-sample-generator, the negative feature sets and the
augmentation data for you, and exports both `.onnx` and `.tflite`. We only need
the `.onnx` — there's no tflite-runtime wheel for Apple silicon, which is why
`Listener` passes `inference_framework="onnx"` in the first place.

Locally is possible but not worth it for a one-off: `torch` and
`piper-sample-generator` aren't installed here, and the negative feature files
are several GB. Neither belongs in `requirements.txt` — the app never trains,
it only loads.

## The config

`python -m openwakeword.train --training_config kestrel.yml --generate_clips
--augment_clips --train_model` (each stage is a separate flag; run them
together the first time). Keys the installed version reads:

| Key | What to put |
|---|---|
| `target_phrase` | `["hey kestrel"]` — a list; add spellings that sound the same if the TTS mangles it |
| `model_name` | `hey_kestrel` — this becomes the filename, and so the phrase the UI shows |
| `n_samples` / `n_samples_val` | upstream's automatic notebook uses tens of thousands / a few thousand |
| `custom_negative_phrases` | **the highest-leverage knob** — see below |
| `false_positive_validation_data_path` | the held-out hours the threshold is calibrated against |
| `target_false_positives_per_hour` | what "good" means; lower trades misses for quiet |
| `feature_data_files`, `background_paths`, `rir_paths` | the downloaded negative/noise/reverb sets |
| `piper_sample_generator_path` | checkout path for the TTS generator |
| `output_dir` | where the `.onnx` lands |
| `steps`, `batch_n_per_class`, `layer_size`, `max_negative_weight`, `model_type`, `augmentation_rounds`, `augmentation_batch_size`, `tts_batch_size`, `total_length`, `background_paths_duplication_rate` | leave at the notebook's defaults unless a result is bad |

### Negatives specific to this phrase

The generic negative corpus teaches it that ordinary speech isn't the wake word.
What it can't teach is which *near misses* matter for this particular phrase.
For "hey kestrel", the things worth listing in `custom_negative_phrases`:

- `kestrel` alone, and `hey kestrel` said as part of a longer sentence
- `hey Castro`, `hey crystal`, `hey Kestral`, `Chestnut`, `kettle`, `cul-de-sac`
- anything you actually say a lot that starts with "hey" — "hey, can you…"

That last one is the real-world failure: the model has "hey" reinforced by every
positive sample, so a phrase-initial "hey" plus any /k/ word is the shape most
likely to summon it by accident.

## Installing the result

```bash
mkdir -p backend/data/wake
cp hey_kestrel.onnx backend/data/wake/
```

Restart the backend. That is the whole install: `config.WAKE_MODEL` is that
exact path and `VOICE_WAKE_WORD` picks it up as soon as the file is there, so
there is no env var to set and no line to edit. Nothing else changes either —
`/api/voice/status` reports `wake_word: "hey kestrel"` and the console's
tooltip, the armed-state note and the `ready` frame all follow it. The model is
~1MB, so it belongs in git next to the code that loads it.

Until the file exists the wake word falls back to a pretrained phrase, which is
why the mic answers to "hey jarvis" while every other surface in the app says
kestrel. That fallback is deliberate: a configured-but-missing model file fails
inside `Listener`'s constructor, i.e. when the socket opens, and would take the
mic down with it. `VOICE_WAKE_WORD` still overrides both if you want to point
at a model somewhere else — but a path typo there is a wake word that never
fires, and silence is an ambiguous symptom.

## After it's in

Only one knob is worth touching: `VOICE_WAKE_THRESHOLD` (default 0.5). Raise it
if it summons itself, lower it if you have to say the phrase twice. Judge it by
leaving it armed for an evening rather than by testing it deliberately — the
false-positive rate is a property of ordinary conversation in the room, which is
exactly what you don't produce when you're checking whether it works.
