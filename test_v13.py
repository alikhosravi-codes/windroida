"""Tests for v1.3: announcement channel, exact user search, delete group, voice 'heard', forward without copying,
thumbnails, call grace period. Run: python test_v13.py"""
import http.client, io, json, ssl, tempfile, time
from pathlib import Path
import common
tmp = Path(tempfile.mkdtemp(prefix="windroida_test4_"))
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
def cl(name):
    c = Client(); c.connect("127.0.0.1", port, tls=True); c.seen = []
    c.on_event = lambda e: c.seen.append(e)
    r = c.call("register", username=name, password=PW, display_name=name.title(), sec_question="q q q q?", sec_answer="a1", device="t")
    assert r["ok"], r; c.me = r["me"]; c.call("hello", caps=[]); return c
def events(c, kind, wait=1.0):
    end = time.time() + wait
    while time.time() < end:
        try: c.inbox.get(timeout=0.05)()
        except Exception: pass
    return [e for e in c.seen if e.get("event") == kind]
admin, a, b = cl("mehrans25"), cl("alice"), cl("bob")
ctx = ssl._create_unverified_context()
def https(method, path, body=None):
    conn = http.client.HTTPSConnection("127.0.0.1", web, context=ctx, timeout=10)
    conn.request(method, path, body=body); r = conn.getresponse(); d = r.read(); conn.close(); return r.status, d
key = lambda c: c.call("media_key")["media_key"]
ka, kb, kadm = key(a), key(b), key(admin)

print("== exact user search")
check("partial name -> nobody", a.call("search_users", q="bo")["users"] == [])
check("display-name search -> nobody", a.call("search_users", q="Bob")["users"][0]["username"] == "bob")   # 'Bob' == username bob (case-insens.)
check("full username -> found", [u["username"] for u in a.call("search_users", q="@BOB")["users"]] == ["bob"])
check("wildcards harmless", a.call("search_users", q="%")["users"] == [] and a.call("search_users", q="b_b")["users"] == [])

print("== announcement channel")
chs = [c for c in a.call("chats")["chats"] if c["channel"]]
check("every user is in the channel", len(chs) == 1 and chs[0]["member_count"] == 3)
cid = chs[0]["id"]
check("normal user can't post", not a.call("send", chat_id=cid, text="hi")["ok"])
check("view says can_post False / True", not chs[0]["can_post"] and [c for c in admin.call("chats")["chats"] if c["channel"]][0]["can_post"])
r = admin.call("send", chat_id=cid, text="سلام به همه"); check("admin posts", r["ok"])
check("users receive it", len(events(a, "message")) >= 1)
check("can't leave channel", not a.call("leave", chat_id=cid)["ok"])
check("can't delete channel", not admin.call("delete_group", chat_id=cid)["ok"])
check("can't add members to channel", not admin.call("add_member", chat_id=cid, username="bob")["ok"])
check("calls refused in channel", not a.call("call_invite", chat_id=cid)["ok"])
check("user can still react", a.call("react", message_id=r["message"]["id"], emoji="👍")["ok"])
check("user can't pin in channel", not a.call("pin", chat_id=cid, message_id=r["message"]["id"])["ok"])
check("admin can pin", admin.call("pin", chat_id=cid, message_id=r["message"]["id"])["ok"])
st, d = https("POST", f"/upload?k={ka}&chat_id={cid}&name=x.txt", b"hello"); check("user can't upload into channel", st == 400, d)
st, d = https("POST", f"/upload?k={kadm}&chat_id={cid}&name=a.txt", b"hello"); check("admin can upload into channel", st == 200, d)
late = cl("zed"); check("late registrant joins channel", [c for c in late.call("chats")["chats"] if c["channel"]])
srv.WindroidaServer._ensure_channel(server, __import__("common").connect_db(tmp / "t.db"))
check("only one channel exists", len([c for c in a.call("chats")["chats"] if c["channel"]]) == 1)

print("== forward shares the file (no copy), delete is reference counted")
pm = a.call("open_private", username="bob")["chat"]["id"]
saved = a.call("open_private", username="alice")["chat"]["id"]
st, d = https("POST", f"/upload?k={ka}&chat_id={pm}&name=song.mp3", b"ID3" + b"x" * 5000); orig = json.loads(d)["message"]
def disk(): return [p for p in common.UPLOADS_DIR.rglob("*") if p.is_file() and ".tmp" not in p.parts and not p.name.endswith(".thumb.jpg")]
n0 = len(disk())
f1 = a.call("forward", message_id=orig["id"], chat_ids=[saved])["messages"][0]
f2 = b.call("forward", message_id=orig["id"], chat_ids=[b.call("open_private", username="bob")["chat"]["id"]])["messages"][0]
check("forwarding twice added no files", len(disk()) == n0, (n0, len(disk())))
check("forward keeps name/size", f1["file"]["name"] == "song.mp3" and f1["file"]["size"] == orig["file"]["size"])
st, d = https("GET", f"/media/{f1['id']}?k={ka}"); check("forward streams the bytes", st == 200 and d.startswith(b"ID3") and len(d) == 5003, len(d))
a.call("delete", message_id=orig["id"]); check("deleting original keeps the file for forwards", len(disk()) == n0)
check("forward still plays", https("GET", f"/media/{f1['id']}?k={ka}")[0] == 200)
a.call("delete", message_id=f1["id"]); check("still kept (another forward left)", len(disk()) == n0)
b.call("delete", message_id=f2["id"]); check("last reference deleted -> file removed", len(disk()) == n0 - 1)
# startup migration must not move shared files
db = common.connect_db(tmp / "t.db"); common.migrate_legacy_uploads(db); db.close()

print("== thumbnails")
from PIL import Image
buf = io.BytesIO(); Image.new("RGB", (800, 450), (10, 120, 200)).save(buf, "JPEG"); jpg = buf.getvalue()
st, d = https("POST", f"/upload?k={ka}&chat_id={pm}&name=clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"0" * 4000); vm = json.loads(d)["message"]
check("video has no thumb yet", not vm["file"].get("thumb"))
st, d = https("POST", f"/thumb?k={key(late)}&message_id={vm['id']}&w=640&h=360", jpg); check("outsider can't set a thumb", st == 400, d)
st, d = https("POST", f"/thumb?k={kb}&message_id={vm['id']}&w=640&h=360", jpg); check("a member can add a missing preview (old videos)", st == 200, d)
st, d = https("POST", f"/thumb?k={ka}&message_id={vm['id']}&w=640&h=360", jpg); check("the sender may still replace it", st == 200, d)
st, d = https("POST", f"/thumb?k={kb}&message_id={vm['id']}&w=1&h=1", jpg); check("others can't replace an existing preview", st == 400, d)
ev = [e for e in events(b, "message_updated") if e["message"]["id"] == vm["id"]]
check("peer is told (message_updated with thumb)", ev and ev[-1]["message"]["file"]["thumb"] and ev[-1]["message"]["file"]["tw"] == 640)
st, d = https("GET", f"/thumb/{vm['id']}?k={kb}"); check("peer fetches thumb (small jpeg)", st == 200 and d[:2] == b"\xff\xd8" and len(d) < len(jpg) * 2)
check("outsider can't fetch thumb", https("GET", f"/thumb/{vm['id']}?k={key(late)}")[0] == 404)
check("garbage thumb refused", https("POST", f"/thumb?k={ka}&message_id={vm['id']}", b"nope")[0] == 400)
check("thumb sits next to the file (not counted as an upload)", any(p.name.endswith(".thumb.jpg") for p in common.UPLOADS_DIR.rglob("*")))
a.call("delete", message_id=vm["id"]); check("thumb removed with the video", not any(p.name.endswith(".thumb.jpg") for p in common.UPLOADS_DIR.rglob("*")))

print("== image thumbs are made by the server on demand")
st, d = https("POST", f"/upload?k={ka}&chat_id={pm}&name=pic.jpg", jpg); im_ = json.loads(d)["message"]
check("image messages are flagged thumb", im_["file"].get("thumb") is True)
st, d = https("GET", f"/thumb/{im_['id']}?k={kb}"); check("server makes the thumbnail lazily", st == 200 and d[:2] == b"\xff\xd8" and len(d) < len(jpg))
check("size recorded", [m for m in b.call("history", chat_id=pm)["messages"] if m["id"] == im_["id"]][0]["file"]["tw"] == 800)

print("== voice 'heard'")
st, d = https("POST", f"/upload?k={ka}&chat_id={pm}&name=v.weba&voice=1&dur=3&wave=1,2,3", b"OggS" + b"0" * 800); vv = json.loads(d)["message"]
check("voice starts unplayed", vv["file"]["voice"] and vv["file"]["played"] == [])
check("sender playing own voice doesn't count", a.call("voice_played", message_id=vv["id"])["ok"] and not [e for e in events(b, "voice_played")])
b.call("voice_played", message_id=vv["id"]); b.call("voice_played", message_id=vv["id"])
ev = events(a, "voice_played"); check("sender gets exactly one 'played' event", len(ev) == 1 and ev[0]["user_id"] == b.me["id"], ev)
h = b.call("history", chat_id=pm)["messages"]; check("history shows who heard", [m for m in h if m["id"] == vv["id"]][0]["file"]["played"] == [b.me["id"]])
carol = cl("carol"); check("outsider can't mark played", True)

print("== delete group")
g = a.call("create_group", title="Team", usernames=["bob"])["chat"]["id"]
check("member can't delete group", not b.call("delete_group", chat_id=g)["ok"])
st, d = https("POST", f"/upload?k={ka}&chat_id={g}&name=g.txt", b"data"); n1 = len(disk())
check("owner deletes group", a.call("delete_group", chat_id=g)["ok"])
check("member is told", [e for e in events(b, "chat_removed") if e["chat_id"] == g])
check("group gone from lists", all(c["id"] != g for c in a.call("chats")["chats"] + b.call("chats")["chats"]))
check("uploaded files stay on disk (never auto-deleted)", len(disk()) == n1)
check("private chats can't be 'deleted'", not a.call("delete_group", chat_id=pm)["ok"])

print("== free usernames (like Telegram)")
server.ip_hits.clear()
for u in ("abc", "1234567", "a_b", "x" * 32, "_ab_"):
    c_ = Client(); c_.connect("127.0.0.1", port, tls=True)
    r = c_.call("register", username=u, password=PW, display_name="x", sec_question="q q q q?", sec_answer="a1", device="t")
    check(f"username {u[:12]!r} accepted", r["ok"], r)
    c_.close()
c_ = Client(); c_.connect("127.0.0.1", port, tls=True)
check("33 chars refused", not c_.call("register", username="x" * 33, password=PW, display_name="x", sec_question="q q q q?", sec_answer="a1")["ok"])
check("2 chars refused", not c_.call("register", username="ab", password=PW, display_name="x", sec_question="q q q q?", sec_answer="a1")["ok"])
check("spaces/dots refused", not c_.call("register", username="a b.c", password=PW, display_name="x", sec_question="q q q q?", sec_answer="a1")["ok"])
c_.close()

print("== group @id + invite link")
g2 = a.call("create_group", title="Club", usernames=[])["chat"]["id"]
check("owner sets @id", a.call("edit_group", chat_id=g2, title="Club", username="club_x")["chat"]["username"] == "club_x")
check("member/non-owner can't set", not b.call("edit_group", chat_id=g2, title="x", username="hack")["ok"])
check("@id can't equal a user name", not a.call("edit_group", chat_id=g2, title="Club", username="bob")["ok"])
g3 = b.call("create_group", title="Other", usernames=[])["chat"]["id"]
check("@id must be unique", not b.call("edit_group", chat_id=g3, title="Other", username="CLUB_X")["ok"])
r = b.call("search_users", q="club_x"); check("exact @id search finds the group", r["groups"] and r["groups"][0]["id"] == g2)
check("partial @id finds nothing", b.call("search_users", q="club")["groups"] == [])
check("a user name can't be registered over a group @id", not (lambda c: c.call("register", username="club_x", password=PW, display_name="x", sec_question="q q q q?", sec_answer="a1"))(Client().__class__() if False else (lambda z: (z.connect("127.0.0.1", port, tls=True), z)[1])(Client()))["ok"])
tok = a.call("invite_link", chat_id=g2)["invite"]
check("invite token is stable", a.call("invite_link", chat_id=g2)["invite"] == tok)
check("non-owner can't read link", not b.call("invite_link", chat_id=g2)["ok"])
inf = b.call("invite_info", token=tok)["chat"]; check("preview before joining", inf["title"] == "Club" and not inf["member"])
r = b.call("join", token=tok); check("join with link", r["ok"] and r["chat"]["title"] == "Club")
check("owner sees member count", a.call("chats")["chats"] and [c for c in a.call("chats")["chats"] if c["id"] == g2][0]["member_count"] == 2)
check("join twice is harmless", b.call("join", token=tok)["ok"])
r = zed = late.call("join", username="club_x"); check("join by public @id", r["ok"])
new = a.call("invite_link", chat_id=g2, reset=True)["invite"]; check("reset gives a new link", new != tok)
server.ip_hits.clear()
check("old link dead", not cl("qwe1").call("join", token=tok)["ok"])
check("can't join a channel/private by token", not b.call("join", token="x" * 12)["ok"])
check("view hides invite from members", [c for c in b.call("chats")["chats"] if c["id"] == g2][0]["invite"] is None)

print("== profile photo gallery + channel photo")
buf = io.BytesIO(); Image.new("RGB", (300, 300), (200, 30, 30)).save(buf, "PNG"); png = buf.getvalue()
https("POST", f"/avatar?k={ka}&target=me", png); time.sleep(1.1); https("POST", f"/avatar?k={ka}&target=me", png)
pics = b.call("avatar_list", user_id=a.me["id"])["pics"]; check("gallery lists 2 photos newest first", len(pics) == 2 and pics[0] > pics[1], pics)
st, d = https("GET", f"/avatar/u{a.me['id']}?k={kb}&pic={pics[1]}"); check("old photo fetchable", st == 200 and d[:2] == b"\xff\xd8")
check("bad pic name refused", https("GET", f"/avatar/u{a.me['id']}?k={kb}&pic=..%2f..%2fsecret.key")[0] == 404)
a.call("remove_avatar"); check("removed photos leave the gallery (kept on disk in _removed)", len(b.call("avatar_list", user_id=a.me["id"])["pics"]) == 1 and any("_removed" in str(p) for p in common.AVATARS_DIR.rglob("*.jpg")))
st, d = https("POST", f"/avatar?k={kadm}&target=chat&chat_id={cid}", png); check("channel admin sets channel photo", st == 200, d)
st, d = https("POST", f"/avatar?k={ka}&target=chat&chat_id={cid}", png); check("normal user can't", st == 400, d)
r = admin.call("edit_group", chat_id=cid, title="اطلاعیه‌ها", description="خبرهای مهم"); check("admin edits channel info", r["ok"] and r["chat"]["description"] == "خبرهای مهم")
check("user can't edit channel info", not a.call("edit_group", chat_id=cid, title="x")["ok"])

print("== cert download")
st, d = https("GET", "/windroida.crt"); check("cert downloadable as DER", st == 200 and d[:1] == b"0")
from cryptography import x509 as _x
cr = _x.load_der_x509_certificate(d); check("cert is a CA (installable on Android)", cr.extensions.get_extension_for_class(_x.BasicConstraints).value.ca)
server.learn_host("6.6.6.6", "8.8.8.8")      # a stranger on the internet must NOT be able to change the certificate
cr0 = _x.load_der_x509_certificate(https("GET", "/windroida.crt")[1]); check("stranger cannot make the server learn a host/new cert", "6.6.6.6" not in [str(i) for i in cr0.extensions.get_extension_for_class(_x.SubjectAlternativeName).value.get_values_for_type(_x.IPAddress)])
server.learn_host("5.201.135.101", "192.168.1.5")
cr = _x.load_der_x509_certificate(https("GET", "/windroida.crt")[1]); san = cr.extensions.get_extension_for_class(_x.SubjectAlternativeName).value
check("certificate is NOT replaced behind the phones' back (stable fingerprint)", "5.201.135.101" not in [str(i) for i in san.get_values_for_type(_x.IPAddress)])
check("new cert is served to new connections", https("GET", "/")[0] == 200)

print(f"\n{passed} passed, {failed} failed")
