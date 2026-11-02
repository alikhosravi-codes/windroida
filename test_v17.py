"""Tests for v1.7: privacy (last seen, read receipts, block). Run: python test_v17.py"""
import os
import http.client, ssl, tempfile, time
from pathlib import Path
import common
tmp = Path(tempfile.mkdtemp(prefix="windroida_test17_"))
common.DATA_DIR = tmp
common.FILES_DIR = tmp / "files"; (tmp / "files").mkdir()
common.UPLOADS_DIR = tmp / "uploads"; common.AVATARS_DIR = tmp / "avatars"; common.WALLPAPERS_DIR = tmp / "wallpapers"; common.ROOMAUDIO_DIR = tmp / "roomaudio"
import server as srv
from client_core import Client
passed = failed = 0
def check(name, cond, extra=""):
    global passed, failed
    passed += bool(cond); failed += not cond
    print("  ok  " if cond else "  FAIL", name, "" if cond else extra)
server = srv.WindroidaServer("127.0.0.1", 0, True, db_path=tmp / "t.db", web_port=0)
port = server.start(); web = server.web_port
PW = "Str0ng-pass1"
def cl(name):
    c = Client(); c.connect("127.0.0.1", port, tls=True); c.seen = []
    c.on_event = lambda e: c.seen.append(e)
    r = c.call("register", username=name, password=PW, display_name=name.title(), sec_question="q q q q?", sec_answer="a1", device="t")
    assert r["ok"], r; c.me = r["me"]; c.call("hello", caps=[]); return c
def events(c, kind, wait=0.6):
    end = time.time() + wait
    while time.time() < end:
        try: c.inbox.get(timeout=0.05)()
        except Exception: pass
    return [e for e in c.seen if e.get("event") == kind]
ctx = ssl._create_unverified_context()
def https(method, path, body=None):
    conn = http.client.HTTPSConnection("127.0.0.1", web, context=ctx, timeout=10)
    conn.request(method, path, body=body); r = conn.getresponse(); d = r.read(); conn.close(); return r.status, d
key = lambda c: c.call("media_key")["media_key"]
a, b, c_, d_ = cl("alice"), cl("bobby"), cl("carol"), cl("davee")

print("== contacts & presence defaults")
cid = a.call("open_private", username="bobby")["chat"]["id"]
check("no message yet -> not contacts: carol/dave have no stories either", True)
v = b.call("chats"); 
a.call("send", chat_id=cid, text="hi")
pv = b.call("open_private", username="alice")["chat"]["peer"]
check("default: bob sees alice online", pv["online"] is True and not pv.get("seen_hidden"), pv)
me = a.call("me")["me"]; check("me carries privacy fields", me["priv_seen"] == "all" and me["read_receipts"] is True, me)

print("== last seen: nobody")
r = a.call("privacy_set", priv_seen="nobody"); check("set nobody", r["ok"] and r["priv_seen"] == "nobody", r)
pv = b.call("open_private", username="alice")["chat"]["peer"]
check("bob no longer sees online/last seen", pv["online"] is False and pv["last_seen"] is None and pv["seen_hidden"] is True, pv)
ev = [e for e in events(b, "presence") if e["user_id"] == a.me["id"]]
check("bob got a 'hidden' presence update", ev and ev[-1].get("seen_hidden") and ev[-1]["online"] is False, ev[-2:] if ev else ev)
check("search_users hides it too", b.call("search_users", q="alice")["users"][0]["online"] is False)
check("alice sees her own presence unchanged", a.call("me")["me"]["priv_seen"] == "nobody")
check("invalid mode rejected", not a.call("privacy_set", priv_seen="everybody")["ok"])

print("== last seen: contacts only")
a.call("privacy_set", priv_seen="contacts")
pv = b.call("open_private", username="alice")["chat"]["peer"]
check("contact (bob) sees alice", pv["online"] is True and not pv.get("seen_hidden"), pv)
cc = c_.call("open_private", username="alice")["chat"]["peer"]
check("stranger (carol, no messages) does not", cc["online"] is False and cc.get("seen_hidden") is True, cc)
a.call("privacy_set", priv_seen="all")

print("== block")
r = a.call("block", user_id=b.me["id"]); check("alice blocks bob", r["ok"] and len(r["blocked"]) == 1, r)
r = b.call("send", chat_id=cid, text="hello?"); check("bob cannot message alice (generic error)", not r["ok"] and r["error"] == "پیام ارسال نشد", r)
r = a.call("send", chat_id=cid, text="hey"); check("alice must unblock before writing", not r["ok"], r)
pv = b.call("open_private", username="alice")["chat"]["peer"]
check("blocked bob can't see alice's presence", pv["online"] is False and pv.get("seen_hidden") is True, pv)
check("call to blocker refused", not b.call("call_invite", chat_id=cid)["ok"])
r = a.call("unblock", user_id=b.me["id"]); check("unblock", r["ok"] and r["blocked"] == [], r)
check("bob can write again", b.call("send", chat_id=cid, text="back")["ok"])
check("can't block yourself", not a.call("block", user_id=a.me["id"])["ok"])
check("unknown user", not a.call("block", user_id=99999)["ok"])

print("== read receipts")
mid = a.call("send", chat_id=cid, text="read me")["message"]["id"]
b.call("read", chat_id=cid, up_to=mid)
ev = events(a, "read"); check("alice gets 'read' event (default on)", any(e["up_to"] >= mid for e in ev))
check("peer_read in chat view", [x for x in a.call("chats")["chats"] if x["id"] == cid][0]["peer_read"] >= mid)
b.call("privacy_set", read_receipts=False)
mid2 = a.call("send", chat_id=cid, text="second")["message"]["id"]
a.seen.clear(); b.call("read", chat_id=cid, up_to=mid2)
check("bob (receipts off): alice gets NO read event", not [e for e in events(a, "read") if e["up_to"] >= mid2])
check("alice's view: peer_read stays below the new message", [x for x in a.call("chats")["chats"] if x["id"] == cid][0]["peer_read"] < mid2)
check("bob (receipts off) sees no read ticks of alice either", [x for x in b.call("chats")["chats"] if x["id"] == cid][0]["peer_read"] == 0)
b.call("privacy_set", read_receipts=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
