import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from account_review import export, note, review, savings, shadow, synth
from account_review.pricing import PriceBook, prompt_tokens

HERE = Path(__file__).parent
BOOK = PriceBook.load()


# --- pricing matches the router's Go code -------------------------------------------------

def test_pricing_matches_router_go_vectors():
    vectors = json.loads((HERE / "cost_vectors.json").read_text())
    assert len(vectors) >= 100
    for v in vectors:
        geo = v["inference_geo"] or None
        got_in = BOOK.input_cost(v["model"], v["provider"], v["input_tokens"], v["cache_creation_tokens"],
                                 v["cache_read_tokens"], v["cache_creation_1h_tokens"], geo)
        prompt = prompt_tokens(v["input_tokens"], v["cache_creation_tokens"], v["cache_read_tokens"], v["provider"])
        got_out = BOOK.output_cost(v["model"], v["provider"], prompt, v["output_tokens"], geo)
        assert got_in == pytest.approx(v["input_cost_usd"], abs=1e-12), v
        assert got_out == pytest.approx(v["output_cost_usd"], abs=1e-12), v


def test_gateway_rows_use_anthropic_token_convention():
    # anthropic_gateway reports input_tokens without cached tokens, like anthropic.
    a = BOOK.input_cost("claude-opus-5-5", "anthropic", 1000, 0, 50000)
    g = BOOK.input_cost("claude-opus-5-5", "anthropic_gateway", 1000, 0, 50000)
    assert a == pytest.approx(g)


# --- savings --------------------------------------------------------------------------------

def _row(**kw):
    base = {"requested_model": "claude-opus-5-5", "decision_model": "claude-sonnet-5-5", "decision_provider": "anthropic",
                "input_tokens": 2000, "output_tokens": 500, "cache_creation_tokens": 0, "cache_read_tokens": 80000,
                "subscription_served": False}
    base.update(kw)
    i = BOOK.input_cost(base["decision_model"], base["decision_provider"], base["input_tokens"],
                        base["cache_creation_tokens"], base["cache_read_tokens"])
    p = prompt_tokens(base["input_tokens"], base["cache_creation_tokens"], base["cache_read_tokens"],
                      base["decision_provider"])
    o = BOOK.output_cost(base["decision_model"], base["decision_provider"], p, base["output_tokens"])
    sub = base["subscription_served"]
    base.update(api_rate_input_cost_usd=i, api_rate_output_cost_usd=o,
                actual_input_cost_usd=0.0 if sub else i, actual_output_cost_usd=0.0 if sub else o)
    return base


def test_unrouted_turn_saves_nothing():
    r = _row(decision_model="claude-opus-5-5")
    assert savings.savings([r], BOOK).savings_usd == pytest.approx(0, abs=1e-12)


def test_subscription_turn_is_not_free():
    paid, sub = _row(), _row(subscription_served=True)
    assert savings.savings([sub], BOOK).savings_usd == pytest.approx(savings.savings([paid], BOOK).savings_usd)
    # the docs query counts the whole baseline as saved on a subscription turn
    assert savings.docs_sql_savings([sub], BOOK).actual_usd == 0
    assert savings.docs_sql_savings([sub], BOOK).savings_usd > savings.docs_sql_savings([paid], BOOK).savings_usd


def test_cross_provider_turn_baseline_uses_cache_rates():
    # Served by OpenAI: input_tokens includes cached tokens. The docs query prices them all at
    # Opus's full input rate; the router's rules price cache reads at the cache rate.
    r = _row(decision_model="gpt-5.6-terra", decision_provider="openai", input_tokens=82000)
    docs = savings.docs_sql_savings([r], BOOK)
    fixed = savings.savings([r], BOOK)
    assert docs.baseline_usd > 3 * fixed.baseline_usd


def test_bridge_ends_on_corrected_savings():
    rows = [r for r in synth.generate("week1") if r["blind_experiment_arm"] == "router_on"]
    steps = savings.bridge(rows, BOOK)
    assert steps[0]["savings_usd"] == round(savings.docs_sql_savings(rows, BOOK).savings_usd, 2)
    assert steps[-1]["savings_usd"] == round(savings.savings(rows, BOOK).savings_usd, 2)


def test_missing_price_is_skipped_not_zero():
    r = _row()
    r["api_rate_input_cost_usd"] = None
    s = savings.savings([r], BOOK)
    assert s.rows_skipped == 1 and s.rows_priced == 0


# --- synthetic export + review ------------------------------------------------------------

@pytest.mark.parametrize("week", ["week1", "week2"])
def test_synthetic_rows_fit_the_export_schema(week):
    assert export.validate(synth.generate(week)) == []


def test_synth_is_deterministic():
    assert synth.generate("week1")[:50] == synth.generate("week1")[:50]


def test_week1_holds_on_translation_errors():
    r = review.review(synth.generate("week1"), BOOK)
    assert r["decision"] == "HOLD"
    failing = {e["user_id"] for e in r["engineers"] if e["checks"]["router_failures"] == "fail"}
    assert failing == {"e08", "e22"}


def test_week2_widens_after_fix():
    r = review.review(synth.generate("week2"), BOOK)
    assert r["decision"] == "WIDEN"
    assert r["opt_in"]["router_failures"] == 0
    assert r["opt_in"]["savings"]["savings_usd"] > 0


def test_docs_query_overstates_savings_on_this_account():
    r = review.review(synth.generate("week1"), BOOK)
    assert r["opt_in"]["docs_sql_savings"]["savings_usd"] > 2 * r["opt_in"]["savings"]["savings_usd"]


# --- export client ------------------------------------------------------------------------

def test_export_client_pages_dedupes_and_waits_out_429():
    calls, slept = [], []
    pages = [
        (429, {"retry-after": "2"}, b""),
        (200, {"x-weave-next-cursor": "c1", "x-weave-has-more": "true"}, b'{"id":"a","recorded_at":"1"}\n{"id":"b","recorded_at":"2"}\n'),
        (200, {"x-weave-next-cursor": "c2", "x-weave-has-more": "false"}, b'{"id":"b","recorded_at":"2"}\n{"id":"c","recorded_at":"3"}\n'),
    ]

    def fetch(url, headers):
        calls.append(url)
        return pages.pop(0)

    c = export.ExportClient("https://r.example", "ra_test", fetch=fetch, sleep=slept.append)
    rows = c.decisions("2026-11-02T00:00:00Z")
    assert [r["id"] for r in rows] == ["a", "b", "c"]
    assert slept == [2.0]
    assert "since=" in calls[1] and "cursor=c1" in calls[2] and "since=" not in calls[2]


def test_export_client_refuses_routing_key():
    with pytest.raises(ValueError):
        export.ExportClient("https://r.example", "rk_test")


# --- shadow -------------------------------------------------------------------------------

def test_shadow_posts_anthropic_body_and_summarizes():
    seen = []

    def post(url, headers, body):
        seen.append((url, json.loads(body)))
        req = json.loads(body)
        model = "claude-sonnet-5-5" if "tools" in req else "claude-haiku-5-5"
        return 200, json.dumps({"model": model, "provider": "anthropic", "reason": "prose"}).encode()

    out = shadow.run("https://r.example", "rk_test", post=post)
    assert seen[0][0] == "https://r.example/v1/route"
    assert out["requests"] == 24 and out["errors"] == 0
    assert out["by_kind"]["debug"] == {"claude-sonnet-5-5": 4}
    assert "reason" not in out["results"][0]


# --- note number check --------------------------------------------------------------------

def _review():
    return review.review(synth.generate("week1"), BOOK)


def test_number_check_passes_on_review_numbers():
    r = _review()
    d = note.note_input(r)
    s = d["savings"]
    text = (f"HOLD for now. The opt-in group saved ${s['savings_usd']} ({s['savings_pct']}%) this week. "
            "e08 and e22 failed router_failures; we will keep tool turns on Anthropic models.")
    assert note.number_check(text, d) == []


def test_number_check_blocks_invented_numbers_and_wrong_decision():
    d = note.note_input(_review())
    problems = note.number_check("WIDEN now: you saved $9,999 this week.", d)
    assert any("9,999" in p for p in problems)
    assert any("gate says HOLD" in p for p in problems)


def test_note_record_and_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(note, "RECORDINGS", tmp_path)
    r = _review()
    s = note.note_input(r)["savings"]
    text = f"HOLD. Savings were ${s['savings_usd']} ({s['savings_pct']}%). e08 and e22 hit tool errors."
    fake = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(
        model="claude-opus-5-5", content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1500, output_tokens=120))))
    rec = note.draft(r, mode="record", client=fake)
    assert rec["status"] == "ok" and rec["cost_usd"] > 0
    again = note.draft(r, mode="replay")
    assert again["text"] == text
