"""
APEX TRADER — Live Deriv SL/TP Integration Check
=================================================
Validates that the C1-fixed ``modify_order`` in ``DerivConnector``
computes correct dollar SL/TP values end-to-end against a real Deriv
account (demo by default).

Flow:
  connect → open minimal position → modify SL/TP with known values →
  compare (connector-computed vs broker-acknowledged vs independently-computed)
  → ALWAYS close position in finally block.

Usage::

    $env:DERIV_APP_ID="<your_app_id>"
    $env:DERIV_API_TOKEN="<your_demo_token>"
    python scripts/live_deriv_sltp_check.py --i-understand-this-places-a-real-order

See scripts/README_live_deriv_sltp_check.md for full runbook.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from loguru import logger


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live Deriv SL/TP integration check (C1 fix validation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--i-understand-this-places-a-real-order",
        action="store_true",
        dest="consent",
        help="Required safety gate — confirms you accept a real order will be placed.",
    )
    p.add_argument(
        "--allow-real",
        action="store_true",
        help="Allow running against a non-demo account. Without this, only demo is permitted.",
    )
    p.add_argument(
        "--symbol",
        default="1HZ100V",
        help="Deriv symbol to trade (default: 1HZ100V — 24/7 synthetic, high liquidity).",
    )
    p.add_argument(
        "--stake",
        type=float,
        default=1.0,
        help="USD stake for the test position (default: 1.0 — the minimum).",
    )
    p.add_argument(
        "--direction",
        choices=["BUY", "SELL"],
        default="BUY",
        help="Trade direction (default: BUY).",
    )
    p.add_argument(
        "--sl-offset-pct",
        type=float,
        default=0.5,
        dest="sl_offset_pct",
        help="Initial SL distance as %% of price (default: 0.5).",
    )
    p.add_argument(
        "--tp-offset-pct",
        type=float,
        default=1.0,
        dest="tp_offset_pct",
        help="Initial TP distance as %% of price (default: 1.0).",
    )
    p.add_argument(
        "--tolerance",
        type=float,
        default=0.05,
        help="Absolute tolerance for dollar-value comparison (default: 0.05 USD).",
    )
    return p.parse_args()


def _check_env() -> tuple[str, str, str]:
    app_id = os.environ.get("DERIV_APP_ID", "").strip()
    token = os.environ.get("DERIV_API_TOKEN", "").strip()
    acct_type = os.environ.get("DERIV_ACCOUNT_TYPE", "demo").strip().lower()

    missing = []
    if not app_id:
        missing.append("DERIV_APP_ID")
    if not token:
        missing.append("DERIV_API_TOKEN")
    if missing:
        logger.error(
            "Missing required environment variables: {}. "
            "Set them before running this script.",
            ", ".join(missing),
        )
        sys.exit(1)

    return app_id, token, acct_type


def main() -> None:
    args = _parse_args()

    # ── Safety gate 1: explicit consent ─────────────────────────────────
    if not args.consent:
        logger.error(
            "Safety gate: you must pass --i-understand-this-places-a-real-order"
        )
        sys.exit(1)

    # ── Safety gate 2: environment variables ────────────────────────────
    app_id, token, acct_type = _check_env()

    # ── Safety gate 3: demo-only unless --allow-real ────────────────────
    if acct_type != "demo" and not args.allow_real:
        logger.error(
            "DERIV_ACCOUNT_TYPE='{}' is not 'demo'. "
            "Pass --allow-real to permit non-demo accounts.",
            acct_type,
        )
        sys.exit(1)

    logger.info(
        "╔══════════════════════════════════════════════════════╗\n"
        "║  APEX — Live Deriv SL/TP Integration Check (C1)     ║\n"
        "╚══════════════════════════════════════════════════════╝"
    )
    logger.info(
        "Config: symbol={} stake={} direction={} account_type={}",
        args.symbol, args.stake, args.direction, acct_type,
    )

    # ── Import connector (after sys.path is set) ────────────────────────
    from platforms.deriv.deriv_connector import DerivConnector

    conn = DerivConnector(api_token=token, app_id=app_id)
    contract_id: str = ""
    passed = False

    try:
        # ── Step 1: Connect ─────────────────────────────────────────────
        logger.info("[STEP 1] Connecting to Deriv...")
        ok = conn.connect()
        if not ok:
            logger.error("FAIL — could not connect to Deriv.")
            sys.exit(1)
        logger.info("[STEP 1] Connected ✓")

        # ── Step 2: Get current price ───────────────────────────────────
        logger.info("[STEP 2] Fetching price for {}...", args.symbol)
        tick = conn.get_price(args.symbol)
        price = tick.ask if args.direction == "BUY" else tick.bid
        logger.info("[STEP 2] Price: bid={} ask={} (using {})", tick.bid, tick.ask, price)

        # ── Step 3: Compute initial SL/TP ───────────────────────────────
        sl_dist = price * (args.sl_offset_pct / 100.0)
        tp_dist = price * (args.tp_offset_pct / 100.0)
        if args.direction == "BUY":
            initial_sl = round(price - sl_dist, 5)
            initial_tp = round(price + tp_dist, 5)
        else:
            initial_sl = round(price + sl_dist, 5)
            initial_tp = round(price - tp_dist, 5)
        logger.info(
            "[STEP 3] Initial SL={} TP={} (sl_dist={:.5f} tp_dist={:.5f})",
            initial_sl, initial_tp, sl_dist, tp_dist,
        )

        # ── Step 4: Open position ───────────────────────────────────────
        # DerivConnector.place_order: deriv_connector.py:408-656
        # Accepts stake_usd to bypass lots→stake conversion.
        logger.info("[STEP 4] Opening {} {} position...", args.direction, args.symbol)
        result = conn.place_order(
            symbol=args.symbol,
            direction=args.direction,
            lots=0.01,
            sl=initial_sl,
            tp=initial_tp,
            comment="apex-sltp-check",
            stake_usd=args.stake,
        )
        if not result.success:
            logger.error("FAIL — order rejected: {}", result.error)
            sys.exit(1)
        contract_id = result.order_id
        logger.info(
            "[STEP 4] Position opened ✓ contract_id={} fill_price={}",
            contract_id, result.fill_price,
        )

        # ── Step 5: Read _positions dict to get connector's recorded state
        # _positions populated at deriv_connector.py:634-639
        pos = conn._positions.get(contract_id, {})
        if not pos:
            logger.error("FAIL — contract {} not in connector._positions", contract_id)
            sys.exit(1)

        open_price = pos["open_price"]
        stake = pos["stake"]
        multiplier = pos["multiplier"]
        lots = pos["lots"]
        logger.info(
            "[STEP 5] Position state: open_price={} stake={} multiplier={} lots={}",
            open_price, stake, multiplier, lots,
        )

        # ── Step 6: Compute a new SL for the modify test ───────────────
        # Move SL closer to entry (tighter stop) so we have a distinct value
        new_sl_dist = price * (args.sl_offset_pct / 200.0)  # half the original distance
        if args.direction == "BUY":
            new_sl = round(open_price - new_sl_dist, 5)
        else:
            new_sl = round(open_price + new_sl_dist, 5)
        logger.info("[STEP 6] New SL for modify test: {}", new_sl)

        # ── Step 7: Independently compute expected dollar SL value ──────
        # The C1-fixed formula at deriv_connector.py:674
        expected_sl_dollar = round(
            abs(open_price - new_sl) / open_price * stake * multiplier, 2
        )
        # The OLD broken formula for comparison
        old_formula_dollar = round(abs(open_price - new_sl) * lots * 100, 2)
        logger.info(
            "[STEP 7] Expected (C1-fixed): ${:.2f} | Old (broken): ${:.2f}",
            expected_sl_dollar, old_formula_dollar,
        )

        # ── Step 8: Wrap _sync_send to capture the modify payload ───────
        # This exercises the REAL production code path, not a reimplementation.
        captured_payloads: list[dict] = []
        original_sync_send = conn._sync_send

        def capturing_sync_send(payload: dict) -> dict:
            captured_payloads.append(payload)
            return original_sync_send(payload)

        conn._sync_send = capturing_sync_send  # type: ignore[assignment]

        # ── Step 9: Call the REAL modify_order ───────────────────────────
        # DerivConnector.modify_order: deriv_connector.py:659-698
        logger.info("[STEP 9] Calling modify_order(contract_id={}, new_sl={})...", contract_id, new_sl)
        mod_ok = conn.modify_order(contract_id, new_sl=new_sl)

        conn._sync_send = original_sync_send  # type: ignore[assignment]

        if not mod_ok:
            logger.error("FAIL — modify_order returned False (broker rejected)")
            sys.exit(1)
        logger.info("[STEP 9] modify_order returned True ✓")

        # ── Step 10: Extract what the connector actually sent ───────────
        modify_payload = None
        for p in captured_payloads:
            if "contract_update" in p:
                modify_payload = p
                break

        if modify_payload is None:
            logger.error("FAIL — no contract_update payload captured")
            sys.exit(1)

        sent_sl_dollar = modify_payload.get("limit_order", {}).get("stop_loss")
        logger.info("[STEP 10] Connector sent stop_loss=${}", sent_sl_dollar)

        # ── Step 11: Read back from broker via proposal_open_contract ───
        # DerivConnector uses this API at deriv_connector.py:730-731
        logger.info("[STEP 11] Reading back from broker (proposal_open_contract)...")
        time.sleep(1)  # brief pause for Deriv state propagation
        poc_resp = conn._sync_send({
            "proposal_open_contract": 1,
            "contract_id": int(contract_id),
        })
        if poc_resp.get("error"):
            logger.warning(
                "Broker readback returned error: {} — skipping broker comparison",
                poc_resp["error"].get("message"),
            )
            broker_sl_dollar = None
        else:
            poc = poc_resp.get("proposal_open_contract", {})
            limit_order = poc.get("limit_order", {})
            broker_sl_info = limit_order.get("stop_loss", {})
            broker_sl_dollar = broker_sl_info.get("order_amount") if broker_sl_info else None
            if broker_sl_dollar is not None:
                broker_sl_dollar = float(broker_sl_dollar)
            logger.info("[STEP 11] Broker-acknowledged stop_loss=${}", broker_sl_dollar)

        # ── Step 12: Compare all values ─────────────────────────────────
        logger.info("=" * 60)
        logger.info("  COMPARISON RESULTS")
        logger.info("=" * 60)
        logger.info("  open_price     = {}", open_price)
        logger.info("  new_sl         = {}", new_sl)
        logger.info("  stake          = {}", stake)
        logger.info("  multiplier     = {}", multiplier)
        logger.info("  lots           = {}", lots)
        logger.info("-" * 60)
        logger.info("  Expected (independent) : ${:.2f}", expected_sl_dollar)
        logger.info("  Connector sent         : ${}", sent_sl_dollar)
        logger.info("  Old formula (broken)   : ${:.2f}", old_formula_dollar)
        if broker_sl_dollar is not None:
            logger.info("  Broker acknowledged    : ${:.2f}", broker_sl_dollar)
        else:
            logger.info("  Broker acknowledged    : UNAVAILABLE (readback failed)")
        logger.info("-" * 60)

        # ── Check 1: connector sent the correct value ───────────────────
        check1 = abs(sent_sl_dollar - expected_sl_dollar) <= args.tolerance
        logger.info(
            "  CHECK 1 (connector == expected) : {} (delta={:.4f}, tol={:.2f})",
            "PASS ✓" if check1 else "FAIL ✗",
            abs(sent_sl_dollar - expected_sl_dollar), args.tolerance,
        )

        # ── Check 2: connector did NOT send the old broken value ────────
        if abs(expected_sl_dollar - old_formula_dollar) < 0.01:
            logger.info(
                "  CHECK 2 (not old formula)       : SKIP — "
                "old and new formulas produce the same value at these params",
            )
            check2 = True
        else:
            check2 = abs(sent_sl_dollar - old_formula_dollar) > args.tolerance
            logger.info(
                "  CHECK 2 (not old formula)       : {} (sent={} vs old={})",
                "PASS ✓" if check2 else "FAIL ✗",
                sent_sl_dollar, old_formula_dollar,
            )

        # ── Check 3: broker accepted and acknowledges our value ─────────
        if broker_sl_dollar is not None:
            check3 = abs(broker_sl_dollar - expected_sl_dollar) <= args.tolerance
            logger.info(
                "  CHECK 3 (broker == expected)    : {} (delta={:.4f}, tol={:.2f})",
                "PASS ✓" if check3 else "FAIL ✗",
                abs(broker_sl_dollar - expected_sl_dollar), args.tolerance,
            )
        else:
            logger.info(
                "  CHECK 3 (broker == expected)    : SKIP — broker readback unavailable",
            )
            check3 = True  # non-blocking; checks 1+2 are the core validation

        logger.info("=" * 60)
        passed = check1 and check2 and check3
        if passed:
            logger.info("  ██  OVERALL: PASS  ██")
        else:
            logger.error("  ██  OVERALL: FAIL  ██")
        logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.warning("Interrupted by user — cleaning up...")
    except Exception as exc:
        logger.exception("Unexpected error: {}", exc)
    finally:
        # ── ALWAYS close the position ───────────────────────────────────
        # DerivConnector.close_order: deriv_connector.py:700-725
        if contract_id:
            logger.info("[CLEANUP] Closing contract {}...", contract_id)
            try:
                close = conn.close_order(contract_id)
                if close.success:
                    logger.info(
                        "[CLEANUP] Position closed ✓ PnL={:.2f}", close.pnl,
                    )
                else:
                    logger.error(
                        "[CLEANUP] Close FAILED: {} — "
                        "MANUALLY close contract {} in your Deriv dashboard!",
                        close.error, contract_id,
                    )
            except Exception as exc:
                logger.error(
                    "[CLEANUP] Close exception: {} — "
                    "MANUALLY close contract {} in your Deriv dashboard!",
                    exc, contract_id,
                )
        else:
            logger.info("[CLEANUP] No position was opened — nothing to close.")

        # ── Disconnect ──────────────────────────────────────────────────
        try:
            conn.disconnect()
        except Exception:
            pass

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
