from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple, List

@dataclass
class EdgeStopConfig:
    enabled: bool = False
    lev_dd: float = 0.08          # trigger if lev_roi <= -8%
    z_revert: float = 0.55        # abs(z_now) <= abs(z_entry) * 0.55
    z_vel_cycles: int = 3         # require N consecutive moves toward 0
    no_bounce: float = 0.02       # require no bounce better than -2% lev ROI
    hard_max_lev_dd: float = 0.15 # emergency kill at -15% lev ROI
    z_hist_max: int = 8           # keep small history

def set_entry_z_if_missing(state: Dict[str, Any], entry_z: Optional[float]) -> None:
    es = state.setdefault("edge_stop", {})
    if es.get("entry_z") is None and entry_z is not None:
        try:
            es["entry_z"] = float(entry_z)
        except Exception:
            pass

def _push_hist(es: Dict[str, Any], key: str, val: float, maxn: int) -> None:
    h = es.setdefault(key, [])
    h.append(val)
    if len(h) > maxn:
        del h[:-maxn]

def update_edge_state(
    state: Dict[str, Any],
    z_now: Optional[float],
    lev_roi: Optional[float],
    symbol: str,
    side: str,
) -> None:
    es = state.setdefault("edge_stop", {})
    es["symbol"] = symbol
    es["side"] = side
    if z_now is not None:
        try:
            _push_hist(es, "z_hist", float(z_now), int(es.get("z_hist_max", 8)) or 8)
        except Exception:
            pass
    if lev_roi is not None:
        try:
            _push_hist(es, "lev_roi_hist", float(lev_roi), 12)
        except Exception:
            pass

def _z_decaying_toward_zero(z_hist: List[float], n: int) -> bool:
    if len(z_hist) < n + 1:
        return False
    tail = z_hist[-(n + 1):]
    for a, b in zip(tail, tail[1:]):
        if abs(b) >= abs(a):
            return False
    return True

def should_edge_stop(
    state: Dict[str, Any],
    cfg: EdgeStopConfig,
    z_now: Optional[float],
    lev_roi: Optional[float],
) -> Tuple[bool, str, Dict[str, Any]]:
    es = state.get("edge_stop") or {}
    entry_z = es.get("entry_z")

    details: Dict[str, Any] = {
        "lev_roi": lev_roi,
        "z_now": z_now,
        "entry_z": entry_z,
        "cfg": cfg.__dict__,
    }

    if not cfg.enabled:
        return (False, "edge_stop_disabled", details)

    if lev_roi is None:
        return (False, "edge_stop_no_lev_roi", details)

    # Emergency kill
    if lev_roi <= -abs(cfg.hard_max_lev_dd):
        details["rule"] = "hard_max_lev_dd"
        return (True, "edge_stop_hard_kill", details)

    # Smart stop requires z info
    if z_now is None or entry_z is None:
        return (False, "edge_stop_no_z", details)

    try:
        z_now_f = float(z_now)
        entry_z_f = float(entry_z)
    except Exception:
        return (False, "edge_stop_bad_z", details)

    # DD gate
    if lev_roi > -abs(cfg.lev_dd):
        return (False, "edge_stop_dd_not_met", details)

    # Z must have reverted toward mean enough (magnitude shrunk)
    if abs(z_now_f) > abs(entry_z_f) * float(cfg.z_revert):
        details["rule"] = "z_not_reverted_enough"
        return (False, "edge_stop_hold", details)

    z_hist = (es.get("z_hist") or [])
    z_decaying = _z_decaying_toward_zero(z_hist, int(cfg.z_vel_cycles))
    details["z_decaying"] = z_decaying
    if not z_decaying:
        return (False, "edge_stop_hold", details)

    # No bounce check (avoid stopping if it already bounced meaningfully)
    roi_hist = (es.get("lev_roi_hist") or [])
    if roi_hist:
        best = max(roi_hist[-12:])
        details["best_lev_roi_recent"] = best
        if best > -abs(cfg.no_bounce):
            details["rule"] = "bounce_detected"
            return (False, "edge_stop_hold", details)

    details["rule"] = "edge_decay_stop"
    return (True, "edge_stop_triggered", details)

