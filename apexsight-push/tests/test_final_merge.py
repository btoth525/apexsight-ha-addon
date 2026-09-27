"""The FINAL push keeps what only the bridge knows.

Run:  PYTHONPATH=. python3 tests/test_final_merge.py

/v1/notify style-renders every push that carries raw event fields, and the renderer's values used
to win outright. On the final push that threw away everything the bridge had worked out:

  - Frigate's AI story (headline with the traffic-light dot, and the summary). The shipped
    "notifications say what happened" feature never reached a lock screen.
  - The GIF scoped to THIS review's window. The renderer only knows the event, whose preview.gif
    starts at the EVENT's start: on a re-linked long-lived track that was ~50 min before the
    review (2 of 20 measured alerts), so the notification showed unrelated footage.
  - The still pinned inside the review.

The instant "alert" stage must stay fully rendered, and a bridge that sent nothing extra must
degrade to exactly the rendered push.
"""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("APEX_DATA_DIR", tempfile.mkdtemp(prefix="apextest_final_"))
os.environ.setdefault("APEX_SECRET_KEY", "test")
os.environ.setdefault("PAIRING_CODE", "APEX-TEST-0000")

from app import main, render  # noqa: E402

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


BASE = "https://frigate.example.com"
DET = "1790456900.440607-obrorv"
REVIEW_GIF = f"{BASE}/api/Front_Driveway/start/1790456890/end/1790456910/preview.gif"
PINNED = f"{BASE}/api/Front_Driveway/recordings/1790456900.5/snapshot.jpg?height=720"

# What the relay's renderer produces for the final stage with the default style.
R = render.render({"camera": "Front_Driveway", "camera_name": "Front Driveway", "labels": ["car"],
                   "detection_id": DET, "frigate_base_url": BASE}, {}, "final")
R_ARGS = (R["title"], R["body"], R["snapshot_url"], R["thumbnail_url"])


def bridge_body(**kw):
    fields = {"title": "", "body": "", "snapshot_url": "", "thumbnail_url": "",
              "ai_title": "", "ai_summary": ""}
    fields.update(kw)
    return SimpleNamespace(**fields)


check("sanity: the renderer's final GIF is the EVENT preview", R["snapshot_url"].endswith(
    f"/api/events/{DET}/preview.gif"))

# ---- 1. The AI story survives ----
story = bridge_body(title="\U0001F7E2 Car parks in the driveway", body="A silver sedan pulls in.",
                    ai_title="Car parks in the driveway", ai_summary="A silver sedan pulls in.",
                    snapshot_url=REVIEW_GIF, thumbnail_url=PINNED)
t, b, s, th = main._merge_final(story, {}, *R_ARGS)
check("AI headline (with its dot) wins over the rendered title",
      t == "\U0001F7E2 Car parks in the driveway")
check("AI summary wins over the rendered body", b == "A silver sedan pulls in.")

# ---- 2. No story → the rendered text stands ----
plain = bridge_body(title="\U0001f697 Car", body="Front Driveway", snapshot_url=REVIEW_GIF,
                    thumbnail_url=PINNED)
t, b, _, _ = main._merge_final(plain, {}, *R_ARGS)
check("no AI title keeps the style-rendered title", t == R["title"])
check("no AI summary keeps the style-rendered body", b == R["body"])

# An empty headline with a summary: the bridge left the fallback title, so don't take it.
half = bridge_body(title="\U0001f697 Car", body="A silver sedan pulls in.", ai_title="",
                   ai_summary="A silver sedan pulls in.")
t, b, _, _ = main._merge_final(half, {}, *R_ARGS)
check("summary-only story keeps the rendered title", t == R["title"])
check("summary-only story still carries the summary", b == "A silver sedan pulls in.")

# ---- 3. finalGif (default / True) → the bridge's REVIEW-window GIF ----
for label, style in (("default style", {}), ("finalGif True", {"finalGif": True})):
    _, _, s, th = main._merge_final(plain, style, *R_ARGS)
    check(f"{label}: snapshot is the review-window GIF, not the event preview", s == REVIEW_GIF)
    check(f"{label}: thumbnail is the bridge's pinned still", th == PINNED)

# ---- 4. finalGif False → a still, and it's the bridge's ----
_, _, s, th = main._merge_final(plain, {"finalGif": False}, *R_ARGS)
check("finalGif False: snapshot is the bridge's still, never a GIF", s == PINNED)
check("finalGif False: thumbnail is the bridge's still", th == PINNED)

# ---- 5. A bridge with no media keeps the rendered media ----
_, _, s, th = main._merge_final(bridge_body(), {}, *R_ARGS)
check("empty bridge snapshot keeps the rendered snapshot", s == R["snapshot_url"])
check("empty bridge thumbnail keeps the rendered thumbnail", th == R["thumbnail_url"])
_, _, s, _ = main._merge_final(bridge_body(), {"finalGif": False}, *R_ARGS)
check("finalGif False with no bridge still keeps the rendered snapshot", s == R["snapshot_url"])

# ---- 6. A malformed stored style never raises ----
try:
    _, _, s, _ = main._merge_final(plain, ["not", "a", "dict"], *R_ARGS)
    check("a non-dict style falls back to the GIF default without raising", s == REVIEW_GIF)
except Exception as exc:  # noqa: BLE001
    check(f"a non-dict style must not raise ({exc!r})", False)

# ---- End to end through /v1/notify: what APNs would actually be handed ----
from fastapi.testclient import TestClient  # noqa: E402

CODE = os.environ["PAIRING_CODE"]
sent_payloads = []


async def fake_deliver(pairing_code, payload, collapse_id="", gate=None):
    sent_payloads.append(payload)
    return {"devices": 1, "sent": 1, "failed": 0, "pruned": 0, "suppressed": 0, "errors": []}


main.apns.is_configured = lambda: True
main.apns.deliver_to_pairing = fake_deliver
main.db.devices_for = lambda code: [{"device_token": "a" * 64, "environment": "production",
                                     "updated_at": 1}]

RAW = {"pairing_code": CODE, "camera": "Front_Driveway", "camera_name": "Front Driveway",
       "labels": ["car"], "detection_id": DET, "frigate_base_url": BASE, "review_id": "rev-1",
       "collapse_id": "rev-1"}

with TestClient(main.app) as c:
    r = c.post("/v1/notify", json={**RAW, "title": "\U0001f697 Car", "body": "Front Driveway",
                                   "stage": "alert",
                                   "snapshot_url": f"{BASE}/api/events/{DET}/snapshot.jpg"})
    check("instant alert posts OK", r.status_code == 200)
    alert = sent_payloads[-1]
    check("instant alert stays style-rendered (title)",
          alert["aps"]["alert"]["title"] == render.render(
              {"camera_name": "Front Driveway", "labels": ["car"]}, {}, "alert")["title"])
    check("instant alert stays style-rendered (crop, not the bridge's URL)",
          alert.get("snapshot_url", "").endswith("&quality=70"))

    r = c.post("/v1/notify", json={**RAW, "stage": "final", "silent": True,
                                   "title": "\U0001F7E2 Car parks in the driveway",
                                   "body": "A silver sedan pulls in.",
                                   "ai_title": "Car parks in the driveway",
                                   "ai_summary": "A silver sedan pulls in.",
                                   "snapshot_url": REVIEW_GIF, "thumbnail_url": PINNED})
    check("final posts OK", r.status_code == 200)
    final = sent_payloads[-1]
    check("final push carries the AI headline to APNs",
          final["aps"]["alert"]["title"] == "\U0001F7E2 Car parks in the driveway")
    check("final push carries the AI summary to APNs",
          final["aps"]["alert"]["body"] == "A silver sedan pulls in.")
    check("final push carries the review-window GIF to APNs", final.get("snapshot_url") == REVIEW_GIF)
    check("final push carries the pinned still to APNs", final.get("thumbnail_url") == PINNED)

# ---- The merge is wired to the final stage only, inside the render guard ----
src = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py")).read()
render_block = src[src.index("rendered = render.render("):src.index("render failed")]
check("the merge runs only for stage == final", '== "final":' in render_block
      and "_merge_final(" in render_block)

# ---- The cropped still is byte-identical on both sides ----
# The phone's notification media cache keys on the exact URL. The instant push carries the
# renderer's crop and the final push the bridge's; if the strings drift, the follow-up re-downloads
# a picture the phone already has.
os.environ["FRIGATE_BASE_URL"] = BASE
import bridge  # noqa: E402

payload = bridge._build_alert({"id": "r1", "camera": "Front_Driveway", "severity": "alert",
                               "data": {"objects": ["car"], "detections": [DET]}}, final=False)
instant = render.render({"camera": "Front_Driveway", "labels": ["car"], "detection_id": DET,
                         "frigate_base_url": BASE}, {}, "alert")
check("bridge and renderer build the SAME crop URL", payload["snapshot_url"] == instant["snapshot_url"])
check("the crop is capped at quality=70", instant["snapshot_url"].endswith("&quality=70"))
check("the crop never asks for a height (it would UPSCALE small crops)",
      "height=" not in instant["snapshot_url"])

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
