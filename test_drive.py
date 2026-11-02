"""Smoke test for op_drive_token (Cloud Drive SSO bridge).  Run:  python test_drive.py"""
import hashlib
import hmac
import sys
import tempfile
import time
from pathlib import Path

import common
import server as srv
from client_core import Client

tmp = Path(tempfile.mkdtemp(prefix="windroida_drive_test_"))
(tmp / "files").mkdir()
common.FILES_DIR = tmp / "files"
common.UPLOADS_DIR = tmp / "uploads"
DB = tmp / "test.db"

passed = failed = 0
def check(name, cond, extra=""):
    global passed, failed
    if cond: passed += 1; print("  ok  ", name)
    else: failed += 1; print("  FAIL", name, extra)

server = srv.WindroidaServer("127.0.0.1", 0, db_path=DB)
port = server.start()

def new_client():
    c = Client(); c.connect("127.0.0.1", port); return c

PW = "Str0ng-pass1"
a = new_client()
r = a.call("register", username="alice", password=PW, display_name="Alice", sec_question="اسم اولین مدرسه‌ات؟", sec_answer="x1")
check("alice registered", r["ok"], r)

# not logged in yet -> drive_token must be refused (it's not in the `public` ops set)
z = new_client()
r = z.call("drive_token")
check("drive_token refused when not logged in", not r.get("ok", True), r)

r = a.call("drive_token")
check("drive_token ok when logged in", r["ok"], r)
tok = r.get("token", ""); port_hint = r.get("port")
check("token has 4 dot-separated parts", len(tok.split(".")) == 4, tok)
check("port hint present and looks like a port", isinstance(port_hint, int) and 1 <= port_hint <= 65535, port_hint)

username, expiry_s, nonce, sig = tok.split(".")
check("token identifies alice", username == "alice", username)

secret_path = tmp / "drive_sso.key"
check("drive_sso.key written to data dir", secret_path.exists())
secret = secret_path.read_text().strip()
check("secret looks like 32+ bytes hex", len(secret) >= 32)
expected = hmac.new(secret.encode(), f"{username}:{expiry_s}:{nonce}".encode(), "sha256").hexdigest()
check("signature verifies against drive_sso.key", hmac.compare_digest(expected, sig))

r2 = a.call("drive_token")
tok2 = r2.get("token", "")
check("two tokens in a row differ (nonce prevents collision)", tok2 != tok, (tok, tok2))

# Ghab (قاب) gets its own token with its own key: a Cloud Drive token must never verify for Ghab
z2 = new_client()
check("ghab_token refused when not logged in", not z2.call("ghab_token").get("ok", True))
g = a.call("ghab_token")
check("ghab_token ok", g["ok"] and len(g.get("token", "").split(".")) == 4, g)
gu, ge, gn, gs = g["token"].split(".")
gkey = (tmp / "ghab_sso.key").read_text().strip()
check("ghab_sso.key exists and differs from drive key", len(gkey) >= 32 and gkey != secret)
check("ghab sig verifies with ghab key", hmac.compare_digest(hmac.new(gkey.encode(), f"{gu}:{ge}:{gn}".encode(), "sha256").hexdigest(), gs))
check("ghab sig does NOT verify with drive key", not hmac.compare_digest(hmac.new(secret.encode(), f"{gu}:{ge}:{gn}".encode(), "sha256").hexdigest(), gs))
check("ghab port hint", g.get("port") == 8084, g.get("port"))

server.stop() if hasattr(server, "stop") else None
print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
