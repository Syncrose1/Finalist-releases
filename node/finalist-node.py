"""The Finalist Node as one executable (PyInstaller's entry point): the same as `python -m finalist_node`,
and, with --voice, the node's voice (voice/voiced.py), which the node starts itself when a station speaks."""
import os
import sys

# PyInstaller points LD_LIBRARY_PATH (DYLD_* on a Mac) at its own bundled libraries, and every program the
# node starts inherits it: the system's /bin/sh then died on the bundle's readline ("undefined symbol:
# rl_trim_arg_from_keyseq", 31 Oct), so the browser never opened its screen, and Claude Code, Tailscale
# and the voice engine were at risk too. Children get the system's own settings back; this process has
# already loaded what it needs.
if getattr(sys, "frozen", False):
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        orig = os.environ.pop(var + "_ORIG", None)
        if orig is not None:
            os.environ[var] = orig
        else:
            os.environ.pop(var, None)

# Opened from a file manager or as a Mac .app there's no terminal: print into nothing rather than fail.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

if "--voice" in sys.argv:
    sys.argv.remove("--voice")
    import runpy
    # named here so the executable carries everything voiced.py and speakable.py import (they ship as data)
    import argparse, asyncio, atexit, collections, http.client, io, json, os, queue, re, signal, struct  # noqa: F401,E401
    import subprocess, threading, time, traceback, uuid, wave  # noqa: F401,E401
    import numpy, onnxruntime, websockets, websockets.asyncio.server  # noqa: F401,E401
    from finalist_node import config
    runpy.run_path(str(config.NODE / "voice" / "voiced.py"), run_name="__main__")
else:
    from finalist_node.server import main
    main()
