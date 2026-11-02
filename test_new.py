"""Tests for the newer features: persistent sessions, media streaming (Range), HTTP upload, voice-call signalling.
Run:  python test_new.py"""
import http.client
import json
import sys
import tempfile
import time
from pathlib import Path

import os
os.environ["WINDROIDA_CALL_GRACE"] = "3"
import common
import server as srv
from client_core import Client

tmp = Path(tempfile.mkdtemp(prefix="windroida_test2_"))
(tmp / "files").mkdir()
common.FILES_DIR = tmp / "files"
common.UPLOADS_DIR = tmp / "uploads"
DB = tmp / "test.db"
passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ok  ", name)
    else:
        failed += 1
        print("  FAIL", name, extra)


def collect(c, kinds, wait=2.0, call_id=None):
    """Wait until an event of one of `kinds` shows up on client c; returns it (or None)."""
    end = time.time() + wait
    while time.time() < end:
        try:
            c.inbox.get(timeout=0.05)()
        except Exception:
            pass
        for e in c.seen:
            if e["event"] in kinds and (call_id is None or e.get("call_id") == call_id):
                c.seen.remove(e)
                return e
    return None


server = srv.WindroidaServer("127.0.0.1", 0, db_path=DB, web_port=0)
port = server.start()
web = server.web_port
PW = "Str0ng-pass1"


ALL = []


def client():
    c = Client()
    c.seen = []
    c.on_event = lambda e: c.seen.append(e)      # keep every pushed event until a test asks for it
    c.connect("127.0.0.1", port)
    ALL.append(c)
    return c


def http_req(method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", web, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    out = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), data)
    conn.close()
    return out


print("== persistent session")
a = client()
r = a.call("register", username="alice", password=PW, display_name="Alice", sec_question="سوال تستی؟",
           sec_answer="x1", device="test-browser")
check("register returns a session token", r["ok"] and len(r.get("session", "")) > 30, r)
tok = r["session"]
a.close()
a2 = client()
r = a2.call("resume", token=tok)
check("resume with token logs back in", r["ok"] and r["me"]["username"] == "alice", r)
check("resumed connection can use the API", a2.call("chats")["ok"])
bad = client()
check("garbage token rejected", not bad.call("resume", token="x" * 43)["ok"])
b = client()
r = b.call("register", username="bob", password=PW, display_name="Bob", sec_question="سوال تستی؟", sec_answer="y1")
btok = r["session"]
r = b.call("change_password", old_password=PW, new_password="Other-pass9")
b2 = client()
check("changing password keeps this device's session", b2.call("resume", token=btok)["ok"])
# a second device logs in, then the first changes password -> second is signed out
b3 = client()
r = b3.call("login", username="bob", password="Other-pass9", device="phone")
tok3 = r["session"]
b.call("change_password", old_password="Other-pass9", new_password="Third-pass8")
check("password change signs out other devices", not client().call("resume", token=tok3)["ok"])
check("current device stays signed in", client().call("resume", token=btok)["ok"])
a2.call("logout")
time.sleep(0.2)
check("logout kills the session token", not client().call("resume", token=tok)["ok"])

print("== media streaming")
a = client()
a.call("login", username="alice", password=PW)
r = a.call("hello", caps=["call"])
akey = r["media_key"]
check("hello returns media key + ice servers", r["ok"] and akey and r["ice_servers"], r)
b = client()
b.call("login", username="bob", password="Third-pass8")
bkey = b.call("hello", caps=["call"])["media_key"]
chat = a.call("open_private", username="bob")["chat"]["id"]

payload = bytes(range(256)) * 4096          # 1 MiB of known bytes
status, hd, body = http_req("POST", f"/upload?k={akey}&chat_id={chat}&name=clip.mp4&text=hi", payload,
                            {"Content-Type": "application/octet-stream"})
j = json.loads(body) if status == 200 else {}
check("HTTP upload works", status == 200 and j.get("ok"), (status, body[:200]))
msg = j.get("message", {})
check("message reports media kind", msg.get("file", {}).get("kind") == "video", msg)
check("upload size recorded", msg.get("file", {}).get("size") == len(payload))
mid = msg.get("id")
ev = collect(b, ["message"])
check("peer got the upload as a message event", ev and ev["message"]["id"] == mid)

status, hd, body = http_req("GET", f"/media/{mid}?k={bkey}")
check("member can fetch media", status == 200 and body == payload)
check("served as video/mp4 inline with Range support", hd["content-type"] == "video/mp4" and
      hd["accept-ranges"] == "bytes" and hd["content-disposition"].startswith("inline"), hd)
status, hd, body = http_req("GET", f"/media/{mid}?k={bkey}", headers={"Range": "bytes=1000-1999"})
check("Range request -> 206 with exact slice", status == 206 and body == payload[1000:2000] and
      hd["content-range"] == f"bytes 1000-1999/{len(payload)}", (status, hd))
status, hd, body = http_req("GET", f"/media/{mid}?k={bkey}", headers={"Range": f"bytes={len(payload) - 10}-"})
check("open-ended Range (seek to the end)", status == 206 and body == payload[-10:])
status, hd, body = http_req("GET", f"/media/{mid}?k={bkey}", headers={"Range": "bytes=-5"})
check("suffix Range", status == 206 and body == payload[-5:])
status, hd, body = http_req("GET", f"/media/{mid}?k={bkey}", headers={"Range": f"bytes={len(payload) + 5}-"})
check("unsatisfiable Range -> 416", status == 416)
status, _, _ = http_req("GET", f"/media/{mid}?k=bogus")
check("bad key refused", status == 403)

c = client()
c.call("register", username="carol", password=PW, sec_question="سوال تستی؟", sec_answer="z1")
ckey = c.call("hello", caps=[])["media_key"]
status, _, _ = http_req("GET", f"/media/{mid}?k={ckey}")
check("non-member can't stream", status == 404)
status, _, body = http_req("POST", f"/upload?k={ckey}&chat_id={chat}&name=x.txt", b"hello")
check("non-member can't upload", status == 400, (status, body))
status, _, body = http_req("POST", f"/upload?k={akey}&chat_id={chat}&name=a.html", b"<script>alert(1)</script>")
hid = json.loads(body)["message"]["id"]
status, hd, body = http_req("GET", f"/media/{hid}?k={akey}")
check("HTML upload is never inline (download only)", hd["content-disposition"].startswith("attachment") and
      hd["content-type"] == "application/octet-stream" and hd["x-content-type-options"] == "nosniff", hd)
status, _, _ = http_req("POST", f"/upload?k={akey}&chat_id={chat}&name=e.bin", b"")
check("empty upload refused", status == 400)
status, hd, body = http_req("POST", f"/upload?k={akey}&chat_id={chat}&name=%D8%B3%D9%84%D8%A7%D9%85.mp3", b"ID3" + b"0" * 50)
pm = json.loads(body)["message"]
check("Persian file name preserved", pm["file"]["name"] == "سلام.mp3" and pm["file"]["kind"] == "audio", pm)
a.call("delete", message_id=mid)
status, _, _ = http_req("GET", f"/media/{mid}?k={bkey}")
check("deleted file is gone", status == 404)
check("no temp files left behind", not list((common.UPLOADS_DIR / ".tmp").glob("*")))

print("== voice call signalling")
a = client(); a.call("login", username="alice", password=PW); a.call("hello", caps=["call"])
b = client(); b.call("login", username="bob", password="Third-pass8"); b.call("hello", caps=["call"])
d = client(); d.call("login", username="bob", password="Third-pass8")        # desktop: no call capability
r = a.call("call_invite", chat_id=chat)
check("invite succeeds", r["ok"], r)
cid = r.get("call_id")
ev = collect(b, ["call_incoming"], call_id=cid)
check("callee rings with caller info", ev and ev["call_id"] == cid and ev["from"]["username"] == "alice", ev)
check("client without call support is not rung", collect(d, ["call_incoming"], 0.3) is None)
r = c.call("call_end", call_id=cid)
check("outsider can't touch the call", not r["ok"])
r = b.call("call_signal", call_id=cid, data={"type": "offer", "sdp": "x"})
check("no signalling before the call is answered", not r["ok"])
r = a.call("call_invite", chat_id=chat)
check("second call while ringing refused", not r["ok"], r)
check("accept", b.call("call_accept", call_id=cid)["ok"])
ev = collect(a, ["call_accepted"], call_id=cid)
check("caller is told it was accepted", ev and ev["call_id"] == cid)
check("offer relayed", a.call("call_signal", call_id=cid, data={"type": "offer", "sdp": "v=0"})["ok"])
ev = collect(b, ["call_signal"])
check("callee receives the offer", ev and ev["data"]["sdp"] == "v=0", ev)
b.call("call_signal", call_id=cid, data={"type": "ice", "candidate": {"candidate": "c"}})
ev = collect(a, ["call_signal"])
check("ICE candidate relayed back", ev and ev["data"]["type"] == "ice")
time.sleep(1.1)
check("hang up", a.call("call_end", call_id=cid)["ok"])
ev = collect(b, ["call_ended"], call_id=cid)
check("peer is told the call ended", ev and ev["reason"] == "hangup", ev)
h = a.call("history", chat_id=chat, limit=5)["messages"]
check("call is logged in the chat", any("📞 تماس صوتی" in m["body"] for m in h), [m["body"] for m in h])
check("both users are free again", a.call("call_invite", chat_id=chat)["ok"])
cid2 = None
ev = collect(b, ["call_incoming"]); cid2 = ev["call_id"]
check("callee can reject", b.call("call_end", call_id=cid2)["ok"])
ev = collect(a, ["call_ended"], call_id=cid2)
check("caller sees 'rejected'", ev and ev["reason"] == "rejected", ev)
# caller drops the connection while ringing/in call
r = a.call("call_invite", chat_id=chat); collect(b, ["call_incoming"], call_id=r["call_id"])
b.call("call_accept", call_id=r["call_id"])
a.close()
ev = collect(b, ["call_peer"], 3, call_id=r["call_id"])
check("peer drop -> other side told 'reconnecting' (call survives)", ev and ev["state"] == "reconnecting", ev)
a = client(); a.call("login", username="alice", password=PW); a.call("hello", caps=["call"])
rj = a.call("call_rejoin", call_id=r["call_id"])
check("dropped user rejoins the same call", rj["ok"] and rj["peer_online"], rj)
ev = collect(b, ["call_peer"], 3, call_id=r["call_id"])
check("other side told 'connected'", ev and ev["state"] == "connected", ev)
a.close()
ev = collect(b, ["call_ended"], 6, call_id=r["call_id"])
check("no rejoin within grace -> call ends", ev and ev["reason"] == "disconnected", ev)
a = client(); a.call("login", username="alice", password=PW); a.call("hello", caps=["call"])
g = a.call("create_group", title="G", usernames=["bob"])["chat"]["id"]
check("group calls refused", not a.call("call_invite", chat_id=g)["ok"])
for x in ALL:
    if x is not a and x is not c:
        x.close()
time.sleep(0.5)
r = a.call("call_invite", chat_id=chat)
check("offline callee -> clear error", not r["ok"] and "آنلاین" in r["error"], r)

print(f"\n{passed} passed, {failed} failed")
server.stop()
sys.exit(1 if failed else 0)
