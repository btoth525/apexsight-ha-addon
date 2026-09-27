"""The bridge's own Frigate reads go to the LAN API; the phones' URLs stay public.

Run:  PYTHONPATH=. python3 tests/test_frigate_lan_reads.py

`frigate_base_url` is the PUBLIC, login-gated hostname. The bridge sent its server-side reads
there unauthenticated (the event's best frame, the recordings probe, the AI review story) and
every one was a 401. So the AI story never arrived, the follow-up push always waited out the full
25s poll, and the GIF window never centred. The same trap as the 1.27.1 talk-live fix.

The dangerous regression in the other direction is the LAN address leaking into a PAYLOAD: a
phone on cellular can't reach it, so the notification would have no picture. These pin both.
"""
import importlib
import os
import time

os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")
os.environ["FRIGATE_BASE_URL"] = "https://frigate.example.com"
os.environ["FRIGATE_RTSP_HOST"] = "10.9.8.7"
os.environ.pop("FRIGATE_API_URL", None)
import bridge  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


PUBLIC = "https://frigate.example.com"
LAN = "http://10.9.8.7:5000"
check("server-side reads go to the LAN API port", bridge.FRIGATE_API_URL == LAN)
check("the public URL is untouched", bridge.FRIGATE_BASE_URL == PUBLIC)

RS, RE = 1790456824.286, 1790456910.5          # an 86 s Front_Driveway review
DET = "1790443750.953932-so4xfh"               # a re-linked parked-car track, started ~50 min early
SFT = 1790446962.007913                        # its best frame: well before this review


class Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content

    def json(self):
        return self._body


calls = []


def fake_get(url, timeout=None, **_):
    calls.append(url)
    if "/api/review/" in url:
        return Resp(body={"data": {"metadata": {
            "title": "Car parks in the driveway", "scene": "A silver sedan pulls in and parks.",
            "potential_threat_level": 1, "confidence": 0.9}}})
    if "/api/events/" in url:
        return Resp(body={"data": {"snapshot_frame_time": SFT}})
    if "/recordings/" in url:
        return Resp(content=b"\xff\xd8\xff\xe0jpeg")
    return Resp(status=404)


bridge.requests.get = fake_get
AFTER = {"id": "1790456824.286429-srtnro", "camera": "Front_Driveway", "severity": "alert",
         "start_time": RS, "end_time": RE,
         "data": {"objects": ["car"], "detections": [DET], "thumb_time": RS + 5}}

payload = bridge._build_alert(AFTER, final=True)

check("every Frigate read went to the LAN API", calls and all(u.startswith(LAN) for u in calls))
check("the event is fetched exactly ONCE (it feeds both the GIF window and the still)",
      sum("/api/events/" in u for u in calls) == 1)
check("the AI story was read", any("/api/review/" in u for u in calls))
check("the recordings probe ran on the LAN", any("/recordings/" in u for u in calls))

leaks = [k for k, v in payload.items() if isinstance(v, str) and ("10.9.8.7" in v or ":5000" in v)]
check(f"no payload field carries the LAN address (leaked: {leaks})", not leaks)
check("frigate_base_url in the payload is still the PUBLIC one",
      payload["frigate_base_url"] == PUBLIC)
check("the GIF points at the public host", payload["snapshot_url"].startswith(PUBLIC + "/api/"))
check("the pinned still points at the public host",
      payload["thumbnail_url"].startswith(PUBLIC + "/api/Front_Driveway/recordings/"))
gs = int(payload["snapshot_url"].split("/start/")[1].split("/")[0])
ge = int(payload["snapshot_url"].split("/end/")[1].split("/")[0])
check("the GIF window is bounded now that the best frame is known", ge - gs <= 21)

check("the AI headline made it into the title", "Car parks in the driveway" in payload["title"])
check("a trusted level-1 story interrupts", payload["silent"] is False and payload["threat_level"] == 1)

# ---- An auth failure is not waited out ----
calls.clear()
bridge.requests.get = lambda url, timeout=None, **_: (calls.append(url), Resp(status=401))[1]
t0 = time.time()
story = bridge._review_ai_story("rev-x")
check("a 401 story read returns None", story is None)
check("a 401 is not polled for the full 25 s", time.time() - t0 < 1.0 and len(calls) == 1)

# ---- The last sleep never overruns the deadline ----
calls.clear()
bridge.requests.get = lambda url, timeout=None, **_: (calls.append(url), Resp(status=404))[1]
t0 = time.time()
bridge._review_ai_story("rev-y", wait_s=2.0)
check("the wait stays inside its deadline (was up to +5 s)", time.time() - t0 < 2.6)

# ---- No LAN host configured: old behaviour, public URL ----
os.environ["FRIGATE_RTSP_HOST"] = ""
importlib.reload(bridge)
check("no frigate_rtsp_host falls back to frigate_base_url", bridge.FRIGATE_API_URL == PUBLIC)
os.environ["FRIGATE_RTSP_HOST"] = "10.9.8.7:8554"
importlib.reload(bridge)
check("a host:port in frigate_rtsp_host still reads Frigate on :5000",
      bridge.FRIGATE_API_URL == LAN)

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
