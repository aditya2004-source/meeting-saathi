"""Phase 7: founder admin analytics -- Overview/Customers/Subscriptions/
Usage/Expenses/Feedback. Every number here comes from app.admin_analytics's
real queries; nothing is hardcoded or simulated. All routes are gated the
same way the existing /{slug}/dashboard already is (see app/main.py) --
slug must match settings.admin_url_slug (else 404, never revealing that
admin functionality exists at all) and the session must be a real logged-in
admin.
"""
import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import admin_analytics, auth, db
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


def _guarded(request: Request, slug: str) -> RedirectResponse | JSONResponse | None:
    """Returns a response to short-circuit with, or None if the caller may
    proceed -- same not-found-for-wrong-slug / redirect-to-login-if-not-admin
    pattern as app.main's existing admin routes.
    """
    if slug != settings.admin_url_slug or not settings.admin_url_slug:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not auth.is_admin_session(request):
        return RedirectResponse(url=f"/{slug}/login", status_code=303)
    return None


def _period_params(request: Request) -> tuple[str, str, str, str, str]:
    """Returns (preset, from_date, to_date, start_iso, end_iso)."""
    preset = request.query_params.get("preset", "30d")
    from_date = request.query_params.get("from", "")
    to_date = request.query_params.get("to", "")
    start, end = admin_analytics.resolve_period(preset, from_date, to_date)
    return preset, from_date, to_date, start, end


@router.get("/{slug}/overview")
def admin_overview(request: Request, slug: str):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard

    preset, from_date, to_date, start, end = _period_params(request)
    revenue = admin_analytics.revenue_in_period_inr(start, end)
    cost = admin_analytics.estimated_cost_in_period_inr(start, end)
    plan_mix = admin_analytics.plan_mix()
    total_customers = sum(plan_mix.values())
    revenue_cost_profit = admin_analytics.revenue_cost_profit_by_month()
    meeting_volume_weekly = admin_analytics.meeting_volume_by_week()
    document_usage = admin_analytics.document_usage()

    return templates.TemplateResponse(
        "admin/overview.html",
        {
            "request": request,
            "slug": slug,
            "active": "overview",
            "page_title": "Overview",
            "preset": preset,
            "from_date": from_date,
            "to_date": to_date,
            "active_users": admin_analytics.count_active_users(start, end),
            "new_users": admin_analytics.count_new_users(start, end),
            "paid_users": admin_analytics.count_paid_users(),
            "free_users": admin_analytics.count_free_users(),
            "mrr": admin_analytics.compute_mrr_inr(),
            "revenue": revenue,
            "cost": cost,
            "net_profit": round(revenue - cost["total"], 2),
            "conversion": admin_analytics.free_to_paid_conversion(),
            "meeting_volume": admin_analytics.meeting_volume(start, end),
            "success_rate": admin_analytics.processing_success_rate(start, end),
            "avg_duration_seconds": admin_analytics.average_duration_seconds(start, end),
            "revenue_cost_profit": revenue_cost_profit,
            "max_rcp": max((m["revenue"] for m in revenue_cost_profit), default=0) or 1,
            "plan_mix": plan_mix,
            "total_customers": total_customers,
            "meeting_volume_weekly": meeting_volume_weekly,
            "max_weekly": max((c for _, c in meeting_volume_weekly), default=0) or 1,
            "document_usage": document_usage,
            "max_doc_generated": max((v["generated"] for v in document_usage.values()), default=0) or 1,
        },
    )


@router.get("/{slug}/customers")
def admin_customers(request: Request, slug: str):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard
    return templates.TemplateResponse(
        "admin/customers.html",
        {
            "request": request,
            "slug": slug,
            "active": "customers",
            "page_title": "Customers",
            "customers": db.list_customers(),
        },
    )


@router.post("/{slug}/customers/{customer_id}/status")
def admin_set_customer_status(request: Request, slug: str, customer_id: str, status: str = Form(...)):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard
    if status not in ("active", "blocked"):
        return JSONResponse({"error": "invalid status"}, status_code=400)
    db.set_customer_status(customer_id, status)
    return RedirectResponse(url=f"/{slug}/customers", status_code=303)


@router.get("/{slug}/subscriptions")
def admin_subscriptions(request: Request, slug: str):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard

    with db._connect() as conn:  # noqa: SLF001 - admin-only reporting query
        rows = conn.execute(
            """SELECT s.*, c.email AS customer_email, c.name AS customer_name
               FROM subscriptions s JOIN customers c ON c.id = s.customer_id
               ORDER BY s.created_at DESC"""
        ).fetchall()
    subscriptions = [dict(row) for row in rows]
    payments = db.list_payments()

    return templates.TemplateResponse(
        "admin/subscriptions.html",
        {
            "request": request,
            "slug": slug,
            "active": "subscriptions",
            "page_title": "Subscriptions",
            "subscriptions": subscriptions,
            "payments": payments,
            "mrr": admin_analytics.compute_mrr_inr(),
            "monthly_active": sum(1 for s in subscriptions if s["plan"] == "monthly" and s["status"] == "active"),
            "yearly_active": sum(1 for s in subscriptions if s["plan"] == "yearly" and s["status"] == "active"),
        },
    )


@router.get("/{slug}/usage")
def admin_usage(request: Request, slug: str):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard

    preset, from_date, to_date, start, end = _period_params(request)
    document_usage = admin_analytics.document_usage()

    return templates.TemplateResponse(
        "admin/usage.html",
        {
            "request": request,
            "slug": slug,
            "active": "usage",
            "page_title": "Usage",
            "preset": preset,
            "from_date": from_date,
            "to_date": to_date,
            "meeting_volume": admin_analytics.meeting_volume(start, end),
            "success_rate": admin_analytics.processing_success_rate(start, end),
            "avg_duration_seconds": admin_analytics.average_duration_seconds(start, end),
            "document_usage": document_usage,
        },
    )


@router.get("/{slug}/expenses")
def admin_expenses(request: Request, slug: str):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard

    preset, from_date, to_date, start, end = _period_params(request)
    cost = admin_analytics.estimated_cost_in_period_inr(start, end)
    revenue = admin_analytics.revenue_in_period_inr(start, end)
    meeting_count = admin_analytics.meeting_volume(start, end)

    return templates.TemplateResponse(
        "admin/expenses.html",
        {
            "request": request,
            "slug": slug,
            "active": "expenses",
            "page_title": "Expenses",
            "preset": preset,
            "from_date": from_date,
            "to_date": to_date,
            "cost": cost,
            "revenue": revenue,
            "net_profit": round(revenue - cost["total"], 2),
            "cost_per_meeting": round(cost["total"] / meeting_count, 2) if meeting_count else 0,
            "fixed_costs": db.list_fixed_costs(),
            "current_month": datetime.date.today().strftime("%Y-%m"),
        },
    )


@router.post("/{slug}/expenses/fixed-costs")
def admin_add_fixed_cost(
    request: Request, slug: str, effective_month: str = Form(...), amount_inr: float = Form(...), note: str = Form("")
):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard
    db.add_fixed_cost(effective_month=effective_month, amount_inr=amount_inr, note=note.strip())
    return RedirectResponse(url=f"/{slug}/expenses", status_code=303)


@router.get("/{slug}/admin-feedback")
def admin_feedback(request: Request, slug: str):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard
    return templates.TemplateResponse(
        "admin/feedback.html",
        {
            "request": request,
            "slug": slug,
            "active": "feedback",
            "page_title": "Feedback",
            "feedback": db.list_feedback(),
            "category_counts": admin_analytics.feedback_category_counts(),
        },
    )


@router.post("/{slug}/admin-feedback/{feedback_id}/resolve")
def admin_resolve_feedback(request: Request, slug: str, feedback_id: str):
    guard = _guarded(request, slug)
    if guard is not None:
        return guard
    db.resolve_feedback(feedback_id)
    return RedirectResponse(url=f"/{slug}/admin-feedback", status_code=303)
