"""HiveAR commands (WAX-0002). Each carries the primary key of what it
creates, so a handler returns nothing and a retried request cannot post twice.
Every input is bounded and strict: a browser or a script sending the wrong
kind of value gets a refusal, never a server error and never a guess.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

from hivear.currency import MINOR_UNIT_DIGITS

# Whole minor units, as a JSON number. Bounded so that no sum of them can
# overflow a 64-bit column, and strict so that "100", 1.0, and true are refused.
Amount = Annotated[int, Field(strict=True, gt=0, le=10**15)]
# Text a person typed. NUL cannot be stored and control characters have no place in it.
Text = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")]
BucketCode = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]


def _supported_currency(code: str) -> str:
    if code not in MINOR_UNIT_DIGITS:
        raise ValueError(f"{code} is not a currency this installation takes")
    return code


def _iso_date_text(value):
    # From JSON a date is text. A number would be read as days since 1970.
    if isinstance(value, datetime) or not isinstance(value, (str, date)):
        raise ValueError("a date is written as YYYY-MM-DD")
    return value


def _plausible_date(value: date) -> date:
    if not date(1900, 1, 1) <= value <= date(2200, 1, 1):
        raise ValueError("the date is outside the range this system records")
    return value


def _plausible_instant(value: datetime) -> datetime:
    """In UTC, and within what every layer can store and read back."""
    try:
        value = value.astimezone(UTC)
    except OverflowError:
        raise ValueError("received_at is outside the range this system records") from None
    if value < datetime(1900, 1, 1, tzinfo=UTC) or value > datetime.now(UTC) + timedelta(days=1):
        raise ValueError("received_at must be after 1900 and not more than a day ahead")
    return value


CurrencyCode = Annotated[str, AfterValidator(_supported_currency)]
CalendarDate = Annotated[date, BeforeValidator(_iso_date_text), AfterValidator(_plausible_date)]
Instant = Annotated[AwareDatetime, AfterValidator(_plausible_instant)]


class Command(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PlaceAccount(Command):
    account_pk: UUID = Field(default_factory=uuid4)
    client_account_number: Text
    debt_type: Text
    contract_type: Literal["third_party_contingency", "purchase"]
    currency_code: CurrencyCode
    list_date: CalendarDate
    itemization_date: CalendarDate
    opening_balances: dict[BucketCode, Amount] = Field(min_length=1, max_length=50)


class OpenDispute(Command):
    account_pk: UUID
    dispute_pk: UUID = Field(default_factory=uuid4)


class PostPayment(Command):
    payment_pk: UUID = Field(default_factory=uuid4)
    account_pks: list[UUID] = Field(min_length=1, max_length=200)
    amount_minor_units: Amount
    currency_code: CurrencyCode
    received_at: Instant | None = None
    allocation_method: Literal["even_split", "oldest_first"] = "oldest_first"
    # The consumer's own directions: {account_pk: amount}.
    directed_allocations: dict[UUID, Amount] = Field(default_factory=dict)
    # Per-payment waterfall override: {account_pk: {bucket_code: amount}}.
    bucket_amounts: dict[UUID, dict[BucketCode, Amount]] = Field(default_factory=dict)
    # Accounts whose bucket amounts the consumer directed. Required for a
    # disputed account; without it bucket amounts there are refused.
    consumer_directed_accounts: list[UUID] = Field(default_factory=list, max_length=200)


class ReversePayment(Command):
    payment_pk: UUID
