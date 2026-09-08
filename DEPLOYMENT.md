# Deployment runbook (Phase 9)

Everything code-side for this is done and tested. The steps below need real
accounts/credentials/decisions that only the founder can provide -- this
document is what to actually do, in order, to go live.

## 1. Provision a VPS

- 2 vCPU / 4GB RAM is reasonable **once Phase 3's `TRANSCRIPTION_PROVIDER=assemblyai`
  is actually set** (a real AssemblyAI key is required for that to take effect --
  see step 3). Without it, local Whisper/pyannote runs on this box for every
  meeting; budget 4 vCPU+ instead if deferring that.
- Any provider works (Hetzner, DigitalOcean, etc.) -- just needs Docker installed.

## 2. Point a domain at it

- Buy/use a domain, add an A record pointing at the VPS's IP.
- Update `Caddyfile` in this repo: replace `meetingsaathi.example.com` with the
  real domain.
- Update `Admin personal/extension(personal)/manifest.json`'s
  `externally_connectable.matches` -- replace the same placeholder with
  `https://<your-real-domain>/*` (keep `http://localhost:8420/*` for local dev).
  Re-run `python scripts/build_extension_zip.py` afterward so the downloadable
  zip reflects the change.
- Update `Admin personal/extension(personal)/background.js`, `offscreen.js`,
  AND `popup.js`'s `DEFAULT_SERVER_BASE_URL` constant to the real
  `https://<your-domain>` -- all THREE files independently hardcode this, no
  shared config (see their own comments; popup.js's copy is easy to miss).

## 3. Fill in real secrets

Copy `.env.example` to `.env` on the VPS (never commit `.env`) and fill in:

- `GEMINI_API_KEY` -- **the app will not start at all without this set** (confirmed
  via a real container boot test: `app/docgen/engine.py` constructs its Gemini
  client at import time, so a missing key crashes every route, not just document
  generation) -- already have one; **enable paid billing** before real volume
  (the free tier's 20/day + 250k-tokens/minute caps will be hit fast, confirmed
  live in Phase 3's spike test).
- `ASSEMBLYAI_API_KEY` + `TRANSCRIPTION_PROVIDER=assemblyai` -- required for the
  VPS sizing above to hold; get a key at assemblyai.com.
- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` -- from Razorpay Dashboard > Settings
  > API Keys. Start in **test mode** and run Phase 10's full journey test before
  switching to live keys.
- `RAZORPAY_WEBHOOK_SECRET` -- set when you configure the webhook URL at
  Razorpay Dashboard > Settings > Webhooks, pointed at
  `https://<your-domain>/billing/webhook/razorpay`. Enable at minimum:
  `subscription.activated`, `subscription.charged`, `subscription.completed`,
  `subscription.cancelled`, `subscription.paused`, `payment.failed`.
- Create the real Razorpay Plans (Dashboard > Subscriptions > Plans, one per
  billing_cycle+currency combination) and paste their real `plan_...` ids into
  `app/billing/plans.py`, replacing the `plan_placeholder_...` values.
- `RESEND_API_KEY` -- from resend.com (100 emails/day free), so OTP codes
  actually get emailed instead of only logged.
- `SESSION_SECRET_KEY`, `ADMIN_URL_SLUG`, `ADMIN_USERNAME`, `ADMIN_PASSWORD` --
  generate real random values: `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.
- `CORS_ALLOWED_ORIGINS` -- set explicitly once you know the extension's real
  id (see manifest.json's pinned `key` -- the id is
  `bflaaogdbjndnpjadgdnliajmfoihjgf` as long as that key isn't changed) and the
  real domain, e.g. `chrome-extension://bflaaogdbjndnpjadgdnliajmfoihjgf,https://<your-domain>`.
- `DISABLE_API_DOCS=true` -- turns off the public `/docs`, `/redoc`, and
  `/openapi.json` routes FastAPI serves by default. Pure hardening, no
  functional impact; leave unset for local dev.

## 4. Deploy

```
git clone <this repo> && cd meeting-saathi
# fill in .env, Caddyfile, manifest.json as above
docker compose up -d --build
```

Confirm `https://<your-domain>/healthz` returns `{"ok": true, "db": true}`.

## 5. Backups

`scripts/backup.py` + `scripts/reap_working_dirs.py` already exist (Phase 0).
`scripts/backup.py` writes to `<project_root>/backups`, which inside the
container is `/app/backups` -- part of the container's own writable layer,
NOT the persisted `/data` volume. `docker-compose.yml` already bind-mounts
`./backups:/app/backups` on the host for exactly this reason: without it,
every backup would be silently destroyed on the next `docker compose up -d
--build`. Run backup.py via `docker compose exec -T app python3
scripts/backup.py --keep 7` (note: `scripts/systemd/meeting-saathi-backup.*`
below are bare-metal templates from before this project used Docker --
adapt their `ExecStart` to the `docker compose exec` form, they don't work
as-is against a container deployment) and set that up on a cron/systemd-timer
pointed at the container's `/data` volume. **Actually restore a backup once**
to confirm it works, don't just assume the script does.

## 6. Monitoring

Add a free-tier error tracker (e.g. Sentry's free tier) -- set its DSN as an
env var and wire it into `app/main.py`'s exception handling once chosen; not
done in this codebase yet, deliberately left as a founder choice since it
requires creating an account. At minimum, set up a free uptime monitor
(e.g. UptimeRobot) pinging `/healthz` every few minutes.

## 7. Chrome Web Store (optional but recommended)

Sideloading (this repo's current "Load Unpacked" install flow) works but
shows Chrome's "not from the store" friction and has no auto-update. Publish
to the Chrome Web Store ($5 one-time) once stable -- `/install`'s "Download
ZIP" step becomes a store link instead; nothing else in the pairing/auth flow
changes.

## Before real customers touch it

Run Phase 10's full manual journey end-to-end against this real deployment
in Razorpay test mode first -- see the plan file's Phase 10 section.
