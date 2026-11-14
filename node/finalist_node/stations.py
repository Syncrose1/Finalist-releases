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
from . import moderation

KINDS = ("history", "comms", "exam", "data")
# a data-interpretation station on a real film (14 Nov 2026): the circuit's station id -> the node's dataset
FILM_SETS = {"cxr": "cxr", "axr": "axr", "ct-head": "cthead", "msk-xr": "msk"}
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
        """Sends the words in (or a list of content blocks: a film and its words, 14 Nov 2026); streams the reply out;
        returns it. Raises if the process has gone."""
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
The mark scheme must start with these core items, exactly as written, in this order, then add 10-16 items specific to this case (the key questions or explanations, the red flags, the reasoning). Weights 1-3 (a weight multiplies the item's 0/1/2). In a history station weight by clinical importance, with communication items 1-2 at most, since ICE already has its own core item. In a COMMUNICATION station (breaking news, explaining, consent, an angry or worried person) the weighting is the other way round, as in real UK finals: the process carries the station, so the communication steps (setting, what they know and want to know, the warning shot, saying it plainly, silence, responding to emotion and the cue, honest framing without false promises, chunk-and-check, a shared plan and safety net) are weighted 2-3; specific clinical knowledge (exact prognosis figures, named tests, named side effects or referrals) is weight 1 each, at most three such items, domain 'reasoning', and the rest of the knowledge belongs in the examiner's questions. Write an honesty item as communication ('gives an honest, hopeful picture without promising'), not as a fact to recite. Keep it to what a well-prepared final-year student could cover in 9 spoken minutes.
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
    ("Thanks the patient, covers them, and gives them privacy to dress", "professionalism", 1),
    ("Presents the findings concisely: the key positives and relevant negatives", "reasoning", 2),
    ("Offers to complete the examination with the right further examinations and investigations", "reasoning", 1),
    ("Gives a sensible most likely diagnosis and differentials", "reasoning", 2),
]
# A miss here fails the station whatever the score: patient safety only, never communication. None are fixed: allergies are always on the
# checklist, and become pass/fail only where the case makes them matter, as travel does (Raahat,
# 25 Oct). The case names its own, and the examiner can judge one the case didn't foresee.
# Procedures and management (10 Nov 2026, peer-run stations only: Claude doesn't run these as a room). The standard
# content every procedure or management mark sheet looks for; the case adds the steps of this one.
CORE["procedure"] = [
    ("Introduces self, confirms the patient's identity (name, date of birth, wristband) and gains consent", "safety", 1),
    ("Explains the procedure and why it's needed, in plain words", "communication", 1),
    ("Asks about allergies and anything that rules out or changes the procedure", "safety", 1),
    ("Hand hygiene and appropriate PPE", "safety", 1),
    ("Prepares and checks the equipment (expiry dates, packaging intact)", "data", 1),
    ("Positions the patient comfortably and keeps their dignity", "professionalism", 1),
    ("Disposes of waste, and any sharps at the point of use, safely", "safety", 2),
    ("Explains the result or the aftercare and what happens next, and thanks the patient", "communication", 1),
    ("Documents the procedure (and labels any samples at the bedside)", "professionalism", 1),
]
CORE["management"] = [
    ("Introduces self, confirms the patient's identity and gains consent where possible", "professionalism", 1),
    ("Hand hygiene and appropriate PPE", "safety", 1),
    ("Gathers the information needed: the history, observations, charts and results", "data", 2),
    ("Uses a systematic approach (A to E when the patient is unwell)", "data", 2),
    ("Recognises how unwell the patient is and escalates early to a senior (SBAR)", "safety", 2),
    ("Gives clear, safe, prioritised management", "reasoning", 2),
    ("Reassesses after each intervention and plans monitoring", "safety", 1),
    ("Explains to the patient what is happening and checks their understanding", "communication", 1),
    ("Documents and hands over clearly", "professionalism", 1),
]
# Data interpretation on a real film (14 Nov 2026). Raahat: mark as these stations are really marked, not on his say-so.
# No UK school publishes its mark scheme for a film station, so the core is the consensus of UK undergraduate teaching,
# each item traceable: check the film is the right patient's and readable before reading it (RIPE: TalkOSCE's and the
# Queen's Belfast finals notes' method); read it in a fixed, systematic order (the RCR undergraduate curriculum's core
# competence; Oscemed: a systematic read earns marks even when the diagnosis isn't obvious); describe a finding before
# naming it, placing it by zone and side, because a plain film shows zones that don't match the lobes (an open
# undergraduate imaging text; Radiology Cafe: describe the zone when you can't be sure of the lobe); present in a
# structure, then give a likely diagnosis or differential and the next step (Oscemed). Marked as the other stations
# are, a weighted checklist plus a global grade: the evidence supports using both (Cunnington et al. 1996; a systematic
# review of checklists against global ratings).
CORE["data"] = [
    ("Confirms the film is the right patient's, the date and the type of film (or says they'd check them)", "safety", 1),
    ("Comments on whether the film is adequate before reading it (for a chest film: rotation, inspiration, projection, exposure)", "data", 1),
    ("Reads the film in a fixed, systematic order", "data", 2),
    ("Describes the abnormality before naming it: what it looks like, where (side and zone), and its features", "data", 2),
    ("Uses accurate descriptive terms (zones rather than lobes on a plain film; opacity or lucency before a diagnosis)", "communication", 1),
    ("Gives a sensible most likely diagnosis and differential", "reasoning", 2),
    ("Suggests appropriate next steps: urgency, further imaging or tests, management", "reasoning", 2),
    ("Presents in a clear, organised structure: film, findings, interpretation, plan", "communication", 1),
]
CRITICAL = {"history": [], "comms": [], "exam": [], "procedure": [], "management": [], "data": []}
CONSULTATION = ["Balance of open, closed and reflective questions", "Structure: an effective, organised interview",
                "Understandable language, no jargon", "Recognises and responds to cues",
                "Rapport: empathy, invites the patient's contribution, collaborates"]
# a film station's presentation to the examiner, in place of the consultation skills (14 Nov 2026)
DATA_PRESENTATION = ["A clear, organised structure: film, findings, interpretation, plan", "Precise, accurate descriptive terms",
                     "Concise and confident: commits to an interpretation", "Answers the examiner's questions directly"]
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


def _settle_scheme(case, station_id, kind, spoken):
    """A new case's mark scheme with the station's moderated overrides applied and the lint's safe fixes made; the
    flags are kept on the case (owner only: the case is hidden from the candidate; the debug panel shows it)."""
    try:
        scheme = moderation.apply_scheme(case.get("mark_scheme"), moderation.overrides(station_id))
        r = moderation.lint(scheme, kind, spoken=spoken)
        case["mark_scheme"] = r["scheme"]
        if spoken:
            case["lint"] = r["flags"]
    except Exception:            # a lint must never stop a station
        pass


def _past(data, station_id):
    seen = []
    files = [f for d in (data / "sims", data / "peer") if d.is_dir() for f in d.glob(f"*-{station_id}-*.json")]
    for f in sorted(files, key=lambda f: f.name)[-6:]:
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
    prompt += moderation.case_note(station["id"])      # what moderating real runs taught (11 Nov 2026)
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
        _settle_scheme(sim["case"], station["id"], station["kind"], spoken=True)
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


# ---- a data interpretation station on a real film (14 Nov 2026) -----------------------------------------------------
# Raahat: the examiner reads the film's label as the truth, and its own view of the image as weaker evidence ("underrate
# the visual encoder's accuracy somewhat, but not entirely"); no medical imaging model on people's machines. The film is
# a vetted dataset item: its label, any other findings and the radiologists' boxes come from the node's own manifest,
# never from the browser. The browser sends only which film, and the atlas's teaching for its finding.

FILM_NAME = {"cxr": "chest radiograph", "axr": "abdominal radiograph", "cthead": "non-contrast CT head", "msk": "radiograph"}
FILMS = {}      # sim id -> {"mime", "data"}: the image itself, kept out of the saved run


def film_facts(set_name, it, labels, box_space=1024):
    """The ground truth an examiner gets: the labelled finding, any others, and (from the radiologists' boxes, on a
    frontal chest film) the side, which the boxes give reliably, and the third of the film, which is only approximate."""
    facts = {"finding": it.get("answer") or labels.get(it.get("label"), it.get("label")), "label": it.get("label"),
             "others": [labels.get(x, x) for x in (it.get("also") or []) if x != it.get("label")]}
    boxes = [b for b in (it.get("boxes") or []) if b.get("finding") == it.get("label")]
    if set_name == "cxr" and boxes:
        sp = it.get("box_space") or box_space
        sides, thirds = set(), set()
        for b in boxes:
            x, y, w, h = b["box"]
            cx, cy = (x + w / 2) / sp, (y + h / 2) / sp
            # a frontal film is viewed as if facing the patient: the image's left is the patient's right
            sides.add("right" if cx < 0.45 else "left" if cx > 0.55 else "central")
            thirds.add("upper" if cy < 0.4 else "middle" if cy < 0.6 else "lower")
        facts["side"] = "both sides" if {"right", "left"} <= sides else ", ".join(sorted(sides))
        facts["third"] = ", ".join(t for t in ("upper", "middle", "lower") if t in thirds)
    return facts


def _facts_text(f, film_name):
    t = f"The labelled finding on this {film_name}: {f['finding']}."
    if f.get("others"):
        t += f" Also labelled: {', '.join(f['others'])}."
    if f.get("side"):
        t += (f" From the radiologists' boxes, it lies on the patient's {f['side']}, roughly in the {f['third']} part of the film"
              " (the side is reliable; the zone is approximate, as boxes don't follow the anterior ribs).")
    return t


DATA_CASE_SCHEMA = _obj({"brief": _str, "diagnosis": _str, "critical": _strs, "examiner_questions": _strs,
                         "mark_scheme": CASE_SCHEMA["properties"]["mark_scheme"]})
DATA_CASE_PROMPT = """Write the hidden case for a DATA INTERPRETATION station on a real film: the candidate is shown the film and presents it to the examiner, as in UK finals.
STATION: {title}
THE FILM (the truth, never shown to the candidate): {facts}
WHAT FINALIST'S ATLAS TEACHES FOR THIS FINDING (use it for the case items, the questions and the next steps): {teach}
The station's own notes: {notes}

Write:
- brief: 40-70 words, second person. You are an FY1; say who asks you to present this film and why it was taken, in a way that fits the finding without giving it away (for a normal film, a reason a film is commonly requested). The film's identifying details have been removed for teaching: say so in one short sentence.
- diagnosis: the finding in one line, with its side and location where known.
- examiner_questions: 2-3 questions an examiner asks after the presentation, each one plain question of at most 15 words, as real examiners ask them ("What would you do next?"): one on what to do next, and one about a feature, cause or complication of this finding.
- critical: usually empty; at most one patient-safety item where missing it would plausibly cause harm in THIS case (e.g. not recognising a tension or large pneumothorax as urgent).
- mark_scheme: start with these core items, exactly as written, in this order, then add 4-8 items specific to this film (identifying the finding; locating it by side and zone; the features that describe it; associated signs; a sensible differential; the specific next steps). Weights 1-3.
{core}
Reply with JSON only."""


def _data_examiner_system(sim):
    c, f = sim["case"], sim.get("film_facts") or {}
    name = FILM_NAME.get(sim.get("film_set"), "film")
    return f"""You are the examiner in a DATA INTERPRETATION station in a UK medical finals OSCE (Southampton CPSA). The candidate has been shown a {name} and presents it to you. Brief, neutral and professional, as a real examiner is: no teaching, no hints, no praise or criticism during the station.
The brief the candidate read: {c.get('brief')}
{_facts_text(f, name)}
You also see the film itself (attached to the first message). Your own reading of medical images is markedly less reliable than a radiologist's: treat what you see as supporting evidence only. Where your view and the label disagree, the label is right. Never reveal the finding.
How to run it:
- While the candidate presents, reply only with a brief, neutral prompt ("Go on." "Thank you.") or nothing more than a few words. Don't ask leading questions, don't correct them, don't add findings.
- If they ask for clinical details, give only what the brief says; otherwise "Just the film, please."
- When they say they've finished, or when the whistle goes, ask your questions one at a time, each in a single short sentence, moving on once they've answered: {json.dumps(c.get('examiner_questions') or [])}
- When you've asked them all, or time is clearly up, say "Thank you, that's the end of the station."
Reply with only what you say next.""" + ("\nYou're speaking out loud: words only, no stage directions." + heard_note(sim, "you") if sim.get("voice") else "")


def _film_block(sim_id, text):
    """The first message an examiner gets: the film, then the words."""
    img = FILMS.get(sim_id)
    if not img:
        return text
    return [{"type": "image", "source": {"type": "base64", "media_type": img["mime"], "data": img["data"]}}, {"type": "text", "text": text}]


def start_data(model, data, station, entry, emit, film, voice=False):
    """A film station: the case is written around a vetted dataset film, the brief streamed as soon as it's written;
    the examiner's Claude starts during the reading time and is shown the film."""
    sid = secrets.token_urlsafe(9)
    sim = {"id": sid, "station": station["id"], "title": station["title"], "kind": "data", "model": model,
           "case": None, "turns": [], "phase": "examiner", "started": time.time(), "ready": threading.Event(),
           "primed": primed(data, station["id"]), "voice": bool(voice), "vocab": list(station.get("vocab") or [])[:40],
           "film_set": film["set"], "film_id": film["id"], "film_facts": film["facts"],
           "file": str(data / "sims" / f"{date.today().isoformat()}-{station['id']}-{sid}.json")}
    FILMS[sid] = {"mime": film["mime"], "data": film["data"]}
    with LOCK:
        SIMS[sid] = sim
    emit({"sim": sid, "film": {"set": film["set"], "id": film["id"]}})
    name = FILM_NAME.get(film["set"], "film")
    prompt = DATA_CASE_PROMPT.format(title=station["title"], facts=_facts_text(film["facts"], name), teach=(film.get("teach") or "")[:3000],
                                     notes=_notes(entry), core=_core("data")) + moderation.case_note(station["id"])
    try:
        try:
            sim["case"] = ask(model, CASE_SYSTEM, prompt, on_text=_streaming_fields(["brief"], emit), effort="low", schema=DATA_CASE_SCHEMA)
        except (ValueError, RuntimeError):
            sim["case"] = ask(model, CASE_SYSTEM, prompt, effort="low", schema=DATA_CASE_SCHEMA)
        sim["case"]["person"] = {}
        sim["case"]["script"], sim["case"]["cue"] = "No patient: the candidate presents a film to the examiner.", ""
        _settle_scheme(sim["case"], station["id"], "data", spoken=True)
    except Exception as e:
        sim["error"] = str(e)
        raise
    finally:
        sim["ready"].set()
    emit({"ready": True, "brief": sim["case"]["brief"], "person": {}})

    def warm():                     # the examiner starts now and is shown the film, so the first reply doesn't wait
        try:
            live = Live(model, _data_examiner_system(sim))
            live.turn(_film_block(sid, "This is the film the candidate is about to present. Reply with only: Ready."), lambda t: None)
            with LOCK:
                if sid in SIMS and "examiner" not in LIVE.get(sid, {}):
                    LIVE.setdefault(sid, {})["examiner"] = live
                    return
            live.close()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()


# ---- teach me this (3 Nov 2026) ---------------------------------------------------------------------
# Untimed and unmarked: Claude as a tutor for one station. Learn it step by step, run it informally
# with pauses to teach, be quizzed, or ask what examiners reward. Kept in its own folder, never a run.

TEACH_KIND = {"history": "a history-taking station", "comms": "a communication station", "exam": "a narrated physical examination station",
              "data": "a data interpretation station: presenting a film to the examiner"}


def teach(model, data, station, entry, emit, voice=False):
    sid = secrets.token_urlsafe(9)
    sim = {"id": sid, "station": station["id"], "title": station["title"], "kind": "teach", "of": station["kind"], "model": model,
           "case": {"person": {}}, "notes": _notes(entry), "core": _core(station["kind"]),
           "turns": [], "phase": "tutor", "started": time.time(), "ready": threading.Event(), "voice": bool(voice),
           "vocab": list(station.get("vocab") or [])[:40],
           "file": str(data / "teach" / f"{date.today().isoformat()}-{station['id']}-{sid}.json")}
    sim["ready"].set()
    with LOCK:
        SIMS[sid] = sim
    emit({"sim": sid, "ready": True})

    def warm():                               # the tutor starts now, so the first reply doesn't wait for it
        try:
            live = Live(model, _tutor_system(sim))
            with LOCK:
                if sid in SIMS and "tutor" not in LIVE.get(sid, {}):
                    LIVE.setdefault(sid, {})["tutor"] = live
                    return
            live.close()
        except Exception:
            pass
    threading.Thread(target=warm, daemon=True).start()


def _tutor_system(sim):
    of = sim.get("of")
    manner = ("You are speaking out loud, and your words are turned into speech: write only what you say, no markdown, no lists, "
              "no stage directions. Keep each reply short: two to five sentences, then hand back to the student. Write numbers and doses the way you'd say them."
              + heard_note(sim, "you")) if sim.get("voice") else \
             ("Keep each reply short: a few sentences, or a short list when steps really are a list, then hand back to the student. "
              "Plain text: **bold** for the one thing to remember is fine; no headings.")
    run = ("play the examiner's side: ask what they'd do next, and give findings only for what they name" if of == "exam"
           else "play the patient, in character, in everyday British English")
    return f"""You are a warm, sharp clinical tutor, a registrar who loves teaching, helping a final-year UK medical student (Southampton CPSA finals) learn ONE station: "{sim['title']}", {TEACH_KIND.get(of, 'a station')}.

What the mark scheme rewards (its core items):
{sim.get('core')}
What the student's own notes for this station say:
{sim.get('notes') or '(none)'}

This is untimed and informal: there is no clock and no mark. Follow the student's lead. They may ask you to:
- teach the station step by step: go through it in the order a candidate does it, one chunk at a time, each with why it matters and the words to use, then check with a quick question before moving on;
- run it in an informal teaching format: set the scene in a line, {run}, and step out to teach whenever there's something worth learning, starting that part with "Pause:" and returning with "Back in the station:". Point out what they did well too;
- quiz them: one question at a time, wait for the answer, then say what was right, what was missing, and the next question;
- explain what examiners look for, and where students lose marks, including the safety step that fails a station.

How you teach:
- Short, concrete, one idea at a time. Ask, don't lecture: get them to say it before you do.
- Take the steps, their order, the positioning and the phrasing from the mark scheme and the notes above when they cover it; they are the reference for this station.
- UK practice only (NICE, BNF, Resuscitation Council UK, GMC, the relevant UK societies). If you aren't sure of a fact, a dose or a threshold, say so plainly rather than guess.
- Encouraging and honest: praise what is right, correct what isn't, kindly and clearly.
- {manner}
Reply with only your next words to the student."""


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
    who = {"you": "Student" if sim.get("kind") == "teach" else "Candidate", "patient": sim["case"]["person"].get("name") or "Patient",
           "examiner": "Examiner", "tutor": "Tutor"}
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
- When the candidate performs or describes an examination step, reply as the EXAMINER with ONLY what they specifically looked for or tested, from the findings: you don't ask, you don't get. "I'm checking the capillary refill" gets the capillary refill and nothing else; "I'm looking for clubbing" gets clubbing, not the rest of the hands. Give each sign they name, specific and brief ("On palpation, the apex beat is displaced to the 6th intercostal space, anterior axillary line."). Never volunteer a sign they didn't name, even one in the same part of the body, and never a hint of what to do next. A sign they name that the findings don't mention is normal. If they name only a region or a broad step without saying what they're looking for ("I examine the hands", "I look at the chest"), ask: "What are you looking for?" From the end of the bed, give only the patient's general appearance in a line, plus anything around the bed they say they're looking for. If a step would hurt the patient, you may add how the patient reacts, in the examiner's words ("The patient winces.").
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
- Never coach, never mark, never step out of role, never mention the script or the exam. If the student speaks to the examiner, stay quiet: reply with nothing but "*waits*".{moderation.patient_note(sim.get('station'))}{body_map.patient_rules(sim)}
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
    film = sim.get("kind") == "data"                          # a film station: the examiner throughout (14 Nov 2026)
    system = (_tutor_system(sim) if who == "tutor" else _data_examiner_system(sim) if film and who == "examiner"
              else _examiner_system(sim) if who == "examiner" else _room_system(sim))
    if film and on_role:
        on_role("examiner")
    ask_for = ("Your next words, as the tutor:" if who == "tutor" else "Your next words, as the examiner:" if who == "examiner"
               else "Your reply, starting with PATIENT: or EXAMINER:" if room else "Your next words, in character:")
    prompt = (f"{'The session' if who == 'tutor' else 'The station'} so far:\n{_transcript(sim)}\n\n"
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
    words = body_map.Filter(on_text, step, on_reject) if who == "patient" else None
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
            reply = live.turn(_film_block(sim_id, prompt) if film else prompt, stream)   # the first turn carries everything so far
        else:
            note = sim.pop("cut_note", "")
            said_by = "The student: " if who == "tutor" else "The candidate: "
            reply = live.turn(f"{note}{text.strip() if debug else said_by + text.strip()}\n\n" + ask_for, stream)
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


MARK_SYSTEM = """You are an experienced examiner for Southampton's final-year CPSA, marking an OSCE station on the risr/assess iPad mark sheet from its transcript. Be fair, specific and honest: this student is practising before a mock, and inflated marks would hurt them. Judge the content and the manner the words show, never typing speed, spelling or body language (if the station was spoken, the note before the JSON says how the transcript was made). Physical actions (hand hygiene, positioning, a chaperone) can't be typed: assume they were done and never mark them down. Mark each item from what was actually said. Score 2 when the substance of the item was done, even if the wording wasn't textbook or a minor detail was absent; score 1 only when a real part of it is missing, and say which part; score 0 when it wasn't attempted. Don't use 1 as the safe default: a reasonable, recognisable attempt at the whole item earns 2. The overall grade is your own judgement of the performance, made independently: don't derive it from the item marks. But calibrate it as real UK finals examiners do (anchors from published examiner guidance and benchmarking studies):
- D, borderline pass: the just-acceptable day-one FY1. A systematic history that may lack fine detail and lean on rote questioning rather than responding to the patient; acceptable rapport; reaches a reasonable differential for a common presentation. Borderline candidates typically score about 50-60% of a checklist (one published borderline benchmark: 56%).
- C, pass: covers the main elements with some omissions, sensible reasoning, decent communication. Typically about 60-70%. Most competent candidates land here; a real student who scored 60% got a C.
- B, good: fluent and patient-centred, few omissions, a well-reasoned differential. Typically about 70-80% (a published "good" benchmark: 81%).
- A, excellent: polished, comprehensive within the time, responsive to cues throughout. Typically 80%+.
- E, borderline fail: misses enough that you can't be confident they're safe, or a muddled structure; typically 40-50%. F, clear fail: unsafe, or the task was not done; typically under 40%.
No one completes every item in 9 minutes, and examiners aren't looking for perfection. Don't grade more than one letter below what the score and these anchors suggest unless you name the reason in the verdict (a pass/fail miss, or a genuine safety concern). For a communication station, the communication anchors in the note before the JSON replace the checklist-percentage anchors. Reply with JSON only."""

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
                  f"Your character and story: {c.get('script')} Reply with JSON only."
                  + (" The student spoke and was transcribed by speech recognition, which also split their sentences at short pauses: "
                     "lines that look cut off, and your own 'sorry, I didn't catch that' after them, were the microphone, not the student. "
                     "Don't count them as the student trailing off or being unclear." if sim.get("voice") else ""))
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


def mark(sim_id, data, emit, ended_early=False, draft="", with_patient=True, exclude_benchmark=None):
    """Marks the transcript, streaming the grade and verdict first. The patient
    gives their own mark in a separate call, alongside, so it's independent.
    A draft is what was still being typed when the bell went: it counts."""
    sim = _get(sim_id)
    c = sim["case"]
    if draft.strip():
        sim["turns"].append({"who": "you", "text": draft.strip()[:4000] + " [cut off by the bell]", "at": time.time()})
    if not any(x["who"] == "you" for x in sim["turns"]):
        return _blank(sim, data, emit)
    if sim.get("kind") == "data":
        with_patient = False                      # a film station has no patient to mark it
    patient = {}
    pt = threading.Thread(target=_patient_mark, args=(sim, patient), daemon=True)
    if with_patient:
        pt.start()
    note = "The candidate stopped the station early; mark what happened.\n" if ended_early else ""
    if sim.get("kind") == "exam":
        note += ("This was a NARRATED examination: the candidate said each step aloud as they performed it, and the examiner gave the findings. "
                 "For this station, ignore the rule about assuming physical actions were done: mark each step, including hand hygiene, positioning "
                 "and exposure, only if they said it. Mark the presentation on whether it states the findings they were actually given, accurately. "
                 "Weigh the grade as real examination stations are weighted: most of the credit is for a systematic, fluent, safe examination that "
                 "elicits the signs; presenting and interpreting them is a minority. A candidate who examines well and finds the key signs can still "
                 "pass without naming the diagnosis (a C, or a D if the presentation is also weak): a missing diagnosis alone never takes the grade "
                 "below D. If the examiner gave a sign the candidate hadn't asked for, don't mark the candidate down for not eliciting it.\n")
    if sim.get("kind") == "comms":
        note += ("This was a COMMUNICATION station. Weigh the grade as real UK finals communication stations are weighted: most of the "
                 "credit is for the process (structure, the warning shot, saying it plainly, responding to emotion and the cue, honesty "
                 "without false promises, checking understanding, a shared plan). Specific clinical knowledge, in the conversation or in "
                 "answer to your questions, is extra credit: few candidates recall exact figures or named referrals in the moment. A "
                 "candidate who handles the conversation well passes without it (a C, or a D if the process is also uneven): missing "
                 "knowledge alone never takes the grade below D, unless what they said was wrong in a way that would harm the patient. "
                 "Saying honestly that the outlook isn't known yet, that tests will tell, and that they'll share it when they know, without "
                 "false reassurance, is an accepted answer to a prognosis question at this stage: score any honesty or prognosis item at "
                 "least 1 for it, and 2 if it also gave some realistic hope or a clear next step. Grade the conversation, not the checklist "
                 "percentage: A, a structured, warm, honest conversation (SPIKES or similar) that picks up the cue and the person's main "
                 "fear; B, the same with one important miss (e.g. the cue or ICE not explored); C, a sound, kind structure with two or "
                 "three such misses; D, the news given but the structure or empathy clearly uneven; E or F, the news withheld, given "
                 "harmfully, or the person left unsupported. Count misses as an experienced examiner would: a missed cue and unexplored ICE about "
                 "the same fear are ONE miss, not two; honest uncertainty without much hope is a minor point, not an important miss; and "
                 "knowledge items (fertility, named contacts, staging) never count as misses for the grade.\n")
    if sim.get("kind") == "data":
        f = sim.get("film_facts") or {}
        note += ("This was a DATA INTERPRETATION station: there was no patient; the candidate presented a real film to the examiner and answered "
                 "the examiner's questions. " + _facts_text(f, FILM_NAME.get(sim.get("film_set"), "film")) + " The film is attached; your own "
                 "reading of medical images is markedly less reliable than a radiologist's, so the label is the truth and what you see is "
                 "supporting evidence only: never mark a description down because you can't see it yourself when it fits the label, and credit a "
                 "reasonable location or feature you can't verify. Mark what was SAID: the order, the description, the interpretation and the "
                 "plan. Weigh the grade as these stations are marked: most credit for a systematic read and correctly finding and describing "
                 "the key abnormality; a candidate who reads systematically, describes the abnormality accurately and gives a safe next step "
                 "can pass without the exact diagnosis (a C, or a D if the description is also weak). Missing the key abnormality entirely is "
                 "at best a D, and an E or F if the next step would be unsafe. Saying they would check the patient's details and the date "
                 "counts for the identity item (the film's details were removed). For the consultation mark, read it as the presentation to "
                 "the examiner: structure, precise terms, concise and confident delivery.\n")
    if sim.get("voice"):
        note += ("The candidate SPOKE this station and their words were transcribed by speech recognition: ignore mistranscribed words, "
                 "missing punctuation and odd spellings (a drug name heard as a near-miss is the drug name, 'scar' for 'scan'), and mark what they evidently said. "
                 "The turn detector also ends a turn at a short pause, so one spoken thought can be split across consecutive 'you' turns, each "
                 "looking cut off mid-sentence; read consecutive fragments as one statement. When the patient or examiner says they didn't "
                 "catch something straight after such a fragment, that was the recogniser or the turn detector, not the candidate: never "
                 "mark clarity, fluency, structure or 'fragmented' explanations down for it, and don't let it lower the consultation mark."
                 + (f" Terms this station's students commonly use: {'; '.join(sim.get('vocab') or [])}." if sim.get("vocab") else "") + "\n")
    critical = CRITICAL[sim["kind"]] + [x for x in c.get("critical") or [] if x]
    # moderation (11 Nov 2026): the station's accepted answers and notes, and one or two moderated runs as worked
    # examples, kept short; a scheme item the moderator removed or rewrote is marked as they left it
    try:
        note += moderation.marking_note(sim.get("station"), sim.get("kind"), exclude=exclude_benchmark)
        scheme = moderation.apply_scheme(c.get("mark_scheme") or [], moderation.overrides(sim.get("station")))
    except Exception:
        scheme = c.get("mark_scheme") or []
    mark_prompt = MARK_PROMPT.format(
        title=sim["title"], brief=c.get("brief"), diagnosis=c.get("diagnosis"), script=c.get("script"),
        cue=c.get("cue"), scheme=json.dumps(scheme), critical=json.dumps(critical),
        cscale=", ".join(f"{i + 1} {s}" for i, s in enumerate(CONSULTATION_SCALE)),
        consultation="; ".join(DATA_PRESENTATION if sim.get("kind") == "data" else CONSULTATION), transcript=_transcript(sim), note=note)
    out = None
    if sim.get("kind") == "data" and FILMS.get(sim_id):
        # the marker sees the film too (weighed below the label): a one-turn Claude with the image, JSON asked for in words
        live = None
        try:
            live = Live(sim["model"], MARK_SYSTEM)
            out = _json(live.turn(_film_block(sim_id, mark_prompt), _streaming_fields(["grade", "verdict"], emit), timeout=240))
        except (ValueError, RuntimeError):
            out = None
        finally:
            if live:
                live.close()
    if out is None:
        try:
            out = ask(sim["model"], MARK_SYSTEM, mark_prompt, on_text=_streaming_fields(["grade", "verdict"], emit),
                      effort="medium", schema=MARK_SCHEMA)
        except (ValueError, RuntimeError):     # once more, quietly
            out = ask(sim["model"], MARK_SYSTEM, mark_prompt, effort="medium", schema=MARK_SCHEMA)
    if with_patient:
        pt.join(120)
    return _finish(sim, data, emit, out, patient)


def _finish(sim, data, emit, out, patient):
    _close_live(sim.get("id"))
    FILMS.pop(sim.get("id"), None)
    c = sim["case"]
    # The station score as risr/assess totals it: each item's 0-2 times its weight, plus
    # the consultation domain (1-5) and the patient's mark (1-3). The grade is separate.
    items = out.get("items") or []
    w = lambda i: max(1, int(i.get("weight") or 1))
    if sim.get("kind") == "comms":
        # communication stations score the process; a knowledge item counts once (weight 1), even in a case written before
        # this rule (10 Nov 2026). The scheme's own domains say which items are knowledge.
        dom = {(x.get("item") or "").strip().lower(): x.get("domain") for x in (c.get("mark_scheme") or [])}
        # (older cases sometimes filed a communication step under reasoning: one about the cue, a fear, emotion, silence or
        # honesty keeps its weight)
        talk = re.compile(r"\b(cue|fear|worr|concern|feel|emotion|silence|honest|empath|listen)", re.I)
        knowledge = lambda i: dom.get((i.get("item") or "").strip().lower()) == "reasoning" and not talk.search(i.get("item") or "")
        w = lambda i: 1 if knowledge(i) else max(1, int(i.get("weight") or 1))
        for i in items:
            i["weight"] = w(i)
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
    out["run"] = f.stem                 # the run's name on the node: what a report of it points at
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


# ---- peer-run stations (10 Nov 2026) -------------------------------------------------------------------
# Two or three students run the station together, in person: one is the candidate, one plays the patient from notes
# on their phone, one examines with the mark scheme as ticks. Student-marked: no Claude in the room. The case is the
# same shape as a Claude station's, plus what humans need that Claude doesn't: notes a fellow student can act from,
# and an expected answer for each examiner question. Kept in DATA/peer, so it can be run again without a new call.

PEER_KINDS = ("history", "comms", "exam", "procedure", "management")

PEER_SCHEMA = {**CASE_SCHEMA, "properties": {
    **CASE_SCHEMA["properties"],
    "actor": _obj({"first_words": _str, "story": _strs, "only_if_asked": _strs, "cue": _str, "cue_when": _str,
                   "feelings": _str, "never_volunteer": _strs, "asks": _strs}),
    "examiner_answers": _strs,
}, "required": [*CASE_SCHEMA["required"], "actor", "examiner_answers"]}

# Procedures and management: the candidate does the task (on a model or a part-task trainer, with the patient beside
# it); the examiner hands over whatever the task produces, when it's asked for or the step is done. As with a narrated
# exam, the case keeps its fields, read differently: "script" is what the examiner gives.
CASE_PROMPT_TASK = """Write one fresh case for this {what} station: the candidate does the task on a manikin or part-task trainer, with the patient sitting beside it to talk to, explaining as they go; the examiner gives any information the candidate asks for.

STATION: {title} ({kind})
WHAT IT TESTS (the student's revision notes, including the steps):
{notes}

{variety}
Return exactly this JSON:
{{
  "brief": "The door note: setting, who the patient is and why the task is needed (one or two lines of history), the task, and what the examiner will ask at the end. Second person. Under 90 words.",
  "person": {{"name": "", "age": 0, "sex": "female|male", "who": "the patient", "manner": "how they come across, in a line"}},
  "opening": "How the patient greets the candidate, and anything they say unprompted (one line).",
  "script": "WHAT THE EXAMINER GIVES, only when the candidate asks for it or does the step that reveals it: the patient's details for the identity check, their allergies, the readings or results the task produces (e.g. the capillary glucose, the peak flow, the observations), and for an unwell patient the findings at each step of A to E and how they change with each treatment. Specific and quantified, in UK units. 100-300 words.",
  "cue": "What the patient says or does that a good candidate responds to (e.g. anxious about needles, a faint last time, pain), in a line.",
  "diagnosis": "The point of the station: why the task is needed, what the result means, and the safety issue that matters most.",
  "critical": ["Usually none; at most one pass/fail item: a patient-safety miss in THIS case (e.g. the wrong patient's sample, a sharp left out, a drug given despite the allergy, not escalating a peri-arrest patient); else empty"],
  "examiner_questions": ["2-3 questions the examiner asks at the end: what the result means, what to do next, a complication or a safety point"],
  "mark_scheme": [{{"item": "a specific, observable thing the candidate should do or say", "domain": "data|reasoning|communication|professionalism|safety", "weight": 1}}]
}}
The mark scheme must contain these core items, exactly as written, then 8-14 items specific to this task and case: the steps that matter, in UK practice (as the notes give them), the safety checks, and acting on the result. Order the whole scheme as the candidate would do it (preparation first, the core disposal and documentation items last), so an examiner can follow along. Weights 1-3.
CORE:
{core}"""

PEER_ADDENDUM = """

THIS CASE IS FOR STUDENTS PRACTISING TOGETHER, IN PERSON: one is the candidate, one plays {who} from notes on a phone, one examines with the mark scheme and asks the questions. None of them is a trained actor or examiner. So also return:
"actor": the notes for the student playing {who}, in the second person ("You are..."), in short plain lines they can glance at mid-station:
  "first_words": what they say first, word for word;
  "story": {story};
  "only_if_asked": 4-10 facts they give only when asked directly, each starting with what the question is about (e.g. "Smoking: 20 a day for 30 years");
  "cue": the cue, word for word as they say or show it (the same as "cue" above);
  "cue_when": when to drop it, and what to do if the candidate picks it up, or ignores it;
  "feelings": how to play the emotion: how they look and sound, and how that changes if the candidate is kind or rushes them;
  "never_volunteer": what they must not say unless asked (the clinching facts);
  "asks": 1-3 questions they ask the candidate, and when (e.g. "Near the end: 'Is it cancer?'").
"examiner_answers": one expected answer for each examiner question, in the same order: what a well-prepared final-year student should say, as short points the examiner can check against at a glance, separated by "; ". UK practice only (NICE, BNF, Resuscitation Council UK, the relevant UK societies), with the exact threshold, drug or dose where the question turns on it. Where more than one answer is acceptable, say so. Never guess a number: give the principle instead."""

STORY = {
    "history": "the story as 5-10 short lines, in the order they'd tell it to open questions, each with its details (when it started, what makes it better or worse, how it affects them)",
    "comms": "who they are and what has happened so far, as 4-8 short lines: what they know, what they want to know, and what they're afraid of",
    "exam": "4-6 short lines: who they are, the one line of history they give if asked, and how to behave while being examined (where it hurts, what to do when touched there, what position they can't lie in)",
    "procedure": "4-6 short lines: who they are, why they're having it done, what they know about it, and how they feel about it",
    "management": "4-6 short lines: who they are, how unwell they feel and how to show it (and how it changes as they're treated), and what they say when asked",
}


def _peer_prompt(station, entry, variety):
    kind = station["kind"]
    if kind == "exam":
        p = CASE_PROMPT_EXAM.format(title=station["title"], notes=_notes(entry), variety=variety, core=_core("exam"))
    elif kind in ("procedure", "management"):
        p = CASE_PROMPT_TASK.format(what="practical procedure" if kind == "procedure" else "clinical management",
                                    title=station["title"], kind=kind, notes=_notes(entry), variety=variety, core=_core(kind))
    else:
        p = CASE_PROMPT.format(title=station["title"], kind=kind, notes=_notes(entry), variety=variety, core=_core(kind))
    return p + PEER_ADDENDUM.format(who="the patient (or the person in the room)", story=STORY[kind])


def peer_case(model, data, station, entry, emit):
    """Writes a case for a peer-run station (one Claude call on this node's account), streaming the brief as soon as
    it's written, and keeps it in DATA/peer. Emits {"case", "name"} at the end."""
    if station["kind"] not in PEER_KINDS:
        raise KeyError("This kind of station can't be run with friends.")
    seen = _past(data, station["id"])
    variety = f"Make the patient {random.choice(TWISTS)}."
    if seen:
        variety += " Don't repeat these earlier cases for this station: " + " | ".join(seen)
    prompt = _peer_prompt(station, entry, variety) + moderation.case_note(station["id"])
    try:
        case = ask(model, CASE_SYSTEM, prompt, on_text=_streaming_fields(["brief"], emit), effort="low", schema=PEER_SCHEMA, timeout=240)
    except (ValueError, RuntimeError):
        case = ask(model, CASE_SYSTEM, prompt, effort="low", schema=PEER_SCHEMA, timeout=240)     # once more, quietly
    _settle_scheme(case, station["id"], station["kind"], spoken=False)     # in person: only the overrides and duplicates
    sid = secrets.token_urlsafe(9)
    name = f"{date.today().isoformat()}-{station['id']}-{sid}"
    d = data / "peer"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps({"id": sid, "station": station["id"], "title": station["title"],
                                                "kind": station["kind"], "model": model, "started": time.time(), "case": case}, indent=1))
    emit({"case": case, "name": "peer/" + name})
    return case


def peer_cases(data, station_id, limit=20):
    """Cases this account already has for a station, newest first: those written for friends, and those from its own
    Claude runs (which lack the actor's notes and the expected answers; the app fills the notes from the script). Never
    the diagnosis: whoever starts the room may be the candidate."""
    out = []
    for src, folder in (("peer", "peer"), ("run", "sims")):
        d = data / folder
        for f in d.glob(f"*-{station_id}-*.json") if d.is_dir() else []:
            try:
                s = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            c = s.get("case") or {}
            if s.get("station") != station_id or not c.get("brief") or not c.get("mark_scheme"):
                continue
            p = c.get("person") or {}
            out.append({"name": f"{src}/{f.stem}", "source": src, "at": s.get("started"),
                        "person": {"name": p.get("name"), "age": p.get("age")}})
    return sorted(out, key=lambda r: r["at"] or 0, reverse=True)[:limit]


def peer_case_get(data, name):
    """One kept case, by the name peer_cases gave it."""
    m = re.fullmatch(r"(peer|run)/([\w-]+)", name or "")
    if not m:
        return None
    f = data / ("peer" if m[1] == "peer" else "sims") / f"{m[2]}.json"
    if not f.is_file():
        return None
    s = json.loads(f.read_text())
    return {"station": s.get("station"), "title": s.get("title"), "kind": s.get("kind"), "case": s.get("case")}
