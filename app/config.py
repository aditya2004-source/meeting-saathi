import logging
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("meeting_saathi")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Gemini API (free tier -- used for document generation)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"

    # AssemblyAI (paid, pay-as-you-go) -- replaces local whisper+pyannote
    # for chunked/streaming diarization's pyannote-fallback branch only
    # (app.pipeline.diarize.diarize_chunk()). Confirmed in production: that
    # branch's local pyannote step took up to 630s for one ~50s chunk on
    # this 4-core box (CPU thread oversubscription); a single AssemblyAI
    # call does transcription + speaker diarization together, offloaded to
    # their infrastructure, at ~$0.17/hour of audio. The DOM-primary fast
    # path (the common case in a real multi-person meeting) is unaffected
    # and still free/local.
    assemblyai_api_key: str = ""
    # "auto" (default): unchanged pre-Phase-3 behavior -- AssemblyAI only
    # ever fires on diarize_chunk()'s sparse-DOM-coverage fallback branch,
    # local faster-whisper handles the common good-coverage case regardless
    # of whether a key is set. "assemblyai": genuinely primary -- the
    # good-coverage branch also uses AssemblyAI (transcription only, DOM
    # events still supply speaker attribution) instead of local
    # faster-whisper, so a VPS deploy never needs to run local Whisper for a
    # normal meeting. Requires assemblyai_api_key to be set; falls back to
    # "auto" behavior if it isn't (see app/pipeline/diarize.py).
    transcription_provider: str = "auto"

    # Local speech-to-text
    whisper_model_size: str = "small"
    whisper_compute_type: str = "int8"
    # CTranslate2 intra-op threads (parallelism within one transcribe() call)
    # and inter-op/pipeline workers (parallelism across concurrent transcribe()
    # calls, relevant once chunked/fallback diarization runs transcribe()
    # concurrently with pyannote). Was 4 (matching this box's core count),
    # but that's PER transcribe() call -- with orchestrator_streaming.py's
    # _CHUNK_EXECUTOR now allowing up to 2 chunks to process concurrently,
    # each pairing transcribe()+pyannote (2 more-way concurrency), 4 here
    # would let a single moment's CPU demand reach 2 chunks x 2 (transcribe
    # + pyannote) x 4 threads = 16 threads competing for 4 real cores.
    # Confirmed in production: this exact oversubscription drove pyannote's
    # embedding stage to 575s for one ~50s chunk. 2 keeps total in-flight
    # threads roughly matched to real core count instead.
    whisper_cpu_threads: int = 2
    # faster-whisper's BatchedInferencePipeline batch size -- ~2x CPU/int8
    # speedup per faster-whisper's own README benchmark.
    whisper_batch_size: int = 8
    # "translate" runs Whisper's built-in translate-to-English decoding
    # (no-op for already-English speech, translates any other detected
    # language directly) instead of the default "transcribe", so bilingual
    # Hindi/English meetings produce an English-only transcript before
    # Gemini ever sees it. Configurable so it's a one-line revert if
    # translation quality is ever worse than "transcribe" + Gemini-side
    # translation for some meeting.
    whisper_task: str = "translate"

    # Diarization ("who said what") — local pyannote.audio, since audio comes
    # from the Chrome extension as a single mixed recording, not per-speaker
    # tracks from a vendor. Free HuggingFace token, not a paid API.
    huggingface_token: str = ""
    # torch.set_num_threads() for pyannote's segmentation/embedding models --
    # pyannote has no thread setting of its own, it just inherits plain
    # PyTorch config. Reduced from 4 alongside whisper_cpu_threads above, for
    # the same reason: with up to 2 chunks processing concurrently, 4 here
    # would badly oversubscribe this box's 4 real cores (confirmed in
    # production -- see whisper_cpu_threads' comment for the real numbers).
    pyannote_torch_threads: int = 2
    # Chunked/streaming diarization: minimum fraction of a chunk's duration
    # that must be covered by Meet active-speaker-tile events (speaker_events)
    # before we trust them as the primary (fast, no audio ML) diarization
    # source; below this, fall back to running pyannote on that chunk.
    chunk_pyannote_coverage_threshold: float = 0.5
    # How many chunks' transcribe/diarize work app.orchestrator_streaming
    # runs concurrently (ThreadPoolExecutor(max_workers=...)). Was a fixed
    # module constant tuned to this one box's 4 real cores -- now
    # configurable since a Phase 9 VPS deploy (a different core count, and
    # with transcription_provider="assemblyai" offloading the CPU-heavy work
    # entirely) shouldn't be stuck with a constant sized for this machine.
    chunk_executor_max_workers: int = 2
    # speaker_from_dom_events() attributes a segment to the nearest
    # *preceding* DOM speaker-change event, carried forward indefinitely by
    # default. If the extension's MutationObserver stalls (tab backgrounded,
    # a Meet UI change it doesn't recognize) that stale event would otherwise
    # keep confidently mislabeling everyone as whoever spoke last before the
    # stall -- past this many seconds without a fresher event, attribution
    # reverts to "Unknown" instead.
    speaker_event_max_staleness_seconds: float = 300.0

    # Speaker reconciliation: a single recovery Gemini pass that collapses a
    # failed diarization's many "Unidentified speaker N" labels back into the
    # true, small participant set (see app/pipeline/speaker_reconcile.py and
    # app/docgen/reconcile_prompt.py). Only fires when at least
    # `speaker_reconcile_min_dominance` of the transcript (by character count)
    # is attributed to unidentified speakers AND at least
    # `speaker_reconcile_min_labels` distinct such labels exist -- a normal
    # meeting the DOM scrape named correctly never comes close (its dominance
    # stays well under `speaker_reconcile_min_dominance`), so this costs zero
    # extra Gemini calls in the common case.
    #
    # `min_labels` is 2 (not the old 6): on this deployment the Meet DOM
    # active-speaker scrape currently captures nothing, so essentially every
    # meeting arrives with all-placeholder labels and dominance ~1.0. At 2, a
    # normal 2-person call still gets the one recovery pass that pulls real
    # names out of the transcript; the dominance gate is what keeps a
    # well-diarized meeting from paying for it. One meeting/day, so the extra
    # call is negligible. Raise this again once the DOM scrape is fixed
    # (extension/content_script.js active-speaker selectors).
    speaker_reconcile_enabled: bool = True
    speaker_reconcile_min_dominance: float = 0.4
    speaker_reconcile_min_labels: int = 2

    # Quality mode: spend extra Gemini calls on getting ONE meeting's documents
    # as right as possible, rather than minimising calls across many meetings.
    # When on: extraction gets a second verification/gap-fill pass, and every
    # Gemini-written document gets a refine pass against the deterministic
    # validator's findings (see app/docgen/validate.py). Roughly doubles the
    # per-document call count (~14-17 calls for a full 8-document set) -- sized
    # for the free tier's 20 requests/day when the user processes one meeting a
    # day. A call that fails (e.g. quota exhausted mid-run) leaves the
    # un-refined version in place rather than failing the document.
    docgen_quality_mode: bool = True

    # Transactional email (OTP codes) -- see app/email_sender.py. Without a
    # key set, emails are logged instead of sent (loud, obvious, safe for a
    # from-scratch checkout or the test suite -- never silently pretends to
    # have delivered something it didn't).
    resend_api_key: str = ""
    email_from_address: str = "Meeting Saathi <onboarding@resend.dev>"

    # Phase 2: free-trial / subscription entitlement engine (see
    # app/entitlement.py). Every threshold here is deliberately generous --
    # these are safety ceilings and abuse guards, not the expected common
    # case ("unlimited" for a paid customer should stay true in practice).
    free_meeting_allowance: int = 3
    # Bounds start *attempts* per customer per day, separate from the free
    # allowance -- a failed/never-captured attempt doesn't burn free quota
    # (see entitlement.record_meeting_completed(), only called on success),
    # so without this a scripted caller could spam free /meetings/start
    # calls indefinitely at zero quota cost, still burning server resources.
    max_start_attempts_per_day: int = 10
    # A paid ("unlimited") customer crossing this many meetings in a
    # calendar month doesn't get blocked -- it just flags the account for
    # the founder to look at in the admin panel (Phase 7).
    fair_use_alert_threshold: int = 100
    # A genuine circuit-breaker, well above fair_use_alert_threshold -- only
    # reached by runaway abuse or a client-side bug, not real usage.
    fair_use_hard_ceiling: int = 300
    # Enforced server-side in /meetings/{run_id}/chunk and .../finalize
    # (see app/main.py) by comparing the chunk `sequence` against this
    # divided by the extension's fixed 50s chunk interval -- not yet
    # verified against Gemini's actual context/output limits at this length
    # (see Phase 3's audit note); don't advertise this publicly until that's
    # confirmed.
    max_meeting_duration_seconds: int = 10800  # 3 hours

    # Phase 7 (admin analytics): estimated per-meeting API cost, in INR --
    # deliberately an estimate, not tracked per-meeting (see
    # app/admin_analytics.py's module docstring for why). Gemini defaults to
    # 0 while on the free tier; update this once paid billing is enabled.
    # AssemblyAI's ~$0.17/hour at a rough reference INR rate.
    estimated_gemini_cost_per_call_inr: float = 0.0
    assemblyai_cost_per_hour_inr: float = 14.0

    # Razorpay (Phase 6: subscriptions). Get keys from the Razorpay
    # Dashboard > Settings > API Keys; the webhook secret is set separately
    # when configuring the webhook URL (Dashboard > Settings > Webhooks).
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # Output storage
    base_storage_dir: Path = Path.home() / "Downloads" / "Meeting Saathi"
    keep_raw_recording: bool = False
    # IANA timezone used to display meeting dates/times in generated
    # documents and the dashboard -- explicit rather than relying on the
    # server's ambient system timezone, so it's correct regardless of where
    # this is deployed.
    report_timezone: str = "Asia/Kolkata"

    # Local service
    port: int = 8420

    # On server startup, any meeting run still in a non-terminal state
    # (idle/received/transcribing/.../chunk_processing/...) whose last update
    # is older than this is marked "failed" -- the worker thread that was
    # driving it died with the previous process, so it would otherwise sit
    # "in progress" forever on the dashboard. Generous enough that a quick
    # restart mid-meeting still lets a live chunked recording resume (the
    # extension keeps POSTing chunks, refreshing updated_at well within this
    # window). See app.db.fail_stale_runs(), called from app/main.py's
    # startup hook. Set via STALE_RUN_MINUTES in .env; 0 disables the sweep.
    stale_run_minutes: int = 120

    # Multi-user sharing (Phase 1: sharing with BA testers) -- each caller
    # identifies itself with a plain user_name string (no login/password),
    # capped at this many meeting-starts per day to keep any one person from
    # burning the whole team's shared Gemini quota. See app.db.count_runs_today().
    daily_meeting_limit: int = 3
    # Admin dashboard: unguessable-URL + real login, replacing the old
    # shared-secret-in-a-query-string (?admin_token=...) scheme, which sat
    # in a query string on the same base URL (/) customers already know and
    # use every day. `admin_url_slug` is the only way to even reach the
    # login page at all -- `<server>/{admin_url_slug}/login` -- any other
    # slug 404s exactly like a route that doesn't exist, rather than
    # revealing "wrong password" (which would confirm admin functionality
    # lives there). `admin_username`/`admin_password` are then checked by
    # the login form itself. All set via ADMIN_URL_SLUG/ADMIN_USERNAME/
    # ADMIN_PASSWORD in .env.
    admin_url_slug: str = ""
    admin_username: str = ""
    admin_password: str = ""
    # Signs the session cookie set on successful login (Starlette's
    # SessionMiddleware -- see app/main.py) -- no server-side session store
    # needed for a single-owner admin panel. Set via SESSION_SECRET_KEY in
    # .env; changing it invalidates every existing session (forces re-login).
    session_secret_key: str = ""
    # Whether the session cookie gets the browser-enforced Secure attribute
    # (only sent back over HTTPS). True is correct for how this actually
    # runs: local systemd on 127.0.0.1:8420 behind a Cloudflare Tunnel that
    # terminates HTTPS at the edge -- real users only ever see the https://
    # tunnel URL, and both real browsers and curl treat 127.0.0.1/localhost
    # as a secure context too, so this doesn't break local
    # `curl http://127.0.0.1:8420/...` verification either.
    session_cookie_https_only: bool = True

    # Comma-separated list of allowed CORS origins, e.g.
    # "chrome-extension://abcdefghijklmnop,https://meetingsaathi.com". "*"
    # (the default, matching pre-Phase-1 behavior) keeps every origin allowed
    # -- safe to leave as-is for local/self-use, but should be set explicitly
    # once real customer sessions exist in production, since a wildcard is
    # unnecessarily permissive against a server that now holds real accounts.
    # Set via CORS_ALLOWED_ORIGINS in .env.
    cors_allowed_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        if self.cors_allowed_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    # Production-deployment hardening: FastAPI serves interactive API docs
    # (/docs, /redoc) and the raw OpenAPI schema (/openapi.json) publicly by
    # default. Harmless functionally (no secrets in the schema), but there's
    # no reason to expose the full route/shape inventory to the public
    # internet once real customer accounts exist -- default False so local
    # dev/testing is unaffected; set DISABLE_API_DOCS=true in production.
    disable_api_docs: bool = False

    # Internal paths -- project_root itself is not env-configurable (it's
    # derived from this file's own location), but db_path/working_dir are,
    # same pattern as base_storage_dir above. Needed for Railway: the
    # container filesystem is ephemeral and wiped on every redeploy, so a
    # real deployment points these three at a mounted persistent Volume
    # (e.g. DB_PATH=/data/runs.sqlite3, WORKING_DIR=/data/working) via env
    # vars. The defaults below are untouched, so the existing local/systemd
    # deployment needs zero env changes and keeps working exactly as before.
    project_root: Path = Path(__file__).resolve().parent.parent
    db_path: Path = project_root / "data" / "runs.sqlite3"
    working_dir: Path = project_root / "working"


def validate_settings(candidate: "Settings") -> None:
    """Fail loudly on dangerous-blank config; warn on merely-inconvenient blanks.

    A blank session_secret_key would silently sign every session cookie with
    the empty string -- SessionMiddleware doesn't refuse to start, it just
    makes the signature meaningless (anyone can forge a valid-looking session
    cookie). Real deployments (including this project's own .env) always set
    this; only a from-scratch checkout without a .env would ever hit this.

    Admin credentials being blank is already fail-closed (the login/dashboard
    routes 404/reject rather than allow access), so it doesn't raise -- but
    it's worth surfacing, since "admin panel unreachable" is easy to mistake
    for a bug rather than an unset .env value.
    """
    if not candidate.session_secret_key:
        raise RuntimeError(
            "SESSION_SECRET_KEY is not set. Set it in .env before starting "
            "the server -- an empty secret would sign session cookies with "
            "a constant, forgeable value."
        )
    if not (candidate.admin_url_slug and candidate.admin_username and candidate.admin_password):
        logger.warning(
            "ADMIN_URL_SLUG/ADMIN_USERNAME/ADMIN_PASSWORD are not all set -- "
            "the admin dashboard is unreachable until they are."
        )


settings = Settings()
validate_settings(settings)
settings.base_storage_dir.mkdir(parents=True, exist_ok=True)
settings.working_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
