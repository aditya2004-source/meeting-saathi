# Admin personal — the old working Meeting Saathi (server-based)

This is the version that worked before the standalone rebuild (phases 1–6).
Restored from git `HEAD` on 2026-09-01. Use this for your own meetings while the
standalone extension is still being finished.

```
Admin personal/
  extension(personal)/   ← load THIS folder in chrome://extensions (Load unpacked)
```

## What this version needs (all already set up on this machine)

| Piece | Status | How to run |
|---|---|---|
| **Python server** on `http://localhost:8420` | running as a systemd user service | `systemctl --user status meeting-saathi.service` — start with `systemctl --user start meeting-saathi.service`, or manually: `cd ~/projects/meeting-saathi && uvicorn app.main:app --host 127.0.0.1 --port 8420` |
| **Gemini API key** | in `~/projects/meeting-saathi/.env` (`GEMINI_API_KEY=…`) | already there |
| **Whisper / pyannote models** | downloaded | already there |

Verified 2026-09-01: `meeting-saathi.service` = active, `curl localhost:8420` = HTTP 200.

## Load it in Chrome

1. `chrome://extensions` → make sure the shareable **"Meeting Saathi"** (v2.0.0,
   the `extension/` folder) is **not** also loaded — run only one at a time.
2. **Load unpacked** → select `~/projects/meeting-saathi/Admin personal/extension(personal)`
3. Card should say **"Meeting Saathi (Personal)"**, version **1.0.0**, description
   "…sends them to your local Meeting Saathi…", site access includes `localhost:8420`.

**Naming (so the two never get mixed up):**
- **"Meeting Saathi (Personal)"** = this folder — your private build, talks to the
  Python server on your laptop. Do not share it.
- **"Meeting Saathi"** = `~/projects/meeting-saathi/extension/` (v2.0.0) — the
  server-less build you hand to other people (their own Gemini key).
4. Popup → grant mic once → join a Meet → click the icon → record. The server
   transcribes (Whisper + pyannote) and generates the documents; watch progress
   at `http://localhost:8420`.

## The standalone rebuild lives separately

`~/projects/meeting-saathi/extension/` (v2.0.0) — no server, your own Gemini key,
still being fixed. Don't load both at once.

## Caveat on this old server's document generation

`app/docgen/*` has in-progress changes (BRD/FRD/User-Stories removed, SOW added).
The transcript + MOM + Meeting Analysis path is fine; some other document buttons
may error. To get an exactly-as-committed server: `git stash` in the project root
(this also parks the standalone work — recover it with `git stash pop`).
