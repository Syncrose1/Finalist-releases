"""Disputes, moderation, benchmarks and station overrides (11 Nov 2026, Raahat: "a point of manual
grading and then calibration").

  report     anyone using the node flags an AI-marked run, with an optional reason. The report is kept
             on the run itself and in the node's queue, so the owner sees every account's in one place.
  moderate   the owner agrees the grade and the item scores, fixes the mark scheme for this run and for
             the station, and writes a one-line reason. The run keeps the examiner's original
             (`feedback.moderated`, `feedback.examiner_grade`, as Yousif's breaking-bad-news run, 10 Nov).
  benchmark  every moderated run becomes a worked example for its station and its kind: a trimmed
             transcript, the agreed grade, the agreed items that matter and the reason. The examiner is
             shown the one or two most relevant when it marks (same station first, else same kind).
  overrides  per-station data the case writer and the marker read: examiner notes, accepted answers,
             patient notes, and scheme items removed or rewritten.
  lint       a cheap, deterministic check on every new mark scheme ("station markschemes typically
             generate 2 stupid markscheme items on average"): the safe cases are fixed, the rest flagged.

Everything lives in the node's data folder, beside users/: moderation/queue.json,
moderation/benchmarks.json, moderation/station-overrides.json. Plain JSON, one writer (this node).
"""
import json
import re
import secrets
import threading
import time
from pathlib import Path

from . import config

ROOT = Path(config.load()["data"]).expanduser() / "moderation"      # the server sets it from its own CFG
LOCK = threading.Lock()
GRADES = "ABCDEF"
TAGS = ("too_harsh", "too_soft", "unfair_item", "patient", "examiner", "other")


def _read(name, empty):
    f = ROOT / name
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return empty


def _write(name, obj):
    ROOT.mkdir(parents=True, exist_ok=True)
    f = ROOT / name
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(f)


# ---- overrides: the owner's corrections to a station, read by the case writer and the marker ------

def overrides(station):
    o = _read("station-overrides.json", {}).get(station) or {}
    return {"examiner_notes": o.get("examiner_notes") or [], "accepted_answers": o.get("accepted_answers") or [],
            "patient_notes": o.get("patient_notes") or [], "remove": o.get("remove") or [], "rewrite": o.get("rewrite") or {}}


def _norm(s):
    return re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower()).split()


def _key(s):
    return " ".join(_norm(s))


def apply_scheme(scheme, ov):
    """A mark scheme with the station's removed items dropped and rewritten ones replaced."""
    gone = {_key(x) for x in ov.get("remove") or []}
    rw = {_key(k): v for k, v in (ov.get("rewrite") or {}).items()}
    out = []
    for i in scheme or []:
        k = _key(i.get("item"))
        if k in gone:
            continue
        out.append({**i, "item": rw[k]} if k in rw else i)
    return out


def case_note(station):
    """What the case writer is told about this station, or empty."""
    o = overrides(station)
    lines = []
    if o["remove"]:
        lines.append("Never put these items in the mark scheme (the moderator removed them): " + " | ".join(o["remove"]))
    if o["rewrite"]:
        lines.append("Write these items as the moderator rewrote them: " + " | ".join(f"not '{a}' but '{b}'" for a, b in o["rewrite"].items()))
    if o["examiner_notes"]:
        lines.append("The moderator's notes on this station: " + " | ".join(o["examiner_notes"]))
    if o["patient_notes"]:
        lines.append("For the patient's script: " + " | ".join(o["patient_notes"]))
    return ("\nMODERATED RULES FOR THIS STATION (from reviewing real runs; follow them):\n- " + "\n- ".join(lines) + "\n") if lines else ""


def patient_note(station):
    o = overrides(station)
    return ("\n- The moderator's notes on playing this station: " + " | ".join(o["patient_notes"])) if o["patient_notes"] else ""


# ---- benchmarks ---------------------------------------------------------------------------------

def _excerpt(turns, head=900, tail=500, each=220):
    """The transcript, trimmed to stay cheap as a worked example: each turn capped, the middle cut."""
    lines = []
    for t in turns or []:
        who = {"you": "Candidate", "examiner": "Examiner"}.get(t.get("who"), "Patient")
        x = re.sub(r"\s+", " ", t.get("text") or "").strip()
        lines.append(f"{who}: {x[:each] + ('…' if len(x) > each else '')}")
    s = "\n".join(lines)
    if len(s) <= head + tail:
        return s
    a, b = s[:head], s[-tail:]
    return a[:a.rfind("\n")] + "\n[…]\n" + b[b.find("\n") + 1:]


def benchmarks():
    return _read("benchmarks.json", [])


def add_benchmark(sim_doc, run_name, account=None):
    """Makes (or replaces) the benchmark for a moderated run."""
    fb = sim_doc.get("feedback") or {}
    mod = fb.get("moderated") or {}
    if not mod.get("grade"):
        raise ValueError("That run hasn't been moderated.")
    agreed = mod.get("items") or [{"item": i.get("item"), "score": i.get("score"), "weight": i.get("weight")} for i in fb.get("items") or []]
    was = {(i.get("item") or ""): i.get("score") for i in mod.get("examiner_items") or []}
    b = {"id": run_name, "station": sim_doc.get("station"), "title": sim_doc.get("title"), "kind": sim_doc.get("kind"),
         "account": account or sim_doc.get("account"), "voice": bool(sim_doc.get("voice")),
         "excerpt": _excerpt(sim_doc.get("turns")), "grade": mod["grade"], "examiner_grade": mod.get("examiner_grade"),
         "score": fb.get("score"), "items": [{"item": i.get("item"), "score": i.get("score"), "weight": i.get("weight") or 1,
                                              **({"examiner": was[i.get("item")]} if was.get(i.get("item")) not in (None, i.get("score")) else {})}
                                             for i in agreed],
         "reason": mod.get("note") or "", "at": mod.get("at") or time.time()}
    with LOCK:
        xs = [x for x in benchmarks() if x.get("id") != run_name]
        xs.append(b)
        _write("benchmarks.json", xs)
    return b


def examples_for(station, kind, exclude=None, n=2):
    """The 1-2 most relevant benchmarks: this station's newest first, then the same kind's."""
    xs = [x for x in benchmarks() if x.get("id") != exclude]
    same = sorted([x for x in xs if x.get("station") == station], key=lambda x: -x.get("at", 0))
    kin = sorted([x for x in xs if x.get("station") != station and x.get("kind") == kind], key=lambda x: -x.get("at", 0))
    return (same + kin)[:n]


def _example_text(b):
    # only the items that teach something: not full marks, or where the moderator changed the examiner's score
    telling = [i for i in b["items"] if i.get("score") != 2 or "examiner" in i][:10]
    items = "\n".join(f"  {i['score']}/2 x{i['weight']}: {i['item']}" + (f" (examiner first gave {i['examiner']})" if "examiner" in i else "")
                      for i in telling)
    same = "" if not b.get("title") else f" ({b['title']})"
    return (f"AGREED GRADE {b['grade']}" + (f", where the AI examiner first gave {b['examiner_grade']}" if b.get("examiner_grade") and b["examiner_grade"] != b["grade"] else "")
            + f"{same}. Moderator's reason: {b['reason']}\nItems not at full marks, or changed by the moderator:\n{items or '  (all at full marks)'}\n"
            f"TRANSCRIPT EXCERPT:\n{b['excerpt']}")


def marking_note(station, kind, exclude=None):
    """What the marker is told from moderation: the accepted answers and notes, and the worked examples."""
    o = overrides(station)
    parts = []
    if o["accepted_answers"]:
        parts.append("ACCEPTED ANSWERS (agreed by the moderator; credit them): " + " | ".join(o["accepted_answers"]))
    if o["examiner_notes"]:
        parts.append("MODERATOR'S NOTES FOR THIS STATION: " + " | ".join(o["examiner_notes"]))
    ex = examples_for(station, kind, exclude)
    if ex:
        parts.append("MODERATED BENCHMARKS (real runs a human examiner moderated: calibrate your grade against them; they are "
                     "examples of the standard, never of this candidate):\n" + "\n---\n".join(_example_text(b) for b in ex))
    return ("\n".join(parts) + "\n") if parts else ""


# ---- reports and the queue ------------------------------------------------------------------------

def find_run(data, run=None, station=None, at=None):
    """A run file in an account's sims, by name, or by station and time (runs saved before the name was kept:
    the run that started last before `at`, an ISO time or seconds)."""
    d = data / "sims"
    if run:
        if not re.fullmatch(r"[\w-]+", run):
            return None
        f = d / f"{run}.json"
        return f if f.is_file() else None
    if not station or not re.fullmatch(r"[\w-]+", station) or not d.is_dir():
        return None
    if isinstance(at, str):
        from datetime import datetime
        try:
            at = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            at = None
    best = None
    for f in d.glob(f"*-{station}-*.json"):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        st = s.get("started") or 0
        if s.get("feedback") and (at is None or st <= at + 60) and (best is None or st > best[0]):
            best = (st, f)
    return best[1] if best else None


def report(f, account, email, reason="", tags=None):
    """Flags a run for review: on the run file and in the queue."""
    s = json.loads(f.read_text())
    if not s.get("feedback"):
        raise ValueError("That run hasn't been marked yet.")
    tags = [t for t in (tags or []) if t in TAGS]
    r = {"id": secrets.token_hex(5), "at": time.time(), "by": email or account, "reason": (reason or "").strip()[:1000], "tags": tags}
    s.setdefault("reports", []).append(r)
    f.write_text(json.dumps(s, indent=1))
    with LOCK:
        q = _read("queue.json", [])
        q.append({**r, "account": account, "file": str(f), "run": f.stem, "station": s.get("station"), "title": s.get("title"),
                  "kind": s.get("kind"), "grade": (s.get("feedback") or {}).get("grade"), "status": "open"})
        _write("queue.json", q)
    return r


def queue(include_done=False):
    """The reported runs, newest first, each with what moderating it needs."""
    from . import stations
    out = []
    for e in sorted(_read("queue.json", []), key=lambda e: -e.get("at", 0)):
        if e.get("status") != "open" and not include_done:
            continue
        try:
            s = json.loads(Path(e["file"]).read_text())
        except (OSError, ValueError, KeyError):
            continue
        c = s.get("case") or {}
        out.append({**{k: v for k, v in e.items() if k != "file"},
                    "brief": c.get("brief"), "diagnosis": c.get("diagnosis"), "person": c.get("person") or {},
                    "voice": bool(s.get("voice")), "turns": [{"who": t["who"], "text": t["text"]} for t in s.get("turns") or []],
                    "feedback": s.get("feedback"), "scheme": c.get("mark_scheme") or [],
                    "lint": c.get("lint") if "lint" in c else lint(c.get("mark_scheme"), s.get("kind"), spoken=True)["flags"],
                    "overrides": overrides(s.get("station")), "core": [i for i, _, _ in stations.CORE.get(s.get("kind"), [])]})
    return out


def _rescore(fb):
    items = fb.get("items") or []
    w = lambda i: max(1, int(i.get("weight") or 1))
    cm = int((fb.get("consultation") or {}).get("mark") or 1)
    pm = (fb.get("patient") or {}).get("mark")
    got = sum(w(i) * min(2, max(0, int(i.get("score") or 0))) for i in items) + cm + (pm or 0)
    total = sum(2 * w(i) for i in items) + 5 + (3 if pm else 0)
    fb["score"], fb["points"] = round(100 * got / (total or 1)), [got, total]


def moderate(entry_id, body, by="the owner"):
    """The owner's decision on a reported run. body: grade, scores {item: 0-2}, remove [item], rewrite {old: new},
    examiner_notes, accepted_answers, patient_notes (lists of lines to add), reason, station_wide (bool: the scheme
    fixes apply to the station's future runs too; default true)."""
    with LOCK:
        q = _read("queue.json", [])
        e = next((x for x in q if x.get("id") == entry_id), None)
    if not e:
        raise KeyError("No such report.")
    f = Path(e["file"])
    s = json.loads(f.read_text())
    fb = s.get("feedback") or {}
    grade = str(body.get("grade") or fb.get("grade") or "")[:1].upper()
    if grade not in GRADES:
        raise ValueError("The agreed grade is A to F.")
    reason = str(body.get("reason") or "").strip()[:400]
    if not reason:
        raise ValueError("Write a one-line reason: it's what the examiner learns from.")
    prior = fb.get("moderated") or {}
    examiner_grade = prior.get("examiner_grade") or fb.get("examiner_grade") or fb.get("grade")
    examiner_items = prior.get("examiner_items") or [{"item": i.get("item"), "score": i.get("score")} for i in fb.get("items") or []]
    examiner_score = prior.get("examiner_score", fb.get("score"))
    remove = [str(x) for x in body.get("remove") or [] if str(x).strip()]
    rewrite = {str(a): str(b) for a, b in (body.get("rewrite") or {}).items() if str(a).strip() and str(b).strip()}
    scores = {_key(k): v for k, v in (body.get("scores") or {}).items()}
    items = []
    for i in apply_scheme(fb.get("items") or [], {"remove": remove, "rewrite": rewrite}):
        k = _key(i.get("item"))
        # a rewritten item keeps its score unless given one under either wording
        orig = next((a for a in rewrite if rewrite[a] == i.get("item")), None)
        v = scores.get(k, scores.get(_key(orig)) if orig else None)
        items.append({**i, "score": min(2, max(0, int(v)))} if v is not None else i)
    fb["items"] = items
    fb["grade"] = grade
    fb["examiner_grade"] = examiner_grade
    _rescore(fb)
    fb["moderated"] = {"grade": grade, "examiner_grade": examiner_grade, "examiner_score": examiner_score,
                       "examiner_items": examiner_items, "items": [{"item": i.get("item"), "score": i.get("score"), "weight": i.get("weight")} for i in items],
                       "removed": remove, "rewritten": rewrite, "by": by, "at": time.time(), "note": reason, "report": entry_id}
    s["feedback"] = fb
    if s.get("case") and (remove or rewrite):
        s["case"]["mark_scheme"] = apply_scheme(s["case"].get("mark_scheme"), {"remove": remove, "rewrite": rewrite})
    f.write_text(json.dumps(s, indent=1))
    with LOCK:
        ov = _read("station-overrides.json", {})
        o = ov.setdefault(s.get("station"), {})
        if body.get("station_wide", True):
            o["remove"] = sorted(set(o.get("remove") or []) | set(remove))
            o["rewrite"] = {**(o.get("rewrite") or {}), **rewrite}
        for k in ("examiner_notes", "accepted_answers", "patient_notes"):
            add = [str(x).strip() for x in body.get(k) or [] if str(x).strip()]
            if add:
                o[k] = (o.get(k) or []) + [x for x in add if x not in (o.get(k) or [])]
        o["updated"] = time.time()
        _write("station-overrides.json", ov)
        q = _read("queue.json", [])
        for x in q:
            if x.get("file") == e["file"] and x.get("status") == "open":
                x.update(status="moderated", done=time.time())
        _write("queue.json", q)
    bench = add_benchmark(s, f.stem, e.get("account"))
    return {"feedback": fb, "benchmark": bench, "overrides": overrides(s.get("station"))}


def dismiss(entry_id, reason=""):
    """The examiner was right: close the report without a change (it doesn't become a benchmark)."""
    with LOCK:
        q = _read("queue.json", [])
        for x in q:
            if x.get("id") == entry_id:
                x.update(status="dismissed", done=time.time(), note=str(reason or "")[:400])
                _write("queue.json", q)
                return x
    raise KeyError("No such report.")


# ---- the mark-scheme lint -------------------------------------------------------------------------
# Deterministic and cheap, run on every case as it's written. Safe fixes only (an exact duplicate; a purely
# physical item in a spoken or typed station, which can't be evidenced): the rest is flagged for the owner.

STOP = set("a and the of to or in on for with any their his her them they it is be as at by an if".split())
PHYSICAL = re.compile(r"\b(wash(es|ing)? (their |his |her )?hands|hand hygiene|hand gel|alcohol gel|gloves?|apron|ppe|"
                      r"positions?|positioning|sits? (down|at eye level)|exposes (the|him|her)|chaperone|bleep|"
                      r"draws? the curtain|eye contact|nods?|body language|posture|tissues?)\b", re.I)
SPOKEN = re.compile(r"\b(asks?|explains?|offers?|says?|checks?|tells?|confirms?|introduces?|invites?|summari[sz]es?|"
                    r"gains?|explores?|acknowledges?|elicits?|discuss|names?)\b", re.I)
VAGUE = re.compile(r"\b(well|appropriately|appropriate|effectively|effective|good|properly|adequate(ly)?|overall|professional(ly)?|"
                   r"nicely|suitably|clearly)\b", re.I)
EVERYTHING = re.compile(r"\b(full|complete|comprehensive|thorough|every)\b (\w+ ){0,2}(examination|assessment|systems review|review of systems|risk assessment|explanation)\b|\bevery\b|\ball (the )?(possible )?(differentials|causes|side effects|risks|options|complications|regimens)\b", re.I)
TALK = re.compile(r"\b(cue|fear|worr|concern|feel|emotion|silence|honest|empath|listen)", re.I)


def _toks(s):
    return {t for t in _norm(s) if t not in STOP and len(t) > 2}


def _sim(a, b):
    a, b = _toks(a), _toks(b)
    return len(a & b) / (len(a | b) or 1)


def _parts(s):
    """How many separate things one item asks for: its comma, semicolon, colon and 'and' separated parts."""
    body = s.split(":", 1)[1] if ":" in s else s
    body = re.sub(r"\([^)]*\)", "", body)
    return len([p for p in re.split(r",|;|\band\b|\bor\b|/", body) if p.strip()])


def lint(scheme, kind, spoken=False, core=None):
    """{"scheme": the fixed scheme, "flags": [{item, rule, why, fixed}]} for a case's mark scheme. spoken: a station
    run in words (Claude's history and communication rooms), where a physical action can't be evidenced."""
    from . import stations
    core_items = [i for i, _, _ in (stations.CORE.get(kind) or [])] if core is None else core
    core_keys = {_key(c) for c in core_items}
    flags, out, seen = [], [], {}
    knowledge = 0
    for i in scheme or []:
        text = (i.get("item") or "").strip()
        k = _key(text)
        is_core = k in core_keys
        if k in seen:
            flags.append({"item": text, "rule": "duplicate", "why": "The same item twice.", "fixed": True})
            continue
        if spoken and kind in ("history", "comms") and PHYSICAL.search(text) and not SPOKEN.search(text) and not is_core:
            flags.append({"item": text, "rule": "physical", "why": "A physical action in a station run in words: it can't be evidenced.", "fixed": True})
            continue
        seen[k] = text
        out.append(i)
        if is_core:
            continue
        if spoken and kind in ("history", "comms") and PHYSICAL.search(text):
            flags.append({"item": text, "rule": "physical", "why": "Partly a physical action ('" + PHYSICAL.search(text)[0] + "'): only the spoken part can be marked.", "fixed": False})
        near = next((o for o in list(seen.values())[:-1] if _sim(o, text) >= 0.55), None)
        if near:
            flags.append({"item": text, "rule": "near_duplicate", "why": f"Nearly the same as '{near}'.", "fixed": False})
        restates = next((c for c in core_items if _sim(c, text) >= 0.45), None)
        if restates and not near:
            flags.append({"item": text, "rule": "restates_core", "why": f"Restates the core item '{restates}'.", "fixed": False})
        if kind == "comms" and i.get("domain") == "reasoning" and not TALK.search(text):
            knowledge += 1
            if int(i.get("weight") or 1) > 1:
                flags.append({"item": text, "rule": "knowledge_weight", "why": f"A knowledge item weighted x{i.get('weight')} in a communication station (counts once).", "fixed": False})
        n = _parts(text)
        if n >= 7 or (EVERYTHING.search(text) and kind != "exam"):
            flags.append({"item": text, "rule": "too_much", "why": (f"Asks for {n} things in one item" if n >= 7 else f"Asks for '{EVERYTHING.search(text)[0]}'") + ": hard to do in 9 minutes, and hard to mark.", "fixed": False})
        if VAGUE.search(text) and len(_toks(text)) <= 5:
            flags.append({"item": text, "rule": "vague", "why": "Too vague to mark: what, specifically, earns it?", "fixed": False})
    if kind == "comms" and knowledge > 3:
        flags.append({"item": "", "rule": "knowledge_count", "why": f"{knowledge} knowledge items in a communication station (at most three).", "fixed": False})
    return {"scheme": out, "flags": flags}


RULES = {"duplicate": "Exact duplicate (removed)", "physical": "Physical action in a spoken station", "near_duplicate": "Near-duplicate",
         "restates_core": "Restates a core item", "knowledge_weight": "Knowledge over-weighted (comms)",
         "knowledge_count": "Too many knowledge items (comms)", "too_much": "Too much for 9 minutes", "vague": "Vague"}


def _lint_existing(data_root):
    """Every kept case on this node, linted: `python -m finalist_node.moderation lint`."""
    from collections import Counter
    n, per, cases = 0, Counter(), 0
    files = list(Path(data_root).glob("users/*/sims/*.json")) + list(Path(data_root).glob("users/*/peer/*.json"))
    owner = Path(config.load()["owner_data"]).expanduser()
    files += list(owner.glob("sims/*.json")) + list(owner.glob("peer/*.json"))
    for f in sorted(set(files)):
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        c = s.get("case") or {}
        if not c.get("mark_scheme"):
            continue
        cases += 1
        r = lint(c["mark_scheme"], s.get("kind"), spoken="peer" not in f.parts)
        for x in r["flags"]:
            n += 1
            per[x["rule"]] += 1
            print(f"{f.stem[:44]:44} {x['rule']:16} {'FIXED ' if x['fixed'] else ''}{x['item'][:90]}")
    print(f"\n{cases} cases, {n} flags ({n / (cases or 1):.1f} a case): " + ", ".join(f"{k} {v}" for k, v in per.most_common()))


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["lint"]:
        _lint_existing(Path(config.load()["data"]).expanduser())
    elif sys.argv[1:2] == ["add"] and len(sys.argv) > 2:      # make an already-moderated run a benchmark
        f = Path(sys.argv[2]).expanduser()
        b = add_benchmark(json.loads(f.read_text()), f.stem)
        print(f"benchmark {b['id']}: {b['title']}, agreed {b['grade']} (examiner {b.get('examiner_grade')}), {len(b['excerpt'])} chars of transcript")
    else:
        print("python -m finalist_node.moderation lint | add <run.json>")
