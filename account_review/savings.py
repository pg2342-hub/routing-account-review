"""Savings against the model each engineer would have used without routing.

Two calculations are kept side by side:

* ``docs_sql_savings`` reproduces the "Computing savings yourself" query in
  weave-os/router docs/ANALYTICS_EXPORT.md: baseline = input_tokens and output_tokens
  at the requested model's base rates, actual = actual_input_cost_usd +
  actual_output_cost_usd.
* ``savings`` prices the same token work at the baseline model's rates with the
  router's own cost rules (cache reads and writes at their multipliers, provider
  token conventions, long context, US geo) and compares it with the API-rate cost of
  the served turn, so subscription-served turns are not counted as free.

The baseline still assumes the baseline model would have used the same tokens. The
router repo's bench/token-efficiency kit measures how far that assumption is off;
this report does not try to.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .pricing import ANTHROPIC_FAMILY, PriceBook, prompt_tokens

Row = dict[str, Any]


def _i(row: Row, key: str) -> int:
    v = row.get(key)
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _f(row: Row, key: str) -> float | None:
    v = row.get(key)
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def baseline_model(row: Row) -> str | None:
    return row.get("blind_experiment_baseline_model") or row.get("requested_model")


def served_api_rate_cost(row: Row) -> float | None:
    """What the served turn costs at API rates (subscription-served turns included)."""
    i, o = _f(row, "api_rate_input_cost_usd"), _f(row, "api_rate_output_cost_usd")
    if i is None or o is None:
        return None
    return i + o


def baseline_cost(row: Row, book: PriceBook) -> float | None:
    """The same token work priced at the baseline model, with the router's cost rules."""
    model = baseline_model(row)
    served_provider = row.get("decision_provider") or ""
    if not model:
        return None
    provider = book.default_provider(model)
    if provider is None:
        return None
    cw, cr = _i(row, "cache_creation_tokens"), _i(row, "cache_read_tokens")
    total_prompt = prompt_tokens(_i(row, "input_tokens"), cw, cr, served_provider)
    # Re-express the prompt in the baseline provider's token convention.
    input_tokens = total_prompt - cw - cr if provider in ANTHROPIC_FAMILY else total_prompt
    geo = row.get("inference_geo")
    i = book.input_cost(model, provider, input_tokens, cw, cr, _i(row, "cache_creation_1h_tokens"), geo)
    o = book.output_cost(model, provider, total_prompt, _i(row, "output_tokens"), geo)
    if i is None or o is None:
        return None
    return i + o


@dataclass
class Savings:
    baseline_usd: float = 0.0
    served_usd: float = 0.0
    rows_priced: int = 0
    rows_skipped: int = 0
    subscription_rows: int = 0

    @property
    def savings_usd(self) -> float:
        return self.baseline_usd - self.served_usd

    @property
    def savings_pct(self) -> float:
        return 100 * self.savings_usd / self.baseline_usd if self.baseline_usd else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_usd": round(self.baseline_usd, 2),
            "served_usd": round(self.served_usd, 2),
            "savings_usd": round(self.savings_usd, 2),
            "savings_pct": round(self.savings_pct, 1),
            "rows_priced": self.rows_priced,
            "rows_skipped": self.rows_skipped,
            "subscription_rows": self.subscription_rows,
        }


def savings(rows: Iterable[Row], book: PriceBook) -> Savings:
    out = Savings()
    for row in rows:
        served = served_api_rate_cost(row)
        base = baseline_cost(row, book)
        if served is None or base is None:
            out.rows_skipped += 1
            continue
        out.baseline_usd += base
        out.served_usd += served
        out.rows_priced += 1
        out.subscription_rows += bool(row.get("subscription_served"))
    return out


@dataclass
class DocsSqlSavings:
    baseline_usd: float = 0.0
    actual_usd: float = 0.0
    rows_priced: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def savings_usd(self) -> float:
        return self.baseline_usd - self.actual_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_usd": round(self.baseline_usd, 2),
            "actual_usd": round(self.actual_usd, 2),
            "savings_usd": round(self.savings_usd, 2),
            "rows_priced": self.rows_priced,
        }


def docs_sql_savings(rows: Iterable[Row], book: PriceBook) -> DocsSqlSavings:
    """The docs query, row for row: requested model's base rates, actual_* costs.

    The query joins model_prices on model alone. The price book lists one row per
    provider binding, so a literal join double counts multi-provider models; this
    function uses the first binding only, which is the most favourable reading.
    """
    out = DocsSqlSavings()
    for row in rows:
        ai, ao = _f(row, "actual_input_cost_usd"), _f(row, "actual_output_cost_usd")
        model = row.get("requested_model")
        found = book.get(model) if model else None
        if ai is None or found is None:  # WHERE actual_input_cost_usd IS NOT NULL
            continue
        price = found[0]
        out.baseline_usd += _i(row, "input_tokens") / 1e6 * price.input_per_1m + _i(row, "output_tokens") / 1e6 * price.output_per_1m
        out.actual_usd += ai + (ao or 0.0)
        out.rows_priced += 1
    return out


def bridge(rows: list[Row], book: PriceBook) -> list[dict[str, Any]]:
    """Walk from the docs query's number to the corrected one, one fix at a time.

    1. the docs query as written
    2. served side at API rates, so subscription-served turns are not counted as free
    3. baseline counts every cached token (Anthropic-family rows report input_tokens
       without them, other providers include them) and prices cache writes and reads
       at their multipliers, with long context and US geo as the router does.
       This equals ``savings``.
    """
    docs = docs_sql_savings(rows, book)
    priced = [r for r in rows if _f(r, "actual_input_cost_usd") is not None and book.get(r.get("requested_model") or "")]
    served_api = sum(served_api_rate_cost(r) or 0.0 for r in priced)
    final = savings(priced, book)
    steps = [
        ("Docs query as written", docs.savings_usd),
        ("Subscription turns priced at API rates", docs.baseline_usd - served_api),
        ("Cached tokens counted and priced at cache rates", final.savings_usd),
    ]
    return [{"step": s, "savings_usd": round(v, 2)} for s, v in steps]
