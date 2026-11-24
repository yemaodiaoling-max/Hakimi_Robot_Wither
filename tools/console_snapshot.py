"""Read-only regression snapshot of the memory / relationship console.

    python tools/console_snapshot.py --port 18768 --token-file TOKEN --out snap.json
    python tools/console_snapshot.py --compare a.json b.json

Fetches every static page (as sha256) and every GET API with the parameter
combinations the front-end uses, so two console builds serving the same data
can be compared field by field.
"""
import argparse, hashlib, json, sys, urllib.parse, urllib.request
from pathlib import Path

STATIC = ["/", "/index.html", "/app.js", "/layout.js", "/styles.css", "/identity.html",
          "/identity-member-context.js", "/identity.js", "/identity.css",
          "/vertex-budget.js", "/vertex-budget.css"]
VOLATILE = {"started_at", "uptime_seconds", "now", "generated_at", "served_at", "checked_at", "age_seconds"}


def fetch(port, path, token=None, params=None):
    url = f"http://127.0.0.1:{port}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in VOLATILE}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def snapshot(port, token):
    out = {}
    def api(path, **params):
        status, body = fetch(port, path, token, params)
        try:
            data = scrub(json.loads(body))
        except ValueError:
            data = body.decode("utf-8", "replace")[:200]
        out[path + ("?" + urllib.parse.urlencode(sorted(params.items())) if params else "")] = [status, data]
        return data.get("data") if isinstance(data, dict) else None
    for path in STATIC:
        status, body = fetch(port, path)
        out["static " + path] = [status, hashlib.sha256(body).hexdigest()]
    api("/health")
    api("/api/status")
    groups = api("/api/groups") or {}
    group_ids = [""] + [str(g.get("id") or g.get("session") or g.get("key")) for g in (groups.get("groups") or [])][:4]
    for g in group_ids:
        people = api("/api/people", q="", group=g, page=1, page_size=30) or {}
        graph = None
        for measure in ("affinity", "closeness", "interaction"):
            for source in ("assisted", "terra", "manual"):
                metric = "interaction" if measure == "interaction" else source
                data = api("/api/graph", group=g, limit=35, threshold=0, metric=metric, measure=measure, source=source)
                graph = graph or data
        api("/api/graph", group=g, limit=35, threshold=0, metric="assisted", measure="affinity", source="assisted", focus="hakimi")
        ids = [str(p.get("id")) for p in (people.get("people") or people.get("items") or [])][:5]
        for pid in ids:
            api("/api/person", id=pid, group=g)
            api("/api/graph", group=g, limit=35, threshold=0, metric="assisted", measure="affinity", source="assisted", focus=pid.replace("person:", ""))
        for edge in ((graph or {}).get("edges") or [])[:5]:
            a, b = (str(edge.get(k, "")).split(":", 1)[-1] for k in ("source", "target"))
            if a != "hakimi" and b != "hakimi":
                api("/api/relation", a=a, b=b, group=g)
        mem = api("/api/memories", group=g, q="", offset=0, limit=20) or {}
        for m in (mem.get("memories") or mem.get("items") or [])[:3]:
            api("/api/memory", id=m.get("memory_id"), offset=0, limit=2000)
            api("/api/identity/memory", id=m.get("memory_id"), group=g, limit=6000)
    catalog = api("/api/identity/catalog") or {}
    api("/api/identity/links")
    for side in ("old", "new"):
        for group in (catalog.get(side + "_groups") or [])[:3]:
            api("/api/identity/members", side=side, group=group.get("id"), q="")
    api("/api/vertex-budget")
    return out


def compare(a, b):
    A, B = json.loads(Path(a).read_text()), json.loads(Path(b).read_text())
    bad = [k for k in sorted(set(A) | set(B)) if A.get(k) != B.get(k)]
    for k in bad:
        print("DIFF", k)
    print(f"{len(A)} vs {len(B)} entries, {len(bad)} differ")
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18768)
    p.add_argument("--token-file")
    p.add_argument("--out")
    p.add_argument("--compare", nargs=2)
    a = p.parse_args()
    if a.compare:
        sys.exit(compare(*a.compare))
    token = Path(a.token_file).read_text().strip()
    snap = snapshot(a.port, token)
    Path(a.out).write_text(json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True))
    errors = sum(1 for v in snap.values() if v[0] >= 500)
    print(f"{len(snap)} entries, {errors} server errors -> {a.out}")
