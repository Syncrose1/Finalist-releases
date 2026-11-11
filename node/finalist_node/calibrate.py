"""The calibration check (11 Nov 2026): re-mark every moderated benchmark with today's examiner and report how far
it drifts from the agreed grade and item scores. Run it after changing the examiner (MARK_SYSTEM, MARK_PROMPT, the
station notes): each benchmark costs one Claude call, so it never runs on its own.

  cd node && python3 -m finalist_node.calibrate            # shows the cost, asks, then marks
  python3 -m finalist_node.calibrate --dry                 # only the cost
  python3 -m finalist_node.calibrate --yes --only bbn      # no question; one station's benchmarks

A benchmark is never shown as a worked example while it's being re-marked (it would be marking itself), and the
patient's own mark isn't asked for (the drift is the examiner's), so it's exactly one call each.
"""
import argparse
import json
import secrets
import shutil
import sys
import threading
from pathlib import Path

from . import config, moderation
from . import stations as sim

# Sonnet's API list price, to show what each call would cost off the subscription (Claude Code runs it on the plan)
PRICE_IN, PRICE_OUT = 3 / 1e6, 15 / 1e6
OUT_TOKENS = 1800


def _run_file(cfg, b):
    roots = [Path(cfg["owner_data"]).expanduser(), *Path(cfg["data"]).expanduser().glob("users/*")]
    for r in roots:
        f = r / "sims" / f"{b['id']}.json"
        if f.is_file():
            return f
    return None


def _estimate(s, b):
    c = s.get("case") or {}
    chars = (len(sim.MARK_SYSTEM) + len(sim.MARK_PROMPT) + 2500 + len(json.dumps(c.get("mark_scheme") or []))
             + sum(len(str(c.get(k) or "")) for k in ("brief", "diagnosis", "script", "cue"))
             + sum(len(t.get("text") or "") + 12 for t in s.get("turns") or [])
             + len(moderation.marking_note(s.get("station"), s.get("kind"), exclude=b["id"])))
    return chars // 4


def remark_one(model, f, b):
    s = json.loads(f.read_text())
    live = {**s, "id": "calibrate-" + secrets.token_hex(4), "ready": threading.Event(), "file": None}
    live["ready"].set()
    # the scheme as moderated: removed and rewritten items are what the agreed scores are against
    live["case"] = {**(s.get("case") or {})}
    tmp = sim.CWD / f"calibrate-{secrets.token_hex(4)}"
    with sim.LOCK:
        sim.SIMS[live["id"]] = live
    try:
        return sim.mark(live["id"], tmp, lambda o: None, with_patient=False, exclude_benchmark=b["id"])
    finally:
        with sim.LOCK:
            sim.SIMS.pop(live["id"], None)
        shutil.rmtree(tmp, ignore_errors=True)


def drift(b, out):
    agreed = {moderation._key(i["item"]): i for i in b["items"]}
    rows, diffs = [], []
    for i in out.get("items") or []:
        a = agreed.get(moderation._key(i.get("item")))
        if not a:
            continue
        d = int(i.get("score") or 0) - int(a.get("score") or 0)
        diffs.append(d)
        if d:
            rows.append(f"    {'+' if d > 0 else ''}{d}  {a['score']}->{i.get('score')}  {a['item'][:80]}")
    g = (out.get("grade") or "?")[:1]
    steps = "ABCDEF".find(b["grade"]) - "ABCDEF".find(g) if g in "ABCDEF" else None
    return g, steps, diffs, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-mark the moderated benchmarks and report drift.")
    ap.add_argument("--yes", action="store_true", help="don't ask before spending the calls")
    ap.add_argument("--dry", action="store_true", help="only print the cost")
    ap.add_argument("--only", help="only this station's benchmarks")
    a = ap.parse_args(argv)
    cfg = config.load()
    moderation.ROOT = Path(cfg["data"]).expanduser() / "moderation"
    bs = [b for b in moderation.benchmarks() if not a.only or b.get("station") == a.only]
    plan = []
    for b in bs:
        f = _run_file(cfg, b)
        if not f:
            print(f"  (skipping {b['id']}: its run file is gone)")
            continue
        plan.append((b, f, _estimate(json.loads(f.read_text()), b)))
    if not plan:
        print("No benchmarks yet: moderate a reported run first (/dev/moderate).")
        return 0
    tin = sum(p[2] for p in plan)
    usd = tin * PRICE_IN + len(plan) * OUT_TOKENS * PRICE_OUT
    print(f"Calibration: {len(plan)} benchmark{'s' * (len(plan) != 1)} = {len(plan)} Claude call{'s' * (len(plan) != 1)} ({cfg['model']}, effort medium), "
          f"about {tin:,} tokens in and {len(plan) * OUT_TOKENS:,} out: ~${usd:.2f} at API prices; on Claude Code it comes out of the plan's usage.")
    if a.dry:
        return 0
    if not a.yes and input("Go? [y/N] ").strip().lower() != "y":
        return 1
    if not sim.available():
        print("Claude Code isn't installed here.")
        return 2
    grades, all_d = [], []
    for b, f, _ in plan:
        print(f"\n{b['title']} ({b['id']}): agreed {b['grade']}" + (f", first examiner {b['examiner_grade']}" if b.get("examiner_grade") else ""))
        try:
            out = remark_one(cfg["model"], f, b)
        except Exception as e:
            print(f"  failed: {e}")
            continue
        g, steps, diffs, rows = drift(b, out)
        grades.append(steps)
        all_d += diffs
        print(f"  now {g}: " + ("matches" if steps == 0 else f"{abs(steps)} grade{'s' * (abs(steps) != 1)} {'harsher' if steps < 0 else 'softer'}" if steps is not None else "no grade"))
        print(f"  items: {sum(1 for d in diffs if d)} of {len(diffs)} differ, mean |drift| {sum(map(abs, diffs)) / (len(diffs) or 1):.2f}/2")
        print("\n".join(rows) if rows else "    (every item agrees)")
        print(f"  verdict: {out.get('verdict', '')[:300]}")
    if grades:
        exact = sum(1 for x in grades if x == 0)
        print(f"\nSummary: grade agrees on {exact} of {len(grades)}; mean grade drift {sum(abs(x or 0) for x in grades) / len(grades):.2f} letters; "
              f"item agreement {sum(1 for d in all_d if not d)}/{len(all_d)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
