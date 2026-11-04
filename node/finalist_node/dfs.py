"""Finalist DFS (Datasets-For-Stations), 2 Nov 2026 (Raahat's idea).

Real data for interpretation stations, served from the user's own node: nothing is hosted by Finalist.
Each set is a manifest shipped with the node (data/dfs/<set>.json): the items we've vetted, each with
its answer, its licence and credit, and where it lives at its source with a sha256. Installing a set
fetches its items from the source (PTB-XL's ECGs from PhysioNet's open mirror), checks each against
the manifest, and keeps it, ready to serve; an item not yet fetched is fetched when first asked for.

Who may use it: the node's owner, anyone the owner allows by email (relaxed on purpose: these are
open datasets, not the owner's Claude), and a friend holding a live share.
"""

import hashlib
import json
import threading
import urllib.request
from pathlib import Path

from .config import NODE

LOCK = threading.Lock()


def _store(cfg):
    p = Path(cfg["data"]).expanduser() / "dfs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def manifest(name):
    try:
        return json.loads((NODE / "data/dfs" / f"{name}.json").read_text())
    except (OSError, ValueError):
        return None


def sets():
    return sorted(p.stem for p in (NODE / "data/dfs").glob("*.json")) if (NODE / "data/dfs").is_dir() else []


# ---- who may use it -------------------------------------------------------------------------------

def allowed_emails(cfg):
    try:
        return [e for e in json.loads((_store(cfg) / "allow.json").read_text()) if isinstance(e, str)]
    except (OSError, ValueError):
        return []


def set_allowed(cfg, emails):
    clean = sorted({e.strip().lower() for e in emails if isinstance(e, str) and "@" in e})[:50]
    (_store(cfg) / "allow.json").write_text(json.dumps(clean, indent=1))
    return clean


def may_use(w, cfg, guest=False):
    return bool(w and (w.owner or guest or (w.email or "").lower() in allowed_emails(cfg)))


# ---- items ------------------------------------------------------------------------------------------

def _get(url, rng=None):
    """A source file, or a byte range of one; waits when the source says too many requests (Zenodo
    allows about one a second, 3 Nov 2026)."""
    import time, urllib.error
    h = {"User-Agent": "FinalistNode"} | ({"Range": f"bytes={rng[0]}-{rng[1]}"} if rng else {})
    for wait in (2, 5, 15, 40, None):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429 or wait is None:
                raise
            time.sleep(min(120, int(e.headers.get("Retry-After") or wait)))


def _zip_member(f):
    """One file out of a zip at its source, by byte range: its local header, whose name and extra field
    say where the data starts, then the data (stored or deflated). One request, never the whole zip."""
    import struct, zlib
    b = _get(f["zip"], (f["offset"], f["offset"] + 30 + 512 + f["csize"]))
    if b[:4] != b"PK\x03\x04":
        raise ValueError("the source zip has changed")
    ln, le = struct.unpack("<HH", b[26:30])
    raw = b[30 + ln + le:30 + ln + le + f["csize"]]
    return raw if f["method"] == 0 else zlib.decompress(raw, -15)


def _ecg(hea: bytes, dat: bytes):
    """A WFDB format-16 record (12 interleaved int16 leads) as millivolts, lead by lead."""
    import struct
    lines = hea.decode().strip().splitlines()
    n_sig, n = int(lines[0].split()[1]), int(lines[0].split()[3])
    leads, gains, bases = [], [], []
    for l in lines[1:1 + n_sig]:
        p = l.split()
        g = p[2].split("/")[0]
        gain, base = (float(g.split("(")[0]), int(g.split("(")[1].rstrip(")"))) if "(" in g else (float(g), 0)
        leads.append(p[-1].upper()); gains.append(gain or 1000.0); bases.append(base)
    raw = struct.unpack(f"<{n * n_sig}h", dat[: n * n_sig * 2])
    return {lead: [round((raw[k * n_sig + j] - bases[j]) / gains[j], 3) for k in range(n)] for j, lead in enumerate(leads)}


def _path(cfg, name, item_id):
    return _store(cfg) / name / f"{item_id}.json"


def fetch(cfg, name, it):
    """One item from its source, checked against the manifest, converted and kept."""
    out = _path(cfg, name, it["id"])
    if out.exists():
        return json.loads(out.read_text())
    blobs = {}
    for ext, f in it["files"].items():
        b = _zip_member(f) if "zip" in f else _get(f["url"])
        if hashlib.sha256(b).hexdigest() != f["sha256"]:
            raise ValueError(f"{it['id']}: the source file doesn't match what was vetted")
        blobs[ext] = b
    if it["kind"] == "ecg":
        data = {"id": it["id"], "rate": it.get("rate", 100), "leads": _ecg(blobs["hea"], blobs["dat"])}
    elif it["kind"] == "image":            # a photograph (fundus, 3 Nov 2026), kept as it came, as base64
        import base64
        f = it["files"]["img"]
        if f.get("window"):                # a CT slice (CQ500, 4 Nov 2026): windowed into a PNG here
            from . import dicom
            data = {"id": it["id"], "mime": "image/png", "data": base64.b64encode(dicom.png(blobs["img"], tuple(f["window"]))).decode()}
        else:
            data = {"id": it["id"], "mime": f.get("mime", "image/jpeg"), "data": base64.b64encode(blobs["img"]).decode()}
    else:
        raise ValueError(f"unknown kind {it['kind']}")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(f".{threading.get_ident()}.tmp")       # two fetches of one item never share a file
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.replace(out)
    return data


def item(cfg, name, item_id):
    """One item, fetched now if it isn't kept yet (a second or so): a station never waits for a whole set."""
    m = manifest(name)
    it = next((x for x in (m or {}).get("items", []) if x["id"] == item_id), None)
    if not it:
        return None
    return fetch(cfg, name, it)


# 3 Nov 2026: a set is never installed in one go behind a progress bar (Raahat: it restarted whenever the
# page was left mid-way, since the fetching lived in the page's request; and a thousand cases would be a
# long wait). Opening a set starts this instead: on its own thread, four at a time, it fetches whatever
# isn't kept yet, while the station serves what is and fetches any item asked for on the spot.
WARMING = set()


def warm(cfg, name):
    m = manifest(name)
    with LOCK:
        if not m or name in WARMING:
            return False
        have = {p.stem for p in (_store(cfg) / name).glob("*.json")} if (_store(cfg) / name).is_dir() else set()
        todo = [it for it in m["items"] if it["id"] not in have]
        if not todo:
            return False
        WARMING.add(name)
    # a set served out of one zip on Zenodo goes one at a time, a little apart: it turns away a crowd
    zipped = any("zip" in f for it in todo for f in it["files"].values())

    def run():
        from concurrent.futures import ThreadPoolExecutor
        import time
        try:
            if zipped:
                for it in todo:
                    _quiet(fetch, cfg, name, it)
                    time.sleep(1.2)
                return
            with ThreadPoolExecutor(max_workers=4) as pool:
                for _ in pool.map(lambda it: _quiet(fetch, cfg, name, it), todo):
                    pass
        finally:
            with LOCK:
                WARMING.discard(name)
    threading.Thread(target=run, daemon=True).start()
    return True


def _quiet(f, *a):
    """Three tries, a little apart (the source turns away the odd request when four arrive at once);
    still unreachable, it's fetched when asked for, or on the next warm."""
    import time
    for wait in (1.5, 4, None):
        try:
            return f(*a)
        except Exception as e:
            if wait is None:
                FAILED[a[-1]["id"]] = str(e)[:160]
                return None
            time.sleep(wait)


FAILED = {}


def catalogue(cfg, name):
    """What a station needs to choose items: everything but the source files."""
    m = manifest(name)
    if not m:
        return None
    have = {p.stem for p in (_store(cfg) / name).glob("*.json")} if (_store(cfg) / name).is_dir() else set()
    return {"name": m["name"], "licence": m["licence"], "credit": m["credit"], "source": m["source"], "labels": m["labels"],
            "items": [{k: v for k, v in x.items() if k != "files"} | {"ready": x["id"] in have} for x in m["items"]]}


def status(cfg):
    out = {}
    for name in sets():
        m = manifest(name) or {"items": []}
        d = _store(cfg) / name
        have = sum(1 for _ in d.glob("*.json")) if d.is_dir() else 0
        out[name] = {"name": m.get("name"), "items": len(m["items"]), "ready": have, "installed": have >= len(m["items"]) > 0,
                     "warming": name in WARMING}
    return out


def install(cfg, name, emit):
    """Fetch every item of a set, eight at a time, saying how far it's got; items already kept are
    skipped. (One after another, 51 ECGs took two minutes: 2 Nov.) Kept for Finalist pages from before
    3 Nov 2026; newer ones never wait for this, see warm()."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    m = manifest(name)
    if not m:
        raise KeyError(f"No dataset called {name}.")
    items, bad, done = m["items"], 0, 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(fetch, cfg, name, it): it for it in items}
        for f in as_completed(jobs):
            done += 1
            try:
                f.result()
            except Exception as e:                  # one unreachable item doesn't stop the rest
                bad += 1
                emit({"skipped": jobs[f]["id"], "why": str(e)[:200]})
            emit({"done": done, "of": len(items)})
    emit({"installed": name, "failed": bad})
