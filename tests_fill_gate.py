#!/usr/bin/env python3
"""Fill-gate checks: YES leg floor(0.48)/cap(0.9) and NO leg (no floor)."""
from datetime import datetime, timezone
from decimal import Decimal

from re_execution import plan_leg_attempts

NOW = datetime.now(timezone.utc)
BASE = {"leg": "buy_yes_new", "token_id": "T1", "side": "BUY", "outcome": "YES",
        "cap": "0.9", "floor": "0.48"}


def leg(**kw):
    d = dict(BASE)
    d.update(kw)
    return plan_leg_attempts(d, {"best_ask": Decimal(kw.get("ask", "0.5")), "asks": [], "tick_size": "0.01"},
                             Decimal("10"), NOW, 0)


def main():
    assert leg(ask="0.30")["status"] == "below_floor", "ask below floor must not fill"
    assert leg(ask="0.48")["status"] == "below_floor", "floor is strict >0.48"
    assert leg(ask="0.55")["status"] == "send_fak", "in-window ask must send FAK"
    assert leg(ask="0.90")["status"] == "send_fak", "ask==cap is allowed (<=0.9)"
    assert leg(ask="0.95", cap="0.9")["status"] == "abort_above_cap", "ask>cap must abort"
    nofloor = leg(ask="0.30", floor="")
    assert nofloor["status"] == "send_fak", "no floor -> cheap ask still fills"
    print("PASS 6 gate scenarios")


if __name__ == "__main__":
    main()
