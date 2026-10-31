"""The node's settings: ~/.config/finalist-node/config.json, over defaults that work on any machine.

  owner_id          the Finalist account that owns this node, set by pairing (the first run prints a
                    code; enter it in Finalist's settings, signed in)
  supabase_url      Finalist's Supabase project and its anon key: public values (they're in every
  supabase_anon_key page Finalist serves), built in so the node needs no Finalist checkout
  data              runs, drill rounds and evaluations, under data/users/<account id>
  owner_data        the owner's: on a PC with the Revision System, its data folder (one history),
                    else `data`
  stations, drills  the station list with learning points, and the recall drills: shipped in data/
                    (Finalist's own content, public-safe)
  circuit           the private ranking and past-finals record: only on a PC with the Revision
                    System; elsewhere there is none (and no Ace the OSCEs (elevated) from that node)
  origins           pages allowed to call the node from a browser
  voice_python, voice_port
                    the voice; absent means typed stations only
  public_port       the tailnet (and, while shared, the internet's) HTTPS port
  model             Claude model alias for stations (follows the newest Sonnet)
"""
import json
import os
from pathlib import Path

HOME = Path.home()
import sys
# where data/ and voice/ are: beside the package, or inside the bundle when run as one executable
NODE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
FILE = Path(os.environ.get("FINALIST_NODE_CONFIG", HOME / ".config/finalist-node/config.json"))
REVISION = HOME / ".local/share/revise"

DEFAULTS = {
    "port": 47620,
    "owner_id": None,
    "supabase_url": "https://zmvdlokhequswpdwtqay.supabase.co",
    "supabase_anon_key": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InptdmRsb2toZXF1c3dwZHd0cWF5Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM1NDAxNjYsImV4cCI6MjA4OTExNjE2Nn0.1UShpzv1bqp-E5SjCSBY29Ymgybyvv_r_PkeSsWfsRs",
    "data": str(HOME / ".local/share/finalist-node"),
    "owner_data": str(REVISION) if REVISION.is_dir() else str(HOME / ".local/share/finalist-node"),
    "stations": str(NODE / "data/stations.json"),
    "drills": str(NODE / "data/drills.json"),
    "circuit": str(HOME / "RevisionSystem/circuit.json"),
    # the school's own files (videos, booklets, PDFs) and Raahat's notes: like the ranking, only on a PC
    # with the Revision System; served to privileged accounts, and only the files a station names
    "school": str(HOME / "FinalYearReady"),
    "school_index": str(HOME / "RevisionSystem/web/binder-content.json"),
    "origins": ["https://raahat.sh", "http://127.0.0.1:47613", "http://localhost:47613", "http://localhost:3000"],
    "voice_python": "",          # from source: a Python with numpy, websockets and onnxruntime (else this one)
    "voice_port": 47615,
    "public_port": 8443,
    "model": "sonnet",
    "owner_email": None,      # for the node's screen; saved at pairing
    "open_window": True,      # open the node's screen in the browser when it starts (--no-window: don't)
}


def load():
    cfg = dict(DEFAULTS)
    if FILE.exists():
        cfg.update(json.loads(FILE.read_text()))
    return cfg


def save_owner(owner_id, email=None):
    """Pairing: record whose node this is."""
    FILE.parent.mkdir(parents=True, exist_ok=True)
    cur = json.loads(FILE.read_text()) if FILE.exists() else {}
    cur["owner_id"] = owner_id
    if email:
        cur["owner_email"] = email
    FILE.write_text(json.dumps(cur, indent=1))
