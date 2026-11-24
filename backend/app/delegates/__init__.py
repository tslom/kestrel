"""Other models (Antigravity, Codex via their CLIs), sent on errands.

Claude is the orchestrator and the only model that sees personal data. Modules:
boundary.py (what an outside agent gets and reaches; the only place an outside CLI
starts), registry.py (providers and readiness), store.py (tables), accounts.py (sign-ins,
usage, account rules), runs.py (errands and variant batches), api.py (/api/delegates/*).
Held by tests/test_delegate_boundary.py. Design: DESIGN.md → Other models.
"""
