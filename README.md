# routing-account-review

[![ci](https://github.com/pg2342-hub/routing-account-review/actions/workflows/ci.yml/badge.svg)](https://github.com/pg2342-hub/routing-account-review/actions/workflows/ci.yml)

Demo page: https://pranavgudise.dev/routing-account-review/

A weekly account review for a model-routing rollout. It reads the routing-decision export of the open-source [Weave Router](https://github.com/weave-os/router), prices every turn against the model the engineer would have used without routing, checks each opt-in engineer's health, and ends with one call for the engineering lead: **WIDEN** routing to the whole team, or **HOLD**.

## What it does

1. **Shadow mode** (`shadow`). Before anyone is routed, it sends a sample of the account's requests to `POST /v1/route`, which returns the model the router would serve without calling a model.
2. **Pull** (`pull`). It reads `GET /v1/analytics/routing-decisions` with a read-only `ra_` key. It pages with the cursor until `X-Weave-Has-More` is false, waits out 429s using `Retry-After`, and de-duplicates on `id`.
3. **Price** (`pricing.py`). It mirrors the router's `catalog/cost.go`: cache writes and reads at their multipliers, 1-hour cache writes, long-context rates, US inference, and the provider token convention (Anthropic-family rows report `input_tokens` without cached tokens; other providers include them). Fast-mode turns (`speed = 'fast'`) are priced at standard rates, since the price book does not publish fast rates. `tests/cost_vectors.json` holds 108 costs computed by the router's own Go functions, and the tests require an exact match.
4. **Savings** (`savings.py`). It compares the same token work at the baseline model (`blind_experiment_baseline_model`, else `requested_model`) with the served turn at API rates, so subscription-served turns are not counted as free. It also reproduces the query in the router's `docs/ANALYTICS_EXPORT.md` ("Computing savings yourself") and shows the gap between the two, one fix at a time.
5. **Review and gate** (`review.py`). Each opt-in engineer is one [golive-gate](https://pypi.org/project/golive-gate/) case with five checks: router-owned failures (critical), unparseable tool arguments, failovers, p90 latency against the comparison group on the same client, and savings. To widen, 9 of 10 engineers must pass every check and none may fail the critical one.
6. **Note** (`note.py`). Claude drafts the weekly note to the engineering lead from the review data only. A number check blocks any figure (in digits) that is not in the review, and any note whose decision differs from the gate. Replies are recorded to `recordings/` by the manual `record` workflow and replayed in CI and on the demo page.

## Example run (synthetic data)

| | Week 1 | Week 2, after the fix |
|---|---|---|
| Decision | HOLD (e08 and e22 hit router-owned failures from tool arguments broken in cross-provider translation) | WIDEN |
| What changed | | Tool-heavy Claude Code turns kept on Anthropic-family models |

Run `python -m account_review.cli review exports/week1.ndjson` for the exact figures; the demo page is built from the same output.

## Run it

```bash
pip install -e ".[dev]"
python -m account_review.cli synth --week week1 --out exports/week1.ndjson
python -m account_review.cli review exports/week1.ndjson          # replays a recorded Claude note if one exists
pytest -q

# live
export WEAVE_ANALYTICS_KEY=ra_...   # read-only
python -m account_review.cli pull --since 2026-11-02T00:00:00Z --until 2026-11-09T00:00:00Z --out exports/live.ndjson
export WEAVE_ROUTER_KEY=rk_...
python -m account_review.cli shadow --record
pip install -e ".[claude]"; export ANTHROPIC_API_KEY=...
python -m account_review.cli review exports/live.ndjson --note record
```

The `record` workflow (manual) records the Claude notes and, if `WEAVE_ROUTER_KEY` is set, the shadow run, then rebuilds the demo page.

## Data

- `data/pricebook.json`: the router's price book (`analytics.PriceBook()`, the data `GET /v1/analytics/models` serves), dumped from weave-os/router at commit f9f86db.
- `data/schema.json`: the export's field dictionary from `internal/analytics/schema.go` at the same commit.
- `account_review/synth.py`: a made-up org of 40 engineers (10 opt-in, 30 comparison). People, traffic and failure rates are invented; field names, meanings and prices are the router's.
- `data/shadow_sample.json`: 24 made-up Claude Code-style requests. Replace with a sample of the account's own requests.

## Limits

- The baseline assumes the usual model would have used the same tokens. The router repo's `bench/token-efficiency` kit measures how far that assumption is off; this report does not.
- Prices are current prices, not those in force when old rows were recorded.

Weave Router is © its authors, Apache-2.0. This repo only reads its public API and copies two data files with attribution.
