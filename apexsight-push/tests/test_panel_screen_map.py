"""Regression tests for PANEL_SCREEN_FOR_SLUG in app/main.py, the soundboard-slug -> Doorpanel
screen-button map. Parsed with ast so the test needs none of main.py's runtime deps.

The rule that matters most: a halloween-ring-* clip plays on a doorbell RING, and the panel owns
its own ring takeover. Mapping one would yank the panel off the ring screen on every ring.

Run:  PYTHONPATH=. python3 tests/test_panel_screen_map.py
"""
import ast
import pathlib

ok = []


def check(name, cond):
    ok.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name)


src = (pathlib.Path(__file__).resolve().parent.parent / "app" / "main.py").read_text()
tree = ast.parse(src)
mapping = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "PANEL_SCREEN_FOR_SLUG" for t in node.targets
    ):
        mapping = ast.literal_eval(node.value)

check("PANEL_SCREEN_FOR_SLUG is a literal dict", isinstance(mapping, dict))
mapping = mapping or {}

check("no ring clip is ever mapped (halloween-ring-* / *ring*)",
      not [k for k in mapping if k.startswith("halloween-ring") or "ring-" in k])
check("every target is a button.door_panel_* entity",
      all(isinstance(v, str) and v.startswith("button.door_panel_") for v in mapping.values()))
check("no slug has stray whitespace (lookup is exact-match after strip)",
      all(k == k.strip() for k in mapping))

expected = {
    "recorded-warning": "button.door_panel_no_soliciting",
    "be-right-there": "button.door_panel_screen_be_right_there",
    "leave-it-at-the-door-please": "button.door_panel_screen_leave_package",
    "thanks-delivery": "button.door_panel_screen_thanks_delivery",
    "halloween-no-soliciting": "button.door_panel_no_soliciting",
    "halloween-be-right-there": "button.door_panel_screen_be_right_there",
    "halloween-leave-package": "button.door_panel_screen_leave_package",
    "halloween-thanks-delivery": "button.door_panel_screen_thanks_delivery",
    "halloween-trick-or-treat": "button.door_panel_screen_trick_or_treat",
    "halloween-candy-bowl": "button.door_panel_screen_trick_or_treat",
    "halloween-scare": "button.door_panel_screen_halloween_scare",
}
for slug, entity in expected.items():
    check(f"{slug} -> {entity}", mapping.get(slug) == entity)

print(f"\n{sum(ok)}/{len(ok)} passed")
raise SystemExit(0 if all(ok) else 1)
