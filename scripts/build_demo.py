"""Build docs/index.html from the review's real output.

    python scripts/build_demo.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from account_review import note, review, savings, shadow, synth  # noqa: E402
from account_review.pricing import PriceBook  # noqa: E402

book = PriceBook.load()
weeks = {}
for week in ("week1", "week2"):
    rows = synth.generate(week)
    r = review.review(rows, book)
    opt = [x for x in rows if x.get("blind_experiment_arm") == "router_on"]
    r["bridge"] = savings.bridge(opt, book)
    n = note.draft(r, mode="replay")
    r["note"] = {k: n.get(k) for k in ("status", "text", "problems", "cost_usd", "latency_s", "model",
                                       "input_tokens", "output_tokens")}
    r["rows"] = len(rows)
    weeks[week] = r

vectors = json.loads((ROOT / "tests" / "cost_vectors.json").read_text())
payload = {
    "weeks": weeks,
    "vectors": len(vectors),
    "pricebook_models": len(json.loads((ROOT / "data" / "pricebook.json").read_text())["models"]),
    "schema_fields": len(json.loads((ROOT / "data" / "schema.json").read_text())["fields"]),
    "shadow": shadow.load_recording(),
}
template = (ROOT / "docs" / "template.html").read_text()
out = template.replace("/*DATA*/null", json.dumps(payload, separators=(",", ":")))
(ROOT / "docs" / "index.html").write_text(out)
subprocess.run([sys.executable, str(ROOT / "scripts" / "demo_chrome.py"), str(ROOT / "docs" / "index.html"),
                "--repo", "routing-account-review"], check=True)
print("wrote docs/index.html")
