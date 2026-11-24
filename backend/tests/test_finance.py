"""providers/finance._format_counterparties — drops the merchant (already the title), joins the rest."""

from app.providers import finance


def test_drops_the_merchant_and_dedups():
    cps = [
        {"name": "Chipotle", "type": "merchant"},
        {"name": "DoorDash", "type": "marketplace"},
        {"name": "DoorDash", "type": "payment_app"},
    ]
    assert finance._format_counterparties(cps, "Chipotle") == "DoorDash"


def test_merchant_match_is_case_insensitive():
    cps = [{"name": "AMAZON", "type": "merchant"}]
    assert finance._format_counterparties(cps, "Amazon") is None


def test_multiple_non_merchant_parties_join_in_order():
    cps = [
        {"name": "PayPal", "type": "payment_app"},
        {"name": "eBay", "type": "marketplace"},
    ]
    assert finance._format_counterparties(cps, "SomeSeller") == "PayPal, eBay"


def test_drops_financial_institution_rails():
    cps = [
        {"name": "Bank of America", "type": "financial_institution"},
        {"name": "Toast", "type": "payment_app"},
    ]
    assert finance._format_counterparties(cps, "Some Cafe") == "Toast"


def test_empty_or_missing_returns_none():
    assert finance._format_counterparties(None, "Amazon") is None
    assert finance._format_counterparties([], "Amazon") is None
    assert finance._format_counterparties([{"name": ""}], "Amazon") is None
