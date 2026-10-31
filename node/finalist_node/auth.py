"""Who is asking: a Finalist (Supabase) session, checked with Supabase itself.

The shared Syncratic project signs sessions with ES256, which the standard library can't verify, so
the node asks Supabase who a token belongs to (`GET /auth/v1/user`), the same check Binder's
middleware makes with `auth.getUser(token)`. Answers are cached until the token expires, so a
station's many requests cost one round trip.
"""
import base64
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request

_CACHE, _LOCK = {}, threading.Lock()


class Who:
    def __init__(self, user, owner_id):
        self.id = user["id"]
        self.email = user.get("email") or ""
        meta = user.get("app_metadata") or {}
        self.owner = self.id == owner_id
        # the owner always sees their own material; anyone else only if flagged on their account
        self.privileged = self.owner or bool(meta.get("privileged"))

    def __repr__(self):
        return f"<{self.email or self.id}{' owner' if self.owner else ''}{' privileged' if self.privileged else ''}>"


def _expiry(token):
    """The token's own exp claim (read, not trusted: Supabase is what vouches for it)."""
    try:
        body = token.split(".")[1]
        return float(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))).get("exp") or 0)
    except Exception:
        return time.time() + 60


def forget():
    """Drop cached answers (after pairing: who owns the node has changed)."""
    with _LOCK:
        _CACHE.clear()


def who(token, cfg):
    """The account behind a session token, or None if Supabase doesn't recognise it."""
    if not token:
        return None
    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    req = urllib.request.Request(f"{cfg['supabase_url']}/auth/v1/user",
                                 headers={"apikey": cfg["supabase_anon_key"], "Authorization": f"Bearer {token}"})
    try:
        user = json.load(urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError:
        return None
    w = Who(user, cfg.get("owner_id"))
    with _LOCK:
        _CACHE[key] = (min(_expiry(token), now + 3600), w)
        if len(_CACHE) > 500:
            for k in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
                _CACHE.pop(k, None)
    return w
