"""SQLAlchemy models. Money is always integer kobo; timestamps are always UTC."""

from datetime import date, datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from owobook.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Language(StrEnum):
    PIDGIN = "pidgin"
    ENGLISH = "english"
    YORUBA = "yoruba"
    AUTO = "auto"


class EntryType(StrEnum):
    CASH_SALE = "cash_sale"
    CREDIT_SALE = "credit_sale"
    PAYMENT = "payment"
    EXPENSE = "expense"


class EntryStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DELETED = "deleted"


class EntrySource(StrEnum):
    VOICE = "voice"
    TEXT = "text"
    IMAGE = "image"


class SessionMode(StrEnum):
    NORMAL = "normal"
    EDITING = "editing"


class Trader(Base):
    __tablename__ = "traders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(120))
    shop_name: Mapped[str | None] = mapped_column(String(120))
    language_pref: Mapped[str] = mapped_column(String(16), default=Language.AUTO)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)

    customers: Mapped[list["Customer"]] = relationship(back_populates="trader")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("trader_id", "normalized_name", name="uq_customer_trader_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    normalized_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    trader: Mapped[Trader] = relationship(back_populates="customers")


class Entry(Base):
    """One line in the ledger: a cash sale, credit sale, payment or expense."""

    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), index=True)
    type: Mapped[str] = mapped_column(String(16))
    item_description: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(32))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(16), default=EntryStatus.PENDING)
    source: Mapped[str] = mapped_column(String(16), default=EntrySource.TEXT)
    raw_text: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    customer: Mapped[Customer | None] = relationship()


class MessageLog(Base):
    __tablename__ = "messages_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trader_id: Mapped[int | None] = mapped_column(ForeignKey("traders.id"), index=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    direction: Mapped[str] = mapped_column(String(8))  # "in" | "out"
    type: Mapped[str] = mapped_column(String(24))
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TraderSession(Base):
    """Conversation state per trader (e.g. 'editing entry 12' after tapping Fix it)."""

    __tablename__ = "sessions"

    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default=SessionMode.NORMAL)
    editing_entry_id: Mapped[int | None] = mapped_column(ForeignKey("entries.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UsageLog(Base):
    """One row per model call, so we can report cost per voice note."""

    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trader_id: Mapped[int | None] = mapped_column(ForeignKey("traders.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    audio_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    est_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
