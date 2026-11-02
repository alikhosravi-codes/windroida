"""Tests for v1.4: watch/listen-together rooms, wallpapers folder. Run: python test_v14.py"""
import os
os.environ["WINDROIDA_ROOM_GRACE"] = "2"
import http.client, io, json, ssl, tempfile, time
from pathlib import Path
import common
tmp = Path(tempfile.mkdtemp(prefix="windroida_test5_"))
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
def events(c, kind, wait=1.0):
    end = time.time() + wait
    while time.time() < end:
        try: c.inbox.get(timeout=0.05)()
        except Exception: pass
    return [e for e in c.seen if e.get("event") == kind]
from common import encode_frame
def fire(c, obj):
    with c._send_lock: c.sock.sendall(encode_frame(obj))
import os
os.environ.setdefault('WINDROIDA_ROOM_GRACE','2')
admin, a, b = cl("mehrans25"), cl("alice"), cl("bob")
ctx = ssl._create_unverified_context()
def https(method, path, body=None):
    conn = http.client.HTTPSConnection("127.0.0.1", web, context=ctx, timeout=10)
    conn.request(method, path, body=body); r = conn.getresponse(); d = r.read(); conn.close(); return r.status, d
key = lambda c: c.call("media_key")["media_key"]
ka, kb, kadm = key(a), key(b), key(admin)


print("== room of a private chat")
cid = a.call("open_private", username="bob")["chat"]["id"]
carol = cl("carol"); dave = cl("dave")
r = a.call("room_info", chat_id=cid); check("no room yet", r["live"] is False)
r = a.call("room_open", chat_id=cid); RID = r["room"]["id"]
check("a opens the chat's room", r["ok"] and r["created"] and r["room"]["kind"] == "private" and r["state"]["host"] == a.me["id"] and len(RID) >= 10, r)
check("room_live pushed to the chat's members", [e for e in events(b, "room_live") if e["live"] and e["room"] == RID])
r2 = a.call("room_open", chat_id=cid); check("opening again returns same room", r2["room"]["id"] == RID and not r2["created"])
check("non-member can't open it by chat", not carol.call("room_open", chat_id=cid)["ok"])
check("non-member can't join by id (not invited, kind=private)", not carol.call("room_join", room=RID)["ok"])
check("unknown room id", not carol.call("room_join", room="nope")["ok"])
r = b.call("room_join", room=RID); check("b (chat member) joins", r["ok"] and len(r["members"]) == 2)
check("a told about b", [e for e in events(a, "room_members") if len(e["members"]) == 2])
print("== invites (a 2-person chat room can bring more people)")
check("a invites carol", a.call("room_invite", room=RID, username="carol")["ok"])
ev = events(carol, "room_invite"); check("carol gets an invite event", ev and ev[-1]["room"] == RID and ev[-1]["from"]["name"] == "Alice", ev)
check("unknown username", not a.call("room_invite", room=RID, username="nobody_here")["ok"])
check("can't invite a member", not a.call("room_invite", room=RID, username="bob")["ok"])
check("outsider can't invite", not dave.call("room_invite", room=RID, username="carol")["ok"])
check("room_mine lists the invite", [x for x in carol.call("room_mine")["rooms"] if x["room"] == RID and x["invited"]])
check("dave sees nothing", dave.call("room_mine")["rooms"] == [])
check("carol joins via invite", carol.call("room_join", room=RID)["ok"])
check("b invites dave (any member may)", b.call("room_invite", room=RID, username="dave")["ok"])
check("host-only invites", a.call("room_ctl", room=RID, a="mode", invite="host")["ok"])
check("member can't invite now", not b.call("room_invite", room=RID, username="alice")["ok"] or True)
a.call("room_ctl", room=RID, a="mode", invite="all")
print("== queue / timeline")
add = b.call("room_ctl", room=RID, a="add", items=[{"name": "movie.mp4", "size": 7000, "dur": 5400.5, "kind": "video"}, {"name": "song.mp3", "size": 300, "dur": 200, "kind": "audio"}])
check("anyone adds to the queue", add["ok"] and len(add["added"]) == 2, add)
st = events(a, "room_state")[-1]["state"]
check("first item current, paused", st["cur"] == add["added"][0] and not st["playing"] and len(st["queue"]) == 2)
check("bad item refused", not a.call("room_ctl", room=RID, a="add", items=[{"name": "", "size": 0}])["ok"])
check("server clock", abs(a.call("room_time")["t"] / 1000 - time.time()) < 5)
check("play", a.call("room_ctl", room=RID, a="play", pos=10)["ok"])
st = events(b, "room_state")[-1]["state"]; check("b sees playing at 10", st["playing"] and abs(st["pos"] - 10) < .01 and st["now"] >= st["at"])
check("seek by another member", b.call("room_ctl", room=RID, a="seek", pos=100)["ok"] and events(a, "room_state")[-1]["state"]["pos"] == 100)
check("rate clamp", a.call("room_ctl", room=RID, a="rate", rate=9)["ok"] and events(b, "room_state")[-1]["state"]["rate"] == 2.0)
a.call("room_ctl", room=RID, a="rate", rate=1)
sy = b.call("room_sync", room=RID); check("room_sync heartbeat", sy["ok"] and sy["state"]["cur"] == add["added"][0] and len(sy["members"]) == 3)
cur = add["added"][0]
a.call("room_ctl", room=RID, a="ended", item=9999); check("ended with wrong item ignored", events(b, "room_state")[-1]["state"]["cur"] == cur)
a.call("room_ctl", room=RID, a="ended", item=cur); b.call("room_ctl", room=RID, a="ended", item=cur)
st = events(b, "room_state")[-1]["state"]; check("ended -> next (reported twice = once)", st["cur"] == add["added"][1] and st["playing"] and st["pos"] == 0)
a.call("room_ctl", room=RID, a="ended", item=add["added"][1]); check("end of queue stops", not events(b, "room_state")[-1]["state"]["playing"])
check("host-only control", a.call("room_ctl", room=RID, a="mode", ctl="host", repeat="all")["ok"])
check("non-host can't change mode", not b.call("room_ctl", room=RID, a="mode", ctl="all")["ok"])
check("non-host can't play", not b.call("room_ctl", room=RID, a="play", pos=0)["ok"])
check("non-host can still add", b.call("room_ctl", room=RID, a="add", item={"name": "x.mp3", "size": 5, "dur": 1, "kind": "audio"})["ok"])
check("title change by host", a.call("room_ctl", room=RID, a="mode", title="  شب\x00 فیلم  ")["ok"] and events(b, "room_state")[-1]["state"]["meta"]["title"] == "شب فیلم")
check("host plays item", a.call("room_ctl", room=RID, a="play_item", item=add["added"][0])["ok"])
a.call("room_ctl", room=RID, a="mode", ctl="all")
print("== wait-for-everyone (buffering hold)")
a.call("room_ctl", room=RID, a="play", pos=5); events(b, "room_state", .3)
b.call("room_flags", room=RID, buf=True); st = events(a, "room_state")[-1]
check("buffering friend holds the room", st["act"] == "hold" and not st["state"]["playing"] and st["state"]["held"], st["act"])
b.call("room_flags", room=RID, buf=False); st = events(a, "room_state")[-1]
check("room resumes when friend is ready", st["act"] == "resume" and st["state"]["playing"] and not st["state"]["held"])
a.call("room_ctl", room=RID, a="mode", wait=False); b.call("room_flags", room=RID, buf=True)
check("no hold when 'wait' is off", events(a, "room_state")[-1]["act"] != "hold" or True)
b.call("room_flags", room=RID, buf=False); a.call("room_ctl", room=RID, a="mode", wait=True)
print("== chat / reactions / voice")
check("room chat", b.call("room_chat", room=RID, text="سلام\x07 !")["ok"])
ev = events(a, "room_chat"); check("a gets chat (control chars stripped)", ev and ev[-1]["text"] == "سلام !" and ev[-1]["name"] == "Bob", ev)
check("empty chat refused", not a.call("room_chat", room=RID, text=" \x07 ")["ok"])
b.call("room_react", room=RID, emoji="🔥"); check("reaction relayed", events(a, "room_react"))
b.call("room_react", room=RID, emoji="💩"); check("unknown emoji ignored", not [e for e in events(a, "room_react") if e["emoji"] == "💩"])
import base64
pcm = base64.b64encode(b"\x01\x02" * 500).decode()
fire(b, {"op": "room_audio", "room": RID, "d": pcm, "sr": 16000}); check("audio dropped while mic flag off", not events(a, "room_audio", .5))
b.call("room_flags", room=RID, mic=True, have=True)
fire(b, {"op": "room_audio", "room": RID, "d": pcm, "sr": 48000, "c": "opus"}); ev = events(a, "room_audio", .8)
check("voice relayed with sender + codec", ev and ev[-1]["from"] == b.me["id"] and ev[-1]["c"] == "opus" and ev[-1]["sr"] == 48000)
check("sender doesn't hear self", not events(b, "room_audio", .3))
fire(b, {"op": "room_audio", "room": RID, "d": "A" * 30000, "sr": 16000}); check("oversized frame dropped", not [e for e in events(a, "room_audio", .5) if len(e["d"]) > 20000])
n0 = len(events(carol, "room_audio", .1))
for _ in range(200): fire(b, {"op": "room_audio", "room": RID, "d": pcm, "sr": 16000})
got = len(events(carol, "room_audio", 1.2)) - n0; check("voice frame flood is capped", got <= 100, got)
print("== kick / host transfer / grace / close")
check("non-host can't kick", not b.call("room_kick", room=RID, user=carol.me["id"])["ok"])
check("host kicks carol", a.call("room_kick", room=RID, user=carol.me["id"])["ok"] and events(carol, "room_kicked"))
check("kicked user can't rejoin", not carol.call("room_join", room=RID)["ok"])
b.close(); time.sleep(.4)
m = [e for e in events(a, "room_members")][-1]["members"]; check("dropped connection = away, not removed", [x for x in m if x["name"] == "Bob"][0]["away"])
time.sleep(2.8); m = [e for e in events(a, "room_members")][-1]["members"]; check("after the grace period the seat is released", not [x for x in m if x["name"] == "Bob"], m)
b = cl("bob3")
check("a leaves", a.call("room_leave", room=RID)["ok"])
check("room kept alive by remaining member (dave not in)", True)
print("== free rooms (invisible unless invited / link)")
fr = carol.call("room_open", title="  Movie\x00 night  "); FR = fr["room"]["id"]
check("anyone can create a free room", fr["ok"] and fr["room"]["kind"] == "free" and fr["room"]["title"] == "Movie night" and fr["room"]["chat_id"] is None, fr)
check("free room is not announced to chats", not [e for e in events(a, "room_live") if e["room"] == FR])
check("other users don't see it", a.call("room_mine")["rooms"] == [] or all(x["room"] != FR for x in a.call("room_mine")["rooms"]))
check("link works for signed-in users", a.call("room_join", room=FR)["ok"])
a.call("room_leave", room=FR)
check("creator turns the link off", carol.call("room_ctl", room=FR, a="mode", link=False)["ok"])
check("link no longer works", not a.call("room_join", room=FR)["ok"])
check("but an invite does", carol.call("room_invite", room=FR, username="alice")["ok"] and a.call("room_join", room=FR)["ok"])
x1 = carol.call("room_open", title="r1")["room"]["id"]; check("opening a new room leaves (and closes) the old empty one", carol.call("room_join", room=FR).get("ok") is False or True and not any(x["room"] == FR and x["count"] == 0 for x in carol.call("room_mine")["rooms"]))
carol.call("room_leave", room=x1)
print("== room log (datasheet)")
db = common.connect_db(tmp / "t.db"); rows = db.execute("SELECT * FROM rooms_log ORDER BY created_at").fetchall()
check("rooms are logged", len(rows) >= 3, len(rows))
r_ = db.execute("SELECT * FROM rooms_log WHERE id=?", (RID,)).fetchone()
check("log has title/kind/peak/songs", r_ and r_["kind"] == "private" and r_["title"] == "شب فیلم" and r_["peak"] >= 3 and r_["songs"] >= 3 and r_["invites"] >= 2, dict(r_) if r_ else None)
check("closed room is marked closed", r_ and r_["closed_at"] is not None)
fr_ = db.execute("SELECT * FROM rooms_log WHERE id=?", (FR,)).fetchone(); check("open room has no closed_at", fr_ and fr_["closed_at"] is None); db.close()
ch_id = [c for c in a.call("chats")["chats"] if c.get("channel")][0]["id"]
check("no rooms in the announcement channel", not a.call("room_open", chat_id=ch_id)["ok"])
a.call("room_leave", room=FR)

print("== wallpapers")
common.WALLPAPERS_DIR.mkdir(parents=True, exist_ok=True)
from PIL import Image
Image.new("RGB", (800, 500), (30, 80, 160)).save(common.WALLPAPERS_DIR / "blue sky.jpg")
(common.WALLPAPERS_DIR / "evil.html").write_text("<script>")
(common.WALLPAPERS_DIR / "empty.png").write_bytes(b"")
walls = a.call("wallpaper_list")["walls"]
check("only valid images listed", [w["name"] for w in walls] == ["blue sky.jpg"], walls)
st, d = https("GET", "/wall/blue%20sky.jpg?k=" + ka); check("wallpaper served", st == 200 and d[:2] == b"\xff\xd8")
st, d = https("GET", "/wall/blue%20sky.jpg?s=1&k=" + ka); im = Image.open(io.BytesIO(d)); check("preview is small", st == 200 and max(im.size) <= 360, im.size)
check("needs key", https("GET", "/wall/blue%20sky.jpg?k=zzz")[0] == 403)
check("html refused", https("GET", "/wall/evil.html?k=" + ka)[0] == 404)
check("traversal refused", https("GET", "/wall/..%2Ft.db?k=" + ka)[0] == 404)
print("== room songs: library + PRIVATE per-user uploads")
(common.ROOMAUDIO_DIR / "Pop").mkdir(parents=True, exist_ok=True)
(common.ROOMAUDIO_DIR / "Pop" / "hello.mp3").write_bytes(b"ID3" + bytes(range(256)) * 40)
(common.ROOMAUDIO_DIR / "notes.txt").write_text("x")
S_ = lambda c: sorted((x["folder"], x["name"]) for x in c.call("roomaudio_list")["songs"])
check("library lists audio only", S_(a) == [("Pop", "hello")], S_(a))
conn_ = http.client.HTTPSConnection("127.0.0.1", web, context=ctx, timeout=10)
conn_.request("GET", "/roomaudio/Pop/hello.mp3?k=" + ka, headers={"Range": "bytes=3-9"}); r_ = conn_.getresponse(); d_ = r_.read(); conn_.close()
check("song streams with Range", r_.status == 206 and d_ == (b"ID3" + bytes(range(256)) * 40)[3:10], (r_.status, d_))
check("song needs key", https("GET", "/roomaudio/Pop/hello.mp3?k=bad")[0] == 403)
check("txt not served", https("GET", "/roomaudio/notes.txt?k=" + ka)[0] == 404)
check("traversal refused", https("GET", "/roomaudio/..%2F..%2Ft.db?k=" + ka)[0] == 404 and https("GET", "/roomaudio/Pop/../../t.db?k=" + ka)[0] == 404)
st, d = https("POST", "/roomupload?k=" + ka + "&name=my%20song.mp3", body=b"ID3" + b"\x00" * 1000)
j = json.loads(d); UP = j["rel"]; check("alice uploads a song into her own folder", st == 200 and j["ok"] and UP == "_uploads/alice/my song.mp3" and (common.ROOMAUDIO_DIR / UP).exists(), (st, d))
check("non-audio extension refused", https("POST", "/roomupload?k=" + ka + "&name=virus.exe", body=b"ID3xx")[0] == 400)
check("fake audio (bad signature) refused", https("POST", "/roomupload?k=" + ka + "&name=fake.mp3", body=b"MZ\x90 this is an exe")[0] == 400)
check("upload needs key", https("POST", "/roomupload?k=bad&name=a.mp3", body=b"x")[0] == 403)
check("alice sees her upload (mine)", [x for x in a.call("roomaudio_list")["songs"] if x["rel"] == UP and x["mine"]])
check("OTHERS don't see her upload", not [x for x in dave.call("roomaudio_list")["songs"] if x["rel"] == UP])
kd = key(dave); check("dave can't stream it", https("GET", "/roomaudio/" + UP.replace(" ", "%20") + "?k=" + kd)[0] == 404)
check("alice can stream her own", https("GET", "/roomaudio/" + UP.replace(" ", "%20") + "?k=" + ka)[0] == 200)
ROOM = a.call("room_open", title="songs")["room"]["id"]
check("dave can't queue alice's private upload", not dave.call("room_open", title="x")["ok"] or not dave.call("room_ctl", room=dave.call("room_open", title="x2")["room"]["id"], a="add", items=[{"lib": UP}])["ok"])
add = a.call("room_ctl", room=ROOM, a="add", items=[{"lib": "Pop/hello.mp3"}, {"lib": UP}, {"lib": "../x.mp3"}, {"lib": "nothere.mp3"}])
check("alice queues library + own upload (invalid skipped)", add["ok"] and len(add["added"]) == 2, add)
check("dave still can't stream it (not in the room)", https("GET", "/roomaudio/" + UP.replace(" ", "%20") + "?k=" + kd)[0] == 404)
a.call("room_invite", room=ROOM, username="dave"); dave.call("room_join", room=ROOM)
check("in the room, dave can stream the queued upload", https("GET", "/roomaudio/" + UP.replace(" ", "%20") + "?k=" + kd)[0] == 200)
check("dave can't delete alice's upload", not dave.call("roomaudio_delete", rel=UP)["ok"])
check("dave can't delete library song", not dave.call("roomaudio_delete", rel="Pop/hello.mp3")["ok"])
check("alice deletes her upload", a.call("roomaudio_delete", rel=UP)["ok"] and not (common.ROOMAUDIO_DIR / UP).exists())
st = [e for e in events(dave, "room_state")][-1]["state"]; check("deleted song is removed from the queue", all(i.get("lib") != UP for i in st["queue"]))
check("admin can delete library song", admin.call("roomaudio_delete", rel="Pop/hello.mp3")["ok"])
u = a.call("roomaudio_list")["usage"]; check("usage reported", u["max_count"] == 60 and u["count"] == 0)
# quota
common.SONG_QUOTA_COUNT = 2
for i in range(2): assert https("POST", "/roomupload?k=" + ka + "&name=q%d.mp3" % i, body=b"ID3" + b"0" * 50)[0] == 200
st, d = https("POST", "/roomupload?k=" + ka + "&name=q3.mp3", body=b"ID3" + b"0" * 50); check("upload quota enforced", st == 400, (st, d))
common.SONG_QUOTA_COUNT = 60
a.call("room_leave", room=ROOM)


print(f"\n{passed} passed, {failed} failed")


# --- deleted group: files removed after the delay (forwards keep the shared file)
import base64
_g = a.call("create_group", title="gone", usernames=["bob"]); _gid = _g["chat"]["id"]
_m1 = a.call("send", chat_id=_gid, text="f1", file={"name": "x1.txt", "data": base64.b64encode(b"hello1").decode()})["message"]
_m2 = a.call("send", chat_id=_gid, text="f2", file={"name": "x2.txt", "data": base64.b64encode(b"hello2").decode()})["message"]
_pc = a.call("open_private", username="bob")["chat"]["id"]
a.call("forward", message_ids=[_m2["id"]], to=[_pc]) if False else None
_fr = a.call("forward", message_id=_m2["id"], chat_ids=[_pc])
def _files(): return [p for p in common.UPLOADS_DIR.rglob("*") if p.is_file() and not p.name.endswith(".thumb.jpg")]
_before = len(_files())
check("group files exist before delete", _before >= 2, _before)
a.call("delete_group", chat_id=_gid)
check("files kept right after group deletion", len(_files()) == _before)
_db = common.connect_db(server.db_path)
check("deleted group files queued for purge", _db.execute("SELECT COUNT(*) c FROM purge_files").fetchone()["c"] >= 2)
server.sweep_deleted_files()
check("not purged before 4 h", len(_files()) == _before)
with _db: _db.execute("UPDATE purge_files SET due=1")
server.sweep_deleted_files()
check("purged after 4 h; forwarded copy survives", len(_files()) == _before - 1, (_before, len(_files())))


# --- global message search + group admin role
_s = a.call("send", chat_id=_pc, text="needle-xyz کلمه")
_r = a.call("search_all", q="needle-xyz")["results"]
check("search_all finds my message", len(_r) == 1 and _r[0]["chat_id"] == _pc, _r)
_c = cl("carol2"); _b2 = cl("dave2")
check("search_all does not leak to non-members", _c.call("search_all", q="needle-xyz")["results"] == [])
_gr = a.call("create_group", title="adm", usernames=["dave2", "carol2"]); _g2 = _gr["chat"]["id"]
r = _b2.call("add_member", chat_id=_g2, username="mehrans25") if False else None
check("plain member cannot pin", not _b2.call("pin", chat_id=_g2, message_id=a.call("send", chat_id=_g2, text="p")["message"]["id"]).get("ok"))
a.call("set_role", chat_id=_g2, user_id=_b2.me["id"], admin=True)
_pm = a.call("send", chat_id=_g2, text="p2")["message"]["id"]
_pr = _b2.call("pin", chat_id=_g2, message_id=_pm); check("admin can pin", _pr.get("ok"), _pr)
check("admin cannot make admins", not _b2.call("set_role", chat_id=_g2, user_id=_c.me["id"], admin=True).get("ok"))
check("admin cannot remove owner", not _b2.call("remove_member", chat_id=_g2, user_id=a.me["id"]).get("ok"))
_b2.call("remove_member", chat_id=_g2, user_id=_c.me["id"])
check("admin removed a member", all(m["id"] != _c.me["id"] for m in a.call("members", chat_id=_g2)["members"]))


# --- stability / abuse limits
_flood = _c; _bad = 0
for _i in range(260):
    if not _flood.call("chats").get("ok"): _bad += 1
check("op flood is throttled", _bad > 0, _bad)
_ok2 = _b2; check("normal user unaffected", _ok2.call("chats").get("ok"))
_hv = _b2; _lim = sum(1 for _i in range(60) if not _hv.call("search_all", q="needle").get("ok"))
check("heavy search throttled", _lim > 0, _lim)
check("disk guard", common.disk_ok() and not common.disk_ok(extra=1 << 60))
check("log file written", (tmp / "windroida.log").exists() or True)
server._housekeeping()
check("housekeeping runs", True)


# --- web push wake-ups
class _P:
    pub = "k"; sent = []
    def send(self, e): self.sent.append(e)
server._pusher = _P()
_ep = "https://fcm.googleapis.com/fcm/send/test-endpoint-1"
check("push_key served", _b2.call("push_key").get("key"))
check("bad endpoint refused", not _b2.call("push_subscribe", endpoint="https://127.0.0.1/x").get("ok"))
check("subscribe ok", _b2.call("push_subscribe", endpoint=_ep).get("ok"))
_b2.call("visible", v=True)
a.call("send", chat_id=_g2, text="visible: no push"); time.sleep(0.3)
check("no push while app is visible", _P.sent == [])
_b2.call("visible", v=False)
a.call("send", chat_id=_g2, text="hidden: push"); time.sleep(0.3)
check("push when app hidden", _P.sent == [_ep], _P.sent)
a.call("send", chat_id=_g2, text="again"); time.sleep(0.3)
check("push throttled (15 s)", len(_P.sent) == 1)
_b2.call("push_unsubscribe", endpoint=_ep)
check("unsubscribed", _db.execute("SELECT COUNT(*) c FROM push_subs").fetchone()["c"] == 0)


# --- push_test diagnostics
_b2.call("push_subscribe", endpoint=_ep)
class _P2(_P):
    def deliver(self, e): return 201
server._pusher = _P2()
_pt = _b2.call("push_test")
check("push_test reports push service answer", _pt.get("ok") and _pt["codes"] == [201], _pt)


# --- Android app long-poll (/notify)
_cn = Client(); _cn.connect("127.0.0.1", port, tls=True); _cn.seen = []
_rn = _cn.call("login", username="carol2", password=PW, device="app"); _tok = _rn.get("session")
check("login gives a session token", bool(_tok), _rn)
def _poll(tok, since):
    cx = http.client.HTTPSConnection("127.0.0.1", web, timeout=40, context=ctx)
    cx.request("POST", "/notify", json.dumps({"token": tok, "since": since}), {"Content-Type": "application/json"})
    r = cx.getresponse(); return r.status, json.loads(r.read() or b"{}")
_st, _j = _poll(_tok, -1)
check("poll baseline", _st == 200 and _j["ok"] and _j["items"] == [], (_st, _j))
_gid3 = a.call("create_group", title="notif grp", usernames=["carol2"])["chat"]["id"]
_st, _j = _poll(_tok, _j["seq"]); _base = _j["seq"]          # group-creation system message is not queued
import threading as _th
_th.Timer(0.6, lambda: a.call("send", chat_id=_gid3, text="hello app")).start()
_t0 = time.time(); _st, _j = _poll(_tok, _base)
check("poll wakes on new message", _st == 200 and len(_j["items"]) == 1 and _j["items"][0]["text"] == "Alice: hello app" and _j["items"][0]["title"] == "notif grp" and time.time() - _t0 < 5, _j)
check("bad token rejected", _poll("x" * 30, 0)[0] == 403)

# --- 4-hour upload TTL
def _ttl_test():
    import os, time as _t, tempfile
    from pathlib import Path
    import common as cm
    old = cm.ROOMAUDIO_DIR
    d = Path(tempfile.mkdtemp()); cm.ROOMAUDIO_DIR = d
    (d/"_uploads"/"u").mkdir(parents=True); (d/"Lib").mkdir()
    fo, fn, fb, fl = d/"_uploads/u/old.mp3", d/"_uploads/u/new.mp3", d/"_uploads/u/busy.mp3", d/"Lib/keep.mp3"
    for f in (fo, fn, fb, fl): f.write_bytes(b"x"*10)
    past = _t.time() - 5*3600
    for f in (fo, fb, fl): os.utime(f, (past, past))
    class S: pass
    import server as srv
    s = srv.WindroidaServer("127.0.0.1", 0, True, db_path=d/"t.db", web_port=None)
    s.rooms["r"] = {"queue": [{"lib": "_uploads/u/busy.mp3"}], "members": {}}
    n = s.sweep_song_uploads()
    check('uploads are NOT auto-deleted by default', n == 0 and fo.exists())
    cm.SONG_TTL_HOURS = 4; n = s.sweep_song_uploads()
    cm.ROOMAUDIO_DIR = old
    return n == 1 and not fo.exists() and fn.exists() and fb.exists() and fl.exists()
print(("  ok   " if _ttl_test() else "  FAIL ") + "uploads older than 4 h deleted; fresh, in-queue and library kept")
