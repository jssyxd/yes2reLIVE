"""WeatherBot execution layer.

OrderIntent -> RiskGate -> ExecutionEngine (Paper | Live) -> Fill -> Position.

This package contains no network, wallet, or credential code by itself. The
adapters (adapters.polymarket) are the only place that touches the CLOB.
"""
