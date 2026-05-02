"""
core/scanner.py — Multi-pair edge scanner for SwarmFi's researcher agent.

Why this exists
===============
A single-pair demo (always ETH→USDC) reads as a toy. Real autonomous DeFi
swarms have to *choose* which pair to act on out of a candidate set, using
a composite edge profile that mirrors how quant desks rank opportunities.

This scanner:
  1. Sweeps a fixed list of Base bluechip pairs (configurable).
  2. Pulls a live price for each input token from CoinGecko.
  3. Scores each pair against a structured **edge profile** combining:
       • momentum   — synthetic 1h % move proxy (CoinGecko 24h delta scaled)
       • bluechip   — strong preference for ETH/USDC/USDT/cbBTC pairs
       • spread     — tighter pairs (stable → stable, ETH → USDC) preferred
       • size_fit   — penalty if the swarm's amount is unrealistic for the pair
  4. Returns a ranked list — the top one is what the researcher commits to.

The edge profile is intentionally simple, transparent, and auditable. Every
sub-score is recorded on 0G Storage alongside the pick so judges (and the
swarm's own risk agent) can see exactly why a pair was chosen.

Inspired by public quant playbooks (Carver's "Systematic Trading", Lopez de
Prado's "Advances in Financial ML") — momentum + carry/spread + liquidity
filter — adapted to a 1-cycle DeFi context.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, asdict
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ── Bluechip pair registry on Base (chain 8453) ───────────────────────────────
#
# These are the pairs the swarm will actually scan. All on Base, all with deep
# liquidity on Uniswap V3, all bluechip enough that the risk agent won't auto-
# reject them as "unknown tokens".
#
# token_in / token_out follow the native-ETH convention: 0x000…000 for native.
# coingecko_id is used to fetch the live price for the *input* token only —
# we don't need a quote for the output side because the LLM just consumes the
# directional signal.

@dataclass(frozen=True)
class PairSpec:
    pair_id:     str
    in_sym:      str
    out_sym:     str
    token_in:    str
    token_out:   str
    chain_id:    int
    coingecko_id: str
    # static priors used by the edge profile
    bluechip_score: float    # 0..1, how "bluechip" the input token is
    spread_score:   float    # 0..1, how tight the typical spread is
    label:          str

PAIRS: list[PairSpec] = [
    PairSpec(
        pair_id="ETH_USDC", in_sym="ETH", out_sym="USDC",
        token_in="0x0000000000000000000000000000000000000000",
        token_out="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        chain_id=8453, coingecko_id="ethereum",
        bluechip_score=1.00, spread_score=0.92,
        label="ETH → USDC",
    ),
    PairSpec(
        pair_id="ETH_USDT", in_sym="ETH", out_sym="USDT",
        token_in="0x0000000000000000000000000000000000000000",
        token_out="0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        chain_id=8453, coingecko_id="ethereum",
        bluechip_score=0.95, spread_score=0.88,
        label="ETH → USDT",
    ),
    PairSpec(
        pair_id="cbBTC_USDC", in_sym="cbBTC", out_sym="USDC",
        token_in="0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        token_out="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        chain_id=8453, coingecko_id="bitcoin",
        bluechip_score=0.95, spread_score=0.85,
        label="cbBTC → USDC",
    ),
    PairSpec(
        pair_id="WETH_USDC", in_sym="WETH", out_sym="USDC",
        token_in="0x4200000000000000000000000000000000000006",
        token_out="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        chain_id=8453, coingecko_id="ethereum",
        bluechip_score=0.90, spread_score=0.93,
        label="WETH → USDC",
    ),
]


def get_pair(pair_id: str) -> PairSpec | None:
    for p in PAIRS:
        if p.pair_id == pair_id:
            return p
    return None


# ── Edge profile scorer ───────────────────────────────────────────────────────

@dataclass
class PairScore:
    """A single pair's edge profile. Each sub-score is in [0, 1]."""
    pair:          PairSpec
    price_usd:     float
    momentum_24h:  float             # raw 24h % change from CoinGecko
    momentum:      float             # normalised momentum sub-score [0..1]
    bluechip:      float
    spread:        float
    size_fit:      float
    composite:     float             # final 0..1 score
    signal:        str               # "strong" | "medium" | "weak"
    reasoning:     str               # one-line plain-English summary

    def to_signal_payload(self, amount_in_wei: int) -> dict[str, Any]:
        """Render as a market-signal dict for the rest of the swarm."""
        return {
            "token_in":       self.pair.token_in,
            "token_out":      self.pair.token_out,
            "token_in_sym":   self.pair.in_sym,
            "token_out_sym":  self.pair.out_sym,
            "chain_id":       self.pair.chain_id,
            "price_usd":      self.price_usd,
            "signal":         self.signal,
            "reason":         self.reasoning,
            "amount_in_wei":  amount_in_wei,
            "edge_profile": {
                "momentum_24h":  self.momentum_24h,
                "momentum":      round(self.momentum, 3),
                "bluechip":      round(self.bluechip, 3),
                "spread":        round(self.spread, 3),
                "size_fit":      round(self.size_fit, 3),
                "composite":     round(self.composite, 3),
            },
        }


def _classify_signal(composite: float) -> str:
    if composite >= 0.72: return "strong"
    if composite >= 0.55: return "medium"
    return "weak"


def _normalise_momentum(pct_24h: float) -> float:
    """
    Map a 24h % change to a [0, 1] momentum score.
    Linear ramp: -5% → 0.0, 0% → 0.5, +5% → 1.0; clamped.
    """
    return max(0.0, min(1.0, (pct_24h + 5.0) / 10.0))


def _size_fit(amount_wei: int, in_sym: str) -> float:
    """
    Penalise sizes that are unrealistic for the pair. We expect roughly
    0.0001..1.0 ETH-equivalent for this swarm; everything else is dampened.
    Returns a [0..1] score.
    """
    eth = amount_wei / 1e18
    # convert btc-denominated pairs to ETH-equivalents loosely (1 cbBTC ≈ 30 ETH at typical prices)
    if in_sym == "cbBTC":
        eth *= 30
    if 0.0001 <= eth <= 1.0:
        return 1.0
    if eth < 0.0001:
        return max(0.3, eth / 0.0001)
    return max(0.3, 1.0 / eth)


def _build_reasoning(p: PairSpec, mom_pct: float, signal: str, composite: float) -> str:
    direction = "up" if mom_pct >= 0 else "down"
    return (
        f"{p.in_sym}/{p.out_sym} on Base · 24h {direction} {abs(mom_pct):.2f}% · "
        f"composite {composite:.2f} · {signal} bluechip momentum signal"
    )


# ── Live price fetcher (CoinGecko, no API key needed) ─────────────────────────
#
# CoinGecko's free tier hard-rate-limits at ~10–30 req/min. The dashboard
# polls /api/scan every 1.5s and chat calls scan_pairs() on every message —
# without caching we'd burst through the limit in seconds, get 429s, and
# silently fall back to all-zero prices. So we serve from an in-process TTL
# cache; only refetch when stale.

_COINGECKO_URL  = "https://api.coingecko.com/api/v3/simple/price"
_CACHE_TTL_SECS = 30  # one fetch every 30s is well under any free-tier cap

import asyncio as _asyncio
import json as _json
import time as _time
from pathlib import Path as _Path

# Persist the cache to disk so a fresh process doesn't have to round-trip
# CoinGecko on first scan (the free tier 429s on burst — bad UX).
_CACHE_FILE = _Path(__file__).resolve().parent.parent / "logs" / "coingecko-cache.json"

_price_cache: dict[str, dict[str, float]] = {}
_price_cache_at: float = 0.0
_price_lock = _asyncio.Lock()


def _load_disk_cache() -> None:
    """Hydrate the in-memory cache from disk on first call."""
    global _price_cache, _price_cache_at
    if _price_cache:
        return  # already populated this process
    try:
        if _CACHE_FILE.exists():
            data = _json.loads(_CACHE_FILE.read_text())
            _price_cache = data.get("cache") or {}
            # Treat the disk read as "now-ish" — even minutes-old prices are
            # better than $0.00 across the dashboard. The 30s TTL still
            # triggers a refresh on the next scan.
            _price_cache_at = float(data.get("at") or 0.0)
    except Exception:
        pass


def _save_disk_cache() -> None:
    """Persist the in-memory cache after a successful fetch."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(_json.dumps({
            "cache": _price_cache,
            "at":    _price_cache_at,
        }))
    except Exception:
        pass


async def _fetch_prices(coingecko_ids: list[str]) -> dict[str, dict[str, float]]:
    """
    Fetch USD price + 24h change for a list of coingecko IDs.
    Cached for _CACHE_TTL_SECS to stay under CoinGecko's free-tier rate limit.
    Returns {id: {"usd": float, "usd_24h_change": float}}.
    """
    global _price_cache, _price_cache_at
    import httpx

    # Hydrate from disk on first call so a fresh process has prices
    _load_disk_cache()

    now = _time.time()
    # Serve from cache if fresh AND covers every requested ID
    if (now - _price_cache_at) < _CACHE_TTL_SECS and all(i in _price_cache for i in coingecko_ids):
        return {i: _price_cache[i] for i in coingecko_ids}

    # Single in-flight refresh — multiple concurrent requests share one fetch
    async with _price_lock:
        # Re-check after acquiring the lock (another caller may have refreshed)
        now = _time.time()
        if (now - _price_cache_at) < _CACHE_TTL_SECS and all(i in _price_cache for i in coingecko_ids):
            return {i: _price_cache[i] for i in coingecko_ids}

        ids = ",".join(sorted(set(coingecko_ids)))
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(_COINGECKO_URL, params={
                    "ids":                 ids,
                    "vs_currencies":       "usd",
                    "include_24hr_change": "true",
                })
                # On 429 (rate limited) keep returning the stale cache so the UI
                # never shows $0.00 — better an old price than no price.
                if r.status_code == 200:
                    fresh = r.json() or {}
                    if fresh:
                        _price_cache.update(fresh)
                        _price_cache_at = now
                        _save_disk_cache()
                        return {i: _price_cache.get(i, {}) for i in coingecko_ids}
                else:
                    log.warning("CoinGecko non-200", status=r.status_code)
        except Exception as exc:
            log.warning("CoinGecko fetch failed", error=str(exc))

        # Network error / 429 / empty body — fall back to whatever we have
        return {i: _price_cache.get(i, {}) for i in coingecko_ids}


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    ranked:  list[PairScore] = field(default_factory=list)
    best:    PairScore | None = None
    fetched_at: str           = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at,
            "best":       asdict(self.best.pair) | {
                "composite": self.best.composite,
                "signal":    self.best.signal,
                "reasoning": self.best.reasoning,
            } if self.best else None,
            "ranked": [
                {
                    "pair_id":    s.pair.pair_id,
                    "label":      s.pair.label,
                    "price_usd":  s.price_usd,
                    "momentum_24h": s.momentum_24h,
                    "composite":  s.composite,
                    "signal":     s.signal,
                }
                for s in self.ranked
            ],
        }


async def scan_pairs(amount_in_wei: int = 1_000_000_000_000_000) -> ScanResult:
    """
    Run the multi-pair edge scanner.
    Returns ranked PairScore list with the best pick promoted.
    """
    from datetime import datetime, timezone

    prices = await _fetch_prices([p.coingecko_id for p in PAIRS])

    scored: list[PairScore] = []
    for p in PAIRS:
        info = prices.get(p.coingecko_id, {})
        price = float(info.get("usd", 0.0)) or 0.0
        mom_pct = float(info.get("usd_24h_change", 0.0)) or 0.0

        momentum_score = _normalise_momentum(mom_pct)
        size_score     = _size_fit(amount_in_wei, p.in_sym)

        # Weighted composite — 40% momentum, 25% bluechip, 20% spread, 15% size
        composite = (
            0.40 * momentum_score
          + 0.25 * p.bluechip_score
          + 0.20 * p.spread_score
          + 0.15 * size_score
        )
        signal = _classify_signal(composite)
        reasoning = _build_reasoning(p, mom_pct, signal, composite)

        scored.append(PairScore(
            pair=p,
            price_usd=price,
            momentum_24h=mom_pct,
            momentum=momentum_score,
            bluechip=p.bluechip_score,
            spread=p.spread_score,
            size_fit=size_score,
            composite=composite,
            signal=signal,
            reasoning=reasoning,
        ))

    scored.sort(key=lambda s: s.composite, reverse=True)
    return ScanResult(
        ranked=scored,
        best=scored[0] if scored else None,
        fetched_at=datetime.now(tz=timezone.utc).isoformat(),
    )


def format_scan_table(result: ScanResult) -> str:
    """Render a compact ANSI table for the CLI demo."""
    if not result.ranked:
        return "  (no pairs scanned)"
    lines = [
        f"  {'pair':<14}{'price':>12}{'24h':>9}{'mom':>7}{'edge':>7}  signal",
        f"  {'-'*14}{'-'*12}{'-'*9}{'-'*7}{'-'*7}  -------",
    ]
    for s in result.ranked:
        marker = " ★" if s is result.best else "  "
        lines.append(
            f"{marker}{s.pair.label:<14}${s.price_usd:>10,.2f}"
            f"{s.momentum_24h:>+8.2f}%{s.momentum:>7.2f}{s.composite:>7.2f}  {s.signal}"
        )
    return "\n".join(lines)
