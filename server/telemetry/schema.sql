-- One row per install per UTC day. Apply with:
--   npx wrangler d1 execute bvt-telemetry --remote --file schema.sql
CREATE TABLE IF NOT EXISTS pings (
  day              TEXT    NOT NULL,  -- YYYY-MM-DD (UTC)
  id               TEXT    NOT NULL,  -- random install ID from the app
  app              TEXT    NOT NULL,
  build            TEXT    NOT NULL,  -- installer / source
  provider         TEXT    NOT NULL,
  country          TEXT,              -- 2-letter code from Cloudflare
  dictations       INTEGER NOT NULL DEFAULT 0,
  meeting_sessions INTEGER NOT NULL DEFAULT 0,
  phone_sessions   INTEGER NOT NULL DEFAULT 0,
  failures         INTEGER NOT NULL DEFAULT 0,
  minutes          INTEGER NOT NULL DEFAULT 0,
  pings            INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (day, id)
);
