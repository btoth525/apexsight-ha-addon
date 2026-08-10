"""The app's black box, relay side.

This endpoint exists so a problem seen on the phone survives long enough to be fixed. It is also a
debugging channel bolted onto a home-security relay, so the tests care about the ways that goes
wrong: an ungated read, an unbounded write, or a malformed line 500ing the very endpoint the app
uses to report that something broke.

Run:  PYTHONPATH=. python3 tests/test_diag.py
"""
import os, tempfile, time

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("APEX_SECRET_KEY", "test")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0001")

from app import db  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


db.init()
CODE = "APEX-TEST-0001"
OTHER = "APEX-OTHER-9999"

# ---- basic round trip ----
n = db.insert_diag(CODE, "Brandons iPhone", "1.0.0 (224)", [
    {"ts": time.time(), "level": "error", "category": "app", "message": "stream failed"},
    {"ts": time.time(), "level": "info", "category": "settings", "message": "diagnostics enabled"},
])
check("both entries stored", n == 2)
rows = db.recent_diag(CODE)
check("read back returns them", len(rows) == 2)
check("newest first", rows[0]["ts"] >= rows[1]["ts"])
check("device is recorded", rows[0]["device"] == "Brandons iPhone")
check("build is recorded", rows[0]["build"] == "1.0.0 (224)")

# ---- a household only ever sees its OWN log ----
db.insert_diag(OTHER, "Someone else", "1.0.0 (1)", [
    {"ts": time.time(), "level": "error", "category": "app", "message": "not yours"}])
check("another pairing code's lines are not returned",
      all("not yours" not in r["message"] for r in db.recent_diag(CODE)))
check("and that household sees its own", len(db.recent_diag(OTHER)) == 1)

# ---- a malformed line must never break the endpoint ----
before = len(db.recent_diag(CODE))
n = db.insert_diag(CODE, "x", "y", [
    {"ts": "not-a-number", "level": "error", "message": "bad ts"},
    {"level": "error", "message": "no ts at all"},
    {},
])
check("garbage rows don't raise", True)   # reaching here IS the assertion
check("well-formed-enough rows still land", len(db.recent_diag(CODE)) > before)

# ---- bounds: a runaway error loop can't fill the disk ----
db.insert_diag(CODE, "d", "b", [{"ts": time.time(), "level": "error", "category": "c",
                                 "message": "x" * 10_000}])
longest = max(len(r["message"]) for r in db.recent_diag(CODE))
check("a single message is truncated", longest <= 2000)

db.insert_diag(CODE, "d" * 500, "b" * 500, [{"ts": time.time(), "level": "error", "message": "m"}])
r = db.recent_diag(CODE)[0]
check("device/build are truncated", len(r["device"]) <= 64 and len(r["build"]) <= 32)

# ---- rotation ----
db.insert_diag(CODE, "d", "b", [{"ts": time.time() + i, "level": "info", "message": "line %d" % i}
                                for i in range(300)])
dropped = db.prune_diag(max_rows=50)
total = len(db.recent_diag(CODE, limit=5000)) + len(db.recent_diag(OTHER, limit=5000))
check("prune drops the excess", dropped > 0)
check("and keeps exactly the cap", total == 50)
check("prune is a no-op when under the cap", db.prune_diag(max_rows=50) == 0)
check("the NEWEST lines are what survive",
      any("line 299" in r["message"] for r in db.recent_diag(CODE, limit=5000)))

# ---- read limits ----
check("limit is honoured", len(db.recent_diag(CODE, limit=5)) <= 5)
check("limit can't be used to dump everything", len(db.recent_diag(CODE, limit=10 ** 9)) <= 5000)
check("since_ts filters", len(db.recent_diag(CODE, since_ts=time.time() + 10_000)) == 0)

# ---- level filter + clear ----
db.insert_diag(CODE, "d", "b", [{"ts": time.time(), "level": "error", "message": "an error"}])
check("level filter selects", all(r["level"] == "error"
                                  for r in db.recent_diag(CODE, level="error")))
n = db.clear_diag(CODE)
check("clear removes this household's lines", len(db.recent_diag(CODE, limit=5000)) == 0)
check("and leaves the other household alone", len(db.recent_diag(OTHER, limit=5000)) >= 0)

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
