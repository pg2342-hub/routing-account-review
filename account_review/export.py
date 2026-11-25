"""Pull the router's routing-decision export (docs/ANALYTICS_EXPORT.md in weave-os/router).

Uses an ``ra_`` analytics key: read-only, it cannot route or spend. Pages with the
opaque cursor until ``X-Weave-Has-More`` is false, waits out 429s using Retry-After,
and de-duplicates on ``id`` (rows are immutable, so a replayed page is a no-op).
"""

from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

DECISIONS = "/v1/analytics/routing-decisions"
MODELS = "/v1/analytics/models"
SCHEMA = "/v1/analytics/schema"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "data" / "schema.json"

Fetch = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


def _urllib_fetch(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


class ExportClient:
    def __init__(self, base_url: str, key: str, fetch: Fetch = _urllib_fetch, sleep=time.sleep, max_pages: int = 500):
        if not key.startswith("ra_"):
            raise ValueError("the export takes a read-only analytics key (ra_...); routing keys (rk_) get a 401")
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {key}", "Accept-Encoding": "gzip"}
        self.fetch = fetch
        self.sleep = sleep
        self.max_pages = max_pages

    def _get(self, path: str, query: dict[str, str] | None = None) -> tuple[dict[str, str], bytes]:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        for _ in range(6):
            status, headers, body = self.fetch(url, self.headers)
            if status == 429:
                self.sleep(float(headers.get("retry-after", "5")))
                continue
            if status != 200:
                raise RuntimeError(f"GET {path} returned {status}: {body[:200]!r}")
            return headers, body
        raise RuntimeError(f"GET {path} still rate limited after 6 tries")

    def decisions(self, since: str, until: str | None = None, limit: int = 10000) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        cursor = ""
        for _ in range(self.max_pages):
            query = {"limit": str(limit), "format": "ndjson"}
            if cursor:
                query["cursor"] = cursor
            else:
                query["since"] = since
                if until:
                    query["until"] = until
            headers, body = self._get(DECISIONS, query)
            for row in parse_ndjson(body.decode("utf-8").splitlines()):
                rows[row["id"]] = row
            cursor = headers.get("x-weave-next-cursor", "")
            if headers.get("x-weave-has-more", "false").lower() != "true" or not cursor:
                break
        return sorted(rows.values(), key=lambda r: (r.get("recorded_at", ""), r["id"]))

    def price_book(self) -> dict[str, Any]:
        return json.loads(self._get(MODELS)[1])

    def schema(self) -> dict[str, Any]:
        return json.loads(self._get(SCHEMA)[1])


def parse_ndjson(lines: Iterable[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines if line.strip()]


def write_ndjson(rows: Iterable[dict[str, Any]], path: str | Path) -> None:
    Path(path).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def read_ndjson(path: str | Path) -> list[dict[str, Any]]:
    return parse_ndjson(Path(path).read_text(encoding="utf-8").splitlines())


def schema_fields() -> dict[str, dict[str, Any]]:
    return {f["name"]: f for f in json.loads(SCHEMA_FILE.read_text())["fields"]}


def validate(rows: Iterable[dict[str, Any]]) -> list[str]:
    """Problems with rows against the export schema: unknown columns, nulls in non-null columns."""
    fields = schema_fields()
    problems = []
    for row in rows:
        for k in row:
            if k not in fields:
                problems.append(f"{row.get('id')}: unknown column {k}")
        for name, f in fields.items():
            if not f["nullable"] and row.get(name) is None:
                problems.append(f"{row.get('id')}: {name} is required")
    return problems
