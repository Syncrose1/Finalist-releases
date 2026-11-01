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


def check(o):
    """A step as the page takes it, or None if it isn't one."""
    if not isinstance(o, dict):
        return None
    if isinstance(o.get("mark"), dict):
        m = o["mark"]
        if m.get("region") not in REGIONS or m.get("kind") not in KINDS or not _short(m.get("label"), 90):
            return None
        out = {"region": m["region"], "kind": m["kind"], "label": _short(m["label"], 90)}
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
    return re.sub(r" +([.,!?;:])", r"\1", re.sub(r"[ \t]{2,}", " ", text)).strip()


class Filter:
    """Between the AI's stream and the candidate: words pass straight through; a tag is held until it
    closes, then becomes a step. Anything that only looked like the start of a tag is let through."""

    def __init__(self, on_text, on_step):
        self.on_text, self.on_step, self.buf = on_text, on_step, ""

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
            try:
                step = check(json.loads(body))
            except ValueError:
                step = None
            if step:
                self.on_step(step)

    def _out(self, t):
        if t:
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
  - a finding at a place (tenderness, a mass, a scar, a murmur heard loudest somewhere, crackles at a base, oedema, a rash): after your words add <map>{{"mark":{{"region":"left-lung-base","kind":"finding","label":"Fine crackles"}}}}</map> with the finding in a few words.
  - a value (pulse, respiratory rate, blood pressure, oxygen saturation, temperature, capillary refill, blood glucose): add <map>{{"info":{{"name":"Pulse","value":"92","note":"regular","flag":false}}}}</map>, flag true when it's abnormal.
  - when the candidate examines the back or spine: add <map>{{"unlock":"spine"}}</map>, once.
  Only for what that step has just found: never ahead, never a hint. A normal finding gets no mark (a normal value still goes in as info). The tag is not spoken or read out. PATIENT lines get no tags.
  Regions (id: name):
{region_list()}"""
