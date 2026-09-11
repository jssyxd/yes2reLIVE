#!/usr/bin/env python3
"""Pure risk gate — stdlib only, no I/O, fully unit-testable.

Fail-closed: any missing/invalid input denies. Decision codes are machine
readable; ``detail`` is for humans.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: decision codes
OK = "ok"
INVALID_INPUT = "invalid_input"
INSUFFICIENT_BALANCE = "insufficient_balance"
MAX_OPEN_POSITIONS = "max_open_positions_reached"
CAPITAL_CAP = "capital_cap_exceeded"
BUDGET_EXCEEDS_BALANCE = "budget_exceeds_balance"

#: check order — first failing check wins (documented so tests pin behaviour)
CHECK_ORDER = (
    INVALID_INPUT,
    MAX_OPEN_POSITIONS,
    CAPITAL_CAP,
    INSUFFICIENT_BALANCE,
    BUDGET_EXCEEDS_BALANCE,
)


def _dec(value, field: str, *, allow_zero: bool = True) -> Decimal:
    """Parse money/count input. Raises ValueError on anything not a finite number."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field}: missing or non-numeric")
    if isinstance(value, Decimal):
        out = value
    else:
        try:
            out = Decimal(str(value).strip())
        except (InvalidOperation, AttributeError, ValueError):
            raise ValueError(f"{field}: not a number") from None
    if not out.is_finite():
        raise ValueError(f"{field}: not finite")
    if out < 0:
        raise ValueError(f"{field}: negative")
    if not allow_zero and out == 0:
        raise ValueError(f"{field}: must be > 0")
    return out


def evaluate(
    *,
    usdc_balance,
    open_positions,
    committed_usdc,
    fire_budget_usdc,
    max_open_positions,
    max_capital_usdc,
) -> dict:
    """Decide whether one more fire of ``fire_budget_usdc`` is allowed.

    Args:
        usdc_balance: free collateral available right now.
        open_positions: number of currently open markets.
        committed_usdc: mark value already deployed in those positions.
        fire_budget_usdc: notional this fire intends to spend (must be > 0).
        max_open_positions: cap on concurrently open markets (>= 1 to trade).
        max_capital_usdc: cap on committed capital.

    Returns:
        ``{"allow": bool, "reason": <code>, "detail": str}``.
    """
    detail = ""
    try:
        balance = _dec(usdc_balance, "usdc_balance")
        open_n = _dec(open_positions, "open_positions")
        committed = _dec(committed_usdc, "committed_usdc")
        budget = _dec(fire_budget_usdc, "fire_budget_usdc", allow_zero=False)
        max_open = _dec(max_open_positions, "max_open_positions")
        max_capital = _dec(max_capital_usdc, "max_capital_usdc")
    except ValueError as exc:
        return {"allow": False, "reason": INVALID_INPUT, "detail": str(exc)}

    if open_n != open_n.to_integral_value():
        return {"allow": False, "reason": INVALID_INPUT, "detail": "open_positions: not an integer"}

    if open_n >= max_open:
        return {
            "allow": False,
            "reason": MAX_OPEN_POSITIONS,
            "detail": f"open positions {int(open_n)} >= max {int(max_open)}",
        }

    projected = committed + budget
    if projected > max_capital:
        return {
            "allow": False,
            "reason": CAPITAL_CAP,
            "detail": f"committed {committed} + budget {budget} > cap {max_capital}",
        }

    if balance <= 0:
        return {"allow": False, "reason": INSUFFICIENT_BALANCE, "detail": "usdc_balance <= 0"}

    if budget > balance:
        return {
            "allow": False,
            "reason": BUDGET_EXCEEDS_BALANCE,
            "detail": f"budget {budget} > balance {balance}",
        }

    detail = (
        f"balance {balance} >= budget {budget}; positions {int(open_n)}/{int(max_open)}; "
        f"committed {committed}+{budget} <= cap {max_capital}"
    )
    return {"allow": True, "reason": OK, "detail": detail}


def _demo() -> None:
    ok = dict(usdc_balance="100", open_positions=1, committed_usdc="5",
              fire_budget_usdc="10", max_open_positions=22, max_capital_usdc=500)
    assert evaluate(**ok)["reason"] == OK
    assert evaluate(**{**ok, "usdc_balance": "5"})["reason"] == BUDGET_EXCEEDS_BALANCE
    assert evaluate(**{**ok, "usdc_balance": "0"})["reason"] == INSUFFICIENT_BALANCE
    assert evaluate(**{**ok, "open_positions": 22})["reason"] == MAX_OPEN_POSITIONS
    assert evaluate(**{**ok, "committed_usdc": 495})["reason"] == CAPITAL_CAP
    assert evaluate(**{**ok, "usdc_balance": None})["reason"] == INVALID_INPUT
    assert evaluate(**{**ok, "fire_budget_usdc": "-1"})["reason"] == INVALID_INPUT
    assert evaluate(**{**ok, "usdc_balance": "NaN"})["reason"] == INVALID_INPUT
    print("risk_gate demo OK")


if __name__ == "__main__":
    _demo()
