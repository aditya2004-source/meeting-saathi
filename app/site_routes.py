"""Phase 4: the public website -- landing, pricing, how-it-works, legal
pages, signup/consent/OTP flow, guided install, and feedback submission.
Every prototype-simulated action here calls the real Phase 1 backend
(app.auth_routes) instead of faking state client-side; see the plan's
Phase 4 section for the full prototype-action -> real-backend mapping.
"""
from pathlib import Path

import markdown as markdown_lib
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, db
from app.billing import plans as plans_module
from app.docgen.render_pdf import extract_mermaid_blocks

router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

_SAMPLE_DOCS_DIR = Path(__file__).resolve().parent / "sample_documents"
_SAMPLE_DOCS = [
    ("mom", "MOM", _SAMPLE_DOCS_DIR / "mom_sample.md"),
    ("meeting_analysis", "Meeting Analysis", _SAMPLE_DOCS_DIR / "meeting_analysis_sample.md"),
    ("business_process_flow", "Business Process Flow", _SAMPLE_DOCS_DIR / "business_process_flow_sample.md"),
]


def _sample_document_previews() -> list[dict]:
    """Landing page's tabbed showcase content -- pushed through the exact
    same markdown+Mermaid rendering path the real document viewer uses
    (app.docgen.render_pdf.extract_mermaid_blocks + the `markdown` library),
    so what a visitor sees here is an honest preview of the real product's
    rendering, not a decorative mockup that could drift from real output.
    """
    previews = []
    for doc_key, label, path in _SAMPLE_DOCS:
        raw = path.read_text(encoding="utf-8")
        transformed, has_mermaid = extract_mermaid_blocks(raw)
        body_html = markdown_lib.markdown(transformed, extensions=["tables", "fenced_code"])
        previews.append({"key": doc_key, "label": label, "body_html": body_html, "has_mermaid": has_mermaid})
    return previews


def _pricing_context() -> dict:
    """Real plan/pricing data from app.billing.plans -- the single source of
    truth for what's charged. Used by both / and /pricing so neither page can
    silently drift from the real billing configuration.

    Only purchasable currencies (real Razorpay plan ids -- currently INR
    only, see app.billing.plans) are included: international pricing stays
    defined in app.billing.plans for later activation, but must not render
    a currency-switcher option or subscribe button that would hit
    /billing/subscribe for a plan Razorpay can't actually create yet.
    """
    by_currency = {}
    for plan in plans_module.all_plans():
        if not plan.is_purchasable:
            continue
        by_currency.setdefault(plan.currency, {})[plan.billing_cycle] = plan
    return {"plans_by_currency": by_currency, "currencies": plans_module.purchasable_currencies()}

# Every PUBLIC page, for sitemap.xml -- adding a new public page means
# adding it here, so the sitemap can't silently drift from what's real.
_PUBLIC_PATHS = [
    "/",
    "/pricing",
    "/how-it-works",
    "/legal/terms-of-service",
    "/legal/privacy-policy",
    "/legal/refund-policy",
    "/legal/ai-disclaimer",
    "/feedback",
    "/signup",
    "/signup/consent",
    "/signup/verify",
    "/install",
    "/login",
]

# The admin_url_slug path is deliberately NEVER added to this list or to
# robots.txt -- its entire security property (Phase 1) is that it's
# unguessable; even a `Disallow:` line would leak it to anyone who reads
# robots.txt.
_PRIVATE_DISALLOW_PATHS = ["/account/", "/dashboard", "/dashboard/", "/healthz", "/billing/"]


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _ctx(request: Request, title: str, description: str, path: str, noindex: bool = False, **extra) -> dict:
    base = _base_url(request)
    return {
        "request": request,
        "page_title": title,
        "meta_description": description,
        "canonical_url": f"{base}{path}",
        "og_image_url": f"{base}/static/icon128.png",
        "noindex": noindex,
        # Every public page auto-detects an existing customer session so the
        # shared topbar (_topbar.html) can show Dashboard/Account/Logout
        # instead of Login/Start Free -- a caller that already computed
        # `customer` itself (e.g. welcome_page, pricing) overrides this via
        # **extra below, so this is only a default, not a re-computation.
        "customer": auth.get_current_customer_optional(request),
        **extra,
    }


def _safe_next_path(next_path: str) -> str:
    """Open-redirect guard for the /login?next=... flow: only an internal,
    same-site path is ever honored. Rejects protocol-relative URLs (//evil)
    and anything carrying a scheme (https://evil, javascript:), which a
    crafted link could otherwise use to redirect a customer's authenticated
    session off-site right after a real OTP login.
    """
    if next_path.startswith("/") and not next_path.startswith("//") and "://" not in next_path:
        return next_path
    return "/dashboard"


def _active_subscription_for_request(request: Request) -> dict | None:
    """Used by both / and /pricing so a logged-in, already-paying customer
    sees their current-plan state instead of live purchase buttons --
    /billing/subscribe also independently rejects a second subscription
    server-side, this is purely the matching customer-facing UX.
    """
    customer = auth.get_current_customer_optional(request)
    return db.get_active_subscription(customer["id"]) if customer else None


@router.get("/", response_class=HTMLResponse)
def landing(request: Request):
    return templates.TemplateResponse(
        "site/landing.html",
        _ctx(
            request,
            "Meeting Saathi -- Turn Meetings Into Documents",
            "Record your Google Meet call and get MOM, Meeting Analysis, and a "
            "Business Process Flow -- automatically, with AI.",
            "/",
            document_previews=_sample_document_previews(),
            **_pricing_context(),
        ),
    )


@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request):
    return templates.TemplateResponse(
        "site/pricing.html",
        _ctx(
            request,
            "Pricing -- Meeting Saathi",
            "3 free meetings, then simple monthly or yearly pricing for unlimited meetings.",
            "/pricing",
            active_subscription=_active_subscription_for_request(request),
            **_pricing_context(),
        ),
    )


@router.get("/how-it-works", response_class=HTMLResponse)
def how_it_works(request: Request):
    return templates.TemplateResponse(
        "site/how_it_works.html",
        _ctx(
            request,
            "How Meeting Saathi Works",
            "From signing up to getting your MOM, Meeting Analysis, and Business Process Flow -- the full journey.",
            "/how-it-works",
        ),
    )


_LEGAL_ROUTES = {
    "terms-of-service": ("terms_of_service", "Terms of Service"),
    "privacy-policy": ("privacy_policy", "Privacy Policy"),
    "refund-policy": ("refund_policy", "Refund and Cancellation Policy"),
    "ai-disclaimer": ("ai_disclaimer", "AI Disclaimer"),
}


@router.get("/legal/{slug}", response_class=HTMLResponse)
def legal_page(request: Request, slug: str):
    if slug not in _LEGAL_ROUTES:
        return HTMLResponse("Not found", status_code=404)
    policy_type, title = _LEGAL_ROUTES[slug]
    policy = db.get_policy(policy_type)
    body_html = markdown_lib.markdown(policy["content"]) if policy else "<p>Not available.</p>"
    return templates.TemplateResponse(
        "site/legal.html",
        _ctx(
            request,
            f"{title} -- Meeting Saathi",
            f"Meeting Saathi's {title.lower()}.",
            f"/legal/{slug}",
            body_html=body_html,
            effective_date=policy["effective_date"] if policy else "",
        ),
    )


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return templates.TemplateResponse(
        "site/signup.html",
        _ctx(request, "Create Your Account -- Meeting Saathi", "Start your 3 free meetings. No card required.", "/signup", step="account"),
    )


@router.get("/signup/consent", response_class=HTMLResponse)
def signup_consent_page(request: Request, email: str = "", name: str = ""):
    return templates.TemplateResponse(
        "site/consent.html",
        _ctx(
            request,
            "Review & Accept -- Meeting Saathi",
            "Review the Terms of Service, Privacy Policy, Refund Policy, and AI Disclaimer before continuing.",
            "/signup/consent",
            noindex=True,
            email=email,
            name=name,
            step="consent",
        ),
    )


@router.get("/signup/verify", response_class=HTMLResponse)
def signup_verify_page(request: Request, email: str = ""):
    return templates.TemplateResponse(
        "site/verify.html",
        _ctx(request, "Verify Your Email -- Meeting Saathi", "Enter the verification code we emailed you.", "/signup/verify", noindex=True, email=email, step="verify"),
    )


@router.get("/welcome", response_class=HTMLResponse)
def welcome_page(request: Request):
    """Sits between OTP verification and the install flow -- a real session
    is required (Phase 1's OTP login stamps request.session["customer_id"]
    on verify), matching /account's pattern rather than /dashboard's
    anonymous ?name= fallback, since there's no legacy caller of this new
    route to stay backward-compatible with.
    """
    customer = auth.get_current_customer_optional(request)
    if customer is None:
        return RedirectResponse(url="/signup", status_code=303)
    return templates.TemplateResponse(
        "site/welcome.html",
        _ctx(
            request,
            "Welcome -- Meeting Saathi",
            "Your account is ready. Install the extension to record your first meeting.",
            "/welcome",
            noindex=True,
            customer=customer,
            step="install",
        ),
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/dashboard"):
    """Lightweight returning-customer entry point: email -> OTP -> verify,
    reusing the same /auth/send-otp + /auth/verify-otp backend as signup.
    No name/consent step -- app.auth_routes.send_otp already skips the
    consent requirement for an already-verified email, so this page simply
    can't move a never-verified email past sending an OTP (that still
    requires a real signup + consent).

    `next` carries where to return the customer after a successful OTP
    verify (e.g. /account or /pricing, when their session expired mid-visit
    there) -- validated here, server-side, before ever being embedded into
    the page, so /login can never be used as an open redirect.
    """
    safe_next = _safe_next_path(next)
    return templates.TemplateResponse(
        "site/login.html",
        _ctx(request, "Log In -- Meeting Saathi", "Log in to your Meeting Saathi account.", "/login", next=safe_next),
    )


@router.get("/install", response_class=HTMLResponse)
def install_page(request: Request):
    return templates.TemplateResponse(
        "site/install.html",
        _ctx(
            request,
            "Install the Extension -- Meeting Saathi",
            "A guided, step-by-step install for the Meeting Saathi Chrome extension.",
            "/install",
            step="install",
        ),
    )


@router.get("/feedback", response_class=HTMLResponse)
def feedback_page(request: Request, submitted: bool = False):
    return templates.TemplateResponse(
        "site/feedback.html",
        _ctx(request, "Send Feedback -- Meeting Saathi", "Report a bug, request a feature, or send general feedback.", "/feedback", submitted=submitted),
    )


@router.post("/feedback")
def submit_feedback(
    request: Request,
    message: str = Form(...),
    category: str = Form("general"),
    email: str = Form(""),
):
    customer_id = request.session.get("customer_id")
    db.create_feedback(message=message.strip(), category=category, email=email.strip(), customer_id=customer_id)
    return RedirectResponse(url="/feedback?submitted=1", status_code=303)


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt(request: Request):
    base = _base_url(request)
    lines = ["User-agent: *"]
    for path in _PRIVATE_DISALLOW_PATHS:
        lines.append(f"Disallow: {path}")
    lines.append(f"Sitemap: {base}/sitemap.xml")
    return "\n".join(lines) + "\n"


@router.get("/sitemap.xml")
def sitemap_xml(request: Request):
    base = _base_url(request)
    urls = "".join(f"<url><loc>{base}{path}</loc></url>" for path in _PUBLIC_PATHS)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return HTMLResponse(content=xml, media_type="application/xml")
