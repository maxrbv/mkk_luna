from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.payment import Currency
from app.schemas.payment import MAX_AMOUNT, PaymentCreate


def _base() -> dict:
    return {
        "amount": "100.00",
        "currency": "USD",
        "description": "test",
        "metadata": {},
        "webhook_url": "https://example.com/hook",
    }


def test_valid_payload_parses():
    payload = PaymentCreate.model_validate(_base())
    assert payload.amount == Decimal("100.00")
    assert payload.currency is Currency.USD


def test_amount_must_be_positive():
    data = _base() | {"amount": "0"}
    with pytest.raises(ValidationError) as exc:
        PaymentCreate.model_validate(data)
    assert "greater than 0" in str(exc.value)


def test_amount_upper_cap():
    data = _base() | {"amount": str(MAX_AMOUNT + 1)}
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate(data)


def test_amount_at_cap_allowed():
    data = _base() | {"amount": str(MAX_AMOUNT)}
    payload = PaymentCreate.model_validate(data)
    assert payload.amount == MAX_AMOUNT


def test_amount_precision_limited_to_two_decimals():
    data = _base() | {"amount": "1.234"}
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate(data)


def test_currency_rejects_unknown():
    data = _base() | {"currency": "BTC"}
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate(data)


def test_webhook_url_requires_http_scheme():
    data = _base() | {"webhook_url": "ftp://example.com"}
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate(data)


def test_metadata_defaults_to_empty_dict():
    data = _base()
    del data["metadata"]
    payload = PaymentCreate.model_validate(data)
    assert payload.metadata == {}
