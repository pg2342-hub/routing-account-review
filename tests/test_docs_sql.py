"""The corrected savings query (data/corrected_savings.sql) gives the same number as savings.py."""

import json
import sqlite3
from pathlib import Path

import pytest

from account_review import savings, synth
from account_review.pricing import PriceBook

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("week", ["week1", "week2"])
def test_corrected_query_matches_python(week):
    rows = [r for r in synth.generate(week) if r["blind_experiment_arm"] == "router_on"]
    cols = sorted({k for r in rows for k in r})
    db = sqlite3.connect(":memory:")
    db.execute(f"create table routing_decisions ({','.join(cols)})")
    db.executemany(f"insert into routing_decisions values ({','.join('?' * len(cols))})",
                   [[int(r[c]) if isinstance(r.get(c), bool) else r.get(c) for c in cols] for r in rows])
    db.execute("create table model_prices (model, provider, input_usd_per_1m, output_usd_per_1m, "
               "cache_write_multiplier, cache_read_multiplier)")
    for m in json.loads((ROOT / "data" / "pricebook.json").read_text())["models"]:
        for p in m["providers"]:
            db.execute("insert into model_prices values (?,?,?,?,?,?)", (m["id"], p["provider"], p["input_usd_per_1m"],
                       p["output_usd_per_1m"], p["cache_write_multiplier"], p["cache_read_multiplier"]))
    db.execute("create table baseline_bindings (model, provider)")
    db.executemany("insert into baseline_bindings values (?,?)",
                   [("claude-opus-5-5", "anthropic"), ("gpt-5.6-sol", "openai")])
    q = (ROOT / "data" / "corrected_savings.sql").read_text()
    q = q.replace("GREATEST(", "MAX(").replace("d.recorded_at >= now() - interval '30 days'", "1=1")  # sqlite dialect
    got = db.execute(q).fetchone()[0]
    assert got == pytest.approx(savings.savings(rows, PriceBook.load()).savings_usd, abs=1e-9)
