# ATLAS Phase 2: Safety foundation report

Date: 2026-11-23. Result: **93 automated tests, all passing.**
Nothing in this phase connects to a real account. Every test runs against a fake exchange.

## 1. What was built

| Component | File | What it guarantees |
|---|---|---|
| Exact money | `atlas/money.py` | All money is an integer count of $0.0001 units. Floats are refused everywhere. The Kalshi fee is computed exactly, rounded up to the cent per order, and checked against exact fractions for about 1,600 price and size combinations. |
| Accounting time | `atlas/clock.py` | The accounting day is midnight to midnight America/New_York. The clock must be verified against exchange time (±2 s, re-checked every 5 minutes) before entries are allowed or the day can roll over. |
| Storage | `atlas/storage.py` | SQLite with WAL and full sync. `BEGIN IMMEDIATE` makes read-check-write steps atomic across processes. An OS-level single-instance lock works on Windows and Linux. |
| Audit log | `atlas/audit.py` | Append-only: triggers block edits and deletes. SHA-256 hash chain, anchors that catch truncation, secret redaction, export. |
| Limits | `atlas/limits.py` | Your spec's defaults. Lowering a limit is immediate; raising one needs "RAISE LIMITS" and takes effect the next day. Absolute ceilings live in code. |
| Owner controls | `atlas/control.py` | Daily authorization is OFF by default, tied to the verified day and the current gateway run, so every restart turns it off. Per-trade approval is ON, forced back ON at boot, bound to the exact order, single use, and expires after 60 s. Modules are OFF by default. |
| Trading locks | `atlas/control.py` | Emergency stop, daily loss, losing streak, drawdown, integrity, watchdog, risk disagreement, reconciliation, audit, security and clock locks. Clearing one needs "RESUME"; integrity-type locks also need a successful reconciliation. An unreadable lock file means locked. |
| Spending ledger | `atlas/ledger.py` | One global budget across all modules, counting fees. Reservations are atomic; unknown outcomes keep their reservation; money is released only on an exchange-confirmed final state; profits never refill the budget. It never silently starts fresh: a missing or foreign ledger is an error. |
| Primary risk manager | `atlas/risk/primary.py` | 30 checks, all evaluated and all reported. The most restrictive limit wins. |
| Shadow risk manager | `atlas/shadowrisk/checker.py` | A separate implementation: no ATLAS imports (enforced by a test) and `Decimal` arithmetic. It rebuilds spending, exposure, profit and loss, and positions from the exchange's own records plus its own journal. It reads limits, authorization and locks read-only, and signs each decision with Ed25519. |
| Execution gateway | `atlas/gateway.py` | The only code that places orders (enforced by a test). Runs the 12-step pipeline, reconciles before every entry, never resubmits after an unknown outcome, follows the disagreement protocol, and handles the emergency stop and loss-limit locks. |
| Watchdog | `atlas/watchdog.py` | A separate process: `python -m atlas.watchdog --data <folder>`. It detects crashed and frozen processes, silent risk managers, database failures, budget breaches, failed reconciliation, exchange outages, audit tampering and clock disagreement. It can only lock, revoke authorization and cancel; it never re-enables. |
| Fake exchange | `atlas/exchange/fake.py` | A Kalshi-like test venue that injects timeouts, server errors, rejections, partial fills, slow cancels and outages. |
| Health and notifications | `atlas/health.py` | Heartbeats, kept separate from the ledger, and a durable notification outbox. |
| Setup | `atlas/paths.py` | `initialize(folder)` creates a new data folder and refuses to overwrite an existing one. |
| Dashboard bridge | `dashboard/real_state.py` | `python server.py --data <folder>` shows the real budget, limits, health, locks, authorization, positions and audit activity, read-only. |

## 2. Part 25 acceptance tests

| Group | Spec test | Status | Test |
|---|---|---|---|
| Spending | $20 cap cannot become $21 | ✅ | `test_daily_cap_cannot_become_21`, `test_lowering_is_immediate_raising_waits_for_tomorrow` |
| | Simultaneous orders cannot bypass the limit | ✅ | `test_simultaneous_processes_cannot_bypass_the_cap` (8 OS processes) |
| | Modules share one global budget | ✅ | `test_modules_share_one_global_budget_and_category_ceilings` |
| | Fees count against the budget | ✅ | `test_fees_count_against_budget`, `test_per_purchase_limit_includes_fees` |
| | Open orders reserve capital | ✅ | `test_open_orders_reserve_capital_and_unknown_keeps_reservation` |
| | Cancels release only after confirmation | ✅ | `test_cancel_releases_only_after_confirmation`, `test_timeout_before_accept_keeps_reservation_until_confirmed` |
| | Partial fills accounted correctly | ✅ | `test_partial_fill_counts_actual_execution`, `test_partial_fill_accounting` |
| | Profits do not replenish the allowance | ✅ | `test_profits_do_not_replenish_budget`, `test_profits_do_not_refill_budget` |
| | Restarts do not reset spending | ✅ | `test_restart_keeps_spending`, `test_restart_does_not_reauthorize_and_keeps_spending` |
| | Failed accounting blocks orders | ✅ | `test_failed_accounting_blocks_orders`, `test_detects_database_failure` |
| Authorization | Disabled authorization blocks entries | ✅ | `test_no_authorization_blocks_entries` |
| | Disabled modules cannot trade | ✅ | `test_disabled_module_cannot_trade` |
| | Per-trade approval required | ✅ | `test_approval_required_and_bound_to_exact_order` |
| | Approval expires on material change | ✅ | same, plus `test_approval_bound_to_exact_order_single_use_and_expiring` |
| | Authorization expires at end of day | ✅ | `test_authorization_expires_at_end_of_day` |
| | Restart does not reauthorize | ✅ | `test_restart_does_not_reauthorize`, `test_restart_voids_pending_approvals` |
| Risk | Daily loss limit | ✅ | `test_daily_loss_limit_locks_and_needs_manual_resume` |
| | Consecutive loss limit | ✅ | `test_consecutive_losses_pause_entries` |
| | Maximum position size | ✅ | `test_max_purchase_is_two_dollars` |
| | Maximum market exposure | ✅ | `test_market_and_concurrent_position_limits`, `test_market_and_event_exposure_limits` |
| | Maximum concurrent positions | ✅ | `test_market_and_concurrent_position_limits`, `test_default_position_and_order_limits` |
| | Correlated exposure controls | ◐ Partial | Event-group limits enforced and tested. Statistical correlation arrives in Phase 4. |
| | Emergency stop blocks every entry route | ✅ | `test_emergency_stop_blocks_every_entry_and_revokes_authorization`. There is one entry route (`test_only_the_gateway_places_orders`). |
| Shadow risk | Rejects what the primary wrongly approves | ✅ | `test_shadow_blocks_order_the_primary_wrongly_approved` |
| | Detects spending discrepancies | ✅ | `test_shadow_detects_spending_discrepancy` |
| | Detects stale budget state | ✅ | `test_shadow_detects_stale_budget_state` |
| | Detects missing authorization | ✅ | `test_shadow_detects_missing_authorization` |
| | Rejects during reconciliation failures | ✅ | `test_shadow_rejects_during_reconciliation_failure` |
| | Detects inconsistent positions | ✅ | same, plus position cross-checks |
| | (extra) Forged signatures rejected | ✅ | `test_forged_shadow_signature_is_rejected` |
| Audit | Records every financial decision | ✅ | `test_every_decision_is_in_the_audit_log` |
| | Detects modified records | ✅ | `test_detects_modified_record`, `test_detects_audit_tampering` |
| | Detects broken hash chains | ✅ | `test_detects_broken_chain_and_gaps`, `test_anchor_catches_truncation` |
| | Preserved through restarts | ✅ | `test_survives_restart` |
| | No credentials in logs | ✅ | `test_credentials_never_enter_the_log` |
| Watchdog | Detects a crashed process | ✅ | `test_detects_crashed_main_process` (real child process) |
| | Detects a frozen process | ✅ | `test_detects_frozen_main_process` (real child process) |
| | Detects risk-manager failure | ✅ | `test_detects_risk_manager_failure` |
| | Detects database failure | ✅ | `test_detects_database_failure` |
| | Disables new orders | ✅ | `test_watchdog_lock_disables_new_orders`, `test_cancels_outstanding_entry_orders` |
| | Does not re-enable after recovery | ✅ | `test_does_not_re_enable_trading_after_recovery` |
| | Works when the dashboard is down | ✅ | `test_runs_as_a_separate_process_without_the_dashboard` |
| | (extra) Gateway fails closed without a watchdog | ✅ | `test_gateway_fails_closed_without_a_healthy_watchdog` |
| Correlation | All five tests | ⏳ Phase 4 | Correlation engine not built yet. |
| Shadow trading | All six tests | ⏳ Phase 5 | Shadow trading system not built yet. |

## 3. Bugs the tests caught during this phase

1. **The gateway trusted a 30-second-old reconciliation.** A market settled on the exchange in between, and the shadow risk manager flagged a position mismatch. The fix: the gateway now reconciles immediately before every entry.
2. **The shadow risk manager rebuilt positions only from fills,** so it missed a position that appeared without matching fills. The fix: it now also compares the exchange's own position list against that history, and against the primary.
3. **A heartbeat timestamped in the future looked "fresh" to the watchdog.** The fix: future timestamps are now treated as a clock problem and lock trading.
4. **The shadow approved orders the gateway later dropped and kept counting them as in flight**, which would have caused false spending-mismatch alarms. The fix: the gateway releases such orders, and the shadow closes the entry only after the exchange confirms no such order exists.

## 4. Known limitations (honest list)

- **The shadow risk manager runs in the gateway's process.** It uses separate code, a separate database and a separate signing key, but moving it to its own OS process, so the gateway can't read its private key, comes with Phase 6.
- **The exchange is a fake.** Real Kalshi and Alpaca clients come in Phase 3, after verifying their current documentation (`docs.kalshi.com` must be allowed on the network).
- **Unrealized drawdown is passed as $0** until live market data exists (Phase 3), so the drawdown check covers realized losses only for now.
- **Notifications are queued** in a durable outbox. Windows desktop delivery comes in Phase 7.
- **The dashboard is read-only** against real data. Owner controls through the gateway come in Phase 7.
- **Watchdog cancellation needs the exchange to be up.** As a backstop, ATLAS uses immediate-or-cancel orders with an exchange-side expiry, so nothing rests for long.
- **Unknown orders** keep their reservation for up to 120 s before the exchange's "no such order" answer releases them. This is conservative by design.
- **Near midnight**, the ledger counts spending by the reservation's day while the shadow counts by fill time. A mismatch locks trading, so it fails closed, but it may need a manual resume.
- **The audit log is tamper-evident, not tamper-proof,** on a computer someone fully controls.
- **Exits and stop-loss orders** are supported by the ledger, but the gateway exit path is built in Phase 6.

## 5. Running the tests (Windows)

```powershell
cd path\to\Zaluea\atlas
python -m pip install -r requirements.txt
python -m pytest tests
```
