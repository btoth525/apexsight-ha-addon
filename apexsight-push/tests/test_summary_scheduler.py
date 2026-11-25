"""The daily-summary scheduler (evening "Your Day" + morning brief): once per day, the morning window,
the user's time zone, and the evening catch-up (1.31.1: only for the day just before today).

APNs and the clock are faked. Run:
  PYTHONPATH=. PAIRING_CODE=APEX-TEST-0000 python3 tests/test_summary_scheduler.py
"""
import asyncio
import datetime as real_dt
import importlib
import json
import os
import tempfile
import types

os.environ["APEX_DATA_DIR"] = tempfile.mkdtemp(prefix="apextest_sched_")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["APEX_SECRET_KEY"] = "testsecret"

import app.main as m  # noqa: E402
importlib.reload(m)
from app import db  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


CODE = m.PAIRING_CODE
TZ = real_dt.timezone(real_dt.timedelta(hours=-6))
NOW = [real_dt.datetime(2026, 11, 25, 12, 0, tzinfo=TZ)]


class FakeDateTime(real_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW[0].astimezone(tz) if tz else NOW[0]


m.datetime = types.SimpleNamespace(datetime=FakeDateTime, timezone=real_dt.timezone,
                                   timedelta=real_dt.timedelta, tzinfo=real_dt.tzinfo)
sent = []


async def fake_deliver(code, payload, collapse_id="", gate=None):
    sent.append((collapse_id, payload["aps"]["alert"]["title"]))
    return {"devices": 1, "sent": 1, "failed": 0, "pruned": 0, "suppressed": 0, "errors": [], "delivered": ["a"]}

m.apns.deliver_to_pairing = fake_deliver
m.apns.is_configured = lambda: True
db.init()
db.upsert_device("a" * 64, CODE, "production", "ios")


def at(day, hour, minute=0):
    NOW[0] = real_dt.datetime(2026, 11, day, hour, minute, tzinfo=TZ)
    asyncio.run(m._maybe_send_recap())


def configure(**kw):
    cfg = {"enabled": True, "hour": 21, "minute": 0, "tz_offset": -6 * 3600,
           "morning_enabled": True, "morning_hour": 7, "morning_minute": 0}
    cfg.update(kw)
    db.set_config(f"recap:{CODE}", json.dumps(cfg))


configure()
# ---- morning brief ----
at(25, 6, 59)
check("morning: nothing before the chosen time", not [s for s in sent if s[0].startswith("brief")])
at(25, 7, 0)
check("morning: sent at the chosen local time", [s[0] for s in sent] == ["brief-2026-11-25"])
at(25, 7, 1)
at(25, 9, 30)
check("morning: at most once per day", [s[0] for s in sent].count("brief-2026-11-25") == 1)
sent.clear()
at(26, 10, 30)
check("morning: a relay that was down past the 3-hour window doesn't send 'overnight' late",
      not [x for x in sent if x[0].startswith("brief")])

# ---- evening, once per day ----
sent.clear()
at(26, 21, 0)
at(26, 21, 1)
at(26, 23, 59)
check("evening: sent once at the chosen local time", [s[0] for s in sent] == ["recap-2026-11-26"])

# ---- catch-up: the previous day's recap was never sent (tz jump / outage across midnight) ----
configure(morning_enabled=False)
sent.clear()
at(27, 20, 0)                       # checked on the 27th, before 21:00
at(28, 1, 0)                        # next check is already the 28th: the 27th's recap never went out
check("catch-up: the day just before today is caught up once", [s[0] for s in sent] == ["recap-2026-11-27"])
at(28, 1, 1)
check("…only once", [s[0] for s in sent] == ["recap-2026-11-27"])

# ---- 1.31.1: a stale last-checked date is NOT a missed day ----
# The recap was switched off on the 21st before its time (last checked that day, never sent), and is
# switched back on mid-morning on the 29th.
db.set_config(f"recap_last_checked:{CODE}", "2026-11-21")
sent.clear()
at(29, 10, 5)
check("re-enabling a recap that was off for days sends no stale catch-up", not sent)
at(29, 21, 0)
check("…and today's recap still goes out on time", [s[0] for s in sent] == ["recap-2026-11-29"])

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
