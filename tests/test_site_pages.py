"""Covers Phase 4's public website: every page renders with a distinct
title, legal pages render real distinct content (not one shared screen
like the prototype), robots.txt/sitemap.xml never leak a private route or
the admin slug, every private route carries a noindex header, and feedback
submission persists a real row.
"""
import re

from fastapi.testclient import TestClient

from app import auth, db
from app.config import settings
from app.main import app

client = TestClient(app)


def _fresh_db(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()


def _logged_in_session(email: str = "priya@example.com") -> TestClient:
    db.create_customer(name="Priya Shah", email=email)
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email=email, policy_type=policy, policy_version="test")
    db.update_customer(db.get_customer_by_email(email)["id"], email_verified=1)
    code = auth.issue_otp(email)
    session = TestClient(app, base_url="https://testserver")
    session.post("/auth/verify-otp", data={"email": email, "code": code})
    return session


_PUBLIC_PAGES = [
    "/",
    "/pricing",
    "/how-it-works",
    "/legal/terms-of-service",
    "/legal/privacy-policy",
    "/legal/refund-policy",
    "/legal/ai-disclaimer",
    "/signup",
    "/login",
    "/install",
    "/feedback",
]


def _title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
    return match.group(1).strip() if match else ""


def test_every_public_page_renders_with_a_title():
    for path in _PUBLIC_PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert _title(response.text), f"{path} has no <title>"


def test_public_pages_have_distinct_titles():
    titles = [_title(client.get(path).text) for path in _PUBLIC_PAGES]
    assert len(titles) == len(set(titles))


def test_legal_pages_render_distinct_content_not_one_shared_screen():
    bodies = {
        slug: client.get(f"/legal/{slug}").text
        for slug in ["terms-of-service", "privacy-policy", "refund-policy", "ai-disclaimer"]
    }
    # Pairwise distinct -- the prototype's bug was all four routes opening
    # the identical screen.
    values = list(bodies.values())
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            assert values[i] != values[j]
    assert "Refund" in bodies["refund-policy"] or "refund" in bodies["refund-policy"]


def test_unknown_legal_slug_404s():
    assert client.get("/legal/does-not-exist").status_code == 404


def test_logo_links_home_on_public_pages():
    for path in ["/", "/pricing"]:
        html = client.get(path).text
        assert '<a class="wordmark" href="/">' in html and "Meeting Saathi" in html


def test_logo_links_home_on_the_dashboard(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = _logged_in_session("priya@example.com")
    monkeypatch.setattr(db, "list_runs", lambda limit=50, user_name=None, client_name=None, customer_id=None: [])
    monkeypatch.setattr(db, "distinct_client_names", lambda **kwargs: [])

    html = session.get("/dashboard").text

    assert '<a class="wordmark" href="/">' in html and "Meeting Saathi</a>" in html


def test_robots_txt_disallows_private_paths_and_never_leaks_the_admin_slug():
    text = client.get("/robots.txt").text
    assert "Disallow: /dashboard" in text
    assert "Disallow: /account/" in text
    assert settings.admin_url_slug not in text


def test_sitemap_lists_only_public_pages():
    text = client.get("/sitemap.xml").text
    for private_fragment in ("/dashboard", "/account", settings.admin_url_slug):
        assert private_fragment not in text
    for path in ["/", "/pricing", "/legal/terms-of-service", "/signup"]:
        assert f"<loc>http://testserver{path}</loc>" in text


def test_private_routes_carry_noindex_header():
    for path in ["/dashboard"]:
        # follow_redirects=False -- an unauthenticated /dashboard now
        # redirects to /login (a public, indexable page), so the noindex
        # header must be asserted on /dashboard's own 303 response, not on
        # wherever the redirect is followed to.
        assert client.get(path, follow_redirects=False).headers.get("x-robots-tag") == "noindex, nofollow"


def test_public_routes_do_not_carry_noindex_header():
    for path in _PUBLIC_PAGES:
        assert "x-robots-tag" not in {k.lower() for k in client.get(path).headers.keys()}


def test_welcome_requires_a_real_session_and_redirects_to_signup():
    anon_client = TestClient(app, follow_redirects=False)
    response = anon_client.get("/welcome")
    assert response.status_code == 303
    assert response.headers["location"] == "/signup"


def test_feedback_submission_persists_a_real_row(tmp_path, monkeypatch):
    db_path = tmp_path / "runs.sqlite3"
    monkeypatch.setattr(settings, "db_path", db_path)
    db.init_db()

    response = client.post(
        "/feedback",
        data={"message": "The Business Process Flow diagram was very helpful.", "category": "feature", "email": "priya@example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/feedback?submitted=1"
    rows = db.list_feedback()
    assert len(rows) == 1
    assert rows[0]["message"] == "The Business Process Flow diagram was very helpful."
    assert rows[0]["category"] == "feature"
    assert rows[0]["status"] == "new"


def test_feedback_page_shows_thank_you_after_submission():
    response = client.get("/feedback", params={"submitted": "1"})
    assert "Thanks" in response.text


def test_session_cookie_lasts_30_days(tmp_path, monkeypatch):
    """Product requirement: OTP once per device, then stay logged in for
    ~30 days without re-verifying -- was hardcoded to 7 days before.
    """
    _fresh_db(tmp_path, monkeypatch)
    db.create_customer(name="Priya Shah", email="priya@example.com")
    for policy in auth.REQUIRED_CONSENT_POLICIES:
        db.record_consent(email="priya@example.com", policy_type=policy, policy_version="test")
    db.update_customer(db.get_customer_by_email("priya@example.com")["id"], email_verified=1)
    code = auth.issue_otp("priya@example.com")
    session = TestClient(app, base_url="https://testserver")

    response = session.post("/auth/verify-otp", data={"email": "priya@example.com", "code": code})

    set_cookie = response.headers.get("set-cookie", "")
    assert "Max-Age=" + str(30 * 24 * 60 * 60) in set_cookie


def test_public_nav_shows_login_and_start_free_when_logged_out():
    for path in ("/", "/pricing"):
        html = client.get(path).text
        assert '<a href="/login">Login</a>' in html
        assert 'href="/signup"' in html
        assert 'href="/dashboard"' not in html
        assert 'href="/account"' not in html


def test_public_nav_shows_dashboard_account_logout_when_logged_in(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = _logged_in_session()

    html = session.get("/pricing").text

    assert '<a href="/dashboard">Dashboard</a>' in html
    assert '<a href="/account">Account</a>' in html
    assert "meetingSaathiLogout()" in html
    assert '<a href="/login">Login</a>' not in html
    # The topbar's own logged-out CTA specifically -- not a blanket "no
    # /signup anywhere" check, since the pricing page's free-tier card
    # legitimately still links there regardless of login state.
    assert '<a class="btn primary" href="/signup">Start Free</a>' not in html


def test_dashboard_and_account_pages_share_the_same_logged_in_nav(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = _logged_in_session()
    monkeypatch.setattr(db, "list_runs", lambda limit=50, user_name=None, client_name=None, customer_id=None: [])
    monkeypatch.setattr(db, "distinct_client_names", lambda **kwargs: [])

    dashboard_html = session.get("/dashboard").text
    account_html = session.get("/account").text

    for html in (dashboard_html, account_html):
        assert '<a href="/dashboard">Dashboard</a>' in html
        assert '<a href="/pricing">Pricing</a>' in html
        assert '<a href="/account">Account</a>' in html
        assert "meetingSaathiLogout()" in html


def test_login_page_embeds_a_safe_next_path():
    html = client.get("/login", params={"next": "/account"}).text
    assert 'const loginNextPath = "/account";' in html


def test_login_page_rejects_an_absolute_url_in_next(tmp_path, monkeypatch):
    """Open-redirect guard: a crafted /login?next=https://evil.com link must
    never make the post-OTP redirect leave the site.
    """
    html = client.get("/login", params={"next": "https://evil.example.com/phish"}).text
    assert 'const loginNextPath = "/dashboard";' in html
    assert "evil.example.com" not in html


def test_login_page_rejects_a_protocol_relative_next():
    html = client.get("/login", params={"next": "//evil.example.com"}).text
    assert 'const loginNextPath = "/dashboard";' in html
    assert "evil.example.com" not in html


def test_login_page_accepts_a_deep_internal_next_path():
    html = client.get("/login", params={"next": "/dashboard?client=Acme"}).text
    assert 'const loginNextPath = "/dashboard?client=Acme";' in html


def test_account_redirect_to_login_carries_next_back_to_account():
    fresh_client = TestClient(app, base_url="https://testserver")
    redirect = fresh_client.get("/account", follow_redirects=False)
    assert redirect.headers["location"] == "/login?next=/account"

    login_html = fresh_client.get(redirect.headers["location"]).text
    assert 'const loginNextPath = "/account";' in login_html


def test_pricing_page_replaces_purchase_ctas_for_an_active_subscriber(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = _logged_in_session()
    customer = db.get_customer_by_email("priya@example.com")
    db.create_pending_subscription(customer["id"], "monthly", "INR", "sub_active123")
    db.update_subscription_status("sub_active123", status="active")

    html = session.get("/pricing").text

    assert "You're on the" in html
    assert "Manage Account" in html
    assert 'data-subscribe="monthly"' not in html
    assert 'data-subscribe="yearly"' not in html


def test_pricing_page_still_offers_purchase_ctas_for_a_free_trial_customer(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    session = _logged_in_session()

    html = session.get("/pricing").text

    assert 'data-subscribe="monthly"' in html
    assert 'data-subscribe="yearly"' in html
    assert "Manage Account" not in html


def test_pricing_page_only_offers_purchasable_currencies():
    """International (USD/EUR/GBP) plans are deferred -- still defined in
    app.billing.plans for later activation, but the pricing page must never
    render a currency option or subscribe button for a currency Razorpay
    can't actually create a subscription for yet (see
    app.site_routes._pricing_context).
    """
    for path in ("/", "/pricing"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'data-plan-block="INR"' in response.text
        for currency in ("USD", "EUR", "GBP"):
            assert f'data-plan-block="{currency}"' not in response.text
            assert f'data-currency="{currency}"' not in response.text
            assert f'value="{currency}"' not in response.text
