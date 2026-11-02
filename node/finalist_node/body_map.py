"""The body map in a station (1 Nov 2026, design locked with Raahat at a3c4d98).

The AI adds map steps to its reply as tags, <map>{...}</map>: the patient marks where a symptom is, in
their own words; in a narrated exam the examiner marks findings, reports values (pulse, BP...) and unlocks
the spinal levels when the back is examined. The tags never reach the candidate as words or voice: the
Filter takes them out of the stream, checks each against the region list (data/body-map.json, exported
from the site's body-regions.ts), and hands it on as a step. A run's steps are its map, kept with it, so a
run is also a recorded macro.
"""

import json
import re

from .config import NODE

try:
    _MAP = json.loads((NODE / "data/body-map.json").read_text())
except (OSError, ValueError):
    _MAP = {"regions": {}, "kinds": [], "tools": []}
REGIONS = _MAP["regions"]
KINDS, TOOLS = set(_MAP["kinds"]), set(_MAP["tools"])
OPEN, CLOSE = "<map>", "</map>"
TAG = re.compile(r"<map>.*?</map> ?", re.S)


def region_list():
    return "\n".join(f"{i}: {n} ({v})" for i, (n, v) in REGIONS.items())


def _short(t, n):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    return t[:n]


def why_not(o):
    """For debug mode: why a tag wasn't taken."""
    if not isinstance(o, dict):
        return "not an object"
    if isinstance(o.get("mark"), dict):
        m = o["mark"]
        if m.get("region") not in REGIONS:
            return f"unknown region {m.get('region')!r}"
        if m.get("kind") not in KINDS:
            return f"unknown kind {m.get('kind')!r}"
        if m.get("kind") == "radiates" and m.get("to") not in REGIONS:
            return f"radiates to unknown region {m.get('to')!r}"
        return "no label"
    if isinstance(o.get("info"), dict):
        return "info needs a name and a value"
    if "unlock" in o:
        return f"unknown tool {o.get('unlock')!r}"
    return "not a mark, info or unlock"


def check(o):
    """A step as the page takes it, or None if it isn't one."""
    if not isinstance(o, dict):
        return None
    if isinstance(o.get("mark"), dict):
        m = o["mark"]
        if m.get("region") not in REGIONS or m.get("kind") not in KINDS or not _short(m.get("label"), 90):
            return None
        out = {"region": m["region"], "kind": m["kind"], "label": _short(m["label"], 90)}
        if m["kind"] == "scar" and m.get("dir") in ("v", "h", "d"):
            out["dir"] = m["dir"]
        if m["kind"] == "radiates":
            if m.get("to") not in REGIONS:
                return None
            out["to"] = m["to"]
        return {"mark": out}
    if isinstance(o.get("info"), dict):
        x = o["info"]
        name, value = _short(x.get("name"), 30), _short(x.get("value"), 40)
        if not name or not value:
            return None
        out = {"name": name, "value": value}
        if _short(x.get("note"), 80):
            out["note"] = _short(x["note"], 80)
        if x.get("flag") is True:
            out["flag"] = True
        return {"info": out}
    if o.get("unlock") in TOOLS:
        return {"unlock": o["unlock"]}
    return None


def strip(text):
    """The words without the tags (and without a tag cut off at the end)."""
    text = TAG.sub("", text or "")
    i = text.find(OPEN)
    text = text[:i] if i >= 0 else re.sub(r"<m(a(p>?)?)?$", "", text)
    text = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text))
    return re.sub(r" +([.,!?;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()


class Filter:
    """Between the AI's stream and the candidate: words pass straight through; a tag is held until it
    closes, then becomes a step. Anything that only looked like the start of a tag is let through."""

    def __init__(self, on_text, on_step, on_reject=None):
        self.on_text, self.on_step, self.on_reject, self.buf, self.sent, self.gap = on_text, on_step, on_reject, "", "", False

    def feed(self, t):
        self.buf += t
        while self.buf:
            i = self.buf.find("<")
            if i < 0:
                self._out(self.buf); self.buf = ""
                return
            if i:
                self._out(self.buf[:i]); self.buf = self.buf[i:]
            if len(self.buf) < len(OPEN):
                if OPEN.startswith(self.buf):
                    return                              # might be a tag: wait for more
                self._out(self.buf[0]); self.buf = self.buf[1:]
                continue
            if not self.buf.startswith(OPEN):
                self._out(self.buf[0]); self.buf = self.buf[1:]
                continue
            j = self.buf.find(CLOSE)
            if j < 0:
                return                                  # inside a tag: wait for the end
            body, self.buf = self.buf[len(OPEN):j], self.buf[j + len(CLOSE):].lstrip(" ")
            self.gap = True                             # the line breaks around a tag aren't the reply's own
            try:
                o = json.loads(body)
                step, why = check(o), None
            except ValueError as e:
                step, why = None, f"not JSON ({e})"
            if step:
                self.on_step(step)
            elif self.on_reject:
                self.on_reject({"raw": body[:400], "why": why or why_not(o)})

    def _out(self, t):
        if self.gap:                                  # after a tag: never more than one blank line
            have = len(self.sent) - len(self.sent.rstrip("\n"))
            lead = len(t) - len(t.lstrip("\n"))
            t = "\n" * max(0, min(lead, 2 - have)) + t[lead:]
            if t.strip():
                self.gap = False
        if t:
            self.sent = (self.sent + t)[-4:]
            self.on_text(t)

    def flush(self):
        """The reply has ended: a tag left open is dropped; anything else goes out."""
        if self.buf and not self.buf.startswith(OPEN[:len(self.buf)]):
            self._out(self.buf)
        self.buf = ""


def patient_rules(sim):
    return f"""
- The body map: the candidate sees a body map of you. When you tell them WHERE a symptom is (pain, a lump, a rash, numbness, swelling, where it spreads), also mark it, after your words, with a tag like <map>{{"mark":{{"region":"rif","kind":"sharp","label":"Sharp, just here"}}}}</map>
  region: one id from the list below. kind: "sharp" (you could point to it with one finger), "diffuse" (a dull ache over an area, hard to pin down), "band" (a strip or a belt round you), or "radiates" (it spreads: add "to" with the region it goes to). label: your own words, under 8 words.
  Mark only a place you've just told them about, once per place; never ahead of what you've said. The tag is not spoken or read out: it's how the map is drawn. If you're not saying where something is, add no tag.
  Regions (id: name):
{region_list()}"""


def exam_rules(sim):
    return f"""
- The body map: the candidate sees a body map of the patient. In an EXAMINER line:
  - a finding at a place: after your words add <map>{{"mark":{{"region":"ruq","kind":"felt","label":"Liver edge 3 cm below costal margin"}}}}</map>, the finding in a few words. "kind" is how it was found:
    "seen" on inspection (jaundice, spider naevi, palmar erythema, bruising, a rash, a deformity), "felt" on palpation (an organ edge, a mass, a lump, oedema, a thrill, a heave),
    "tender" (tenderness, guarding), "dull" to percussion (shifting dullness, a dull base), "heard" on auscultation (a murmur where it's loudest, crackles, a bruit, bowel sounds),
    "sign" for a test or movement (asterixis, tremor, weakness, reflexes, fatigability, a positive special test), "scar" for a scar (add "dir": "v", "h" or "d" for its direction; a sternotomy or midline laparotomy is "v").
  - a value (pulse, respiratory rate, blood pressure, oxygen saturation, temperature, capillary refill, blood glucose): add <map>{{"info":{{"name":"Pulse","value":"92","note":"regular","flag":false}}}}</map>, flag true when it's abnormal.
  - when the candidate examines the back or spine: add <map>{{"unlock":"spine"}}</map>, once.
  A finding on both sides: one mark per side, with exactly the same label (they share one label on the map). Keep labels short.
  Only for what that step has just found: never ahead, never a hint. A normal finding gets no mark (a normal value still goes in as info). The tag is not spoken or read out. PATIENT lines get no tags.
  Regions (id: name):
{region_list()}"""
