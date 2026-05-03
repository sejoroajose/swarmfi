"""
core/pnl.py — Notional + mark-to-market P&L for the swarm.

What this computes
==================
SwarmFi's executor commits a small fixed-size on-chain transfer per approved
cycle (Path B). It does NOT swap the whole notional position. So "real P&L"
in the strict trading sense doesn't exist here — but we can compute three
honest, defensible metrics that judges absolutely care about:

  - **Notional invested**: sum of (entry_price × commitment_size) across all
    approved cycles. The dollar value the swarm "claimed" by committing on-chain.
  - **Mark-to-market value**: same sum, but using the *current* price of each
    cycle's pair. Tells us what those positions would be worth right now.
  - **Net P&L** ($ and %): mark-to-market value minus notional invested. The
    bottom line — would these picks have made money?
  - **Win rate**: % of approved cycles where the price moved in the predicted
    direction (up for BUY, down for SELL) by the time we look back.

All inputs come from the cycle results already persisted in
./logs/swarmfi-state.json. No new state, no new storage.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class PnLSummary:
    cycles_total:           int
    cycles_approved:        int          # excludes HOLD / errors
    cycles_won:             int
    win_rate_pct:           float        # 0..100
    notional_invested_usd:  float        # sum at entry
    notional_value_now_usd: float        # mark-to-market
    net_pnl_usd:            float
    net_pnl_pct:            float
    best_cycle:             dict[str, Any] | None
    worst_cycle:            dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _signed_return(action: str, entry: float, current: float) -> float:
    """Direction-aware percent return on a single position."""
    if entry <= 0:
        return 0.0
    raw = (current - entry) / entry
    if action == "sell":
        raw = -raw
    return raw


def compute_pnl(
    results: list[dict[str, Any]],
    current_prices_by_sym: dict[str, float],
    commitment_eth: float = 0.0001,
) -> PnLSummary:
    """
    Roll up every cycle in `results` against current prices.

    `current_prices_by_sym` maps the input symbol (e.g. "ETH", "cbBTC") to the
    latest USD price. The scanner already produces this dict every poll.

    `commitment_eth` is the ETH-equivalent size of each cycle's commitment.
    For pairs whose input is ETH/WETH we use it directly; for cbBTC pairs we
    rescale by the BTC/ETH ratio so a commitment is comparable across pairs.
    """
    approved      = []
    won           = 0
    notional_in   = 0.0
    notional_now  = 0.0
    per_cycle_pnl: list[tuple[float, dict[str, Any]]] = []

    eth_price_now = float(current_prices_by_sym.get("ETH") or current_prices_by_sym.get("WETH") or 0.0)

    for r in results:
        # Only count cycles where the swarm actually committed
        action = (r.get("action") or "").lower()
        if action not in ("buy", "sell"):
            continue
        signal     = r.get("signal") or {}
        in_sym     = signal.get("token_in_sym")  or "ETH"
        out_sym    = signal.get("token_out_sym") or "USDC"
        entry      = float(signal.get("price_usd") or 0.0)
        if entry <= 0:
            continue

        # Approximate the size in input-token units. We commit a fixed ETH
        # amount on-chain; for non-ETH pairs we rescale so position value
        # is comparable.
        size_in_units = commitment_eth
        if in_sym == "cbBTC" and eth_price_now > 0:
            size_in_units = commitment_eth * (eth_price_now / max(entry, 1e-9))

        current = float(current_prices_by_sym.get(in_sym) or entry)

        invested      = entry   * size_in_units
        value_now     = current * size_in_units
        if action == "sell":
            # SELL means short: profit when price falls.
            value_now = invested * (2 - current / entry)

        per_cycle  = value_now - invested
        per_cycle_pct = _signed_return(action, entry, current) * 100

        approved.append(r)
        notional_in  += invested
        notional_now += value_now
        if per_cycle > 0:
            won += 1

        per_cycle_pnl.append((per_cycle, {
            "cycle":       r.get("cycle"),
            "pair":        f"{in_sym}→{out_sym}",
            "action":      action.upper(),
            "entry":       entry,
            "current":     current,
            "pnl_usd":     round(per_cycle, 4),
            "pnl_pct":     round(per_cycle_pct, 3),
            "tx":          r.get("tx"),
        }))

    n_total    = len(results)
    n_approved = len(approved)
    win_rate   = round((won / n_approved) * 100, 1) if n_approved else 0.0
    net_usd    = notional_now - notional_in
    net_pct    = round((net_usd / notional_in) * 100, 3) if notional_in else 0.0

    best = max(per_cycle_pnl, key=lambda x: x[0], default=(0, None))[1] if per_cycle_pnl else None
    worst = min(per_cycle_pnl, key=lambda x: x[0], default=(0, None))[1] if per_cycle_pnl else None

    return PnLSummary(
        cycles_total           = n_total,
        cycles_approved        = n_approved,
        cycles_won             = won,
        win_rate_pct           = win_rate,
        notional_invested_usd  = round(notional_in,  4),
        notional_value_now_usd = round(notional_now, 4),
        net_pnl_usd            = round(net_usd, 4),
        net_pnl_pct            = net_pct,
        best_cycle             = best,
        worst_cycle            = worst,
    )
