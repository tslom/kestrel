"""HTTP route modules: thin FastAPI glue over providers/ and db, owning no state.
Domain modules that carry a router (`widgets`, `people`, `lists`, `brief`, `leave`,
`trends`, `console`, `voice`) stay at `app/` top level. The router list lives in main.py.
"""
