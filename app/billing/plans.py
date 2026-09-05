"""Single source of truth for pricing/plan configuration -- referenced from
routes and templates, never a hardcoded price string scattered across
either (see the pricing page and POST /billing/subscribe).

Razorpay plan ids below are PLACEHOLDERS -- a real Razorpay account is
needed to create these (Dashboard > Subscriptions > Plans, or the
`POST /v1/plans` API), one plan per (cycle, currency) combination, each
with the display_price_minor amount below as that plan's own amount. Fill
in the real `plan_...` ids before Phase 9 goes live; nothing in this file's
shape needs to change to do that, only the id/currency values.
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
    ("monthly", "INR"): Plan("monthly", "INR", "plan_placeholder_monthly_inr", 29900, "₹"),
    ("yearly", "INR"): Plan("yearly", "INR", "plan_placeholder_yearly_inr", 299900, "₹"),
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
