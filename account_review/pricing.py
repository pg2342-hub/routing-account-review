"""Price a routed turn the way the router prices it.

Mirrors weave-os/router internal/router/catalog/cost.go (EffectiveInputCost,
EffectiveOutputCost, PromptTokens) on the price book that GET /v1/analytics/models
publishes. tests/cost_vectors.json holds outputs of the Go functions themselves, and
the tests require an exact match.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Providers whose upstream reports input_tokens WITHOUT cached tokens
# (TranslationFamily Anthropic in internal/providers/provider.go). Every other
# provider reports a cache-inclusive prompt count.
ANTHROPIC_FAMILY = frozenset({"anthropic", "anthropic_gateway", "wafer_anthropic"})

DEFAULT_PRICEBOOK = Path(__file__).resolve().parent.parent / "data" / "pricebook.json"


@dataclass(frozen=True)
class Price:
    input_per_1m: float
    output_per_1m: float
    cache_write: float
    cache_write_1h: float
    cache_read: float
    us_geo: float
    long_threshold: int | None = None
    long: Price | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Price:
        long_raw = raw.get("long_context")
        long = None
        if long_raw:
            long = cls(
                input_per_1m=long_raw["input_usd_per_1m"],
                output_per_1m=long_raw["output_usd_per_1m"],
                cache_write=long_raw["cache_write_multiplier"],
                cache_write_1h=long_raw["cache_write_1h_multiplier"],
                cache_read=long_raw["cache_read_multiplier"],
                us_geo=raw["us_inference_geo_multiplier"],
            )
        return cls(
            input_per_1m=raw["input_usd_per_1m"],
            output_per_1m=raw["output_usd_per_1m"],
            cache_write=raw["cache_write_multiplier"],
            cache_write_1h=raw["cache_write_1h_multiplier"],
            cache_read=raw["cache_read_multiplier"],
            us_geo=raw["us_inference_geo_multiplier"],
            long_threshold=long_raw["threshold_tokens"] if long_raw else None,
            long=long,
        )

    def for_prompt(self, prompt_tokens: int) -> Price:
        if self.long is None or self.long_threshold is None or prompt_tokens <= self.long_threshold:
            return self
        return self.long


def prompt_tokens(input_tokens: int, cache_creation: int, cache_read: int, provider: str) -> int:
    if provider not in ANTHROPIC_FAMILY:
        return input_tokens
    return input_tokens + max(cache_creation, 0) + max(cache_read, 0)


def fresh_input_tokens(input_tokens: int, cache_creation: int, cache_read: int, provider: str) -> int:
    """Input tokens billed at the base rate (net of cache writes and reads)."""
    fresh = input_tokens if provider in ANTHROPIC_FAMILY else input_tokens - cache_creation - cache_read
    return max(fresh, 0)


class PriceBook:
    def __init__(self, models: list[dict[str, Any]]):
        self._prices: dict[tuple[str, str], Price] = {}
        self._default_provider: dict[str, str] = {}
        for m in models:
            for i, p in enumerate(m["providers"]):
                self._prices[(m["id"], p["provider"])] = Price.from_dict(p)
                if i == 0:
                    self._default_provider[m["id"]] = p["provider"]

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PRICEBOOK) -> PriceBook:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(raw["models"] if isinstance(raw, dict) else raw)

    def get(self, model: str, provider: str | None = None) -> tuple[Price, str] | None:
        provider = provider or self._default_provider.get(model)
        if provider is None:
            return None
        price = self._prices.get((model, provider))
        return (price, provider) if price else None

    def default_provider(self, model: str) -> str | None:
        return self._default_provider.get(model)

    def input_cost(
        self,
        model: str,
        provider: str,
        input_tokens: int,
        cache_creation: int,
        cache_read: int,
        cache_creation_1h: int = 0,
        geo: str | None = None,
    ) -> float | None:
        found = self.get(model, provider)
        if found is None:
            return None
        base, provider = found
        p = base.for_prompt(prompt_tokens(input_tokens, cache_creation, cache_read, provider))
        fresh = fresh_input_tokens(input_tokens, cache_creation, cache_read, provider)
        one_hour = min(max(cache_creation_1h, 0), max(cache_creation, 0))
        five_min = cache_creation - one_hour
        geo_mult = p.us_geo if geo == "us" else 1.0
        return (
            (fresh + five_min * p.cache_write + one_hour * p.cache_write_1h + cache_read * p.cache_read)
            / 1_000_000
            * p.input_per_1m
            * geo_mult
        )

    def output_cost(self, model: str, provider: str, prompt: int, output_tokens: int, geo: str | None = None) -> float | None:
        found = self.get(model, provider)
        if found is None:
            return None
        p = found[0].for_prompt(prompt)
        geo_mult = p.us_geo if geo == "us" else 1.0
        return output_tokens / 1_000_000 * p.output_per_1m * geo_mult
