"""Doorbell ring accounting — the record that the 2026-09-08 miss did not have.

On that night the doorbell was pressed, the relay sent the VoIP pushes, APNs accepted both, and
neither phone rang. The only trace was ONE aggregate line — `ring -> 2 phones (failed 0)` — in a
100-line rolling buffer, which cannot answer "which phone", "whose token is that", or "what did
Apple actually say". These tests pin the parts that make the next miss answerable in one read:
a name attached to every token, a durable per-phone row for every ring, and a bounded log that
discards the OLDEST rows rather than the newest.

Run:  PYTHONPATH=. python3 tests/test_ring_log.py
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

# ---- a VoIP token knows which PHONE it is ----
db.upsert_voip("a" * 64, CODE, "production", device_name="Brandons Iphone")
db.upsert_voip("b" * 64, CODE, "production", device_name="Taylors iPhone")
rows = {r["voip_token"]: r for r in db.voip_tokens_for(CODE)}
check("both phones registered", len(rows) == 2)
check("name is stored", rows["a" * 64]["device_name"] == "Brandons Iphone")

# An older app build sends no name at all. That must not erase a name we already have — an
# unnamed token is exactly the ambiguity this column exists to remove.
db.upsert_voip("a" * 64, CODE, "production", device_name="")
check("an empty name does NOT blank an existing one",
      {r["voip_token"]: r for r in db.voip_tokens_for(CODE)}["a" * 64]["device_name"]
      == "Brandons Iphone")
db.upsert_voip("a" * 64, CODE, "production", device_name="Brandons iPhone 17")
check("a real name still updates",
      {r["voip_token"]: r for r in db.voip_tokens_for(CODE)}["a" * 64]["device_name"]
      == "Brandons iPhone 17")

# ---- every ring writes one row PER PHONE ----
db.insert_ring(CODE, "Brandons Iphone", "aaaaaaaa", True, "ok", "apns-1")
db.insert_ring(CODE, "Taylors iPhone", "bbbbbbbb", False, "400 BadDeviceToken", "apns-2")
rings = db.rings_for(CODE)
check("one row per phone", len(rings) == 2)
check("newest first", rings[0]["token_tail"] == "bbbbbbbb")
check("success is recorded as such", any(r["ok"] and r["device_name"] == "Brandons Iphone"
                                         for r in rings))
check("a failure keeps Apple's reason", any(r["detail"] == "400 BadDeviceToken" for r in rings))
check("the apns-id is kept so Apple's own logs can be chased",
      sorted(r["apns_id"] for r in rings) == ["apns-1", "apns-2"])

# ---- one household never sees another's phones ----
db.insert_ring(OTHER, "Someone else", "cccccccc", True, "ok", "apns-x")
check("cross-household isolation",
      all(r["token_tail"] != "cccccccc" for r in db.rings_for(CODE, limit=500)))
check("and the other household still has its own", len(db.rings_for(OTHER)) == 1)

# ---- bounded, keeping the NEWEST ----
for i in range(db.RING_LOG_MAX_ROWS + 25):
    db.insert_ring(CODE, "Brandons Iphone", f"{i:08d}", True, "ok", f"id-{i}")
kept = db.rings_for(CODE, limit=5000)
check("log is capped", len(kept) <= db.RING_LOG_MAX_ROWS)
newest = f"{db.RING_LOG_MAX_ROWS + 24:08d}"
check("rotation keeps the NEWEST ring, not the oldest",
      any(r["token_tail"] == newest for r in kept))
check("the very first ring has aged out", all(r["token_tail"] != "aaaaaaaa" for r in kept))
check("the other household was not pruned by our rotation", len(db.rings_for(OTHER)) == 1)

# ---- limits are clamped, so a read cannot be used to haul the whole table ----
check("limit is clamped to a sane maximum", len(db.rings_for(CODE, limit=10 ** 6)) <= 500)
check("a nonsense limit still returns something", len(db.rings_for(CODE, limit=0)) >= 1)

# ---- a VoIP push must not be stored-and-forwarded ----
# A doorbell ring is about THIS moment. Without `apns-expiration: 0`, APNs stores an undeliverable
# push and retries it later — the phone rings for a doorstep that is now empty.
import inspect  # noqa: E402
from app import apns  # noqa: E402
src = inspect.getsource(apns.send_voip)
check("send_voip sets apns-expiration: 0", '"apns-expiration": "0"' in src)
check("send_voip returns the apns-id too", src.count("apns_id") >= 3)

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
