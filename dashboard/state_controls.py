"""APEX TRADER — Dashboard Controls Mixin."""

from typing import Any, Optional
from loguru import logger



class ControlsMixin:
    """start/stop/pause/resume/set_risk_mode/emergency_close (event-driven)."""

    _platform_manager: Any

    @property
    def is_live(self) -> bool: ...

    def _persist_control(self, key: str, value: Any, action: str) -> bool:
        store = getattr(self, "_control_store", None)
        if store is None:
            return True
        try:
            store.set(key, value, action=action)
            return True
        except Exception:
            return False

    def _record_control(self, action: str, outcome: str) -> None:
        store = getattr(self, "_control_store", None)
        if store is not None:
            try:
                store.record(action, outcome=outcome)
            except Exception as exc:
                # Audit-storage failure must not suppress a risk-reducing stop
                # or emergency close request.
                logger.error(
                    "[dashboard] control audit write failed for {}: {}",
                    action,
                    type(exc).__name__,
                )

    def start_trading(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            if not bool(getattr(ed, "is_running", False)):
                self._record_control("start", "restart_required")
                return {
                    "status": "restart_required",
                    "message": "The stopped event-driven system must be restarted by its process supervisor",
                }
            self._record_control("start", "accepted")
            return {"status": "running", "message": "Event-driven system is running"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def stop_trading(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            self._record_control("stop", "accepted")
            ed.stop()
            return {"status": "stopped", "message": "Event-driven system stopped"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def pause_trading(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            if not self._persist_control("entries_paused", True, "pause_entries"):
                return {"status": "error", "message": "Could not persist the pause control; engine state was not changed"}
            ed._paused = True
            return {"status": "paused", "message": "Event-driven entries paused (management continues)"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def resume_trading(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            if not self._persist_control("entries_paused", False, "resume_entries"):
                return {"status": "error", "message": "Could not persist the resume control; engine state was not changed"}
            ed._paused = False
            return {"status": "resumed", "message": "Event-driven entries resumed"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def set_risk_mode(self, mode: Optional[str]) -> dict:
        """Manual risk-mode control: tightening only.

        An operator may FREEZE new exposure. Loosening (NORMAL/CAUTION/RECOVERY)
        is derived by the DrawdownGuard from realised loss state and cannot be
        forced from the dashboard, because that would let a request waive the
        daily loss limit.
        """
        if mode is None:
            return {"status": "no_engine_attached", "message": "No engine attached"}
        ed = getattr(self, "_event_driven_system", None)
        ctx = getattr(ed, "_ctx", None) if ed is not None else None
        dd = ctx.drawdown_guard if ctx is not None else None
        if dd is None:
            return {"status": "no_engine_attached", "message": "No engine attached"}
        requested = str(mode).upper()
        if requested != "FROZEN":
            return {
                "status": "rejected",
                "message": "Only FROZEN can be set manually; other modes are derived by the DrawdownGuard",
                "mode": requested,
            }
        try:
            from brain.drawdown_guard import DrawdownMode
            if not self._persist_control("manual_risk_mode", "FROZEN", "set_risk_mode_frozen"):
                return {"status": "error", "message": "Could not persist the freeze control; risk mode was not changed"}
            dd.mode = DrawdownMode.FROZEN
            return {"status": "ok", "message": "Risk mode set to FROZEN", "mode": "FROZEN"}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def emergency_close_all(self) -> dict:
        ed = getattr(self, "_event_driven_system", None)
        if ed is not None:
            self._record_control("emergency_close_all", "requested")
            try:
                from execution.intents import Intent, IntentType
                positions = self._platform_manager.get_all_open_positions()
                for pos in positions:
                    ticket = str(getattr(pos, "order_id", getattr(pos, "ticket", "")))
                    symbol = getattr(pos, "symbol", "")
                    direction = getattr(pos, "direction", "")
                    ed._aggregator.submit([Intent(
                        intent_type=IntentType.CLOSE,
                        position_ticket=ticket,
                        symbol=symbol,
                        direction=direction,
                        reason="EMERGENCY: manual close all",
                    )])
                self._record_control("emergency_close_all", f"submitted:{len(positions)}")
                return {"status": "emergency_close_submitted", "closed": len(positions), "message": f"Submitted {len(positions)} close intents"}
            except Exception as exc:
                self._record_control("emergency_close_all", "failed")
                return {"status": "error", "closed": 0, "message": str(exc)}
        return {"status": "no_engine_attached", "closed": 0}
