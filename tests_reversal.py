#!/usr/bin/env python3
"""Lightweight tests without pytest."""
from paper_reversal_sim import run_scenarios


def main():
    results, failed = run_scenarios()
    for r in results:
        mark = "PASS" if r.get("ok") else "FAIL"
        print(f"{mark} {r['name']}")
    if failed:
        raise SystemExit(failed)


if __name__ == "__main__":
    main()
