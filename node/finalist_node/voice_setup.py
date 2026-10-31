"""Setting up the voice, from the node's screen: one engine on every machine (Raahat, 31 Oct 2026:
"I would prefer one backend"), audio.cpp's prebuilt server, with the models and station voices.

  engine   audio.cpp v0.9.3: Vulkan on Linux and Windows (NVIDIA, AMD and Intel graphics, and the CPU
           when there are none), Metal on a Mac; the CPU-only build if Vulkan won't load
  voices   station-voices-1.zip from Finalist-releases: the ten VCTK clips and their Pocket voice states
  mouth    Pocket TTS (CPU, everywhere), or Ming 3.0 where a graphics card keeps it well ahead of real
           time: timed here, on this machine. On a Mac Ming is Experimental.
  ear      Parakeet TDT 0.6B v3

Everything is pinned (versions and SHA-256) and lands in <data>/voice: engine/, models/, voices/ and
voice.json, which voice_up() reads. A file already there with the right size is kept, so running it
again only fetches what's missing.
"""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

ENGINE = "https://github.com/0xShug0/audio.cpp/releases/download/v0.9.3/"
BUILDS = {   # (os, machine) -> [(asset, size, sha256, backend)], tried in order
    ("linux", "x86_64"): [("audio-v0.9.3-bin-ubuntu-x64-vulkan-portable.tar.gz", 77051422, "10ac6b5cb72bc9266e5a600407dbcc2a3eaf87139a3e01c064bf3035d11f81b8", "vulkan"),
                          ("audio-v0.9.3-bin-ubuntu-x64-cpu-portable.tar.gz", 63289061, "02156f7a7bd9924902cec26be35087d3806c7e348be344b1ec28cac0c1af3929", "cpu")],
    ("windows", "amd64"): [("audio-v0.9.3-bin-windows-x64-vulkan-portable.zip", 82088153, "51f8d8ce6cc7789bc9dc3d15f35e05f143aaf2ce1cbecc525a4104bcdde82a42", "vulkan"),
                           ("audio-v0.9.3-bin-windows-x64-cpu-portable.zip", 30409504, "f5fa4662b7ec323dbe31d37e224e46159b064836a9005da384551a5248771eeb", "cpu")],
    ("darwin", "arm64"): [("audio-v0.9.3-bin-macos-arm64-metal.tar.gz", 34847832, "70a84c2b61ce5ef99547a12affab9b0d40daf08666baf7da4f940d5c7b722819", "metal")],
    ("darwin", "x86_64"): [("audio-v0.9.3-bin-macos-x64-metal.tar.gz", 36812821, "81238e3957bbf840d15094c34262fa93609caa0233c3dccc1e0f64fb47f5e51b", "metal")],
}
HF = "https://huggingface.co/{repo}/resolve/{rev}/{path}"
GGUF = ("audio-cpp/audio.cpp-gguf", "874fcb41ececda20bcbc0782d6b4ea7f7d069ea8")
MODELS = {   # name -> (repo, revision, path, size, sha256)
    "pocket": (*GGUF, "PocketTTS-GGUF/english/pocket-tts-english-q8_0.gguf", 127867968, "3f71cf6a51f93c2f2e554e98970a3f49fb9a98d0c64bed5a815e65be3d2ff588"),
    "ear": (*GGUF, "Parakeet-TDT-0.6B-v3-GGUF/parakeet-tdt-0.6b-v3-q8_0.gguf", 915735760, "6fa498f10f8cb606f041aa9512733cfee842a932359b00b0c45b9b723c4f4f4b"),
    "ming": ("audio-cpp/Ming-3.0-TTS-0.5B-GGUF", "2eb8d37c7f10672e69c8aa33091baa04c461b66c", "ming-3.0-tts-0.5b-q8_0.gguf", 1742280896,
             "fcf2aa25e8e9cf2065a936e506581735c64d6255fb7869f09fbb61a2dde09aa1"),
}
VOICES = ("https://github.com/Syncrose1/Finalist-releases/releases/download/voices-1/station-voices-1.zip", 60399901,
          "8340910073ca0c7c9b157992dac55c8d3a85cc66428501852fca756709d5361f")
MING_BAR = 0.8      # Ming's whole-line time / audio time must be under this. Streaming costs ~20% more (0.65 -> 0.78
                    # on a 3060 over Vulkan, and 0.78 plays smoothly), so this keeps it ahead of playback.
EXPERIMENTAL = "Experimental: please let Raahat know if you're having issues (or if it works well)."

STATE = {"phase": "idle", "step": "", "done": 0, "total": 0, "error": None}
LOCK = threading.Lock()


def root(cfg):
    return Path(cfg["data"]).expanduser() / "voice"


def installed(cfg):
    """voice.json, if the voice is set up and its files are all still there."""
    try:
        v = json.loads((root(cfg) / "voice.json").read_text())
    except (OSError, ValueError):
        return None
    r = root(cfg)
    need = [Path(v["engine"]) / server_name(), r / "voices" / "voices.json", r / "models" / MODELS["ear"][2], r / "models" / MODELS[v["mouth"]][2]]
    return v if all(p.exists() for p in need) else None


def server_name():
    return "audiocpp_server.exe" if os.name == "nt" else "audiocpp_server"


def status(cfg):
    v = installed(cfg)
    return {"installed": v, "setup": dict(STATE), "builds": bool(BUILDS.get(plat()))}


def plat():
    return sys.platform.replace("win32", "windows"), platform.machine().lower()


def start(cfg, log):
    with LOCK:
        if STATE["phase"] in ("downloading", "checking"):
            return False
        STATE.update(phase="downloading", step="Starting", done=0, total=0, error=None)
    threading.Thread(target=_run, args=(cfg, log), daemon=True).start()
    return True


def _fetch(url, dest, size, sha, step):
    """Downloads to dest (verified by size and SHA-256), unless it's already there at that size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size == size:
        return dest
    STATE.update(step=step, done=0, total=size)
    tmp = dest.with_name(dest.name + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "FinalistNode"})
    with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            h.update(b)
            STATE["done"] += len(b)
    if tmp.stat().st_size != size or h.hexdigest() != sha:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{step}: the download didn't match what was expected; try again")
    tmp.replace(dest)
    return dest


def _unpack(archive, into):
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(into)
    else:
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data") if sys.version_info >= (3, 12) else t.extractall(into)
    exe = next((p for p in into.rglob(server_name()) if p.is_file()), None)
    if not exe:
        raise RuntimeError("the voice engine's download had no server in it")
    for p in exe.parent.iterdir():                       # tar keeps modes; a zip may not
        if p.is_file() and p.name.startswith("audiocpp_"):
            p.chmod(p.stat().st_mode | 0o755)
    return exe.parent


def devices(engine, backend):
    """The engine's own list of devices: [(backend, index, name, is_gpu)]."""
    cli = engine / ("audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli")
    env = {**os.environ, "LD_LIBRARY_PATH": str(engine)}
    out = subprocess.run([str(cli), "--list-devices", "--backend", backend], cwd=engine, env=env, capture_output=True, text=True, timeout=60).stdout
    found = []
    for m in re.finditer(r'^(\w+):(\d+) "([^"]+)" \[(\w+)\]', out, re.M):
        kind, i, name, what = m.groups()
        gpu = what.upper() == "GPU" and not re.search(r"llvmpipe|swiftshader|software", name, re.I)
        found.append((kind.lower(), int(i), name, gpu))
    return found


def time_ming(engine, backend, device, models, voices):
    """Ming's whole-line time / audio time on this machine, the model loaded once (a warm-up line first)."""
    v = json.loads((voices / "voices.json").read_text())[0]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # two warm-up lines (a short first one doesn't finish the graphics card's first-run setup), then the timed one
        (tmp / "lines.txt").write_text("Thank you. Please take a seat.\nCould you tell me what brought you in today?\n"
                                       "It started about three days ago, a tight pain across my chest, and it goes down my left arm when I walk up the stairs.\n")
        cli = engine / ("audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli")
        env = {**os.environ, "LD_LIBRARY_PATH": str(engine)}
        r = subprocess.run([str(cli), "--family", "ming3_tts", "--task", "tts", "--backend", backend, "--device", str(device),
                            "--model", str(models / MODELS["ming"][2]), "--voice-ref", str(voices / v["file"]),
                            "--request-option", f"reference_text={v['text']}", "--batch-text-file", str(tmp / "lines.txt"),
                            "--out-dir", str(tmp / "out"), "--batch-manifest-out", str(tmp / "m.json"), "--log"],
                           cwd=engine, env=env, capture_output=True, text=True, timeout=900)
        walls = [float(x) for x in re.findall(r"session\.wall_ms ([0-9.]+)", r.stdout)]
        m = json.loads((tmp / "m.json").read_text())
        items = m.get("items") or m.get("requests") or m
        secs = [i["samples"] / i["sample_rate"] for i in items]
        if len(walls) < 3 or len(secs) < 3 or not secs[2]:
            raise RuntimeError("couldn't time Ming here")
        return walls[2] / 1000 / secs[2]


def _run(cfg, log):
    r = root(cfg)
    try:
        builds = BUILDS.get(plat())
        if not builds:
            raise RuntimeError(f"no voice engine is built for this computer ({' '.join(plat())})")
        engine, backend, devs = None, None, []
        for asset, size, sha, be in builds:
            arch = _fetch(ENGINE + asset, r / "downloads" / asset, size, sha, "The voice engine")
            STATE.update(step="Unpacking the voice engine", done=0, total=0)
            try:
                engine = _unpack(arch, r / "engine" / re.sub(r"\.(tar\.gz|zip)$", "", asset))
                devs = devices(engine, be)
                backend = be
                break
            except Exception as e:
                log(f"voice engine {asset} won't run here ({e}); trying the next")
        if not engine:
            raise RuntimeError("the voice engine wouldn't run on this computer")
        vz = _fetch(VOICES[0], r / "downloads" / "station-voices-1.zip", VOICES[1], VOICES[2], "The station voices")
        if not (r / "voices" / "voices.json").exists():
            with zipfile.ZipFile(vz) as z:
                z.extractall(r / "voices")
        for name, label in (("pocket", "Pocket, the everyday voice"), ("ear", "Parakeet, the ear")):
            repo, rev, path, size, sha = MODELS[name]
            _fetch(HF.format(repo=repo, rev=rev, path=path), r / "models" / path, size, sha, label)
        gpu = next((d for d in devs if d[3]), None)
        choice = {"mouth": "pocket", "backend": "cpu", "device": 0, "engine": str(engine), "gpu": gpu[2] if gpu else None,
                  "ming_rtf": None, "experimental": None, "at": time.time()}
        if gpu:
            repo, rev, path, size, sha = MODELS["ming"]
            _fetch(HF.format(repo=repo, rev=rev, path=path), r / "models" / path, size, sha, "Ming, the richer voice")
            STATE.update(phase="checking", step=f"Timing Ming on {gpu[2]}", done=0, total=0)
            try:
                rtf = time_ming(engine, gpu[0], gpu[1], r / "models", r / "voices")
                choice["ming_rtf"] = round(rtf, 2)
                log(f"Ming on {gpu[2]}: {rtf:.2f}x real time ({'fast enough' if rtf < MING_BAR else 'too slow, so Pocket'})")
                # the graphics card works: the ear uses it either way (Parakeet: 0.08 s there, ~1 s on a CPU)
                choice.update(backend=gpu[0], device=gpu[1])
                if rtf < MING_BAR:
                    choice.update(mouth="ming", experimental=EXPERIMENTAL if sys.platform == "darwin" else None)
            except Exception as e:
                log(f"couldn't time Ming on {gpu[2]}: {e}; Pocket on the CPU it is")
        (r / "voice.json").write_text(json.dumps(choice, indent=1))
        shutil.rmtree(r / "downloads", ignore_errors=True)
        STATE.update(phase="ready", step="", done=0, total=0)
        on = choice["gpu"] if choice["backend"] != "cpu" else "the processor"
        log(f"the voice is set up: {'Ming' if choice['mouth'] == 'ming' else 'Pocket'} on {on}")
    except Exception as e:
        STATE.update(phase="failed", error=str(e))
        log(f"setting up the voice failed: {e}")
