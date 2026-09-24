# Persistence on Railway (SQLite volume)

Filter templates, active markets, and filter settings live in SQLite.
Railway’s container filesystem is **ephemeral** — every redeploy wipes files
under `/app` unless they sit on a Volume.

## One-time setup

1. In Railway → your service → **Volumes** → create a volume.
2. Mount path: **`/data`**
3. Variables → set **`DB_PATH=/data/hw_bot.db`**
4. Redeploy once after the volume is attached.

After that, settings survive forever across deploys/restarts.

## What is stored where

| Data | Location |
|------|----------|
| Templates, active markets, filters, watchlist, signals | SQLite at `DB_PATH` (cloud volume) |
| Mini App `localStorage` (`hw_fbo_tpl`, etc.) | Browser cache only — not source of truth |

On startup the bot resolves `DB_PATH` (env, else `/data/hw_bot.db` if `/data`
is writable, else `./hw_bot.db` for local). Parent dirs are created; if the
volume path is empty but a legacy `./hw_bot.db` / `/app/hw_bot.db` still exists
in the image layer, it is copied once into the volume.

## Recovery note

If templates were lost *before* a volume was attached, pull/restore the old
Railway volume or DB dump if you have one. Do not invent template contents.
