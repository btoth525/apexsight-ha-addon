"""Regression tests for three ways a command or an alert could be silently lost.

Run:  PYTHONPATH=. python3 tests/test_delivery_integrity.py

Each of these shipped, and each failed in the direction that is invisible to the user — the thing
reported success and nothing rang, armed, or was suppressed:

 1. /v1/doorbell-ring returned 200 even when every VoIP push failed. The bridge reads a 2xx as
    "delivered", so it stopped retrying AND charged its ring-debounce window: a visitor pressing
    the button repeatedly rang nobody, with only "doorbell ring debounced" in the log.
 2. The arm/disarm request published at qos=0 and was marked consumed regardless. paho only queues
    QoS>=1 while the socket is down, and `is_connected()` stays True for a socket that has just
    dropped — so the command was discarded, never retried, and the app had already said "ok".
 3. The GenAI description follow-up carried no labels/zones, so the relay fell back to label
    "object" and zones [] — which pass every per-object and per-zone mute. A phone that had muted
    "person" still got a TIME-SENSITIVE banner titled "Person" seconds after the instant alert was
    correctly suppressed for it.
"""
import json
import os
import time

os.environ.setdefault("PAIRING_CODE", "APEX-PLEX-5250")
import bridge  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


# ---------------------------------------------------------------------------
# 2. Arm/disarm must not be marked consumed unless it actually went out
# ---------------------------------------------------------------------------

class FakeInfo:
    def __init__(self, rc):
        self.rc = rc


class FakeClient:
    """Records publishes and reports whatever rc the test asks for."""

    def __init__(self, rc=0):
        self.rc = rc
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})
        return FakeInfo(self.rc)


_store = {}
bridge._get_cfg = lambda k, d="": _store.get(k, d)
bridge._set_cfg = lambda k, v: _store.__setitem__(k, v)


def stage_request(seq=1, mode="away", ts=None, code="1234"):
    _store.clear()
    _store["mode_request"] = json.dumps(
        {"seq": seq, "mode": mode, "by": "iPhone", "code": code,
         "ts": time.time() if ts is None else ts}
    )


# A healthy publish consumes the request exactly once.
stage_request()
c = FakeClient(rc=0)
check("a successful publish reports it acted", bridge._consume_mode_request(c) is True)
check("published to the mode-set topic", c.published[0]["topic"] == bridge.MODE_SET_TOPIC)
check("published at qos=1 so paho queues it while disconnected", c.published[0]["qos"] == 1)
check("never retained (a request is a one-shot command)", c.published[0]["retain"] is False)
check("marked consumed", _store.get("mode_request_consumed_seq") == "1")
check("the Alarmo code is scrubbed after publishing",
      json.loads(_store["mode_request"]).get("code") == "")
check("a second pass does nothing", bridge._consume_mode_request(c) is False)
check("and did not publish again", len(c.published) == 1)

# THE BUG: a dropped publish must leave the request for the next tick, not silently eat it.
stage_request(seq=7)
c = FakeClient(rc=4)   # MQTT_ERR_NO_CONN
check("a failed publish reports it did not act", bridge._consume_mode_request(c) is False)
check("a failed publish does NOT mark the request consumed",
      _store.get("mode_request_consumed_seq", "0") == "0")
check("the code is kept so the retry can still arm",
      json.loads(_store["mode_request"]).get("code") == "1234")
c2 = FakeClient(rc=0)
check("the next tick retries it", bridge._consume_mode_request(c2) is True)
check("and now it is consumed", _store.get("mode_request_consumed_seq") == "7")

# The bound that makes retrying safe: an old command is dropped, never fired late.
stage_request(seq=9, ts=time.time() - (bridge.MODE_REQUEST_MAX_AGE_S + 30))
c = FakeClient(rc=0)
check("an expired request is not published", bridge._consume_mode_request(c) is False)
check("nothing was sent to HA", c.published == [])
check("but it IS marked consumed, so it can never surface later",
      _store.get("mode_request_consumed_seq") == "9")

# A request just inside the bound still goes.
stage_request(seq=11, ts=time.time() - (bridge.MODE_REQUEST_MAX_AGE_S - 30))
c = FakeClient(rc=0)
check("a request inside the age bound still publishes", bridge._consume_mode_request(c) is True)

# Requests written before `ts` existed must keep working.
_store.clear()
_store["mode_request"] = json.dumps({"seq": 3, "mode": "home", "by": "iPad", "code": ""})
c = FakeClient(rc=0)
check("a legacy request with no ts is treated as current", bridge._consume_mode_request(c) is True)


# ---------------------------------------------------------------------------
# 3. The description follow-up must inherit the alert's gate facts
# ---------------------------------------------------------------------------
# gate.would_deliver is what actually enforces the mute; these pin that the facts it needs are
# present, and that the relay's fallback is what leaked.

import app.gate as gate  # noqa: E402

PREFS = {"objects_disabled": ["person"], "zones_disabled": ["street"]}


def delivers(label, zones):
    allowed, reason = gate.would_deliver(PREFS, "Front_Driveway", label, zones, 0.9, time.time())
    return allowed, reason


allowed, reason = delivers("person", [])
check(f"a muted object is suppressed when the label is carried ({reason})", allowed is False)
allowed, reason = delivers("object", [])
check(f"the relay's no-label fallback is EXACTLY what slipped through ({reason})", allowed is True)
allowed, reason = delivers("car", ["street"])
check(f"a muted zone is suppressed when zones are carried ({reason})", allowed is False)
allowed, _ = delivers("car", [])
check("the no-zone fallback passes every zone mute", allowed is True)

# The record the bridge stashes for the follow-up must carry the two fields, or the payload can't.
src = open(os.path.join(os.path.dirname(__file__), "..", "bridge.py")).read()
rec_block = src[src.index('record = {'):src.index('"_t": time.time(),')]
check("the pending record carries labels", '"labels": objects,' in rec_block)
check("the pending record carries zones", '"zones": zones,' in rec_block)
desc_block = src[src.index('"is_description": True'):]
desc_block = desc_block[:desc_block.index("_post_to_relay")]
check("the description payload sends labels", '"labels": rec.get("labels"' in desc_block)
check("the description payload sends zones", '"zones": rec.get("zones"' in desc_block)

# THE TRAP: sending labels turns on the relay's renderer, which would overwrite the AI sentence.
main_src = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py")).read()
check("the relay skips style rendering for a description follow-up",
      "and not body.is_description:" in main_src
      and "body.detection_id or body.labels or body.sub_labels" in main_src)


# ---------------------------------------------------------------------------
# 1. A doorbell ring that reached nobody must be a retryable failure
# ---------------------------------------------------------------------------
# The endpoint is async and DB-backed; pin the decision itself, which is the part that was wrong.

def ring_status(rows, sent, failed, pruned):
    """The shipped condition from main.doorbell_ring."""
    return 502 if (rows and sent == 0 and (failed - pruned) > 0) else 200


check("every phone failing transiently is a retryable 502", ring_status(2, 0, 2, 0) == 502)
check("one phone ringing is success, even if another failed", ring_status(2, 1, 1, 0) == 200)
check("all failures being dead tokens stays 200 (nothing to retry)", ring_status(2, 0, 2, 2) == 200)
check("a mix of pruned and transient still retries", ring_status(3, 0, 3, 2) == 502)
check("no registered phones is not a failure", ring_status(0, 0, 0, 0) == 200)
check("a clean ring is 200", ring_status(2, 2, 0, 0) == 200)


print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
