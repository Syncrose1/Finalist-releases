"""The Finalist Node: Patient and Examiner Claude, the voice and the private material, on the owner's PC.

The browser calls it directly (Finalist never proxies), with the person's Finalist session as a
Bearer token; the node checks it with Supabase (auth.py) before anything else. Phase 1 (28 Oct
2026) serves the owner only; friends come with share mode (phase 3).

  GET  /v1/hello                      public: the Supabase project to sign in to, and what this node has
  GET  /v1/me                         who you are to this node
  POST /v1/station/<what>             start · say · whistle · mark · cut · mark_run · review (lines of JSON, streamed)
  GET  /v1/station/runs?station=      your runs of a station
  GET  /v1/station/run/<name>         one run
  GET  /v1/review?station=            your evaluation and the A guide
  GET  /v1/drills?station=            your recall-drill rounds, and whether you're primed
  POST /v1/drill                      log a round
  POST /v1/drill/check                Claude's verdict on guesses the matcher didn't take
  GET  /v1/circuit                    the ranked circuit, with when each came up (privileged)
  WS   /v1/voice?access_token=        the microphone in, the voices out (voice/voiced.py)
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import auth, config, dfs
from . import voice_setup
from . import stations as sim

CFG = config.load()
NODE = Path(__file__).resolve().parents[1]
VERSION = 1
KINDS = sim.KINDS
LOCK = threading.Lock()
VOICE_LOCK = threading.Lock()
VOICE = {"proc": None}      # the voice this node started, stopped with it


ACTIVITY = []          # the last lines of the log, for the node's own screen


# what the node's screen says for the calls worth a line there; every call still goes to the terminal
SCREEN = {"/v1/station/start": "A Claude station started", "/v1/station/mark": "Examiner Claude marked a run",
          "/v1/drill/check": "Checked a recall drill's answers", "/v1/pair": None}


def log(msg, screen=True):
    print(time.strftime("%H:%M:%S"), msg, flush=True)
    if not screen:
        return
    ACTIVITY.append({"at": time.time(), "msg": msg})
    del ACTIVITY[:-60]


def _stations():
    return json.loads(Path(CFG["stations"]).expanduser().read_text())


def circuit():
    """The private ranking and past-finals record: only on a PC with the Revision System."""
    f = Path(CFG["circuit"]).expanduser() if CFG.get("circuit") else None
    return json.loads(f.read_text()) if f and f.is_file() else None


def station_vocab(sid):
    """A station's own terms, from its drill list (the items' answers), for the listeners of a spoken station."""
    try:
        d = json.loads(Path(CFG["drills"]).read_text()).get(sid) or {}
    except (OSError, ValueError):
        return []
    out = []
    for g in d.get("groups", []):
        for it in g.get("items", []):
            a = (it.get("answer") or "").strip()
            if a and a not in out:
                out.append(a)
    return out[:40]


# ---- the Revision System's day, in Finalist (1 Nov 2026) --------------------------------------------
# Raahat: "port the actual revision system main screen". It's built on their own material (Unified V4,
# QuackQuackMed, their Anki, HOURGLASS), so for now it's theirs alone: the node's owner, and this email.
# revise.py stays the one source of truth; the node passes its state, actions and reading PDFs through.
# A request from another device is marked as the phone, so it never launches anything on this PC.
REVISE_EMAIL = "raahat.shah@gmail.com"


def revise_allowed(w):
    return bool(w and w.owner and (w.email or "").lower() == REVISE_EMAIL)


def revise_request(method, path, body=None, phone=True, headers=None, timeout=30):
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", int(CFG.get("revise_port", 47613)), timeout=timeout)
    h = {"X-Revise": "1", **({"X-Revise-As": "phone"} if phone else {}), **(headers or {})}
    if body is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    return c, c.getresponse()


def stations_by_id():
    return {x["id"]: x for x in _stations()["stations"]}


def content():
    return _stations()["content"]


# ---- the school's files (plan phase 5): what Ace the OSCEs showed beside each station ----------------
FILE_LABEL = {"question": "Question", "video": "Video", "answer": "Model answer", "booklet": "Answer booklet", "info": "Info booklet",
              "values": "Normal values", "chart": "Chart", "guidance": "Guidance", "tto": "TTO form", "syringe": "Syringe driver"}
DOC_TYPES = {".pdf": "application/pdf", ".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".md": "text/plain; charset=utf-8", ".txt": "text/plain; charset=utf-8"}


def school_files(station_id):
    """The files a station names (the Revision System's lists), relative to the school folder: the only
    ones this node will serve. Empty on a node without them."""
    base = Path(CFG["school"]).expanduser()
    if not base.is_dir():
        return []
    out = []
    try:
        entry = json.loads(Path(CFG["school_index"]).expanduser().read_text())["stations"].get(station_id) or {}
    except (OSError, ValueError):
        entry = {}
    for r in entry.get("resources") or []:
        if r.get("path") and (base / r["path"]).is_file():
            out.append({"kind": r.get("kind") or "pdf", "title": r.get("title") or Path(r["path"]).stem, "path": r["path"],
                        "transcript": r.get("transcript"), "duration": r.get("duration"), "yours": bool(r.get("yours"))})
    station = next((x for x in (circuit() or {}).get("stations", []) if x["id"] == station_id), {})
    for key, rel in (station.get("files") or {}).items():
        if (base / rel).is_file():
            out.append({"kind": "video" if key == "video" else "pdf", "title": FILE_LABEL.get(key, key), "path": rel})
    return out


# ---- pairing (plan phase 4): a fresh node prints a code; its owner enters it in Finalist's settings ----
PAIR_CODE = None


def pair_code():
    global PAIR_CODE
    if not PAIR_CODE:
        import secrets
        PAIR_CODE = "-".join(secrets.token_hex(2).upper() for _ in range(2))
    return PAIR_CODE


def data_for(w):
    """Where an account's runs, drills and evaluations live on this node."""
    if w.owner:
        return Path(CFG["owner_data"]).expanduser()
    d = Path(CFG["data"]).expanduser() / "users" / w.id
    d.mkdir(parents=True, exist_ok=True)
    return d


def shared():
    """Material every account reads: the A guides and the drill phrasings Claude has learned."""
    return Path(CFG["owner_data"]).expanduser()


# ---- share mode (plan phase 3, 31 Oct 2026) --------------------------------------------------------
# The owner gives one friend's Finalist account two hours of Claude on this node, from the website.
# Like the Revision System's links (Raahat, 27 Oct): extended only in the last 10 minutes, on the
# same session, so the host has to be there; when it lapses, a station already under way may finish
# (30 minutes) but nothing new starts; while it's live, the owner's own stations wait. While a share
# is live the node is reachable from the internet (Tailscale Funnel), since the friend isn't on the
# tailnet; every request still needs a Finalist session. Afterwards it's tailnet-only again.
SHARE_FOR, EXTEND_IN, FINISH = 2 * 60 * 60, 10 * 60, 30 * 60
SHARE_LOCK = threading.Lock()


def share_file():
    d = Path(CFG["data"]).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d / "share.json"


def node_share():
    try:
        return json.loads(share_file().read_text())
    except (OSError, ValueError):
        return {}


def share_save(s):
    share_file().write_text(json.dumps(s, indent=1))


def friends():
    """Who this node has been shared with, most recent first, so sharing again is one tap."""
    try:
        return json.loads((Path(CFG["data"]).expanduser() / "friends.json").read_text())[:8]
    except (OSError, ValueError):
        return []


def friends_save(xs):
    f = Path(CFG["data"]).expanduser() / "friends.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(xs[:8], indent=1))


def share_live():
    """A friend's share, here or in the Revision System: the owner's own Claude stations wait."""
    s = node_share()
    if s.get("email") and time.time() < s.get("until", 0):
        return True
    try:
        return bool(json.loads((shared() / "share.json").read_text()).get("on"))
    except (OSError, ValueError):
        return False


def is_guest(w):
    s = node_share()
    return bool(s.get("email")) and (w.email or "").lower() == s["email"]


def may_use_claude(w, starting=False):
    """The owner; or the friend holding the share: in its two hours, or (not to start a new station)
    in the half-hour after, so a station under way can finish and be saved."""
    if w.owner:
        return True
    if not is_guest(w):
        return False
    until, now = node_share().get("until", 0), time.time()
    return now < until or (not starting and now < until + FINISH)


_URL = {"at": 0, "url": None}


def public_url():
    """This machine's tailnet HTTPS address (asked of Tailscale at most once a minute)."""
    if time.time() - _URL["at"] < 60:
        return _URL["url"]
    _URL.update(at=time.time(), url=_public_url())
    return _URL["url"]


def _public_url():
    try:
        me = json.loads(subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10).stdout)["Self"]
        return f"https://{me['DNSName'].rstrip('.')}:{CFG['public_port']}"
    except Exception:
        return None


def funnel(on):
    """Open the node to the internet for a share, or close it to the tailnet again. Turning Funnel off
    for a port removes its route too, so the tailnet-only one is put back."""
    port, target = str(CFG["public_port"]), f"http://127.0.0.1:{CFG['port']}"
    run = lambda *a: subprocess.run(["tailscale", *a], capture_output=True, text=True, timeout=20)
    if on:
        r = run("funnel", "--bg", f"--https={port}", target)
        log("open to the internet for the share" if r.returncode == 0 else f"couldn't open it to the internet: {r.stderr.strip()[:200]}")
        return r.returncode == 0
    run("funnel", f"--https={port}", "off")
    run("serve", "--bg", f"--https={port}", target)
    log("closed to the internet again (tailnet only)")
    return True


def public_on():
    """The owner's 'reach it from anywhere' (plan phase 5: in place of the Revision System's Away
    password): open to the internet whenever the node runs, for hospital PCs and the like."""
    try:
        return bool(json.loads((Path(CFG["data"]).expanduser() / "access.json").read_text()).get("public"))
    except (OSError, ValueError):
        return False


def set_public(on):
    f = Path(CFG["data"]).expanduser() / "access.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"public": bool(on)}))


def share_view(w):
    """What this account may know of the share."""
    s, now = node_share(), time.time()
    if not s.get("email"):
        return None
    if w.owner:
        return {"role": "owner", "email": s["email"], "until": s["until"], "now": now, "extend_from": s["until"] - EXTEND_IN,
                "finish_until": s["until"] + FINISH, "link": s.get("link")}
    if is_guest(w):
        return {"role": "guest", "until": s["until"], "now": now, "finish_until": s["until"] + FINISH}
    return None


class LocalOwner:
    """The owner, at the node's own PC (its screen needs no Finalist sign-in)."""
    owner, privileged, id = True, True, None

    @property
    def email(self):
        return CFG.get("owner_email") or ""


def local_status():
    """Everything the node's screen shows, in one go."""
    s, now = node_share(), time.time()
    claude = shutil.which("claude")
    if not claude:
        state = "no-claude"
    elif not CFG["owner_id"]:
        state = "pair"
    elif s.get("email") and now < s.get("until", 0):
        state = "sharing"
    else:
        state = "ready"
    def recent(x):              # its last turn (or its start) in the last five minutes
        return time.time() - max([x.get("started", 0)] + [t.get("at", 0) for t in x.get("turns", [])]) < 300
    # a station under way: not marked, not closed (the X in Finalist), and not abandoned (31 Oct: a closed
    # station kept the heart racing for half an hour)
    live = sum(1 for x in list(sim.SIMS.values()) if not x.get("feedback") and not x.get("closed") and recent(x))
    return {"state": state, "now": now, "port": CFG["port"],
            "claude": bool(claude), "paired": bool(CFG["owner_id"]), "owner": CFG.get("owner_email"),
            "code": pair_code() if not CFG["owner_id"] else None,
            "voice": voice_setup.status(CFG), "ranking": circuit() is not None,
            "tailnet": public_url(), "public": public_on(),
            "share": share_view(LocalOwner()) if s.get("email") and now < s.get("until", 0) + FINISH else None,
            "friends": friends(), "live": live, "activity": ACTIVITY[-12:],
            "dfs": {"sets": dfs.status(CFG), "allow": dfs.allowed_emails(CFG)}}


def share_reaper():
    """Ends a share once its finishing half-hour is over: Funnel off, the friend's access gone."""
    while True:
        time.sleep(30)
        with SHARE_LOCK:
            s = node_share()
            if s.get("email") and time.time() > s.get("until", 0) + FINISH:
                share_save({})
                if not public_on():
                    funnel(False)
                log(f"the share with {s['email']} has ended")


def voice_cmd():
    """How to start the voice: the executable runs it itself (`--voice`); from source, a Python with
    numpy, websockets and onnxruntime (voice_python if set, else this one)."""
    v = voice_setup.installed(CFG)
    if not v:
        return None
    r = voice_setup.root(CFG)
    args = ["--port", str(CFG["voice_port"]), "--engine", v["engine"], "--models", str(r / "models"),
            "--voices", str(r / "voices"), "--mouth", v["mouth"], "--backend", v["backend"], "--device", str(v.get("device", 0)),
            "--ear", v.get("ear", "parakeet"), "--drills", str(Path(CFG["drills"]))]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--voice", *args]
    py = Path(CFG.get("voice_python") or "").expanduser()
    return [str(py if py.is_file() else sys.executable), str(NODE / "voice" / "voiced.py"), *args]


def voice_up(wait=60):
    """The voice on demand (it exits by itself after ten idle minutes, freeing the graphics card)."""
    def listening():
        try:
            socket.create_connection(("127.0.0.1", CFG["voice_port"]), 0.3).close()
            return True
        except OSError:
            return False
    if listening():
        return True
    with VOICE_LOCK:
        if not listening():
            cmd = voice_cmd()
            if not cmd:
                return False
            log("starting the voice (a few seconds; longer the very first time)")
            # In this terminal's process group, printing here: Ctrl+C on the node stops it too
            # (Raahat, 30 Oct: a script they can see and kill, not a background service).
            VOICE["proc"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        end = time.time() + wait
        while time.time() < end:
            if listening():
                return True
            if VOICE["proc"] and VOICE["proc"].poll() is not None:
                return False
            time.sleep(0.25)
    return False


def voice_down():
    p = VOICE["proc"]
    if p and p.poll() is None:
        p.terminate()                   # it stops its engine on the way out
        try:
            p.wait(8)
        except subprocess.TimeoutExpired:
            p.kill()


class Handler(BaseHTTPRequestHandler):
    server_version = "FinalistNode/1"

    def log_message(self, *a):
        pass

    def log_request(self, code="-", size="-"):
        # one line per call, without the query string (the voice's carries the session token)
        path, origin = urllib.parse.urlparse(self.path).path, self.headers.get("Origin") or "no origin"
        if path == "/v1/hello":          # a page looking for the node: worth seeing who, when it fails elsewhere
            log(f"{self.command} looked for by {origin}" + ("" if origin in CFG["origins"] else " (not an allowed page)"), screen=False)
        elif self.command != "OPTIONS" and path not in ("/", "/v1/local/status"):
            log(f"{self.command} {path} {code}", screen=False)
            if SCREEN.get(path) and str(code).startswith("2"):
                ACTIVITY.append({"at": time.time(), "msg": SCREEN[path]})

    # ---- plumbing ------------------------------------------------------------------------------

    def cors(self):
        origin = self.headers.get("Origin")
        if origin and origin in CFG["origins"]:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Revise")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def who(self):
        h = self.headers.get("Authorization", "")
        token = h[7:] if h.startswith("Bearer ") else ""
        if not token:
            token = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("access_token") or [""])[0]
        return auth.who(token, CFG)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    # ---- GET -------------------------------------------------------------------------------------

    def local(self):
        """This PC's own browser, not a request through Tailscale or Funnel (which arrive from
        127.0.0.1 too, but say so in their headers): only it sees the node's screen or stops it."""
        return (self.client_address[0] in ("127.0.0.1", "::1") and not self.headers.get("X-Forwarded-For")
                and not any(k.lower().startswith("tailscale-") for k in self.headers.keys())
                and self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if path in ("/", "/v1/local/status"):
            if not self.local():
                return self.send_json(404, {"error": "not found"})
            if path == "/":
                body = (Path(config.NODE) / "data/ui.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return self.send_json(200, local_status())
        if path == "/v1/hello":
            return self.send_json(200, {"node": VERSION, "supabase_url": CFG["supabase_url"], "supabase_anon_key": CFG["supabase_anon_key"],
                                        "voice": bool(voice_setup.installed(CFG)), "paired": bool(CFG["owner_id"]), "public": public_on(),
                                        "ranking": circuit() is not None})
        w = self.who()
        if not w:
            return self.send_json(401, {"error": "Sign in with your Finalist account."})
        if path == "/v1/me":
            return self.send_json(200, {"id": w.id, "email": w.email, "owner": w.owner, "privileged": w.privileged, "claude": may_use_claude(w),
                                        "share": share_view(w)})
        if path == "/v1/share":
            return self.send_json(200, share_view(w) or {})
        if path == "/v1/dfs" or path.startswith("/v1/dfs/"):
            return self.dfs_get(path, w)
        if path == "/v1/voice":
            if not may_use_claude(w):
                return self.send_json(403, {"error": "This node's voice is its owner's, or a friend's while it's shared with them."})
            return self.voice()
        if path == "/v1/circuit":
            if not w.privileged:
                return self.send_json(403, {"error": "The ranked circuit is for privileged accounts."})
            c = circuit()
            return self.send_json(200, c) if c else self.send_json(404, {"error": "This node has no ranking (it lives with the Revision System)."})
        if path.startswith("/v1/revise/"):
            if not revise_allowed(w):
                return self.send_json(403, {"error": "The revision plan is Raahat's own, for now."})
            return self.revise_get(path[len("/v1/revise"):])
        if path in ("/v1/files", "/v1/file"):
            if not w.privileged:
                return self.send_json(403, {"error": "The school's files are for privileged accounts."})
            files = school_files(q.get("station", ""))
            if path == "/v1/files":
                return self.send_json(200, {"files": files})
            names = {f["path"] for f in files} | {f["transcript"] for f in files if f.get("transcript")}
            rel = q.get("path", "")
            if rel not in names:
                return self.send_json(404, {"error": "not found"})
            return self.send_file(Path(CFG["school"]).expanduser() / rel)
        d = data_for(w)
        if path == "/v1/station/runs":
            return self.send_json(200, sim.runs(d, q.get("station", "")))
        if path.startswith("/v1/station/run/"):
            r = sim.run(d, path[len("/v1/station/run/"):])
            return self.send_json(200 if r else 404, r or {"error": "not found"})
        if path == "/v1/review":
            return self.send_json(200, sim.get_review(d, q.get("station", ""), shared=shared()))
        if path == "/v1/drills":
            return self.send_json(200, {"rounds": sim.drills(d, q.get("station", ""), 10), "primed": sim.primed(d, q.get("station", ""))})
        return self.send_json(404, {"error": "not found"})

    # ---- POST ------------------------------------------------------------------------------------

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/v1/local/"):
            # the node's own screen: this PC only, and a header no other site can send without a
            # preflight this node refuses them. Whoever sits at the owner's PC acts as the owner.
            if not self.local() or self.headers.get("X-Finalist-Node") != "1":
                return self.send_json(403, {"error": "forbidden"})
            if path == "/v1/local/stop":
                self.send_json(200, {"stopping": True})
                log("stopping (from the node's screen)")
                voice_down()
                threading.Thread(target=lambda: (time.sleep(0.4), os._exit(0)), daemon=True).start()
                return
            b = self.body()
            if b is None:
                return self.send_json(400, {"error": "bad json"})
            if not CFG["owner_id"]:
                return self.send_json(409, {"error": "Pair the node with your Finalist account first."})
            me = LocalOwner()
            if path == "/v1/local/public":
                return self.set_reach(b, me)
            if path == "/v1/local/voice/setup":
                voice_setup.start(CFG, log)
                return self.send_json(200, {"voice": voice_setup.status(CFG)})
            if path == "/v1/local/friends/forget":
                friends_save([x for x in friends() if x != str(b.get("email") or "").lower()])
                return self.send_json(200, {"friends": friends()})
            if path.startswith("/v1/local/share"):
                return self.share(path.replace("/v1/local", "/v1", 1), b, me)
            if path == "/v1/local/dfs/allow":
                return self.send_json(200, {"allow": dfs.set_allowed(CFG, b.get("emails") or [])})
            return self.send_json(404, {"error": "not found"})
        b = self.body()
        if b is None:
            return self.send_json(400, {"error": "bad json"})
        w = self.who()
        if not w:
            return self.send_json(401, {"error": "Sign in with your Finalist account."})
        if path == "/v1/pair":
            return self.pair(b, w)
        if path.startswith("/v1/revise/"):
            if not revise_allowed(w):
                return self.send_json(403, {"error": "The revision plan is Raahat's own, for now."})
            try:
                c, r = revise_request("POST", "/api/" + path[len("/v1/revise/"):], b, phone=not self.local())
                data = r.read(); c.close()
                return self.send_json(r.status, json.loads(data or b"{}"))
            except (OSError, ValueError) as e:
                return self.send_json(502, {"error": f"The Revision System didn't answer ({e}). Is it installed on this PC?"})
        if path.startswith("/v1/dfs/") and path.endswith("/install"):
            return self.dfs_install(path[len("/v1/dfs/"):-len("/install")], w)
        if path == "/v1/public":
            return self.set_reach(b, w)
        d = data_for(w)
        if path.startswith("/v1/share"):
            return self.share(path, b, w)
        if path == "/v1/drill":
            x = stations_by_id().get(b.get("station"))
            if not x:
                return self.send_json(404, {"error": "unknown station"})
            sim.log_drill(d, x["id"], b.get("found", 0), b.get("total", 0), b.get("missed"), b.get("seconds"), b.get("guesses"))
            return self.send_json(200, {"primed": sim.primed(d, x["id"])})
        if not may_use_claude(w, starting=path == "/v1/station/start"):
            if is_guest(w):
                return self.send_json(403, {"error": "Your session on this node has ended. Your runs are saved; ask for another to carry on."})
            return self.send_json(403, {"error": "Claude on this node is its owner's. A friend can use it when the owner shares it with their Finalist account."})
        if path == "/v1/drill/check":
            return self.drill_check(b)
        if path.startswith("/v1/station/"):
            return self.station(path[len("/v1/station/"):], b, w, d)
        return self.send_json(404, {"error": "not found"})

    def pair(self, b, w):
        """The first owner: whoever signs in to Finalist and enters the code this node printed."""
        if CFG["owner_id"]:
            return self.send_json(409, {"error": "This node already has an owner."})
        if str(b.get("code") or "").strip().upper() != pair_code():
            return self.send_json(403, {"error": "That isn't the code in the node's terminal."})
        CFG["owner_id"] = w.id
        CFG["owner_email"] = w.email
        config.save_owner(w.id, w.email)
        auth.forget()                    # cached answers were made before there was an owner
        log(f"paired: this node is {w.email}'s")
        return self.send_json(200, {"owner": True, "email": w.email})

    def revise_get(self, sub):
        """/state, or /files/<kind>/<name> (the reading PDFs, streamed with byte ranges for the browser's viewer)."""
        phone = not self.local()
        try:
            if sub == "/state":
                c, r = revise_request("GET", "/api/state", phone=phone)
                data = r.read(); c.close()
                return self.send_json(r.status, json.loads(data or b"{}"))
            if sub.startswith("/files/"):
                rng = self.headers.get("Range")
                c, r = revise_request("GET", sub, phone=phone, headers={"Range": rng} if rng else None, timeout=120)
                self.send_response(r.status)
                self.cors()
                for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Content-Disposition"):
                    if r.getheader(k):
                        self.send_header(k, r.getheader(k))
                self.end_headers()
                try:
                    while True:
                        chunk = r.read(1 << 16)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                c.close()
                return
        except OSError as e:
            return self.send_json(502, {"error": f"The Revision System didn't answer ({e}). Is it installed on this PC?"})
        return self.send_json(404, {"error": "not found"})

    def send_file(self, f):
        """Streamed with byte ranges, so a video seeks and a PDF shows its first pages at once."""
        import re as _re
        size = f.stat().st_size
        start, end = 0, size - 1
        m = _re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        partial = bool(m and (m[1] or m[2]))
        if partial:
            start, end = (int(m[1]), min(int(m[2]) if m[2] else end, end)) if m[1] else (max(0, size - int(m[2])), end)
            if start > end:
                start, end, partial = 0, size - 1, False
        self.send_response(206 if partial else 200)
        self.cors()
        self.send_header("Content-Type", DOC_TYPES.get(f.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Disposition", f'inline; filename="{f.name}"')
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            try:
                while left > 0:
                    chunk = fh.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def set_reach(self, b, w):
        if not w.owner:
            return self.send_json(403, {"error": "Only the node's owner can open it to the internet."})
        on = bool(b.get("on"))
        if on and not funnel(True):
            return self.send_json(502, {"error": "Couldn't open it to the internet (Tailscale Funnel). Is Tailscale up on this PC?"})
        set_public(on)
        if not on and not share_live():
            funnel(False)
        log("open to the internet while it runs" if on else "back to your own devices only")
        return self.send_json(200, {"public": on, "url": public_url()})

    def share(self, path, b, w):
        """Start (or replace), extend and stop a share: the owner's alone."""
        if not w.owner:
            return self.send_json(403, {"error": "Only the node's owner can share it."})
        now = time.time()
        with SHARE_LOCK:
            s = node_share()
            if path == "/v1/share":
                email = str(b.get("email") or "").strip().lower()
                if "@" not in email or len(email) > 200:
                    return self.send_json(400, {"error": "Which Finalist account? Their email."})
                if email == (w.email or "").lower():
                    return self.send_json(400, {"error": "That's you: your own stations need no share."})
                url = public_url()
                if not url or not funnel(True):
                    return self.send_json(502, {"error": "Couldn't open the node to the internet (Tailscale Funnel). Is Tailscale up on this PC?"})
                link = f"https://raahat.sh/finalist/join?node={urllib.parse.quote(url, safe='')}"
                s = {"email": email, "until": now + SHARE_FOR, "started": now, "link": link}
                share_save(s)
                friends_save([email] + [x for x in friends() if x != email])
                log(f"shared with {email} for two hours")
            elif path == "/v1/share/extend":
                if not s.get("email"):
                    return self.send_json(409, {"error": "Nothing is shared just now."})
                if not (s["until"] - EXTEND_IN <= now < s["until"] + FINISH):
                    return self.send_json(409, {"error": "You can extend it in its last 10 minutes."})
                s["until"] = max(s["until"], now) + SHARE_FOR
                share_save(s)
                log(f"extended the share with {s['email']} by two hours")
            elif path == "/v1/share/stop":
                if s.get("email"):
                    share_save({})
                    if not public_on():
                        funnel(False)
                    log(f"stopped sharing with {s['email']}")
            else:
                return self.send_json(404, {"error": "not found"})
        return self.send_json(200, share_view(w) or {})

    def drill_check(self, b):
        x = stations_by_id().get(b.get("station"))
        guesses = [str(g)[:80] for g in (b.get("guesses") or []) if str(g).strip()][:25]
        if not x or not guesses:
            return self.send_json(400, {"error": "nothing to check"})
        web = Path(CFG["drills"]).parent
        try:
            drill = sim.drills_with_aliases(web, shared()).get(x["id"])
            res = sim.check_guesses(CFG["model"], x["title"], drill, guesses)
        except Exception as e:
            return self.send_json(502, {"error": str(e)})
        names = {i["answer"] for g in drill["groups"] for i in g["items"]}
        res = [r if r.get("verdict") != "match" or r.get("item") in names else {**r, "verdict": "valid"} for r in res]
        sim.learn_aliases(shared(), x["id"], [(r["guess"], r["item"]) for r in res if r.get("verdict") == "match"])
        return self.send_json(200, {"results": res})

    # ---- DFS: datasets for stations (2 Nov 2026) ----

    def dfs_get(self, path, w):
        """GET /v1/dfs: what's here and whether you may use it; /v1/dfs/<set>: its catalogue;
        /v1/dfs/<set>/<id>: one item (fetched from its source the first time)."""
        guest = may_use_claude(w)
        may = dfs.may_use(w, CFG, guest=guest)
        parts = [x for x in path.split("/")[3:] if x]
        if not parts:
            return self.send_json(200, {"sets": dfs.status(CFG), "may": may, "owner": bool(w.owner)})
        if not may:
            return self.send_json(403, {"error": "This node's datasets are for its owner and the people they allow."})
        if len(parts) == 1:
            c = dfs.catalogue(CFG, parts[0])
            return self.send_json(200 if c else 404, c or {"error": "not found"})
        try:
            it = dfs.item(CFG, parts[0], parts[1])
        except Exception as e:
            return self.send_json(502, {"error": f"Couldn't fetch it from its source ({str(e)[:120]}). Try again in a moment."})
        return self.send_json(200 if it else 404, it or {"error": "not found"})

    def dfs_install(self, name, w):
        """Fetch a whole set, streamed as lines of JSON so the station can show progress."""
        if not dfs.may_use(w, CFG, guest=may_use_claude(w)):
            return self.send_json(403, {"error": "This node's datasets are for its owner and the people they allow."})
        if not dfs.manifest(name):
            return self.send_json(404, {"error": "not found"})
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        def emit(o):
            self.wfile.write((json.dumps(o) + "\n").encode())
            self.wfile.flush()
        log(f"datasets: installing {name}")
        try:
            dfs.install(CFG, name, emit)
        except (OSError, KeyError) as e:
            emit({"error": str(e)})

    def station(self, what, b, w, d):
        """A Patient/Examiner Claude call, streamed as lines of JSON while Claude writes."""
        self.send_response(200)
        self.cors()
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(obj):
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()

        def mine(sim_id):
            """A station belongs to the account that started it."""
            s = sim.SIMS.get(sim_id)
            if not s or s.get("account") != w.id:
                raise KeyError("That station has ended. Start it again.")
        model, by_id = CFG["model"], stations_by_id()
        try:
            if what == "start":
                x = by_id.get(b.get("id"))
                if not x or x["kind"] not in KINDS:
                    raise KeyError("Claude can only run history and communication stations.")
                # one of you at a time on the owner's Claude: a friend's live share comes first (27 Oct)
                if w.owner and share_live():
                    raise KeyError("A friend's link is live, so your own Claude stations wait until it ends (or stop sharing).")

                def first(o):
                    if "sim" in o:                     # tag the new station with its account before anything else
                        sim.SIMS[o["sim"]]["account"] = w.id
                    emit(o)
                sim.start(model, d, {**x, "vocab": station_vocab(x["id"])}, content().get(x["id"], {}), first, voice=bool(b.get("voice")))
            elif what in ("say", "whistle"):
                mine(b.get("sim"))
                # debug mode (1 Nov 2026): the node's owner, testing, can instruct the room out of character
                # and see the tags the node turned away. Never a friend on a share: it would show the case.
                debug, inspect = bool(b.get("ooc")), bool(b.get("inspect"))
                if (debug or inspect) and not w.owner:
                    raise KeyError("Debug mode is for the node's owner.")
                who = sim.say(b.get("sim"), b.get("text") or "", lambda t: emit({"t": t}), event="whistle" if what == "whistle" else None,
                              on_role=lambda r: emit({"who": r}), on_map=lambda o: emit({"map": o}), debug=debug and what == "say",
                              on_reject=(lambda o: emit({"map_rejected": o})) if inspect else None)
                emit({"done": True, "who": who})
            elif what == "case":                      # debug mode: the hidden case, for the owner testing
                mine(b.get("sim"))
                if not w.owner:
                    raise KeyError("Debug mode is for the node's owner.")
                s_ = sim.SIMS[b.get("sim")]
                emit({"case": s_.get("case"), "map": s_.get("map") or []})
            elif what == "mark":
                mine(b.get("sim"))
                sim.mark(b.get("sim"), d, emit, ended_early=bool(b.get("early")), draft=b.get("draft") or "")
                x = by_id.get((sim.SIMS.get(b.get("sim")) or {}).get("station"))
                if x:
                    threading.Thread(target=review, args=(model, x, d), daemon=True).start()
            elif what == "close":                   # closed in Finalist before it was marked
                mine(b.get("sim"))
                if b.get("sim") in sim.SIMS:
                    sim.SIMS[b["sim"]]["closed"] = True
                emit({"ok": True})
            elif what == "cut":
                mine(b.get("sim"))
                sim.cut(b.get("sim"), b.get("heard") or "")
                emit({"ok": True})
            elif what == "mark_run":
                emit({"feedback": sim.remark(d, b.get("run") or "")})
                x = by_id.get(b.get("id"))
                if x:
                    threading.Thread(target=review, args=(model, x, d), daemon=True).start()
            elif what == "review":
                x = by_id.get(b.get("id"))
                if not x:
                    raise KeyError("unknown station")
                emit({"review": review(model, x, d, quiet=False)})
            else:
                raise KeyError("unknown")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                emit({"error": str(e).strip("'\"")})
            except OSError:
                pass

    # ---- the voice -------------------------------------------------------------------------------

    def voice(self):
        """A WebSocket to the voice, passed through byte for byte once the account is checked."""
        if self.headers.get("Upgrade", "").lower() != "websocket":
            return self.send_json(400, {"error": "websocket only"})
        if not voice_up():
            return self.send_json(503, {"error": "The voice isn't set up on this machine."})
        up = socket.create_connection(("127.0.0.1", CFG["voice_port"]))
        head = "GET / HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in self.headers.items()) + "\r\n"
        up.sendall(head.encode())
        self.close_connection = True
        down = self.connection
        down.settimeout(None)

        def pump(a, z):
            try:
                while True:
                    chunk = a.recv(65536)
                    if not chunk:
                        break
                    z.sendall(chunk)
            except OSError:
                pass
            finally:
                for s in (a, z):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        t = threading.Thread(target=pump, args=(up, down), daemon=True)
        t.start()
        pump(down, up)
        t.join(5)
        up.close()


def review(model, x, d, quiet=True):
    try:
        return sim.review(model, d, x, content().get(x["id"], {}))
    except Exception:
        if not quiet:
            raise


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", int(CFG["port"])), Handler)
    srv.daemon_threads = True      # Ctrl+C ends it even mid-station or with the voice's socket open
    log(f"Finalist Node on http://127.0.0.1:{CFG['port']} (Ctrl+C stops it, and the voice it started)", screen=False)
    ACTIVITY.append({"at": time.time(), "msg": "Started"})
    if not shutil.which("claude"):
        log("Claude Code isn't installed, so this node can't run Claude stations yet. Install it:", screen=False)
        log("  Linux or Mac:  curl -fsSL https://claude.ai/install.sh | bash", screen=False)
        log("  Windows:       irm https://claude.ai/install.ps1 | iex   (in PowerShell)", screen=False)
        log("then run  claude  once to sign in, and start this again.", screen=False)
    if not CFG["owner_id"]:
        log(f"not paired yet: in Finalist's settings (signed in), enter the code  {pair_code()}", screen=False)
    if not voice_setup.installed(CFG):
        log("the voice isn't set up: stations are typed until you set it up on the node's screen", screen=False)
    s = node_share()
    if s.get("email"):
        log(f"still shared with {s['email']} for {max(0, int((s['until'] - time.time()) / 60))} more minutes", screen=False)
    if public_on():
        funnel(True)
        log(f"reachable from anywhere at {public_url()} (your Syncra account is still needed)", screen=False)
    threading.Thread(target=share_reaper, daemon=True).start()
    if CFG.get("open_window", True) and "--no-window" not in sys.argv:
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(f"http://127.0.0.1:{CFG['port']}/")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        voice_down()
        log("stopped")


if __name__ == "__main__":
    main()
