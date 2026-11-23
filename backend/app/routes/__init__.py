"""HTTP route modules — thin FastAPI glue over the domain and provider layers.

Nothing here owns state or rules. A module lands in this package when its whole
job is to expose something that already exists (`providers/`, `db`) over HTTP;
domain modules that happen to carry a router (`widgets`, `people`, `lists`,
`brief`, `leave`, `trends`, `console`, `voice`) stay at `app/` top level,
because moving them here would file the logic under a name that claims it's
only routing.

Wired in `main.py`, which is the one place the router list lives.
"""
