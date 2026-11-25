"""Weekly account review: per-engineer health for the opt-in group, then a gate.

Each opt-in engineer is one golive-gate case. The checks below are the account's
agreed thresholds for widening the rollout from the opt-in group to the whole team.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from statistics import quantiles
from typing import Any

from golive_gate import Case, CheckSet, Gate, evaluate, run_eval
from golive_gate.gates import GO

from .pricing import PriceBook
from .savings import docs_sql_savings, savings

Row = dict[str, Any]

THRESHOLDS = {
    "router_failure_pct": 0.5,  # router-owned failures, % of actions (critical)
    "bad_tool_args_pct": 0.5,  # tool-use blocks whose arguments failed to parse, % of blocks
    "failover_pct": 2.0,  # actions that needed a second upstream, %
    "p90_latency_ratio": 1.2,  # engineer's p90 action latency / comparison group's p90, same client
}


def p90(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return quantiles(values, n=10, method="inclusive")[-1]


def _pct(n: int, d: int) -> float:
    return 100 * n / d if d else 0.0


@dataclass
class EngineerStats:
    user_id: str
    client_app: str
    arm: str
    actions: int
    main_loop_turns: int
    router_failures: int
    tool_blocks: int
    bad_tool_blocks: int
    failovers: int
    cross_format: int
    p90_latency_ms: float
    savings: dict[str, Any]

    @property
    def router_failure_pct(self) -> float:
        return _pct(self.router_failures, self.actions)

    @property
    def bad_tool_args_pct(self) -> float:
        return _pct(self.bad_tool_blocks, self.tool_blocks)

    @property
    def failover_pct(self) -> float:
        return _pct(self.failovers, self.actions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "client_app": self.client_app,
            "arm": self.arm,
            "actions": self.actions,
            "main_loop_turns": self.main_loop_turns,
            "router_failure_pct": round(self.router_failure_pct, 2),
            "bad_tool_args_pct": round(self.bad_tool_args_pct, 2),
            "failover_pct": round(self.failover_pct, 2),
            "cross_format_pct": round(_pct(self.cross_format, self.actions), 1),
            "p90_latency_ms": round(self.p90_latency_ms),
            "savings": self.savings,
        }


def engineer_stats(rows: Iterable[Row], book: PriceBook) -> dict[str, EngineerStats]:
    by_user: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_user[r.get("user_id") or "unknown"].append(r)
    out = {}
    for uid, rs in sorted(by_user.items()):
        out[uid] = EngineerStats(
            user_id=uid,
            client_app=rs[0].get("client_app") or "unknown",
            arm=rs[0].get("blind_experiment_arm") or "unknown",
            actions=len(rs),
            main_loop_turns=sum(r.get("turn_type") == "main_loop" for r in rs),
            router_failures=sum(r.get("failure_owner") == "router" for r in rs),
            tool_blocks=sum(int(r.get("tool_use_blocks") or 0) for r in rs),
            bad_tool_blocks=sum(int(r.get("invalid_tool_args_blocks") or 0) for r in rs),
            failovers=sum(bool(r.get("failover_used")) for r in rs),
            cross_format=sum(bool(r.get("cross_format")) for r in rs),
            p90_latency_ms=p90([float(r["total_latency_ms"]) for r in rs if r.get("total_latency_ms") is not None]),
            savings=savings(rs, book).to_dict(),
        )
    return out


def comparison_p90(rows: Iterable[Row]) -> dict[str, float]:
    by_app: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("blind_experiment_arm") == "passthrough" and r.get("total_latency_ms") is not None:
            by_app[r.get("client_app") or "unknown"].append(float(r["total_latency_ms"]))
    return {app: p90(v) for app, v in by_app.items()}


def build_checks(control_p90: dict[str, float]) -> CheckSet:
    checks = CheckSet()

    @checks.check("router_failures", f"Router-owned failures at or under {THRESHOLDS['router_failure_pct']}% of actions", critical=True)
    def _router(case, s: EngineerStats):
        return s.router_failure_pct <= THRESHOLDS["router_failure_pct"]

    @checks.check("tool_args", f"Unparseable tool arguments at or under {THRESHOLDS['bad_tool_args_pct']}% of tool blocks")
    def _tools(case, s: EngineerStats):
        return s.bad_tool_args_pct <= THRESHOLDS["bad_tool_args_pct"]

    @checks.check("failover", f"Failovers at or under {THRESHOLDS['failover_pct']}% of actions")
    def _failover(case, s: EngineerStats):
        return s.failover_pct <= THRESHOLDS["failover_pct"]

    @checks.check("latency", f"p90 action latency within {THRESHOLDS['p90_latency_ratio']}x the comparison group")
    def _latency(case, s: EngineerStats):
        ref = control_p90.get(s.client_app)
        if not ref:
            return None
        return s.p90_latency_ms <= THRESHOLDS["p90_latency_ratio"] * ref

    @checks.check("saves_money", "Served cost below the baseline cost at API rates")
    def _saves(case, s: EngineerStats):
        return s.savings["savings_usd"] > 0

    return checks


GATE = Gate.from_dict(
    {
        "name": "widen routing from the opt-in group to the whole team",
        "max_critical_failures": 0,
        "rules": [{"name": "opt-in engineers passing every check", "min_pass": 9}],
    }
)


def review(rows: list[Row], book: PriceBook | None = None) -> dict[str, Any]:
    book = book or PriceBook.load()
    stats = engineer_stats(rows, book)
    opt_in = [s for s in stats.values() if s.arm == "router_on"]
    control = comparison_p90(rows)
    checks = build_checks(control)
    cases = [Case(id=s.user_id, input=s.user_id, group=s.client_app) for s in opt_in]
    result = run_eval(lambda uid: stats[uid], cases, checks, runs=1)
    gate = evaluate(GATE, result)
    opt_rows = [r for r in rows if r.get("blind_experiment_arm") == "router_on"]
    per_case = {cr.case_id: {cid: v.result for cid, v in cr.verdicts.items()} for cr in result.case_runs}
    main_turns = sum(r.get("turn_type") == "main_loop" for r in opt_rows)
    return {
        "decision": "WIDEN" if gate.decision == GO else "HOLD",
        "gate": gate.to_dict(),
        "thresholds": THRESHOLDS,
        "checks": [{"id": c.id, "text": c.text, "critical": c.critical} for c in checks],
        "comparison_p90_ms": {k: round(v) for k, v in control.items()},
        "opt_in": {
            "engineers": len(opt_in),
            "actions": len(opt_rows),
            "main_loop_turns": main_turns,
            "savings": savings(opt_rows, book).to_dict(),
            "docs_sql_savings": docs_sql_savings(opt_rows, book).to_dict(),
            "router_failures": sum(r.get("failure_owner") == "router" for r in opt_rows),
            "cross_format_actions": sum(bool(r.get("cross_format")) for r in opt_rows),
        },
        "engineers": [{**s.to_dict(), "checks": per_case.get(s.user_id)} for s in stats.values() if s.arm == "router_on"],
        "comparison_group": {
            "engineers": sum(s.arm == "passthrough" for s in stats.values()),
            "actions": sum(r.get("blind_experiment_arm") == "passthrough" for r in rows),
        },
    }
