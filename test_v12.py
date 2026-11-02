"""Tests for v1.2 features: avatars (separate folder), voice meta, reactions, forward, pin, search, history-around,
sessions, saved messages, chat settings, group edit, shared media, https certificate + redirect. Run: python test_v12.py"""
import http.client, io, json, socket, ssl, sys, tempfile, time
from pathlib import Path
import common
tmp = Path(tempfile.mkdtemp(prefix="windroida_test3_"))
common.DATA_DIR = tmp
common.FILES_DIR = tmp / "files"; (tmp / "files").mkdir()
common.UPLOADS_DIR = tmp / "uploads"; common.AVATARS_DIR = tmp / "avatars"
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
check("server uses https by default (cert made)", server.tls and (tmp / "server.crt").exists())
def cl(name):
    c = Client(); c.connect("127.0.0.1", port, tls=True)
    r = c.call("register", username=name, password=PW, display_name=name.title(), sec_question="q q q q?", sec_answer="a1", device="t")
    assert r["ok"], r; c.me = r["me"]; c.call("hello", caps=[]); return c
a, b, c3 = cl("alice"), cl("bob"), cl("carol")
key = lambda c: c.call("media_key")["media_key"]
ka, kb, kc = key(a), key(b), key(c3)
ctx = ssl._create_unverified_context()
def https(method, path, body=None, headers=None):
    conn = http.client.HTTPSConnection("127.0.0.1", web, context=ctx, timeout=10)
    conn.request(method, path, body=body, headers=headers or {}); r = conn.getresponse(); d = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, d); conn.close(); return out

print("== certificate / https")
import ssl as _s
cert = _s.DER_cert_to_PEM_cert(ctx.wrap_socket(socket.create_connection(("127.0.0.1", web)), server_hostname="x").getpeercert(True))
from cryptography import x509
san = x509.load_pem_x509_certificate(cert.encode()).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
check("cert has localhost + 127.0.0.1 + LAN ip", "localhost" in san.get_values_for_type(x509.DNSName) and any(str(i) == "127.0.0.1" for i in san.get_values_for_type(x509.IPAddress)) and srv.lan_ip() in [str(i) for i in san.get_values_for_type(x509.IPAddress)] + san.get_values_for_type(x509.DNSName))
s = socket.create_connection(("127.0.0.1", web)); s.sendall(b"GET /abc HTTP/1.1\r\nHost: 192.168.1.100:8081\r\n\r\n"); resp = s.recv(2000).decode(); s.close()
check("plain http gets 301 to https", "301" in resp and "Location: https://192.168.1.100:8081/abc" in resp, resp)
st, h_, body = https("GET", "/"); check("index served", st == 200 and b"/static/app.js" in body)
check("static css/js/manifest", all(https("GET", p)[0] == 200 for p in ("/static/app.css", "/static/app.js", "/manifest.webmanifest")))
check("static: no traversal / unknown ext", https("GET", "/static/..%2fserver.py")[0] == 404 and https("GET", "/static/x.py")[0] == 404)

print("== avatars")
from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (800, 500), (30, 90, 200)).save(buf, "PNG"); png = buf.getvalue()
st, _, d = https("POST", f"/avatar?k={ka}&target=me", png); j = json.loads(d)
check("upload own avatar", st == 200 and j["ok"] and j["at"] > 0, d)
files = sorted((tmp / "avatars" / "alice").glob("*")); check("stored in avatars/alice (big + _s)", len(files) == 2 and any(f.name.endswith("_s.jpg") for f in files), files)
check("not inside uploads/", not (tmp / "uploads" / "alice").exists())
check("bad key refused", https("POST", "/avatar?k=nope&target=me", png)[0] == 403)
check("garbage image refused", https("POST", f"/avatar?k={ka}&target=me", b"not an image")[0] == 400)
me = a.call("me")["me"]; check("avatar_at in profile", me["avatar_at"] == j["at"])
uid_a = me["id"]
st, h_, d = https("GET", f"/avatar/u{uid_a}?k={kb}&v=1"); check("others can fetch it (jpeg)", st == 200 and h_["content-type"] == "image/jpeg" and d[:2] == b"\xff\xd8")
st, h_, d2 = https("GET", f"/avatar/u{uid_a}?k={kb}&v=1&s=1"); check("small version smaller", st == 200 and len(d2) < len(d))
check("no key -> 403", https("GET", f"/avatar/u{uid_a}")[0] == 403)
time.sleep(1.1); https("POST", f"/avatar?k={ka}&target=me", png)
check("old photo kept after change (4 files)", len(list((tmp / "avatars" / "alice").glob("*"))) == 4)
a.call("remove_avatar"); check("remove_avatar detaches but keeps files", a.call("me")["me"]["avatar_at"] == 0 and len(list((tmp / "avatars" / "alice").rglob("*.jpg"))) == 4 and (tmp / "avatars" / "alice" / "_removed").is_dir())
check("no avatar -> 404", https("GET", f"/avatar/u{uid_a}?k={kb}")[0] == 404)

print("== chats, group avatar, group edit")
g = a.call("create_group", title="Team", usernames=["bob"])["chat"]; gid = g["id"]
check("owner sets group avatar", json.loads(https("POST", f"/avatar?k={ka}&target=chat&chat_id={gid}", png)[2])["ok"])
check("member (bob) can't set group avatar", https("POST", f"/avatar?k={kb}&target=chat&chat_id={gid}", png)[0] == 400)
check("member sees group avatar", https("GET", f"/avatar/c{gid}?k={kb}")[0] == 200)
check("non-member (carol) can't", https("GET", f"/avatar/c{gid}?k={kc}")[0] == 404)
check("group folder under avatars/_groups", any((tmp / "avatars" / "_groups").glob(f"{gid}_*")))
r = a.call("edit_group", chat_id=gid, title="Team 2", description="hello there"); check("edit group", r["ok"] and r["chat"]["title"] == "Team 2" and r["chat"]["description"] == "hello there")
check("member can't edit group", not b.call("edit_group", chat_id=gid, title="x")["ok"])

print("== messages: voice, react, forward, pin, search, around, saved")
def upload(c, k, chat, name, data, extra=""):
    return json.loads(https("POST", f"/upload?k={k}&chat_id={chat}&name={name}{extra}", data)[2])
r = upload(a, ka, gid, "voice-1.weba", b"\x1aE\xdf\xa3" + b"x" * 3000, "&voice=1&dur=4.2&wave=1,5,9,31,99,x,3")
f = r["message"]["file"]; check("voice message meta", r["ok"] and f["voice"] and f["dur"] == 4.2 and f["wave"] == [1, 5, 9, 31, 31, 3], r)
vid = r["message"]["id"]
check("voice must be audio", not upload(a, ka, gid, "x.txt", b"hello", "&voice=1")["ok"])
check("voice file in uploads folder of alice", any((tmp / "uploads" / "alice").rglob("*.weba")))
m1 = a.call("send", chat_id=gid, text="hello **world**")["message"]
rr = b.call("react", message_id=m1["id"], emoji="❤️"); check("react", rr["ok"] and rr["message"]["reactions"][0]["e"] == "❤️")
check("react twice toggles off", b.call("react", message_id=m1["id"], emoji="❤️")["message"]["reactions"] == [])
check("bad emoji refused", not b.call("react", message_id=m1["id"], emoji="💩x")["ok"])
check("non-member can't react", not c3.call("react", message_id=m1["id"], emoji="👍")["ok"])
pc = a.call("open_private", username="bob")["chat"]["id"]
fr = a.call("forward", message_id=m1["id"], chat_ids=[pc]); check("forward to private chat", fr["ok"] and fr["messages"][0]["fwd"] == "Alice", fr)
check("forward into a chat I'm not in refused", not c3.call("forward", message_id=m1["id"], chat_ids=[pc])["ok"])
check("can't forward a message I can't see", not c3.call("forward", message_id=m1["id"], chat_ids=[c3.call("open_private", username="carol")["chat"]["id"]])["ok"])
check("owner pins", a.call("pin", chat_id=gid, message_id=m1["id"])["ok"])
v = [x for x in a.call("chats")["chats"] if x["id"] == gid][0]; check("pinned in chat view", v["pin_msg"] and v["pin_msg"]["id"] == m1["id"])
check("member can't pin in group", not b.call("pin", chat_id=gid, message_id=m1["id"])["ok"])
a.call("pin", chat_id=gid, message_id=None); v = [x for x in a.call("chats")["chats"] if x["id"] == gid][0]; check("unpin", v["pin_msg"] is None)
for i in range(60): a.call("send", chat_id=gid, text=f"filler {i}") if i % 1 == 0 else None; time.sleep(0.02) if i % 12 == 11 else None
sr = a.call("search_messages", chat_id=gid, q="world"); check("search finds message", sr["ok"] and len(sr["messages"]) == 1)
check("non-member can't search", not c3.call("search_messages", chat_id=gid, q="world")["ok"])
ar = a.call("history", chat_id=gid, around=m1["id"], limit=20); ids = [m["id"] for m in ar["messages"]]
check("history around includes target + newer flag", m1["id"] in ids and ar["has_newer"] and ar["has_more"] is not None, ar.get("error"))
sh = a.call("shared", chat_id=gid, kind="voice"); check("shared voice list", len(sh["messages"]) == 1 and sh["messages"][0]["id"] == vid)
check("shared file kind excludes voice", all(not m["file"].get("voice") for m in a.call("shared", chat_id=gid, kind="file")["messages"]))
sv = a.call("open_private", username="alice"); check("saved messages chat", sv["ok"] and sv["chat"]["saved"] and sv["chat"]["title"] == "پیام‌های ذخیره‌شده")
time.sleep(11); r = a.call("send", chat_id=sv["chat"]["id"], text="note"); check("send to saved", r["ok"], r)
cs = a.call("chat_settings", chat_id=gid, muted=True, fav=True); check("mute+fav per user", cs["chat"]["muted"] and cs["chat"]["fav"] and not [x for x in b.call("chats")["chats"] if x["id"] == gid][0]["muted"])

print("== sessions / devices")
a1 = Client(); a1.connect("127.0.0.1", port, tls=True); a1.call("login", username="alice", password=PW, device="phone")
a2 = Client(); a2.connect("127.0.0.1", port, tls=True); r = a2.call("login", username="alice", password=PW, device="laptop")
ss = a2.call("sessions")["sessions"]; check("sessions list has devices + current flag", len(ss) >= 2 and sum(x["current"] for x in ss) == 1, ss)
other = [x for x in ss if not x["current"]][0]
r = a2.call("terminate_session", session_id=other["id"]); check("terminate other session", r["ok"], (r, ss))
check("terminated device is cut off", not a1.call("sessions")["ok"])
check("terminate others", a2.call("terminate_others")["ok"] and len(a2.call("sessions")["sessions"]) == 1)

print(f"\n{passed} passed, {failed} failed"); server.stop(); sys.exit(1 if failed else 0)
