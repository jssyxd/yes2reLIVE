"""Live (real-money) side of weatherbotyes2re — read-only by default, gated writes only.

Phase ladder (see ``live/README.md``):

* Phase 1 — read-only reconciliation (``reconcile.py``, ``risk_gate.py``, ``creds.py``, ``clob_client.py``)
* Phase 2 — dry-run: sign locally, never submit (``order_plan.py``, ``sign_dryrun.py``)
* Phase 3 — controlled real writes behind a three-gate + sentinel + audit channel
  (``submit.py`` on CLOB v1, ``smoke.py`` for the operator smoke order)
* Phase 3b — execution port so live and paper share one strategy, one logic, one infrastructure
  (``port.py``: ``PaperPort`` vs ``LivePort``; ``v2_transport.py``: the CLOB **v2** channel)

Safety stance that holds across every phase: nothing here places an order by itself. Writes need
an explicitly released method, a fully-passing gate record (flag + env + date phrase) and an
append-only audit line; every refusal is audited too, and the paper path never imports a
third-party SDK. See ``live/README.md`` for the operator manual.
"""
