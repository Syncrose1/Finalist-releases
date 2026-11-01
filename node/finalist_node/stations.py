"""Patient Claude and Examiner Claude: a history or communication station run
against the Claude Code already signed in on this machine (`claude -p`, the
subscription, no API key), so there is no inference stack to keep.

One station is a "sim". Starting one asks Claude for a fresh case written to
the station's learning points: the candidate's brief, a hidden script for the
patient, the examiner's questions and a weighted mark scheme. The browser only
ever sees the brief and the conversation; the script stays here until marking.
Each turn replays the case and transcript to a stateless call, so nothing piles
up in Claude Code's session history. Finished sims are kept in DATA/sims.

Claude runs with every tool switched off, no MCP servers, no settings files
and in an empty folder, so it can only talk.
"""

import json
import random
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

from . import body_map

KINDS = ("history", "comms", "exam")
CLAUDE = shutil.which("claude") or "claude"      # its full path: on Windows it is claude.cmd
CWD = Path(tempfile.gettempdir()) / "revise-sim"      # empty, outside home: no CLAUDE.md for Claude to find (any OS)
SIMS, LOCK = {}, threading.Lock()


def available():
    return bool(shutil.which("claude"))


def _args(model, system, stream, effort=None, schema=None):
    a = [CLAUDE, "-p", "--model", model, *(["--effort", effort] if effort else []), "--tools", "", "--strict-mcp-config", "--setting-sources", "",
         "--no-session-persistence", "--system-prompt", system, *(["--json-schema", json.dumps(schema)] if schema else [])]
    return a + (["--output-format", "stream-json", "--verbose", "--include-partial-messages"] if stream
                else ["--output-format", "json"])


class Live:
    """One Claude process kept open for a role (the patient, then the examiner) for the whole station.
    Each turn sends only the new words, so there's no start-up per reply and the conversation stays in
    Claude's own context: measured 27 Oct, first words in 1.2-1.4 s against 1.4-2.2 s for a fresh
    `claude -p` each turn, and whole replies in about 2 s against 3.1-3.9 s."""

    def __init__(self, model, system):
        CWD.mkdir(parents=True, exist_ok=True)
        self.p = subprocess.Popen(
            [CLAUDE, "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
             "--system-prompt", system, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--include-partial-messages"],
            cwd=CWD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.lock = threading.Lock()

    def turn(self, text, on_text, timeout=120):
        """Sends the words in; streams the reply out; returns it. Raises if the process has gone."""
        with self.lock:
            if self.p.poll() is not None:
                raise RuntimeError("Claude's process has ended")
            self.p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self.p.stdin.flush()
            killer = threading.Timer(timeout, self.p.kill)
            killer.start()
            out = ""
            try:
                for line in self.p.stdout:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    e = d.get("event") or {}
                    if d.get("type") == "stream_event" and e.get("type") == "content_block_delta":
                        t = (e.get("delta") or {}).get("text") or ""
                        if t:
                            out += t
                            on_text(t)
                    elif d.get("type") == "result":
                        if d.get("is_error"):
                            raise RuntimeError(str(d.get("result") or "Claude couldn't reply"))
                        return out or (d.get("result") or "")
                raise RuntimeError("Claude's process ended mid-reply")
            finally:
                killer.cancel()

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except Exception:
            pass


LIVE = {}       # sim id -> {"patient": Live, "examiner": Live}; kept out of the sim, which is saved as JSON


def _close_live(sim_id, role=None):
    with LOCK:
        roles = LIVE.get(sim_id) or {}
        for r in ([role] if role else list(roles)):
            x = roles.pop(r, None)
            if x:
                x.close()
        if not roles:
            LIVE.pop(sim_id, None)


def ask(model, system, prompt, on_text=None, timeout=150, effort=None, schema=None):
    """One call to Claude. With on_text, the reply streams to it as it's written.
    With a schema, Claude Code holds the reply to it and the parsed object comes
    back (it still streams, as the JSON being written). Otherwise the text."""
    CWD.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(_args(model, system, bool(on_text), effort, schema), cwd=CWD, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        if not on_text:
            result = json.loads(proc.stdout.read() or "{}")
            text = result.get("result") or ""
        else:
            text, result = "", None
            for line in proc.stdout:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                e = d.get("event") or {}
                if d.get("type") == "stream_event" and e.get("type") == "content_block_delta":
                    delta = e.get("delta") or {}
                    t = delta.get("text") or delta.get("partial_json") or ""
                    if t:
                        text += t
                        on_text(t)
                elif d.get("type") == "result":
                    result = d
        if not result or result.get("is_error") or result.get("subtype", "success") != "success":
            raise RuntimeError((result or {}).get("result") or proc.stderr.read()[-400:] or "Claude didn't answer")
        if schema:
            if isinstance(result.get("structured_output"), dict):
                return result["structured_output"]
            return _json(result.get("result") or text)
        return result.get("result") or text
    finally:
        killer.cancel()
        proc.wait()


def _json(text):
    """The first whole JSON object in Claude's reply: anything it adds after
    (a note, a second object, a fence) is ignored."""
    t = text.strip()
    for start in (i for i, ch in enumerate(t) if ch == "{"):
        try:
            obj, _ = json.JSONDecoder().raw_decode(t, start)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("Claude's reply wasn't the JSON asked for. Try again.")


# ---- what Claude must return (Claude Code validates against these) -------------------------------

_str, _int = {"type": "string"}, {"type": "integer"}
_strs = {"type": "array", "items": _str}
_obj = lambda props, req=None: {"type": "object", "properties": props, "required": req or list(props)}
DOMAINS = ["data", "reasoning", "communication", "professionalism", "safety"]
CASE_SCHEMA = _obj({
    "brief": _str,
    "person": _obj({"name": _str, "age": _int, "sex": {"type": "string", "enum": ["female", "male"]}, "who": _str, "manner": _str}),
    "opening": _str, "script": _str, "cue": _str, "diagnosis": _str,
    "critical": _strs, "examiner_questions": _strs,
    "mark_scheme": {"type": "array", "items": _obj({"item": _str, "domain": {"type": "string", "enum": DOMAINS},
                                                   "weight": {"type": "integer", "minimum": 1, "maximum": 3}})},
})
MARK_SCHEMA = _obj({
    "grade": {"type": "string", "enum": list("ABCDEF")},
    "verdict": _str,
    "items": {"type": "array", "items": _obj({"item": _str, "weight": _int,
                                             "score": {"type": "integer", "minimum": 0, "maximum": 2}, "note": _str})},
    "critical": {"type": "array", "items": _obj({"item": _str, "met": {"type": "boolean"}, "note": _str})},
    "consultation": _obj({"mark": {"type": "integer", "minimum": 1, "maximum": 5}, "note": _str}),
    "concern": _str,
    "domains": {"type": "array", "items": _obj({"key": {"type": "string", "enum": DOMAINS}, "comment": _str})},
    "missed": _strs, "next_time": _strs, "model_lines": _strs,
})
PATIENT_SCHEMA = _obj({"mark": {"type": "integer", "minimum": 1, "maximum": 3}, "comment": _str})


# ---- the case ------------------------------------------------------------------------------------

CASE_SYSTEM = """You write OSCE stations for Southampton's final-year CPSA (the UK medical finals clinical exam).
Format: 2 minutes reading a word-limited brief outside, 9 minutes inside with a simulated patient, a whistle at 8 minutes when the examiner may ask questions. Stations are marked on a weighted mark scheme across five domains (data gathering; clinical reasoning and management; communication; professional behaviour; patient safety) plus the examiner's overall A-F grade.
You write realistic, UK-NHS-typical cases at the level of a final-year student about to be an FY1. Reply with JSON only."""

CASE_PROMPT = """Write one fresh case for this station.

STATION: {title} ({kind})
WHAT IT TESTS (the student's revision notes for it):
{notes}

{variety}
Return exactly this JSON:
{{
  "brief": "The candidate's instructions as printed on the door: setting, who they're seeing and why, the task, and what the examiner will ask at the end. Second person. Under 90 words, or under 160 when there is information to give (see below).",
  "person": {{"name": "", "age": 0, "sex": "female|male (of the person in the room: it picks their voice)", "who": "the patient, or e.g. 'the patient's daughter'", "manner": "how they come across, in a line"}},
  "opening": "What they say first if asked an open question, in their own words (1-2 sentences): the main complaint and how they feel about it, never the facts that clinch the diagnosis (a recent flight, a swollen leg, the exact radiation): those come only when asked.",
  "script": "Everything the actor knows: the full story with times and details, past history, drugs and allergies, family and social history (living, occupation, substances, travel), ideas, concerns and expectations, and what they only say if asked or if the student picks up a cue. 150-300 words.",
  "cue": "One emotional or verbal cue the actor drops once, early, that a good student explores. It points at the one concern in the script, which the patient tells straight away to any ordinary ICE question ('What's worrying you?'): realistic for a 9-minute station, never layered or hidden deeper than that.",
  "diagnosis": "The working diagnosis or the point of the station, plus the key differentials.",
  "critical": ["Usually none; at most one pass/fail item, the single miss in this case an examiner would call dangerous (a red flag left unscreened that changes the urgency, an allergy before a drug). Patient safety only: something whose miss would plausibly lead to harm in THIS case. Never a communication, rapport or ICE item (those are checklist marks, however important) (e.g. suicidal intent in a low mood history; allergies when a drug is about to be started or the story involves a reaction; travel when the fever or the clot could come from it); else empty"],
  "examiner_questions": ["2-3 questions the examiner asks at the whistle, e.g. summary, differential, next investigations, management"],
  "mark_scheme": [{{"item": "a specific, observable thing the candidate should do", "domain": "data|reasoning|communication|professionalism|safety", "weight": 1}}]
}}
If the task is to give the patient information (breaking news, explaining results, a diagnosis, a procedure or a medicine), the brief must hand the candidate everything they are expected to give, exactly as a real door note does: the result or report itself, with its figures or wording (e.g. "The biopsy report: moderately differentiated adenocarcinoma of the sigmoid colon. The staging CT shows two liver metastases."), the diagnosis, and the plan already agreed (the MDT date, the drug and dose, the follow-up). Never write a brief that says "discuss the results" without giving the results: the candidate can't invent them, and the patient's script must match them.
The mark scheme must start with these core items, exactly as written, in this order, then add 10-16 items specific to this case (the key questions or explanations, the red flags, the reasoning). Weights 1-3 by clinical importance (a weight multiplies the item's 0/1/2); communication items 1-2 at most, since ICE already has its own core item. Keep it to what a well-prepared final-year student could cover in 9 spoken minutes.
CORE:
{core}"""

# Narrated examinations (1 Nov 2026, agreed with Raahat): the candidate says each step as they do it, the
# patient reacts, and the examiner gives a finding only when the step that reveals it is performed. The
# case keeps the same fields, read differently: "script" holds the findings step by step, "cue" how the
# patient reacts (pain, discomfort), "opening" how they greet the candidate.
CASE_PROMPT_EXAM = """Write one fresh case for this NARRATED physical examination station: the candidate examines a simulated patient and says aloud each step as they do it; the examiner tells them what each step finds.

STATION: {title} (examination)
WHAT IT TESTS (the student's revision notes, including the routine):
{notes}

{variety}
Return exactly this JSON:
{{
  "brief": "The door note: setting, who the patient is and why they're here (one line of history), and the task (e.g. 'Examine Mr X's cardiovascular system. Present your findings to the examiner.'). Second person. Under 70 words. Never give away the findings.",
  "person": {{"name": "", "age": 0, "sex": "female|male", "who": "the patient", "manner": "how they come across, in a line"}},
  "opening": "How the patient greets the candidate and anything they say unprompted (one line).",
  "script": "THE FINDINGS, step by step, following the routine in the notes, section by section: for each step what the candidate finds, specific and quantified as an examiner would state it (e.g. 'Hands: clubbing; no splinter haemorrhages; capillary refill 2 s. Radial pulse 88, irregularly irregular; no radio-radial delay.'). Every step not abnormal is stated as normal. Write the patient a real finals station would have: a stable volunteer with a chronic condition, showing typically 3-6 positive findings that fit one coherent diagnosis, the discriminating ones that let a good candidate name it, at finals level. Never stack every sign in the textbook (a real patient has some, not all); no sign the station couldn't show without special equipment (e.g. Kayser-Fleischer rings need a slit lamp). 150-350 words.",
  "cue": "How the patient reacts during the examination: where it hurts and how they show it, anything they need (e.g. 'Winces and guards on palpation of the right upper quadrant; breathless lying flat, so prefers to sit up at 45°'). One or two lines.",
  "diagnosis": "The unifying diagnosis the findings point to, plus the key differentials.",
  "critical": ["Usually none; at most one pass/fail item: a patient-safety miss in THIS case (e.g. not asking about pain before palpating a tender abdomen); else empty"],
  "examiner_questions": ["'Please present your findings.' first, then 1-2 questions: the most likely diagnosis and differentials, the investigations, or the management"],
  "mark_scheme": [{{"item": "a specific, observable thing the candidate should do or say", "domain": "data|reasoning|communication|professionalism|safety", "weight": 1}}]
}}
The mark scheme must start with these core items, exactly as written, in this order, then add 8-14 items specific to this examination and case (the steps of the routine that matter, and eliciting and correctly naming the key signs). Weights 1-3.
CORE:
{core}"""

TWISTS = ["an older patient with several conditions", "a young adult", "a working parent short on time",
          "a patient who is anxious", "a patient who is stoical and plays things down", "a patient who has looked things up online",
          "a patient with a hidden worry they won't raise unless asked", "a patient for whom English is a second language (but fluent enough)",
          "a common, textbook presentation", "a presentation where a red flag must not be missed"]


# The mark sheet, shaped like the ones examiners fill in on iPads in risr/assess
# (Southampton's ACCs are on risr/advance, so its OSCEs very likely are too).
# Southampton doesn't publish its mark schemes (exam security), so the items are
# the standard ones; the structure follows risr/assess's documentation and the
# examiner guides Glasgow and Edinburgh publish:
#   checklist  each item 0 / 1 / 2 (not done, partly, done) times its weight
#   consultation skills  the examiner's 1-5, Glasgow's descriptors
#   the patient's own mark  1-3, given by the simulated patient
#   pass/fail items  a miss fails the station whatever the score (risr's critical stop point)
#   overall grade  A-F, judged separately from the score (Southampton's scale)
# The core is the standard content every OSCE mark sheet looks for, in examiners' words. Raahat's
# mnemonics (WIND, LOST, ETDSN) are their own way of remembering it, not what's marked: travel,
# for one, only earns a mark where the case makes it relevant (Claude adds it as a case item).
CORE = {
    "history": [
        ("Introduces self: name and role", "communication", 1),
        ("Confirms the patient's name and date of birth", "safety", 1), ("Explains the purpose and gains consent", "professionalism", 1),
        ("Opens with an open question and lets the patient talk", "communication", 1),
        ("Takes a drug history and asks about allergies", "safety", 2),
        ("Social history: smoking, alcohol, occupation, home and day-to-day function", "data", 1),
        ("Explores ideas, concerns and expectations (ICE) and responds to them", "communication", 2),
        ("Summarises back to the patient and checks nothing is missed", "communication", 1),
        ("Thanks the patient and explains the next steps", "professionalism", 1),
        ("Presents a concise summary and a prioritised differential to the examiner", "reasoning", 2),
    ],
    "comms": [
        ("Introduces self: name and role", "communication", 1),
        ("Confirms the patient's name and date of birth", "safety", 1), ("Explains the purpose and gains consent", "professionalism", 1),
        ("Finds out what they already know and want to know", "communication", 2),
        ("Explores ideas, concerns and expectations (ICE) and responds to them", "communication", 2),
        ("Explains in chunks, without jargon, checking understanding", "communication", 2),
        ("Acknowledges emotion and responds with empathy", "professionalism", 1),
        ("Agrees a plan together and safety-nets (what to do, when to come back)", "safety", 2),
        ("Offers written information or support and thanks them", "professionalism", 1),
    ],
}
CORE["exam"] = [
    ("Introduces self, confirms the patient's name and date of birth, explains and gains consent", "professionalism", 1),
    ("Washes hands (says so)", "safety", 1),
    ("Positions and exposes the patient appropriately, offering a chaperone where needed", "professionalism", 1),
    ("Asks about pain before examining", "safety", 1),
    ("General inspection from the end of the bed, including around the bed", "data", 1),
    ("A systematic routine in a logical order", "data", 2),
    ("Thanks the patient and offers to help them dress", "professionalism", 1),
    ("Presents the findings concisely: the key positives and relevant negatives", "reasoning", 2),
    ("Offers to complete the examination with the right further examinations and investigations", "reasoning", 1),
    ("Gives a sensible most likely diagnosis and differentials", "reasoning", 2),
]
# A miss here fails the station whatever the score: patient safety only, never communication. None are fixed: allergies are always on the
# checklist, and become pass/fail only where the case makes them matter, as travel does (Raahat,
# 25 Oct). The case names its own, and the examiner can judge one the case didn't foresee.
CRITICAL = {"history": [], "comms": [], "exam": []}
CONSULTATION = ["Balance of open, closed and reflective questions", "Structure: an effective, organised interview",
                "Understandable language, no jargon", "Recognises and responds to cues",
                "Rapport: empathy, invites the patient's contribution, collaborates"]
CONSULTATION_SCALE = ["Well below the expected standard", "Borderline", "Satisfactory", "Good", "Excellent"]
PATIENT_SCALE = ["Below expectations", "Satisfactory", "Highly satisfactory"]


def _notes(entry):
    lines = []
    if entry.get("summary"):
        lines.append(entry["summary"])
    for s in entry.get("sections") or []:
        lines.append(f"- {s['title']}: " + "; ".join(s["items"]))
    if (entry.get("pairs") or {}).get("rows"):
        lines.append(f"- {entry['pairs']['title']}: " + "; ".join(f"{a} = {b}" for a, b in entry["pairs"]["rows"]))
    for k in entry.get("knowledge") or []:
        lines.append(f"- What to know, {k['topic']}: " + "; ".join(k["points"]))
    for k in ("redflags", "tips", "pitfalls"):
        if entry.get(k):
            lines.append(f"- {k}: " + "; ".join(entry[k]))
    return "\n".join(lines).replace("**", "")[:9000]


def _core(kind):
    return "\n".join(json.dumps({"item": i, "domain": d, "weight": w}) for i, d, w in CORE[kind])


def _past(data, station_id):
    d = data / "sims"
    seen = []
    for f in sorted(d.glob(f"*-{station_id}-*.json"))[-6:] if d.is_dir() else []:
        try:
            seen.append(json.loads(f.read_text())["case"]["diagnosis"])
        except (OSError, ValueError, KeyError):
            pass
    return seen


def _field(text, key):
    """A string field from JSON still being written, once its closing quote is in."""
    m = re.search(r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"' % key, text)
    return json.loads('"%s"' % m[1]) if m else None


def _streaming_fields(keys, emit):
    """An on_text that emits each of keys as soon as it's complete."""
    buf, sent = [""], set()

    def on_text(t):
        buf[0] += t
        for k in keys:
            if k not in sent:
                v = _field(buf[0], k)
                if v is not None:
                    sent.add(k)
                    emit({k: v})
    return on_text


def start(model, data, station, entry, emit, voice=False):
    """Writes the case, streaming the brief out as soon as it's written so the
    reading time can start while Claude finishes the patient's script."""
    sid = secrets.token_urlsafe(9)
    sim = {"id": sid, "station": station["id"], "title": station["title"], "kind": station["kind"], "model": model,
           "case": None, "turns": [], "phase": "patient", "started": time.time(), "ready": threading.Event(),
           "primed": primed(data, station["id"]), "voice": bool(voice), "vocab": list(station.get("vocab") or [])[:40],
           # where the run is kept: written after every exchange, so a lost page or an expired
           # link leaves a transcript that can still be read and marked (27 Oct 2026: a Breaking
           # Bad News run was lost when a share link was renewed mid-station)
           "file": str(data / "sims" / f"{date.today().isoformat()}-{station['id']}-{sid}.json")}
    with LOCK:
        SIMS[sid] = sim
    emit({"sim": sid})
    seen = _past(data, station["id"])
    variety = f"Make the patient {random.choice(TWISTS)}."
    if seen:
        variety += " Don't repeat these earlier cases for this station: " + " | ".join(seen)
    if station["kind"] == "exam":
        prompt = CASE_PROMPT_EXAM.format(title=station["title"], notes=_notes(entry), variety=variety, core=_core("exam"))
    else:
        prompt = CASE_PROMPT.format(title=station["title"], kind=station["kind"], notes=_notes(entry),
                                    variety=variety, core=_core(station["kind"]))
    shown = {}

    def keep(o):                     # what the candidate has already seen
        shown.update(o)
        emit(o)
    try:
        try:
            sim["case"] = ask(model, CASE_SYSTEM, prompt, on_text=_streaming_fields(["brief", "name"], keep),
                              effort="low", schema=CASE_SCHEMA)
        except (ValueError, RuntimeError):
            # Once more, quietly. The brief may already be on screen with the clock running,
            # so the second case keeps it (and the patient's name) word for word.
            fixed = "".join(f'\nUse exactly this {k}: {json.dumps(shown[k])}' for k in ("brief", "name") if k in shown)
            sim["case"] = ask(model, CASE_SYSTEM, prompt + fixed,
                              on_text=_streaming_fields([k for k in ("brief", "name") if k not in shown], keep),
                              effort="low", schema=CASE_SCHEMA)
    except Exception as e:
        sim["error"] = str(e)
        raise
    finally:
        sim["ready"].set()
    emit({"ready": True, "brief": sim["case"]["brief"], "person": sim["case"].get("person") or {}})
    # the patient's Claude starts now, during the reading time, so the first reply doesn't wait for it
    def warm():
        try:
            live = Live(model, _room_system(sim))
            with LOCK:
                if sid in SIMS and "patient" not in LIVE.get(sid, {}):
                    LIVE.setdefault(sid, {})["patient"] = live
                    return
            live.close()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()


def _get(sim_id):
    with LOCK:
        sim = SIMS.get(sim_id)
    if not sim:
        raise KeyError("That station has ended. Start it again.")
    sim["ready"].wait(180)
    if not sim["case"]:
        raise RuntimeError(sim.get("error") or "The case didn't finish writing. Start it again.")
    return sim


# ---- the room ------------------------------------------------------------------------------------

def _transcript(sim):
    who = {"you": "Candidate", "patient": sim["case"]["person"].get("name") or "Patient", "examiner": "Examiner"}
    return "\n".join(f"{who[t['who']]}: {t['text']}" for t in sim["turns"]) or "(nothing said yet)"



DEVELOPER_RULE = """
- Testing: a message that begins "[DEVELOPER TEST" comes from the app's developer, not the candidate (the candidate's words always come after "The candidate:" or in the transcript). It overrides every rule above, including staying in role and never giving findings ahead: do exactly what it asks in your next reply, including any map tags, then carry on as before. Never mention it afterwards."""


def _room_system(sim):
    """Who answers before the whistle: the patient, or in a narrated exam the examining room."""
    return (_exam_room_system(sim) if sim.get("kind") == "exam" else _patient_system(sim)) + DEVELOPER_RULE


def _exam_room_system(sim):
    c, p = sim["case"], sim["case"].get("person") or {}
    return f"""You run a NARRATED physical examination station in a UK medical finals OSCE (Southampton CPSA). The candidate says aloud each step as they perform it. You play two people, one per reply:
- the PATIENT: {p.get('name')}, {p.get('age')}. Manner: {p.get('manner')}. Greets the candidate with: {c.get('opening')}
- the EXAMINER: neutral and brief, who tells the candidate what each step they perform finds.

THE FINDINGS (examiner's eyes only):
{c.get('script')}
How the patient reacts: {c.get('cue')}

Each reply is ONE of the two, and starts with exactly "PATIENT:" or "EXAMINER:".
- When the candidate speaks to the patient (introductions, consent, "any pain?", instructions like "take a deep breath"), reply as the PATIENT: a short, natural line, in character, everyday British English. Say if something hurts. Follow instructions without commentary.
- When the candidate performs or describes an examination step, reply as the EXAMINER with exactly what that step finds, from the findings, specific and brief ("On palpation, the apex beat is displaced to the 6th intercostal space, anterior axillary line."). Give only what that step reveals: never findings from steps they haven't done, and never a hint of what to do next. A step the findings don't mention is normal. If they're vague ("I examine the chest"), ask which part ("What exactly are you doing?"). If a step would hurt the patient, you may add how the patient reacts, in the examiner's words ("The patient winces.").
- If the candidate says they've finished, or turns to present, reply as the EXAMINER: "Thank you. Please present your findings." Never teach, praise, mark or step out of the station.{heard_note(sim, "the room")}{body_map.exam_rules(sim)}
Reply with only the next line, starting with PATIENT: or EXAMINER:."""


def _patient_system(sim):
    c, p = sim["case"], sim["case"].get("person") or {}
    return f"""You are a simulated patient (an actor) in a UK medical finals OSCE station. Stay in character the whole time.
You are {p.get('name')}, {p.get('age')}, {p.get('who')}. Manner: {p.get('manner')}.
Your opening words, if asked an open question: {c.get('opening')}
Your script:
{c.get('script')}
A cue to drop naturally early on: {c.get('cue')}

How to play it:
- Speak as a lay person in everyday British English. No medical jargon unless the script gives you a reason to know it; if the student uses jargon, look puzzled or ask what it means.
- Answer only what you're asked. Don't volunteer the rest of the script: a good student has to ask. Open questions get fuller answers, closed questions get short ones.
- Stick to the script. If they ask about something it doesn't mention, the answer is no, or normal: never invent a symptom (a made-up symptom sends a student after the wrong diagnosis). If it says you don't have something, you don't.
- Keep to what you've been asked even when they explain things: don't jump ahead and offer the facts that clinch the diagnosis (the flight, the swollen leg) because they mentioned a possible cause. They still have to ask.
- Your worry is real but not a puzzle: drop the cue once, and tell your concern plainly the moment they ask what you're worried about, what you think it is, or pick up the cue. Don't make them dig past jokes or deflection.
{_manner_rule(sim)}
- React like a person: if they're kind, warm up; if they rush or ignore your worry, become a little less forthcoming.
- Never coach, never mark, never step out of role, never mention the script or the exam. If the student speaks to the examiner, stay quiet: reply with nothing but "*waits*".{body_map.patient_rules(sim)}
Reply with only your next words."""


def heard_note(sim, who):
    """Spoken stations reach Claude through speech recognition, which is general English and can mishear
    medical terms (31 Oct 2026: the ear can't be steered with hotwords, so the listener is). The
    station's own drill terms say what the student is likely to have said."""
    if not sim.get("voice"):
        return ""
    terms = "; ".join(sim.get("vocab") or [])
    return (f"\n- The candidate is speaking, and their words reach {who} through speech recognition, which can mishear medical terms"
            + (f" (this station's include: {terms})" if terms else "")
            + ". If a word looks wrong, take what they most likely said, as someone in the room would have heard it; never remark on odd wording.")


def _manner_rule(sim):
    if sim.get("voice"):
        # (Sound tags like [Sigh] made the voice model fail to stop: 28 Oct. Words only, with feeling in the words.)
        return ("- You are speaking out loud, and your words are turned into speech: write only what you say. No stage directions, no *actions*, "
                "no (brackets), no [tags] (the body map's <map> tags are the one exception: they're never spoken). Keep replies short: usually 1-3 sentences. Let feeling show in the words themselves "
                "(\"Oh, I don't know, I'm just so tired of it.\"). Write numbers and doses the way you'd say them."
                + heard_note(sim, "you"))
    return "- Keep replies short and spoken: usually 1-3 sentences. Put body language in *asterisks* if it matters (e.g. *looks down*)."


def _examiner_system(sim):
    c = sim["case"]
    return f"""You are the examiner in a UK medical finals OSCE station (Southampton CPSA). Brief, neutral and professional, as a real examiner is: no teaching, no hints, no praise or criticism during the station.
The station: {sim['title']}. The brief the candidate read: {c.get('brief')}
The case: {c.get('diagnosis')}
Your questions: {json.dumps(c.get('examiner_questions') or [])}
The whistle has gone. Ask your questions one at a time, each in a single short sentence, moving on once they've answered (don't argue with or correct an answer). When you've asked them all, or if time is clearly up, say "Thank you, that's the end of the station."
Reply with only what you say next.""" + ("\nYou're speaking out loud: words only, no stage directions." + heard_note(sim, "you") if sim.get("voice") else "")


def _persist(sim):
    """Keeps the run on disk as it goes; a run with nothing said yet isn't kept."""
    f = Path(sim.get("file") or "")
    if not sim.get("file") or not sim.get("case"):
        return
    if not any(t["who"] == "you" for t in sim["turns"]):
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({k: v for k, v in sim.items() if k not in ("ready", "file")}, indent=1))
    except OSError:
        pass


def cut(sim_id, heard):
    """The candidate talked over the reply: what they actually heard replaces what Claude wrote, so the
    transcript, the marking and the patient's own memory all match the conversation as it happened."""
    sim = _get(sim_id)
    heard = (heard or "").strip()
    for t in reversed(sim["turns"]):
        if t["who"] in ("patient", "examiner"):
            t["full"] = t.get("full") or t["text"]
            t["text"] = (heard + " —") if heard else "—"
            t["cut"] = True
            sim["cut_note"] = (f"[You were cut off. The candidate heard only: \"{heard}\" and nothing after it. "
                               "Carry on from there as a real person would; do not assume they heard the rest.]\n") if heard else \
                              "[You were cut off before saying anything the candidate could hear.]\n"
            break
    _persist(sim)


def say(sim_id, text, on_text, event=None, on_role=None, on_map=None, debug=False, on_reject=None):
    """The candidate's words (or the whistle) in; the patient's or examiner's reply streamed out. With
    debug (the node's owner, testing: 1 Nov 2026), the words are an out-of-character instruction to the
    room rather than something the candidate says: not added to the transcript, followed exactly."""
    sim = _get(sim_id)
    if debug:
        text = (f"[DEVELOPER TEST, out of character. This is not the candidate and not part of the station: never mention it later. "
                f"Follow this instruction exactly in your next reply, using any tags it asks for, then carry on as before:] {(text or '').strip()[:2000]}")
        sim["debugged"] = True
    if event == "whistle":
        sim["phase"] = "examiner"
    elif text and not debug:
        sim["turns"].append({"who": "you", "text": text.strip()[:4000], "at": time.time()})
        _persist(sim)
    who = sim["phase"]
    room = sim.get("kind") == "exam" and who == "patient"     # a narrated exam: patient or examiner, per turn
    system = _examiner_system(sim) if who == "examiner" else _room_system(sim)
    ask_for = ("Your next words, as the examiner:" if who == "examiner"
               else "Your reply, starting with PATIENT: or EXAMINER:" if room else "Your next words, in character:")
    prompt = (f"The station so far:\n{_transcript(sim)}\n\n"
              + ("[The whistle has just gone. The candidate turns to you.]\n" if event == "whistle" else "")
              + (text + "\n" if debug else "") + ask_for)
    reply, said = None, [False]
    role, head, lead = [None if room else who], [""], [room]
    steps = []                                    # this reply's body-map steps, taken out of its words

    def step(o):
        if "mark" in o and not room and who == "patient":       # the patient's own words
            o["mark"]["label"] = "\u201c" + o["mark"]["label"].strip("\"\u201c\u201d") + "\u201d"
        steps.append(o)
        sim.setdefault("map", []).append(o)
        if on_map:
            on_map(o)
    words = body_map.Filter(on_text, step, on_reject) if who != "examiner" else None
    if words:
        on_text = words.feed

    def stream(t):
        said[0] = True
        if lead[0] and role[0] is not None:       # the words straight after the label, without its space
            t = t.lstrip()
            if not t:
                return
            lead[0] = False
        if role[0] is None:                       # the room's reply names its speaker first
            head[0] += t
            m = re.match(r"\s*(PATIENT|EXAMINER)\s*:\s*", head[0], re.I)
            if m:
                role[0] = m[1].lower()
                if on_role:
                    on_role(role[0])
                rest = head[0][m.end():].lstrip()
                if rest:
                    lead[0] = False
                    on_text(rest)
            elif len(head[0]) > 14:               # no label: the examiner narrates
                role[0] = "examiner"
                if on_role:
                    on_role(role[0])
                lead[0] = False
                on_text(head[0].lstrip())
            return
        on_text(t)
    with LOCK:
        live = (LIVE.get(sim_id) or {}).get(who)
    try:
        if live is None:
            if who == "examiner":
                _close_live(sim_id, "patient")      # the patient has had their turn
            live = Live(sim["model"], system)
            with LOCK:
                LIVE.setdefault(sim_id, {})[who] = live
            sim.pop("cut_note", None)               # the transcript it gets already holds only what was heard
            reply = live.turn(prompt, stream)       # the first turn carries everything so far
        else:
            note = sim.pop("cut_note", "")
            reply = live.turn(f"{note}{text.strip() if debug else 'The candidate: ' + text.strip()}\n\n" + ask_for, stream)
    except Exception:
        _close_live(sim_id, who)
        if said[0]:
            raise
    if reply is None:                               # no live process: one call with the whole transcript, as before
        reply = ask(sim["model"], system, prompt, on_text=stream)
    if words:
        words.flush()
    reply = body_map.strip(reply)
    if room:
        reply = re.sub(r"^\s*(PATIENT|EXAMINER)\s*:\s*", "", reply, flags=re.I)
        who = role[0] or "examiner"
    sim["turns"].append({"who": who, "text": reply, "at": time.time(), **({"map": steps} if steps else {}), **({"debug": True} if debug else {})})
    _persist(sim)
    return who


MARK_SYSTEM = """You are an experienced examiner for Southampton's final-year CPSA, marking an OSCE station on the risr/assess iPad mark sheet from its transcript. Be fair, specific and honest: this student is practising before a mock, and inflated marks would hurt them. The station was typed, not spoken, so judge the content and the manner the words show, not typing speed, spelling or body language. Physical actions (hand hygiene, positioning, a chaperone) can't be typed: assume they were done and never mark them down. Mark each item from what was actually said: an item not attempted, incomplete or inadequate doesn't get the full mark. The overall grade is your own judgement of the performance, made independently: don't derive it from the item marks. But calibrate it as real UK finals examiners do (anchors from published examiner guidance and benchmarking studies):
- D, borderline pass: the just-acceptable day-one FY1. A systematic history that may lack fine detail and lean on rote questioning rather than responding to the patient; acceptable rapport; reaches a reasonable differential for a common presentation. Borderline candidates typically score about 50-60% of a checklist (one published borderline benchmark: 56%).
- C, pass: covers the main elements with some omissions, sensible reasoning, decent communication. Typically about 60-70%. Most competent candidates land here; a real student who scored 60% got a C.
- B, good: fluent and patient-centred, few omissions, a well-reasoned differential. Typically about 70-80% (a published "good" benchmark: 81%).
- A, excellent: polished, comprehensive within the time, responsive to cues throughout. Typically 80%+.
- E, borderline fail: misses enough that you can't be confident they're safe, or a muddled structure; typically 40-50%. F, clear fail: unsafe, or the task was not done; typically under 40%.
No one completes every item in 9 minutes, and examiners aren't looking for perfection. Don't grade more than one letter below what the score and these anchors suggest unless you name the reason in the verdict (a pass/fail miss, or a genuine safety concern). Reply with JSON only."""

MARK_PROMPT = """STATION: {title}
BRIEF: {brief}
THE CASE (hidden from the candidate): {diagnosis}
THE PATIENT'S SCRIPT: {script}
THE CUE: {cue}
CHECKLIST (score each 0 not done, 1 partly, 2 done): {scheme}
PASS/FAIL ITEMS (a miss fails the station): {critical}
As the examiner you may add one the case didn't list, if in how this consultation went a miss was genuinely unsafe (e.g. never asking about allergies when the plan was to start a drug). Use that sparingly, and only for patient safety: an ordinary omission, or a missed cue or ICE, is a lost checklist mark, not a fail.
Information the patient volunteered counts as gathered when the candidate acknowledges it and uses it (follows it up, or builds it into the summary or differential): credit the item, as UK OSCE examiners do. Only mark down an item the candidate never established at all. Don't penalise how the information surfaced, or the candidate sharing a possible diagnosis when the patient asked them directly.
If the transcript ends with words cut off by the bell, they were being said as time ran out: credit what they show.
CONSULTATION SKILLS (one mark 1-5: {cscale}), considering: {consultation}

TRANSCRIPT:
{transcript}

{note}Return exactly this JSON:
{{
  "grade": "A|B|C|D|E|F (A excellent, D borderline pass, E borderline fail, F clear fail)",
  "verdict": "One or two sentences, the overall impression, addressed to the candidate as 'you' (never 'he' or 'she').",
  "items": [{{"item": "each checklist item, in order, as written", "weight": 1, "score": 0, "note": "what they said or missed, briefly"}}],
  "critical": [{{"item": "each pass/fail item, plus any you added", "met": true, "note": "why it mattered here"}}],
  "consultation": {{"mark": 3, "note": "one line on why"}},
  "concern": "Anything dangerous or unprofessional outside the checklist that would worry you, or empty",
  "domains": [{{"key": "data|reasoning|communication|professionalism|safety", "comment": "one line"}}],
  "missed": ["what in the script they never found out, briefly"],
  "next_time": ["the three things that would most raise the mark, concrete"],
  "model_lines": ["2-3 lines a strong candidate might have said at the key moments"]
}}"""

PATIENT_MARK = """The station is over. Step out of the scene and, still as {name}, give your mark as the simulated patient, the way patients mark candidates in UK finals: how it felt to be on the other side of this consultation. Did you feel listened to and understood? Did they explain things so you could follow? Would you be happy to see this student again?
The transcript:
{transcript}

Refer to the candidate as "the student" or "they", never "he" or "she".
Reply with JSON only: {{"mark": 1, "comment": "one or two sentences in your own voice, first person"}} where mark is 1 ({s1}), 2 ({s2}) or 3 ({s3})."""


def _patient_mark(sim, out):
    try:
        c = sim["case"]
        name = (c.get("person") or {}).get("name") or "the patient"
        system = (f"You are the simulated patient who just played {name} in a UK finals OSCE station. "
                  f"Your character and story: {c.get('script')} Reply with JSON only.")
        out.update(ask(sim["model"], system, PATIENT_MARK.format(
            name=name, transcript=_transcript(sim), s1=PATIENT_SCALE[0], s2=PATIENT_SCALE[1], s3=PATIENT_SCALE[2]),
            effort="low", schema=PATIENT_SCHEMA))
    except Exception as e:
        out["error"] = str(e)


def _blank(sim, data, emit):
    """Nothing said: every item 0 and an F, without asking Claude."""
    c = sim["case"]
    items = [{"item": i.get("item"), "weight": max(1, int(i.get("weight") or 1)), "score": 0, "note": ""}
             for i in c.get("mark_scheme") or []]
    out = {"grade": "F", "verdict": "You didn't say anything in the station, so nothing could be marked.",
           "items": items, "critical": [], "consultation": {"mark": 1, "note": ""}, "concern": "",
           "domains": [], "missed": [], "next_time": [], "model_lines": []}
    emit({"grade": "F"})
    return _finish(sim, data, emit, out, {})


def mark(sim_id, data, emit, ended_early=False, draft=""):
    """Marks the transcript, streaming the grade and verdict first. The patient
    gives their own mark in a separate call, alongside, so it's independent.
    A draft is what was still being typed when the bell went: it counts."""
    sim = _get(sim_id)
    c = sim["case"]
    if draft.strip():
        sim["turns"].append({"who": "you", "text": draft.strip()[:4000] + " [cut off by the bell]", "at": time.time()})
    if not any(x["who"] == "you" for x in sim["turns"]):
        return _blank(sim, data, emit)
    patient = {}
    pt = threading.Thread(target=_patient_mark, args=(sim, patient), daemon=True)
    pt.start()
    note = "The candidate stopped the station early; mark what happened.\n" if ended_early else ""
    if sim.get("kind") == "exam":
        note += ("This was a NARRATED examination: the candidate said each step aloud as they performed it, and the examiner gave the findings. "
                 "For this station, ignore the rule about assuming physical actions were done: mark each step, including hand hygiene, positioning "
                 "and exposure, only if they said it. Mark the presentation on whether it states the findings they were actually given, accurately.\n")
    if sim.get("voice"):
        note += ("The candidate SPOKE this station and their words were transcribed by speech recognition: ignore mistranscribed words, "
                 "missing punctuation and odd spellings (a drug name heard as a near-miss is the drug name), and mark what they evidently said."
                 + (f" Terms this station's students commonly use: {'; '.join(sim.get('vocab') or [])}." if sim.get("vocab") else "") + "\n")
    critical = CRITICAL[sim["kind"]] + [x for x in c.get("critical") or [] if x]
    mark_prompt = MARK_PROMPT.format(
        title=sim["title"], brief=c.get("brief"), diagnosis=c.get("diagnosis"), script=c.get("script"),
        cue=c.get("cue"), scheme=json.dumps(c.get("mark_scheme") or []), critical=json.dumps(critical),
        cscale=", ".join(f"{i + 1} {s}" for i, s in enumerate(CONSULTATION_SCALE)),
        consultation="; ".join(CONSULTATION), transcript=_transcript(sim), note=note)
    try:
        out = ask(sim["model"], MARK_SYSTEM, mark_prompt, on_text=_streaming_fields(["grade", "verdict"], emit),
                  effort="medium", schema=MARK_SCHEMA)
    except (ValueError, RuntimeError):     # once more, quietly
        out = ask(sim["model"], MARK_SYSTEM, mark_prompt, effort="medium", schema=MARK_SCHEMA)
    pt.join(120)
    return _finish(sim, data, emit, out, patient)


def _finish(sim, data, emit, out, patient):
    _close_live(sim.get("id"))
    c = sim["case"]
    # The station score as risr/assess totals it: each item's 0-2 times its weight, plus
    # the consultation domain (1-5) and the patient's mark (1-3). The grade is separate.
    items = out.get("items") or []
    w = lambda i: max(1, int(i.get("weight") or 1))
    cm = min(5, max(1, int((out.get("consultation") or {}).get("mark") or 1)))
    pm = min(3, max(1, int(patient.get("mark") or 1))) if "mark" in patient else None
    said = any(x["who"] == "you" for x in sim["turns"])
    got = sum(w(i) * min(2, max(0, int(i.get("score") or 0))) for i in items) + (cm if said else 0) + (pm or 0)
    total = sum(2 * w(i) for i in items) + 5 + (3 if pm else 0)
    out["score"] = round(100 * got / (total or 1))
    out["points"] = [got, total]
    out["consultation"] = {**(out.get("consultation") or {}), "mark": cm, "label": CONSULTATION_SCALE[cm - 1]}
    out["patient"] = {**patient, "label": PATIENT_SCALE[pm - 1]} if pm else None
    out["failed_on"] = [x["item"] for x in out.get("critical") or [] if not x.get("met")]
    out["case"] = {"diagnosis": c.get("diagnosis"), "cue": c.get("cue"), "person": c.get("person")}
    out["primed"] = sim.get("primed")
    sim["feedback"] = out
    d = data / "sims"
    d.mkdir(parents=True, exist_ok=True)
    keep = {k: v for k, v in sim.items() if k not in ("ready", "file")}
    f = Path(sim.get("file") or d / f"{date.today().isoformat()}-{sim['station']}-{sim['id']}.json")
    if said:                            # a run with nothing said isn't worth keeping
        f.write_text(json.dumps(keep, indent=1))
    emit({"feedback": out})
    return out


# ---- past runs -----------------------------------------------------------------------------------

def runs(data, station_id):
    """A station's runs, newest first: the marked ones, and any left unmarked (the page was lost
    or the link ran out before the mark), which can be marked from the transcript later."""
    out = []
    for f in (data / "sims").glob(f"*-{station_id}-*.json") if (data / "sims").is_dir() else []:
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if s.get("station") != station_id:
            continue
        fb = s.get("feedback") or {}
        if not fb and s.get("id") in SIMS:      # still being run
            continue
        out.append({"run": f.stem, "at": s.get("started"), "grade": fb.get("grade"), "score": fb.get("score"),
                    "unmarked": not fb, "person": (s.get("case") or {}).get("person") or {}, "primed": s.get("primed")})
    return sorted(out, key=lambda r: r["at"] or 0, reverse=True)


def run(data, name):
    """One past run: the brief, the conversation and the mark sheet."""
    if not re.fullmatch(r"[\w-]+", name or ""):
        return None
    f = data / "sims" / f"{name}.json"
    if not f.is_file():
        return None
    s = json.loads(f.read_text())
    return {"title": s.get("title"), "at": s.get("started"), "brief": (s.get("case") or {}).get("brief"),
            "person": (s.get("case") or {}).get("person") or {},
            "turns": [{"who": t["who"], "text": t["text"]} for t in s.get("turns") or []], "feedback": s.get("feedback"),
            **({"map": s["map"]} if s.get("map") else {})}


# ---- the station review: your evaluation across runs, and what gets an A -------------------------

REVIEW_SCHEMA = _obj({
    "evaluation": _obj({"trend": _str, "strengths": _strs, "weaknesses": _strs, "improve": _strs}),
    "a_grade": _obj({"summary": _str, "must": _strs, "polish": _strs, "timing": _str, "lines": _strs}),
})

REVIEW_SYSTEM = """You are a senior OSCE examiner and coach for Southampton's final-year CPSA (UK finals: 2 minutes reading, 9 minutes in the station, a whistle at 8; marked on a weighted checklist, consultation skills and an A-F grade where D is the just-acceptable day-one FY1 and A is excellent). You write short, specific, practical coaching for one student on one station. Plain British English, no padding, no generic advice that would fit any station. Address the student as "you".
The marks in the runs may come from older, harsher marking; judge by today's rules, as UK examiners do: information the patient volunteered counts when the student picks it up and uses it; answering honestly when the patient asks directly what it might be is fine; physical actions (hand hygiene) aren't shown because the runs were typed, so never mention them; typing is slower than speaking, so running out of time is partly the medium. Don't repeat a criticism that breaks these rules. Reply with JSON only."""

REVIEW_PROMPT = """STATION: {title} ({kind})
THE STATION'S LEARNING POINTS:
{notes}

{runs}
Return:
- "evaluation": {evaluation}
- "a_grade": what separates an A from a C on THIS station, for a student who already knows the basics:
  "summary": one or two sentences;
  "must": the 5-8 things an A candidate always does here (the specific questions, explanations, red flags and reasoning, in the order they'd come){tailor};
  "polish": 3-5 things that lift a pass to an A (structure, signposting, cue handling, the presentation to the examiner);
  "timing": how an A candidate spends {time} here, in one line (e.g. "0-1 opening, 1-4 the complaint and killers, ...");
  "lines": 2-4 short lines an A candidate might actually say at the key moments."""


def _runs_digest(data, station_id, limit=6):
    """The marked runs of a station, oldest first, cut down to what a review needs."""
    out = []
    for r in reversed(runs(data, station_id)[:limit]):
        s = run(data, r["run"])
        fb = s["feedback"] or {}
        said = [t["text"] for t in s["turns"] if t["who"] == "you"]
        out.append({"date": date.fromtimestamp(r["at"] or 0).isoformat(), "case": (fb.get("case") or {}).get("diagnosis", "")[:300],
                    "grade": fb.get("grade"), "score": fb.get("score"), "verdict": fb.get("verdict"),
                    "items": [f"{i.get('score')}/2 x{i.get('weight')}: {i.get('item')}" for i in fb.get("items") or []],
                    "missed": fb.get("missed"), "next_time": fb.get("next_time"), "concern": fb.get("concern"),
                    "primed": (f"drilled {r['primed']['found']}/{r['primed']['total']} on the recall drill "
                               f"{r['primed']['minutes_before']} min before" if r.get("primed") else "not drilled in the 10 minutes before"),
                    "candidate_said": " | ".join(said)[:2500]})
    return out


def review(model, data, station, entry):
    """Writes (and keeps) the station's review: the evaluation needs marked runs, the A guide doesn't."""
    digest = _runs_digest(data, station["id"]) if station["kind"] in KINDS else []
    if digest:
        runs_text = f"THE STUDENT'S MARKED RUNS OF THIS STATION WITH SIMULATED PATIENTS (oldest first; typed, not spoken, so physical actions aren't shown):\n{json.dumps(digest, indent=1)}\n"
        evaluation = ('your evaluation across these runs. Each run says whether they did the recall drill (typing every screening '
                      'question and killer from memory) in the 10 minutes before. The drill is how you tell lasting knowledge from short-term '
                      'memory, never a step they owe: a good screen with no drill means they know it, so say nothing about the drill; a weak '
                      'screen or missed killers is when to recommend drilling this station; a good screen a few minutes after a drill may be '
                      'short-term memory, worth one neutral line in the trend and nothing more. "trend" (one line: how they\'re doing and whether it\'s improving), '
                      '"strengths" (2-4, specific, with evidence), "weaknesses" (2-4, the patterns that recur, most costly first), '
                      '"improve" (3-5 concrete fixes for the next run, most marks first)')
        tailor = ", flagging with (you) the ones this student has missed"
    else:
        runs_text, evaluation, tailor = "The student hasn't run this station with a simulated patient yet.\n", 'no runs yet, so an empty trend and empty lists', ""
    if station["kind"] == "cse":     # a written Clinical Summary Exam paper, timed, not an OSCE station
        when = f"the paper's {station.get('minutes') or 30} minutes (a written CSE paper: video or notes once, then the answer by hand)"
    else:
        when = "the 9 minutes"
    out = ask(model, REVIEW_SYSTEM, REVIEW_PROMPT.format(title=station["title"], kind=station["kind"], notes=_notes(entry),
                                                         runs=runs_text, evaluation=evaluation, tailor=tailor, time=when),
              effort="medium", schema=REVIEW_SCHEMA, timeout=200)
    if not digest:
        out["evaluation"] = None
    out.update({"station": station["id"], "runs": len(digest), "at": time.time()})
    d = data / "sims" / "review"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{station['id']}.json").write_text(json.dumps(out, indent=1))
    return out


def get_review(data, station_id, shared=None):
    """A profile's review of a station. A profile without one gets the shared A guide (one
    written before any runs, so it's about the station, not about someone)."""
    if not re.fullmatch(r"[\w-]+", station_id or ""):
        return None
    f = data / "sims" / "review" / f"{station_id}.json"
    if not f.is_file() and shared is not None:
        g = shared / "sims" / "review" / f"{station_id}.json"
        if g.is_file():
            out = json.loads(g.read_text())
            a = dict(out.get("a_grade") or {})
            if out.get("runs", 0):      # written against someone's runs: keep the station, drop the person
                untag = lambda s: re.sub(r"\s*\(you\b[^)]*\)", "", s)
                a["must"] = [untag(x) for x in a.get("must") or []]
            return {**out, "a_grade": a, "evaluation": None, "runs": 0, "stale": bool(runs(data, station_id))}
        return None
    if not f.is_file():
        return None
    out = json.loads(f.read_text())
    out["stale"] = station_id and len(runs(data, station_id)) > out.get("runs", 0)
    return out


def remark(data, name):
    """Marks a past run again under today's rules, keeping the first mark in the file."""
    if not re.fullmatch(r"[\w-]+", name or ""):
        raise KeyError("no such run")
    f = data / "sims" / f"{name}.json"
    s = json.loads(f.read_text())
    tmp = CWD / f"remark-{secrets.token_hex(4)}"
    live = {**s, "id": "remark-" + s["id"], "ready": threading.Event()}
    live["ready"].set()
    with LOCK:
        SIMS[live["id"]] = live
    try:
        out = mark(live["id"], tmp, lambda o: None)
    finally:
        with LOCK:
            SIMS.pop(live["id"], None)
        shutil.rmtree(tmp, ignore_errors=True)
    if s.get("feedback"):
        s.setdefault("first_feedback", s["feedback"])
        s["remarked"] = time.time()
    s["feedback"] = out
    f.write_text(json.dumps(s, indent=1))
    return out


# ---- recall drills: the fast, repeatable step between reading and a station ----------------------

PRIMED_WITHIN = 10 * 60      # a drill this recent counts as priming the station


def log_drill(data, station_id, found, total, missed, seconds, guesses=None):
    data.mkdir(parents=True, exist_ok=True)
    rec = {"station": station_id, "at": time.time(), "found": int(found), "total": int(total),
           "missed": [str(m)[:80] for m in (missed or [])][:60], "seconds": int(seconds or 0),
           "guesses": [{k: str(g.get(k, ""))[:80] for k in ("guess", "verdict", "item")} for g in (guesses or [])][:80]}
    with open(data / "drills.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def drills(data, station_id, limit=20):
    """This station's drill rounds, newest first."""
    f = data / "drills.jsonl"
    out = []
    if f.is_file():
        for line in f.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("station") == station_id:
                out.append(r)
    return sorted(out, key=lambda r: r["at"], reverse=True)[:limit]


def primed(data, station_id, now=None):
    """The best drill round in the last ten minutes, if there was one."""
    now = now or time.time()
    recent = [r for r in drills(data, station_id) if now - r["at"] <= PRIMED_WITHIN]
    if not recent:
        return None
    best = max(recent, key=lambda r: (r["found"] / max(1, r["total"]), r["at"]))
    return {"rounds": len(recent), "found": best["found"], "total": best["total"],
            "minutes_before": round((now - max(r["at"] for r in recent)) / 60, 1)}


# ---- checking the guesses the matcher turned down -------------------------------------------------

CHECK_SCHEMA = _obj({"results": {"type": "array", "items": _obj({
    "guess": _str, "verdict": {"type": "string", "enum": ["match", "valid", "wrong"]}, "item": _str, "note": _str})}})


def check_guesses(model, title, drill, guesses):
    """Claude's verdict on each guess the fuzzy matcher didn't accept: the same as an item on the
    list (a phrasing it missed), a fair screening question that isn't on the list, or wrong."""
    items = [f"{g['name']}: " + "; ".join(i["answer"] for i in g["items"]) for g in drill["groups"]]
    prompt = (f"HISTORY STATION: {title}\nTHE DRILL'S LIST (what a student should screen for, by group):\n" + "\n".join(items) +
              "\n\nThe student typed these from memory and the automatic matcher didn't accept them:\n" +
              "\n".join(f"- {g}" for g in guesses) +
              "\n\nFor each guess: \"match\" if it means one of the listed items (set \"item\" to that item's exact text, as listed), "
              "\"valid\" if it's a reasonable thing to screen for in this history that the list lacks (\"item\" empty), or \"wrong\" "
              "if it isn't relevant here or is too vague to count (\"item\" empty). \"note\": a few words, for valid and wrong only. "
              "Be fair but not generous: a vague word that could mean several things is wrong.")
    out = ask(model, "You check a UK final-year medical student's active-recall answers for OSCE history stations. Accurate and brief. Reply with JSON only.",
              prompt, effort="low", schema=CHECK_SCHEMA, timeout=90)
    return out.get("results") or []


def learn_aliases(data, station_id, matches):
    """Phrasings Claude matched to an item, kept so the matcher accepts them next time."""
    f = data / "drill_aliases.json"
    try:
        known = json.loads(f.read_text())
    except (OSError, ValueError):
        known = {}
    st = known.setdefault(station_id, {})
    for guess, item in matches:
        if guess and item and guess.lower() not in [a.lower() for a in st.get(item, [])]:
            st.setdefault(item, []).append(guess[:60])
    data.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(known, indent=1))


def drills_with_aliases(web, data):
    """The drill lists, with every phrasing Claude has matched added as an alias."""
    d = json.loads((web / "drills.json").read_text())
    try:
        known = json.loads((data / "drill_aliases.json").read_text())
    except (OSError, ValueError):
        known = {}
    for sid, items in known.items():
        for g in (d.get(sid) or {}).get("groups", []):
            for it in g["items"]:
                it["aliases"] = it["aliases"] + [a for a in items.get(it["answer"], []) if a not in it["aliases"]]
    return d
