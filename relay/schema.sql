-- Location relay store. A queue, not an archive: rows live here only until the
-- backend pulls and acknowledges them. app.db's location_points is the record.
CREATE TABLE IF NOT EXISTS points (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- pull watermark; never reused
  msg         TEXT NOT NULL,                      -- one OwnTracks location message, verbatim JSON
  received_at INTEGER NOT NULL                    -- epoch ms at the relay
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,                         -- 'last_contact': epoch ms of the phone's latest request
  value TEXT NOT NULL
);
