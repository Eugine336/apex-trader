"""APEX TRADER — Dashboard Controls Mixin."""

from typing import Any, Optional



class ControlsMixin:
    """start/stop/pause/resume/set_risk_mode/emergency_close."""

    _trading_loop: Any
    _platform_manager: Any

    @property
    def is_live(self) -> bool: ...

    def start_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = True
            return {"status": "started", "message": "Trading started"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def stop_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = False
            return {"status": "stopped", "message": "Trading stopped"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def pause_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = False
            return {"status": "paused", "message": "Trading paused"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def resume_trading(self) -> dict:
        if self.is_live:
            self._trading_loop.running = True
            return {"status": "resumed", "message": "Trading resumed"}
        return {"status": "no_engine_attached", "message": "No engine attached"}

    def set_risk_mode(self, mode: Optional[str]) -> dict:
        if not self.is_live or mode is None:
            return {"status": "no_engine_attached", "message": "No engine attached"}
        try:
            dd = self._trading_loop.drawdown
            from risk.risk_engine import DrawdownMode
            dd_mode = DrawdownMode(mode.upper())
            dd._mode = dd_mode
            return {"status": "ok", "message": f"Risk mode set to {mode}", "mode": mode}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def emergency_close_all(self) -> dict:
        if not self.is_live:
            return {"status": "no_engine_attached", "closed": 0}
        closed = self._trading_loop.emergency_close_all_positions(self._platform_manager)
        return {"status": "emergency_close_complete", "closed": closed, "message": f"Closed {closed} positions"}
