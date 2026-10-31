"""The Finalist Node as one executable (PyInstaller's entry point): the same as `python -m finalist_node`,
and, with --voice, the node's voice (voice/voiced.py), which the node starts itself when a station speaks."""
import sys

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
