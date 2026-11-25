"""The weekly note to the account's engineering lead, drafted by Claude.

Claude writes from the review JSON only. Before the note can go out, a number check
requires every figure written in digits to appear in the review (allowing the usual
rounding; spelled-out numbers are not checked),
and the decision word in the note to match the gate. Calls are recorded to
recordings/ so CI and the demo replay them without a key.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

MODEL = "claude-opus-5-5"
PRICE_PER_1M = (4.0, 20.0)  # input, output USD for MODEL (data/pricebook.json)
RECORDINGS = Path(__file__).resolve().parent.parent / "recordings"

PROMPT = """You are the forward deployed engineer on a model-routing account. Write this week's
note to the customer's engineering lead about the opt-in routing group.

Rules:
- 120 words or fewer, plain prose, no headings, no bullet points.
- Open with the decision: WIDEN (turn routing on for the whole team) or HOLD (keep the
  opt-in group, fix first). Use the decision from the data exactly.
- Give savings in dollars and percent from "savings" (the corrected figure). Never quote
  docs_sql_savings as savings.
- If any engineer failed a check, say who (by user id), which check, and the next step.
- Use only numbers that appear in the data. Do not compute new ones.

Data:
{data}
"""

NUM = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?%?")


def _numbers_in(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _numbers_in(v, out)
    elif isinstance(obj, str):
        for m in re.findall(r"\d+(?:\.\d+)?", obj):
            out.add(float(m))


def note_input(review: dict[str, Any]) -> dict[str, Any]:
    failing = [
        {
            "user_id": e["user_id"],
            "client_app": e["client_app"],
            "failed": [k for k, v in (e["checks"] or {}).items() if v == "fail"],
            "router_failure_pct": e["router_failure_pct"],
            "bad_tool_args_pct": e["bad_tool_args_pct"],
        }
        for e in review["engineers"]
        if any(v == "fail" for v in (e["checks"] or {}).values())
    ]
    return {
        "decision": review["decision"],
        "opt_in_engineers": review["opt_in"]["engineers"],
        "main_loop_turns": review["opt_in"]["main_loop_turns"],
        "savings": review["opt_in"]["savings"],
        "docs_sql_savings": review["opt_in"]["docs_sql_savings"],
        "thresholds": review["thresholds"],
        "engineers_failing_checks": failing,
        "gate_reasons": review["gate"]["reasons"],
    }


DECISION = re.compile(r"\b(WIDEN|HOLD)\b")


def number_check(note: str, data: dict[str, Any]) -> list[str]:
    allowed: set[float] = set()
    _numbers_in(data, allowed)
    problems = []
    for raw in NUM.findall(note):
        value = float(raw.strip("$%").replace(",", ""))
        if not any(abs(value - a) <= max(0.051, 0.0051 * abs(a)) or round(a) == value for a in allowed):
            problems.append(f"{raw} is not in the review data")
    # The note opens with its decision. A later mention of the other word ("rerun the
    # gate and come back recommending WIDEN") is a plan, not the call, so only the
    # first decision word is held to the gate.
    stated = DECISION.search(note)
    if not stated:
        problems.append(f"note does not state the decision {data['decision']}")
    elif stated.group(0) != data["decision"]:
        problems.append(f"note says {stated.group(0)} but the gate says {data['decision']}")
    return problems


def _key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


def draft(review: dict[str, Any], mode: str = "replay", client: Any = None) -> dict[str, Any]:
    """mode: replay (recordings only), record (call Claude, save), or live (call, don't save)."""
    data = note_input(review)
    prompt = PROMPT.format(data=json.dumps(data, indent=1, sort_keys=True))
    path = RECORDINGS / f"note-{_key(prompt)}.json"
    if mode == "replay":
        if not path.exists():
            return {"status": "not_recorded", "prompt_key": _key(prompt)}
        rec = json.loads(path.read_text())
    else:
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        t0 = time.perf_counter()
        resp = client.messages.create(model=MODEL, max_tokens=1200, messages=[{"role": "user", "content": prompt}])
        latency = time.perf_counter() - t0
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        usage = resp.usage
        rec = {
            "model": getattr(resp, "model", MODEL),
            "text": text,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": round((usage.input_tokens * PRICE_PER_1M[0] + usage.output_tokens * PRICE_PER_1M[1]) / 1e6, 5),
            "latency_s": round(latency, 2),
        }
        if mode == "record":
            RECORDINGS.mkdir(exist_ok=True)
            path.write_text(json.dumps(rec, indent=1))
    problems = number_check(rec["text"], data)
    return {"status": "ok" if not problems else "blocked", "problems": problems, **rec}
