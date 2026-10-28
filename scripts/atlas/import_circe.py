"""Imports cohorts/*.circe.json into a live ATLAS WebAPI, reads them back and asks WebAPI to render SQL from them.

    python scripts/atlas/import_circe.py --webapi http://localhost:18080/WebAPI

Exit code 0 only when every file was created, round-trips (same concept sets / criteria counts) and WebAPI's Circe
renders non-empty SQL. Standard library only, so it runs wherever Python does. Needs a WebAPI >= 2.13 for the
``DateAdjustment`` criteria the shipped definitions use.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def call(base: str, method: str, path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()[:600]}")


def wait_ready(base: str, seconds: int) -> dict:
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            info = call(base, "GET", "/info")
            if isinstance(info, dict):
                return info
        except (SystemExit, OSError) as exc:
            last = str(exc)
        time.sleep(5)
    raise SystemExit(f"WebAPI at {base} not ready after {seconds}s ({last})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--webapi", default="http://localhost:18080/WebAPI")
    ap.add_argument("--cohorts", default=str(ROOT / "cohorts"))
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for WebAPI to come up")
    ap.add_argument("--cleanup", action="store_true", help="delete the cohort definitions this run created, after checking them")
    a = ap.parse_args()

    info = wait_ready(a.webapi, a.wait)
    print("WebAPI version:", info.get("version"))
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ok = True
    for f in sorted(Path(a.cohorts).glob("*.circe.json")):
        expr = json.loads(f.read_text(encoding="utf-8"))
        name = f"omop-duck-db {f.name.replace('.circe.json', '')} {stamp}"
        created = call(a.webapi, "POST", "/cohortdefinition",
                       {"name": name, "description": "imported by scripts/atlas/import_circe.py", "expressionType": "SIMPLE_EXPRESSION", "expression": expr})
        cid = created["id"]
        back = call(a.webapi, "GET", f"/cohortdefinition/{cid}")
        got = back["expression"] if isinstance(back["expression"], dict) else json.loads(back["expression"])
        same = (len(got.get("ConceptSets", [])) == len(expr.get("ConceptSets", []))
                and len(got.get("InclusionRules", [])) == len(expr.get("InclusionRules", [])))
        sql = call(a.webapi, "POST", "/cohortdefinition/sql", {"expression": expr, "options": {"generateStats": False}})
        text = sql["templateSql"] if isinstance(sql, dict) else str(sql)
        good = same and len(text) > 200 and "@target_cohort_id" in text
        ok &= good
        print(f"{'OK  ' if good else 'FAIL'} {f.name}: id={cid} round_trip={same} sql_chars={len(text)}")
        if a.cleanup:
            call(a.webapi, "DELETE", f"/cohortdefinition/{cid}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
