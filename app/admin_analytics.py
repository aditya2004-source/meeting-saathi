"""Phase 7: query functions backing the founder admin analytics pages.
Kept out of app/db.py (which already has ~40 functions) per the plan's own
suggestion. Every number here is a real query over real tables -- nothing
here is simulated/hardcoded. Where a figure is inherently an estimate
(API cost per meeting), it's clearly named `estimated_...` and the
Expenses page labels it as such, distinct from `payments`-table-backed
actual revenue figures.

Cost estimation is deliberately formulaic rather than tracked per-meeting
(no gemini_call_count/transcription_minutes columns on meeting_runs, unlike
the plan's original suggestion) -- instrumenting every real Gemini/
AssemblyAI call site to count exact usage would touch the working
pipeline's internals for a number that's already only ever presented as an
estimate. Instead: a documented constant call count (app/config.py's own
comment already states "~9 calls" per meeting under docgen_quality_mode)
times a configurable per-call cost, plus AssemblyAI's per-hour rate applied
to the meeting's already-stored real `duration_seconds` when
transcription_provider is "assemblyai". Simpler, no pipeline changes, and
honestly still just an estimate either way.
"""
import datetime
from typing import Any, Optional

from app import db
from app.billing import plans
from app.config import settings

# Matches app/config.py's own documented figure for docgen_quality_mode's
# per-meeting call count (3 extraction + 3 generate + 3 refine).
ESTIMATED_GEMINI_CALLS_PER_MEETING = 9


def resolve_period(preset: str, from_date: str = "", to_date: str = "") -> tuple[str, str]:
    """Returns (start_iso, end_iso) for SQL string comparison against
    created_at. An explicit from_date/to_date (the separate custom-range
    control) always wins over a preset -- the two are mutually exclusive
    controls, not one dropdown with a "Custom" option mixed in.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    if from_date or to_date:
        start = f"{from_date}T00:00:00" if from_date else "0001-01-01"
        end = f"{to_date}T23:59:59" if to_date else now.isoformat()
        return start, end

    end = now.isoformat()
    if preset == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif preset == "7d":
        start = (now - datetime.timedelta(days=7)).isoformat()
    elif preset == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif preset == "all":
        start = "0001-01-01"
    else:  # "30d" default
        start = (now - datetime.timedelta(days=30)).isoformat()
    return start, end


def _scalar(query: str, params: tuple = ()) -> Any:
    with db._connect() as conn:  # noqa: SLF001 - analytics needs raw aggregate queries db.py doesn't expose
        row = conn.execute(query, params).fetchone()
    return row[0] if row else None


# --- Users -----------------------------------------------------------------


def count_active_users(start: str, end: str) -> int:
    return _scalar(
        "SELECT COUNT(*) FROM customers WHERE last_active_at IS NOT NULL AND last_active_at BETWEEN ? AND ?",
        (start, end),
    ) or 0


def count_new_users(start: str, end: str) -> int:
    return _scalar("SELECT COUNT(*) FROM customers WHERE created_at BETWEEN ? AND ?", (start, end)) or 0


def count_paid_users() -> int:
    return _scalar(
        "SELECT COUNT(DISTINCT customer_id) FROM subscriptions WHERE status = 'active'"
    ) or 0


def count_free_users() -> int:
    total = _scalar("SELECT COUNT(*) FROM customers") or 0
    return total - count_paid_users()


def free_to_paid_conversion() -> float:
    total_customers = _scalar("SELECT COUNT(*) FROM customers") or 0
    if total_customers == 0:
        return 0.0
    paying = _scalar("SELECT COUNT(DISTINCT customer_id) FROM payments WHERE status = 'captured'") or 0
    return round(100 * paying / total_customers, 1)


# --- Revenue / MRR -----------------------------------------------------------


def compute_mrr_inr() -> float:
    """Sum of every active subscription's monthly-equivalent INR value
    (yearly plans divided by 12), from the plan's configured display price
    (app.billing.plans) -- not from actual payment amounts, since MRR is a
    forward-looking run-rate metric, not a historical revenue sum.
    """
    with db._connect() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT plan, currency FROM subscriptions WHERE status = 'active'").fetchall()
    total = 0.0
    for row in rows:
        plan = plans.get_plan(row["plan"], row["currency"])
        if plan is None:
            continue
        monthly_value = plan.display_price_minor / 100
        if plan.billing_cycle == "yearly":
            monthly_value /= 12
        # Only INR plans contribute directly; a non-INR plan's contribution
        # to an INR-denominated MRR would need a live FX rate -- out of
        # scope here, flagged rather than silently misconverted.
        if plan.currency == "INR":
            total += monthly_value
    return round(total, 2)


def revenue_in_period_inr(start: str, end: str) -> float:
    """Actual settled revenue, from real payments rows -- uses
    inr_settlement_amount_minor when present (international payments),
    else original_amount_minor (already INR).
    """
    with db._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            """SELECT original_currency, original_amount_minor, inr_settlement_amount_minor
               FROM payments WHERE status = 'captured' AND created_at BETWEEN ? AND ?""",
            (start, end),
        ).fetchall()
    total_minor = 0
    for row in rows:
        if row["inr_settlement_amount_minor"] is not None:
            total_minor += row["inr_settlement_amount_minor"]
        elif row["original_currency"] == "INR":
            total_minor += row["original_amount_minor"]
        # A non-INR payment with no settlement figure yet is excluded
        # rather than guessed at -- see the Phase 6 note on verifying
        # Razorpay's exact settlement field name against a real payload.
    return round(total_minor / 100, 2)


# --- Meetings / usage --------------------------------------------------------


def meeting_volume(start: str, end: str) -> int:
    return _scalar("SELECT COUNT(*) FROM meeting_runs WHERE created_at BETWEEN ? AND ?", (start, end)) or 0


def processing_success_rate(start: str, end: str) -> float:
    with db._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            """SELECT
                   SUM(CASE WHEN state = 'saved' THEN 1 ELSE 0 END) AS saved,
                   SUM(CASE WHEN state IN ('saved', 'failed') THEN 1 ELSE 0 END) AS terminal
               FROM meeting_runs WHERE created_at BETWEEN ? AND ?""",
            (start, end),
        ).fetchone()
    if not row or not row["terminal"]:
        return 0.0
    return round(100 * row["saved"] / row["terminal"], 1)


def average_duration_seconds(start: str, end: str) -> float:
    return _scalar(
        "SELECT AVG(duration_seconds) FROM meeting_runs WHERE state = 'saved' AND created_at BETWEEN ? AND ?",
        (start, end),
    ) or 0.0


def plan_mix() -> dict[str, int]:
    """{"free": n, "monthly": n, "yearly": n} -- current snapshot, not
    period-scoped (a plan-mix trend over time would need subscription_events
    cohort analysis, out of scope for v1)."""
    total_customers = _scalar("SELECT COUNT(*) FROM customers") or 0
    with db._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT plan, COUNT(DISTINCT customer_id) AS n FROM subscriptions WHERE status = 'active' GROUP BY plan"
        ).fetchall()
    mix = {"monthly": 0, "yearly": 0}
    for row in rows:
        if row["plan"] in mix:
            mix[row["plan"]] = row["n"]
    mix["free"] = max(0, total_customers - mix["monthly"] - mix["yearly"])
    return mix


def meeting_volume_by_week(weeks: int = 6) -> list[tuple[str, int]]:
    """[(week_label, count), ...] oldest first -- backs the meeting-volume
    trend chart."""
    now = datetime.datetime.now(datetime.timezone.utc)
    results = []
    for i in range(weeks - 1, -1, -1):
        week_end = now - datetime.timedelta(days=7 * i)
        week_start = week_end - datetime.timedelta(days=7)
        count = meeting_volume(week_start.isoformat(), week_end.isoformat())
        results.append((f"W-{i}" if i else "This wk", count))
    return results


def revenue_cost_profit_by_month(months: int = 6) -> list[dict[str, Any]]:
    """[{month, revenue, cost, profit}, ...] oldest first."""
    now = datetime.datetime.now(datetime.timezone.utc)
    results = []
    for i in range(months - 1, -1, -1):
        # First-of-month arithmetic without a dependency -- step back i
        # months from the current first-of-month.
        year = now.year
        month = now.month - i
        while month <= 0:
            month += 12
            year -= 1
        start = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
        if month == 12:
            end = datetime.datetime(year + 1, 1, 1, tzinfo=datetime.timezone.utc)
        else:
            end = datetime.datetime(year, month + 1, 1, tzinfo=datetime.timezone.utc)
        revenue = revenue_in_period_inr(start.isoformat(), end.isoformat())
        cost = estimated_cost_in_period_inr(start.isoformat(), end.isoformat())["total"]
        results.append({"month": start.strftime("%b"), "revenue": revenue, "cost": cost, "profit": round(revenue - cost, 2)})
    return results


# --- Costs -------------------------------------------------------------------


def estimated_cost_per_meeting_inr(run: dict) -> float:
    gemini_cost = ESTIMATED_GEMINI_CALLS_PER_MEETING * settings.estimated_gemini_cost_per_call_inr
    transcription_cost = 0.0
    if settings.transcription_provider == "assemblyai" and run.get("duration_seconds"):
        transcription_cost = (run["duration_seconds"] / 3600.0) * settings.assemblyai_cost_per_hour_inr
    return round(gemini_cost + transcription_cost, 2)


def estimated_cost_in_period_inr(start: str, end: str) -> dict[str, float]:
    """{"gemini": x, "assemblyai": y, "fixed": z, "total": x+y+z} -- gemini/
    assemblyai are estimates (see module docstring); fixed is the real,
    manually-entered app.db.fixed_costs total for any month overlapping
    this period.
    """
    with db._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT duration_seconds FROM meeting_runs WHERE state = 'saved' AND created_at BETWEEN ? AND ?",
            (start, end),
        ).fetchall()
    meeting_count = len(rows)
    gemini_total = meeting_count * ESTIMATED_GEMINI_CALLS_PER_MEETING * settings.estimated_gemini_cost_per_call_inr
    assemblyai_total = 0.0
    if settings.transcription_provider == "assemblyai":
        total_hours = sum((row["duration_seconds"] or 0) for row in rows) / 3600.0
        assemblyai_total = total_hours * settings.assemblyai_cost_per_hour_inr

    start_month = start[:7]
    end_month = end[:7]
    fixed_total = 0.0
    for cost in db.list_fixed_costs():
        if start_month <= cost["effective_month"] <= end_month:
            fixed_total += cost["amount_inr"]

    total = gemini_total + assemblyai_total + fixed_total
    return {
        "gemini": round(gemini_total, 2),
        "assemblyai": round(assemblyai_total, 2),
        "fixed": round(fixed_total, 2),
        "total": round(total, 2),
    }


# --- Documents / feedback -----------------------------------------------------


def document_usage() -> dict[str, dict[str, int]]:
    """{doc_key: {"generated": n, "viewed": n, "downloaded": n}}."""
    from app.docgen import registry

    result = {}
    for event_type in ("generated", "viewed", "downloaded"):
        counts = db.count_document_events_by_key(event_type)
        for doc_key in registry.DOCUMENTS:
            result.setdefault(doc_key, {}).setdefault(event_type, 0)
            result[doc_key][event_type] = counts.get(doc_key, 0)
    return result


def feedback_category_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in db.list_feedback():
        counts[row["category"]] = counts.get(row["category"], 0) + 1
    return counts


def payments_by_currency(start: str, end: str) -> dict[str, int]:
    """Count of captured payments per original currency, in period -- the
    plan's "market distribution" proxy signal (currency of payment, not
    real IP geolocation)."""
    with db._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            """SELECT original_currency, COUNT(*) AS n FROM payments
               WHERE status = 'captured' AND created_at BETWEEN ? AND ?
               GROUP BY original_currency""",
            (start, end),
        ).fetchall()
    return {row["original_currency"]: row["n"] for row in rows}
