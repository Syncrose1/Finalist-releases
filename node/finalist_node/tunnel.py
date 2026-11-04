"""The node from anywhere, with nothing to set up (4 Nov 2026, Raahat: Tailscale is too much for most
people). A Cloudflare quick tunnel: `cloudflared` (found on PATH, or fetched once from Cloudflare's GitHub
releases into the node's data folder) gives this node a public https://….trycloudflare.com address, with no
account or sign-up. The address changes on every start; the owner's Finalist records it against their account
(Supabase, node_endpoints) so their devices and friends find it. Every request still needs a Finalist sign-in.

Tailscale Funnel, where it's set up, is used as well; the tunnel is the default for everyone else.
Config: "tunnel": false turns it off.
"""

import os, platform, re, shutil, stat, subprocess, threading, time, urllib.request
from pathlib import Path

URL = {"url": None, "at": 0, "error": None}
RELEASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
_proc = {"p": None}


def _asset():
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "amd64"
    if os.name == "nt":
        return f"cloudflared-windows-{arch}.exe"
    if platform.system() == "Darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    return f"cloudflared-linux-{arch}"


def binary(data_dir, log):
    """cloudflared: on PATH, else in <data>/bin, else fetched there now (about 35 MB, once)."""
    found = shutil.which("cloudflared")
    if found:
        return found
    d = Path(data_dir).expanduser() / "bin"
    exe = d / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if exe.exists():
        return str(exe)
    d.mkdir(parents=True, exist_ok=True)
    name = _asset()
    log("fetching cloudflared, to reach this node from anywhere (about 35 MB, once)")
    tmp = d / (name + ".part")
    with urllib.request.urlopen(urllib.request.Request(RELEASE + name, headers={"User-Agent": "FinalistNode"}), timeout=300) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if name.endswith(".tgz"):
        import tarfile
        with tarfile.open(tmp) as t:
            m = next(x for x in t.getmembers() if x.name.endswith("cloudflared"))
            with t.extractfile(m) as src, open(exe, "wb") as out:
                shutil.copyfileobj(src, out)
        tmp.unlink()
    else:
        tmp.replace(exe)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(exe)


def _die_with_us():
    """Linux: cloudflared goes if the node is killed outright, never left running on its own."""
    try:
        import ctypes, signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
    except Exception:
        pass


def start(port, data_dir, log):
    """Keep a quick tunnel up for http://127.0.0.1:<port>, restarting it if it drops."""
    def run():
        wait = 5
        while True:
            try:
                exe = binary(data_dir, log)
                p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                     preexec_fn=_die_with_us if platform.system() == "Linux" else None)
                _proc["p"] = p
                for line in p.stderr:
                    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                    if m and URL["url"] != m.group(0):
                        URL.update(url=m.group(0), at=time.time(), error=None)
                        log(f"reachable from anywhere at {m.group(0)} (a Finalist sign-in is still needed)")
                        wait = 5
                p.wait()
                URL.update(url=None, error=f"the tunnel stopped (code {p.returncode})")
            except Exception as e:
                URL.update(url=None, error=f"{type(e).__name__}: {e}")
            log(f"tunnel down ({URL['error']}): trying again in {wait} s", screen=False)
            time.sleep(wait)
            wait = min(300, wait * 2)
    threading.Thread(target=run, daemon=True, name="tunnel").start()


def stop():
    p = _proc.get("p")
    if p and p.poll() is None:
        p.terminate()
