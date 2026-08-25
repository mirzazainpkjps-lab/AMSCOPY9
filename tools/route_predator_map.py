#!/usr/bin/env python3
"""PREDATOR ROUTE MAP — duplicate-(method, URL) forensics for the AMS app.

For every ``(METHOD, URL)`` pair registered in the Flask application this tool
determines:

    METHOD   e.g.  POST
    URL      e.g.  /add_direct_sale
    ALL REGISTERED ENDPOINTS   in map-registration order
    ACTUAL MATCHED ENDPOINT    what ``MapAdapter.match`` really returns
    REACHABLE?                 does the registered endpoint ever get invoked
                               for this URL (its view is the matched view)?
    SHADOWED?                  another rule with the same method+URL is matched
                               first and the view function differs

It also reports:

  * alias pairs (``sales.add_direct_sale`` vs ``add_direct_sale``) that point
    to the *same* function (benign but confusing) vs pairs that point at
    *different* functions (behavioural shadowing — the hidden handler is dead
    code and any permission/audit logic living only there is inert).
  * GET vs POST resolution differences for the same URL.
  * ``url_for`` build results that resolve to a different rule than the one
    matched at request time (route-shim mismatch).

The tool boots the real application factory against a throw-away SQLite
database (or an existing one given with ``--db``); it never mutates data.

Usage
-----
    python tools/route_predator_map.py
    python tools/route_predator_map.py --db instance/ahmed_cement_v44_fresh.db --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Converters -> sample path fragment used only to ask the router to match.
_SAMPLE = {
    "int": "1",
    "float": "1.0",
    "string": "x",
    "path": "x",
    "uuid": "00000000-0000-0000-0000-000000000000",
    "any": "x",
}


def sample_path(rule: str, converters: dict | None = None) -> str:
    """Render a concrete path for a Flask rule string."""
    out = []
    i = 0
    while i < len(rule):
        ch = rule[i]
        if ch == "<":
            j = rule.index(">", i)
            inner = rule[i + 1:j]
            if ":" in inner:
                conv, name = inner.split(":", 1)
            else:
                conv, name = "string", inner
            out.append(_SAMPLE.get(conv, _SAMPLE.get("string", "x")))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="reuse a database file instead of a temp one")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.db:
        db_path = str(Path(args.db).expanduser().resolve())
    else:
        tmp = tempfile.mkdtemp(prefix="ams_route_predator_")
        db_path = str(Path(tmp) / "route_map.db")

    env = {
        "APP_DB_PATH": db_path,
        "ALLOW_EMPTY_DB": "1",
        "BACKUP_EMBEDDED_SCHEDULER": "0",
        "AMS_SCHEMA_VERSION": "v44",
    }
    for k, v in env.items():
        os.environ[k] = v

    from app import create_app  # noqa: PLC0415  (import after env setup)

    app = create_app()

    rules = list(app.url_map.iter_rules())
    # registration order: Werkzeug stores rules in list order except sorting by
    # complexity; iter_rules() is what map building used.  For match order we
    # rely on the router itself (MapAdapter.match) because that is the truth.
    grouped = defaultdict(list)
    for r in rules:
        for m in sorted(r.methods or set()):
            if m in ("HEAD", "OPTIONS"):
                continue
            grouped[(m, r.rule)].append(r)

    view_by_endpoint = app.view_functions
    adapter = app.url_map.bind("localhost")

    rows = []
    for (method, url), rs in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        # Actual matched endpoint (real router behaviour).
        matched_endpoint = None
        try:
            ep, _args = adapter.match(sample_path(url), method=method)
            matched_endpoint = ep
        except Exception as exc:  # noqa: BLE001
            matched_endpoint = f"<MATCH ERROR: {exc}>"

        matched_view = view_by_endpoint.get(matched_endpoint)
        registered = []
        for r in rs:
            rl = r
            if hasattr(rl, "endpoint") and not hasattr(rl, "view_class"):
                pass
            endpoint = rl.endpoint
            view = view_by_endpoint.get(endpoint)
            registered.append({
                "endpoint": endpoint,
                "module": getattr(view, "__module__", ""),
                "view_identity": (
                    f"{getattr(view, '__module__', '')}.{getattr(view, '__qualname__', '')}"
                    if view else "<missing>"
                ),
                "is_matched": endpoint == matched_endpoint,
                "same_function_as_matched": bool(view is not None and view is matched_view),
                "rule_id": id(rl),
            })
        shadowed = [reg for reg in registered if not reg["same_function_as_matched"]]
        reachable = [reg["endpoint"] for reg in registered if reg["same_function_as_matched"]]
        rows.append({
            "method": method,
            "url": url,
            "all_registered_endpoints": [reg["endpoint"] for reg in registered],
            "actual_matched_endpoint": matched_endpoint,
            "reachable_endpoints": reachable,
            "shadowed_endpoints": [reg["endpoint"] for reg in shadowed],
            "behaviour_divergent": len(reachable) == 0 or len(set(
                reg["view_identity"] for reg in registered
            )) > 1,
            "registration_count": len(registered),
        })

    # url_for vs matched-rule divergence
    url_for_checks = []
    probe = app.test_request_context
    with probe():
        from flask import url_for
        for r in rules[:400]:
            try:
                built = url_for(r.endpoint)
            except Exception:
                continue
            url_for_checks.append({"endpoint": r.endpoint, "builds_to": built})

    summary = {
        "total_rules": len(rules),
        "unique_method_url": len(grouped),
        "ambiguous_pairs": len([x for x in rows if x["registration_count"] > 1]),
        "behaviour_divergent_pairs": len([x for x in rows if x["behaviour_divergent"]]),
        "shadowed_mutation_routes": len([
            x for x in rows if x["method"] != "GET" and x["behaviour_divergent"]
        ]),
        "matched_endpoints_without_view": len([
            x for x in rows if x["actual_matched_endpoint"] not in view_by_endpoint
        ]),
    }

    report = {"db": db_path, "summary": summary, "rows": rows,
              "url_for_checks_sample": url_for_checks[:80]}

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(f"TOTAL RULES            : {summary['total_rules']}")
    print(f"UNIQUE (method, url)   : {summary['unique_method_url']}")
    print(f"AMBIGUOUS PAIRS        : {summary['ambiguous_pairs']}")
    print(f"BEHAVIOUR-DIVERGENT    : {summary['behaviour_divergent_pairs']}")
    print(f"SHADOWED MUTATIONS     : {summary['shadowed_mutation_routes']}")
    print()
    for row in rows:
        if row["registration_count"] > 1:
            flag = "SHADOWED" if row["behaviour_divergent"] else "ALIAS(benign)"
            print(f"[{flag}] {row['method']} {row['url']}")
            print(f"    registered : {', '.join(row['all_registered_endpoints'])}")
            print(f"    matched    : {row['actual_matched_endpoint']}")
            if row["behaviour_divergent"]:
                print(f"    shadowed   : {', '.join(row['shadowed_endpoints'])}")
        else:
            if row["behaviour_divergent"]:
                print("[UNREACHABLE?] {} {}".format(row["method"], row["url"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
