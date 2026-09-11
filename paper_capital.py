"""Shared paper-trading cash ledger for the weatherbot paper simulators.

A single paper account starts with ``paper_initial_capital_usdc`` (default
1000 USDC). Every simulated fill reserves cash from that account. The ledger
lives in the observer ``state`` dict and is therefore serialised to
``data/state.json`` exactly like the rest of the deterministic state.

No real money, no wallet, no CLOB credentials, no order submission.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

ZERO = Decimal("0")

DEFAULT_INITIAL_CAPITAL_USDC = Decimal("1000.00")


def _decimal(value: Any, default: Decimal) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return default
    return parsed if parsed.is_finite() and parsed >= 0 else default


def initial_capital_usdc(state: dict[str, Any]) -> Decimal:
    """Return the paper account's starting capital, defaulting to 1000 USDC."""
    return _decimal(state.get("paper_initial_capital_usdc"), DEFAULT_INITIAL_CAPITAL_USDC)


def total_debit_usdc(state: dict[str, Any]) -> Decimal:
    """Return the running paper cash outflow (principal + fees) so far.

    MAY be negative since 2026-09-04: a negative total debit means realized
    wins exceed cumulative cost (equity = initial - debit > initial). Read
    directly, NOT through ``_decimal`` (whose parsed>=0 filter would clamp a
    negative balance to zero and hide realized profit).
    """
    try:
        parsed = Decimal(str(state.get("paper_total_debit_usdc") or 0))
    except Exception:
        return Decimal("0")
    return parsed if parsed.is_finite() else Decimal("0")


def remaining_capital_usdc(state: dict[str, Any]) -> Decimal:
    """Return the unused paper cash still available for simulated fills."""
    return initial_capital_usdc(state) - total_debit_usdc(state)


def reserve(state: dict[str, Any], amount_usdc: Any) -> Decimal | None:
    """Reserve ``amount_usdc`` of paper cash; return new total debit or None.

    ``None`` means the account does not have enough remaining capital, in which
    case the caller must fail closed and never mutate position state.
    """
    amount = Decimal(str(amount_usdc))
    if amount < 0:
        raise ValueError("paper debit must be non-negative")
    initial = initial_capital_usdc(state)
    if amount > initial - total_debit_usdc(state):
        return None
    state["paper_initial_capital_usdc"] = float(initial)
    new_total = (total_debit_usdc(state) + amount).quantize(Decimal("0.00001"))
    state["paper_total_debit_usdc"] = float(new_total)
    return new_total


def release(state: dict[str, Any], amount_usdc: Any) -> Decimal:
    """Credit paper cash back after a simulated sell (reduce total debit).

    Used by paper exit settlement so overturned positions free capital and
    realized PnL is reflected in the ledger.

    NOTE (2026-09-04 fix): total debit MAY go negative — a negative total
    debit means realized wins exceed cumulative cost, and ``remaining =
    initial - debit`` then correctly reports equity above the starting
    capital. The previous clamp-to-zero silently discarded realized profit
    (e.g. cost 49.06 vs payout 101.86 -> debit clamped from -52.80 to 0.00,
    hiding +52.80 of paper profit).
    """
    amount = Decimal(str(amount_usdc))
    if amount < 0:
        raise ValueError("paper credit must be non-negative")
    initial = initial_capital_usdc(state)
    new_total = (total_debit_usdc(state) - amount).quantize(Decimal("0.00001"))
    state["paper_initial_capital_usdc"] = float(initial)
    state["paper_total_debit_usdc"] = float(new_total)
    return new_total



def close_leg_at_best_bid(
    state: dict[str, Any],
    leg: dict[str, Any],
    books: dict[str, Any] | None = None,
    *,
    closed_by: str = "paper_close",
    settled_at_utc: str | None = None,
) -> dict[str, Any] | None:
    """Sell one open paper leg at its current best bid (paper close).

    Shared close for the B2 sleeve-timeout path and the second-fire (追火)
    old-bucket YES liquidation (2026-09-09): proceeds = best_bid x shares are
    released back to the pool; when the token has no book / no bid the leg is
    written off at 0. Marks the leg settled (``leg_won`` False, ``closed_by``,
    proceeds). ``books`` maps token_id -> ladder dict ({best_bid, ...}); a
    missing token means no bid. Returns a summary dict {shares, bid,
    proceeds_usdc} or None when the leg was already settled / holds no shares.
    """
    if leg is None or leg.get("settled"):
        return None
    try:
        shares = Decimal(str(leg.get("shares") or 0))
    except Exception:  # noqa: BLE001
        shares = ZERO
    if shares <= ZERO:
        return None
    tok = str(leg.get("token_id") or "")
    book = (books or {}).get(tok)
    bid = book.get("best_bid") if book else None
    proceeds = ZERO
    if bid is not None:
        try:
            bid_d = Decimal(str(bid))
            if bid_d > ZERO:
                proceeds = (bid_d * shares).quantize(Decimal("0.0001"))
        except Exception:  # noqa: BLE001
            proceeds = ZERO
    if proceeds > ZERO:
        release(state, proceeds)
    leg["settled"] = True
    leg["leg_won"] = False
    leg["closed_by"] = closed_by
    leg["close_proceeds_usdc"] = str(proceeds)
    # Equity accounts every settled leg as payout - cost; the release already
    # returned proceeds to the pool, so mirror them here as the leg's payout.
    leg["payout_credit_usdc"] = str(proceeds)
    leg["bid_at_close"] = str(bid) if bid is not None else None
    if settled_at_utc is None:
        settled_at_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    leg["settled_at_utc"] = settled_at_utc
    return {
        "shares": str(shares),
        "bid": str(bid) if bid is not None else None,
        "proceeds_usdc": str(proceeds),
    }
