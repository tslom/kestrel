"""Other models, sent on errands.

Claude is the orchestrator and the only model that sees anything personal:
the vault, the Brain, texts, email, money, contacts. Gemini and ChatGPT are
driven through their own CLIs (`gemini`, `codex`) for grunt work, and for
"make N versions" fan-outs that Claude then judges.

  boundary.py   what an outside agent can be given and can reach. The only
                place an outside CLI is started. Stdlib and config only.
  registry.py   which providers exist, and whether each is installed, signed
                in and switched on.
  store.py      the two tables: the switches and the run log.
  runs.py       one errand, or a batch of variants across providers.
  api.py        /api/delegates/*: the providers page, a batch and its files.

The rules are held by tests/test_delegate_boundary.py, not by this paragraph.
"""
