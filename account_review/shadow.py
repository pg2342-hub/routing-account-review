"""Shadow mode: ask the router what it would serve, without serving it.

POST /v1/route takes an Anthropic Messages body and returns {model, provider,
reason} with no upstream call and no model spend (weave-os/router
internal/api/anthropic/route.go). Sending a sample of an account's real requests
through it before go-live shows which models would serve which kinds of turns.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROUTE = "/v1/route"
SAMPLE = Path(__file__).resolve().parent.parent / "data" / "shadow_sample.json"
RECORDING = Path(__file__).resolve().parent.parent / "recordings" / "shadow.json"

Post = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


def _urllib_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def route(base_url: str, key: str, request: dict[str, Any], post: Post = _urllib_post) -> dict[str, Any]:
    if not key.startswith("rk_"):
        raise ValueError("/v1/route takes a router key (rk_...)")
    status, body = post(
        base_url.rstrip("/") + ROUTE,
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json", "anthropic-version": "2023-06-01"},
        json.dumps(request).encode(),
    )
    if status != 200:
        return {"error": status, "detail": body[:300].decode("utf-8", "replace")}
    out = json.loads(body)
    return {"model": out.get("model"), "provider": out.get("provider")}  # reason is prose; not kept


def run(base_url: str, key: str, sample_path: Path = SAMPLE, post: Post = _urllib_post) -> dict[str, Any]:
    sample = json.loads(sample_path.read_text())
    results = []
    for item in sample["requests"]:
        results.append({"id": item["id"], "kind": item["kind"], **route(base_url, key, item["request"], post)})
    return summarize(results)


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, Counter] = {}
    for r in results:
        by_kind.setdefault(r["kind"], Counter())[r.get("model") or f"error {r.get('error')}"] += 1
    return {
        "requests": len(results),
        "errors": sum("error" in r for r in results),
        "by_kind": {k: dict(c.most_common()) for k, c in sorted(by_kind.items())},
        "results": results,
    }


def load_recording() -> dict[str, Any] | None:
    return json.loads(RECORDING.read_text()) if RECORDING.exists() else None
