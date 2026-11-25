"""Synthetic routing-decision export for one example engineering org.

Every row uses the field names and meanings of the router's export schema
(data/schema.json, copied from weave-os/router internal/analytics/schema.go), and
every cost is computed from the real price book with the router's cost rules. The
people, traffic and failure rates are invented and labelled as such everywhere.

Two weeks are generated:

* ``week1``: first week with the router on for an opt-in group of 10 engineers
  (30 engineers stay on passthrough as the comparison group). Claude Code turns that
  the router serves on non-Anthropic models are translated (cross_format), and on
  tool-heavy turns that translation sometimes returns tool arguments that fail to
  parse. Two engineers hit it often.
* ``week2``: same people after the fix agreed with the account: tool-heavy Claude
  Code turns are kept on Anthropic-family models, so cross-format translation only
  happens on turns without tools.
"""

from __future__ import annotations

import hashlib
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from .pricing import PriceBook, prompt_tokens

ENGINEERS = 40
OPT_IN = [f"e{n:02d}" for n in (3, 5, 8, 11, 14, 19, 22, 27, 31, 36)]
CODEX_USERS = {f"e{n:02d}" for n in range(31, 41)}  # e31..e40 use Codex, the rest Claude Code
SUBSCRIPTION_USERS = {"e05", "e14", "e19"}  # Claude Max seats: most of their turns use included quota
TRANSLATION_HIT = {"e08": 0.12, "e22": 0.10}  # week 1: share of their cross-format tool turns that break

CLAUDE_BASELINE = "claude-opus-5-5"
CODEX_BASELINE = "gpt-5.6-sol"

# Served mix when the router is on (model, provider, weight). Invented for the demo.
CLAUDE_MIX = [
    ("claude-opus-5-5", "anthropic", 0.34),
    ("claude-sonnet-5-5", "anthropic", 0.38),
    ("claude-haiku-5-5", "anthropic", 0.10),
    ("gpt-5.6-terra", "openai", 0.10),
    ("gemini-3.5-flash", "google", 0.08),
]
CODEX_MIX = [
    ("gpt-5.6-sol", "openai", 0.40),
    ("gpt-5.6-terra", "openai", 0.42),
    ("gpt-5.6-luna", "openai", 0.18),
]
LATENCY_MS_PER_OUT_TOKEN = {  # rough decode speed, invented
    "claude-opus-5-5": 18,
    "claude-sonnet-5-5": 12,
    "claude-haiku-5-5": 6,
    "gpt-5.6-sol": 16,
    "gpt-5.6-terra": 11,
    "gpt-5.6-luna": 6,
    "gemini-3.5-flash": 7,
}
WEEK_START = {"week1": datetime(2026, 11, 2, tzinfo=UTC), "week2": datetime(2026, 11, 9, tzinfo=UTC)}


def _pick(rng: random.Random, mix):
    r, acc = rng.random(), 0.0
    for model, provider, w in mix:
        acc += w
        if r <= acc:
            return model, provider
    return mix[-1][0], mix[-1][1]


def _family(provider: str) -> str:
    return "anthropic" if provider.startswith("anthropic") else provider


def _row_id(*parts: Any) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20]


def generate(week: str, seed: int = 7, book: PriceBook | None = None) -> list[dict[str, Any]]:
    if week not in WEEK_START:
        raise ValueError(f"week must be one of {sorted(WEEK_START)}")
    book = book or PriceBook.load()
    rng = random.Random(f"{seed}-{week}")
    rows: list[dict[str, Any]] = []
    start = WEEK_START[week]
    for n in range(1, ENGINEERS + 1):
        eng = f"e{n:02d}"
        codex = eng in CODEX_USERS
        opt_in = eng in OPT_IN
        app = "codex" if codex else "claude_code"
        baseline = CODEX_BASELINE if codex else CLAUDE_BASELINE
        turns = rng.randint(110, 190)
        for t in range(turns):
            session = f"s-{eng}-{t // 12}"
            has_tools = rng.random() < 0.62
            if opt_in:
                model, provider = _pick(rng, CODEX_MIX if codex else CLAUDE_MIX)
                if week == "week2" and not codex and has_tools and _family(provider) != "anthropic":
                    model, provider = "claude-sonnet-5-5", "anthropic"
            else:
                model, provider = baseline, "openai" if codex else "anthropic"
            cross = (not codex) and _family(provider) != "anthropic"
            sub = (eng in SUBSCRIPTION_USERS) and provider == "anthropic" and rng.random() < 0.8
            requested_at = start + timedelta(minutes=rng.randint(0, 5 * 24 * 60))
            for kind in ("main_loop", "tool_result") if has_tools else ("main_loop",):
                cr = rng.randint(30_000, 140_000) if rng.random() < 0.85 else 0
                cw = rng.randint(800, 9_000)
                fresh = rng.randint(1_500, 9_000)
                out_tokens = rng.randint(150, 2_200)
                anth = _family(provider) == "anthropic"
                input_tokens = fresh if anth else fresh + cw + cr
                tool_blocks = rng.randint(1, 4) if has_tools else 0
                bad_tools = 0
                error_class = failure_owner = None
                if cross and tool_blocks and week == "week1":
                    p = TRANSLATION_HIT.get(eng, 0.006)
                    if rng.random() < p:
                        bad_tools, error_class, failure_owner = 1, "invalid_tool_args", "router"
                if error_class is None and rng.random() < 0.003:
                    error_class, failure_owner = "upstream_5xx", "provider"
                failover = error_class == "upstream_5xx" and rng.random() < 0.7
                prompt = prompt_tokens(input_tokens, cw, cr, provider)
                api_in = book.input_cost(model, provider, input_tokens, cw, cr, 0, None)
                api_out = book.output_cost(model, provider, prompt, out_tokens, None)
                decode = LATENCY_MS_PER_OUT_TOKEN[model] * out_tokens
                upstream = int(decode + rng.randint(600, 2_400) + (350 if cross else 0))
                route_ms = rng.randint(8, 40) if opt_in else 1
                rid = _row_id(week, eng, t, kind)
                rows.append(
                    {
                        "id": rid,
                        "recorded_at": (requested_at + timedelta(seconds=40)).isoformat().replace("+00:00", "Z"),
                        "requested_at": requested_at.isoformat().replace("+00:00", "Z"),
                        "request_id": f"req-{rid[:12]}",
                        "trace_id": f"tr-{rid}",
                        "session_id": session,
                        "client_app": app,
                        "turn_type": kind,
                        "user_id": eng,
                        "requested_model": baseline,
                        "decision_model": model,
                        "decision_provider": provider,
                        "blind_experiment_arm": "router_on" if opt_in else "passthrough",
                        "blind_experiment_mode": "cohort",
                        "blind_experiment_baseline_model": baseline,
                        "blind_experiment_model_changed": model != baseline,
                        "sticky_hit": rng.random() < 0.55,
                        "failover_used": failover,
                        "cross_format": cross,
                        "error_class": error_class,
                        "failure_owner": failure_owner,
                        "input_tokens": input_tokens,
                        "output_tokens": out_tokens,
                        "cache_creation_tokens": cw,
                        "cache_read_tokens": cr,
                        "subscription_served": sub,
                        "actual_input_cost_usd": 0.0 if sub else api_in,
                        "actual_output_cost_usd": 0.0 if sub else api_out,
                        "api_rate_input_cost_usd": api_in,
                        "api_rate_output_cost_usd": api_out,
                        "route_latency_ms": route_ms,
                        "upstream_latency_ms": upstream,
                        "total_latency_ms": upstream + route_ms,
                        "tool_use_blocks": tool_blocks,
                        "invalid_tool_args_blocks": bad_tools,
                    }
                )
    rows.sort(key=lambda r: (r["recorded_at"], r["id"]))
    return rows
