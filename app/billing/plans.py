"""Single source of truth for pricing/plan configuration -- referenced from
routes and templates, never a hardcoded price string scattered across
either (see the pricing page and POST /billing/subscribe).

International (non-INR) Razorpay plans are intentionally deferred --
multi-currency plan creation isn't currently available on the founder's
Razorpay account. Their Plan entries stay in _PLANS below (still real
pricing, still shown wherever pricing is only being *displayed*) with
is_purchasable=False and a placeholder razorpay_plan_id, so the
architecture doesn't have to be rebuilt when international payments are
enabled later -- only flip is_purchasable and paste in the real
razorpay_plan_id once those plans exist in the Razorpay Dashboard.
"""
from dataclasses import dataclass

BILLING_CYCLES = ("monthly", "yearly")
CURRENCIES = ("INR", "USD", "EUR", "GBP")


@dataclass(frozen=True)
class Plan:
    billing_cycle: str
    currency: str
    razorpay_plan_id: str
    display_price_minor: int  # smallest currency unit (paise/cents), matches Razorpay's own convention
    display_symbol: str
    # Explicit, not inferred from razorpay_plan_id's shape -- the app was
    # previously bitten by an id-string-sniffing bug (total_count guessed
    # from whether "monthly" appeared in the plan id); purchasability gets
    # its own real field for the same reason.
    is_purchasable: bool = False

    @property
    def display_price(self) -> str:
        major = self.display_price_minor / 100
        formatted = f"{major:,.0f}" if major == int(major) else f"{major:,.2f}"
        return f"{self.display_symbol}{formatted}"


# (billing_cycle, currency) -> Plan. Prices are the plan's own defaults
# (₹299/mo, ₹2,999/yr from the approved product reference) converted at a
# rough reference rate for the other currencies -- the founder should treat
# these as a starting point, not a final international pricing decision.
_PLANS: dict[tuple[str, str], Plan] = {
    ("monthly", "INR"): Plan("monthly", "INR", "plan_TZXnf7CMdmdDfm", 29900, "₹", is_purchasable=True),
    ("yearly", "INR"): Plan("yearly", "INR", "plan_TZXpRhkLY9q1NK", 299900, "₹", is_purchasable=True),
    ("monthly", "USD"): Plan("monthly", "USD", "plan_placeholder_monthly_usd", 499, "$"),
    ("yearly", "USD"): Plan("yearly", "USD", "plan_placeholder_yearly_usd", 4999, "$"),
    ("monthly", "EUR"): Plan("monthly", "EUR", "plan_placeholder_monthly_eur", 449, "€"),
    ("yearly", "EUR"): Plan("yearly", "EUR", "plan_placeholder_yearly_eur", 4499, "€"),
    ("monthly", "GBP"): Plan("monthly", "GBP", "plan_placeholder_monthly_gbp", 399, "£"),
    ("yearly", "GBP"): Plan("yearly", "GBP", "plan_placeholder_yearly_gbp", 3999, "£"),
}


def get_plan(billing_cycle: str, currency: str) -> Plan | None:
    return _PLANS.get((billing_cycle, currency.upper()))


def get_plan_by_razorpay_id(razorpay_plan_id: str) -> Plan | None:
    for plan in _PLANS.values():
        if plan.razorpay_plan_id == razorpay_plan_id:
            return plan
    return None


def all_plans() -> list[Plan]:
    return list(_PLANS.values())


def purchasable_currencies() -> list[str]:
    """Currencies with at least one real (non-placeholder) Razorpay plan --
    what the pricing page's currency selector and subscribe buttons should
    actually offer right now. Preserves dict insertion order (Python 3.7+),
    so this stays INR-first the same way CURRENCIES already is.
    """
    seen: list[str] = []
    for plan in _PLANS.values():
        if plan.is_purchasable and plan.currency not in seen:
            seen.append(plan.currency)
    return seen
