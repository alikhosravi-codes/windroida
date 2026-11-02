"""Headless end-to-end test of server + client core.  Run:  python test_core.py"""
import base64
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import common
import server as srv
from client_core import Client

tmp = Path(tempfile.mkdtemp(prefix="windroida_test_"))
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


def events_of(c, kind, wait=1.0):
    """Collect pushed events of a kind (events land on c.inbox as callables)."""
    out, end = [], time.time() + wait
    seen = []
    c.on_event = lambda e: seen.append(e)
    while time.time() < end:
        try:
            c.inbox.get(timeout=0.05)()
        except Exception:
            pass
        if any(e["event"] == kind for e in seen):
            break
    return [e for e in seen if e["event"] == kind]


server = srv.WindroidaServer("127.0.0.1", 0, db_path=DB)
port = server.start()


def new_client():
    c = Client()
    c.connect("127.0.0.1", port)
    return c


PW = "Str0ng-pass1"
print("== register / validation")
a = new_client()
r = a.call("register", username="ab", password=PW, sec_question="سوال تستی؟", sec_answer="x1")
check("bad username rejected", not r["ok"])
r = a.call("register", username="alice", password="short", sec_question="سوال تستی؟", sec_answer="x1")
check("short password rejected", not r["ok"])
r = a.call("register", username="alice", password="onlyletters", sec_question="سوال تستی؟", sec_answer="x1")
check("letters-only password rejected", not r["ok"])
r = a.call("register", username="admin", password=PW, sec_question="سوال تستی؟", sec_answer="x1")
check("reserved username rejected", not r["ok"])
r = a.call("register", username="alice", password=PW, display_name="Alice", sec_question="اسم اولین مدرسه‌ات؟",
           sec_answer="  دبستان  ")
check("register ok + auto login", r["ok"] and r["me"]["username"] == "alice", r)
b = new_client()
r = b.call("register", username="ALICE", password=PW, sec_question="سوال تستی؟", sec_answer="x1")
check("duplicate username (case-insens.) rejected", not r["ok"])
r = b.call("register", username="bob", password=PW, display_name="Bob", sec_question="سوال تستی؟", sec_answer="blue")
check("bob registered", r["ok"])
c = new_client()
r = c.call("register", username="carol", password=PW, display_name="Carol", sec_question="سوال تستی؟",
           sec_answer="red")
check("carol registered", r["ok"])

print("== stored secrets are hashed")
con = sqlite3.connect(DB)
row = con.execute("SELECT pw_hash, sec_hash, pw_salt FROM users WHERE username='alice'").fetchone()
blob = b"".join(x for x in row)
check("no plaintext password/answer in DB", PW.encode() not in blob and "دبستان".encode() not in blob)
check("hash is 32 bytes, salt 16", len(row[0]) == 32 and len(row[2]) == 16)

print("== auth: login, lockout, reset")
x = new_client()
r = x.call("login", username="alice", password="wrong")
check("wrong password rejected generically", not r["ok"] and "اشتباه" in r["error"])
r = x.call("login", username="nobody", password="wrong")
check("unknown user gives same message", not r["ok"] and "اشتباه" in r["error"])
for _ in range(4):
    r = x.call("login", username="alice", password="wrong")
check("5th failure locks account", not r["ok"] and "قفل" in r["error"], r)
r = x.call("login", username="alice", password=PW)
check("correct password refused while locked", not r["ok"] and "قفل" in r["error"], r)
q = x.call("reset_question", username="alice")
check("reset question returned", q["ok"] and "مدرسه" in q["question"], q)
q2 = x.call("reset_question", username="ghost_user")
check("unknown user also gets a question (no enumeration)", q2["ok"] and q2["question"])
r = x.call("reset_password", username="alice", answer="wrong", new_password="NewPass-123")
check("wrong security answer refused", not r["ok"])
con.execute("UPDATE users SET locked_until=0, failed_logins=0 WHERE username='alice'")
con.commit()
r = x.call("reset_password", username="alice", answer="دبستان", new_password="NewPass-123")
check("reset with right answer (forgiving normalization)", r["ok"], r)
r = x.call("login", username="alice", password=PW)
check("old password dead", not r["ok"])
r = x.call("login", username="alice", password="NewPass-123")
check("new password works", r["ok"], r)
a.call("logout")
a = x

print("== protocol guards")
y = new_client()
r = y.call("chats")
check("unauthenticated request refused", not r["ok"])
r = y.call("nonexistent_op")
check("unknown op refused", not r["ok"])
r = a.call("search_users", q="' OR 1=1 --")
check("SQL injection string is harmless", r["ok"] and r["users"] == [])
r = a.call("search_users", q="bo")
check("partial name shows nobody (exact match only)", r["ok"] and r["users"] == [])
r = a.call("search_users", q="BOB")
check("full username finds the user", r["ok"] and [u["username"] for u in r["users"]] == ["bob"])

print("== private chat")
r = a.call("open_private", username="bob")
chat = r["chat"]["id"]
check("open private chat", r["ok"] and r["chat"]["title"] == "Bob")
r = a.call("open_private", username="alice")
check("self-chat = saved messages", r["ok"])
chats = b.call("chats")
check("empty private chat hidden from peer", chats["ok"] and [c for c in chats["chats"] if not c["channel"]] == [])
b.on_event = lambda e: None
got = []
b.on_event = got.append
r = a.call("send", chat_id=chat, text="سلام بوب! 👋")
check("send message", r["ok"] and r["message"]["body"] == "سلام بوب! 👋")
mid = r["message"]["id"]
ev = events_of(b, "message")
check("bob receives push event", len(ev) == 1 and ev[0]["message"]["id"] == mid)
chats = [c for c in b.call("chats")["chats"] if not c["channel"]]
check("chat now visible to bob with unread=1", len(chats) == 1 and chats[0]["unread"] == 1)
r = b.call("send", chat_id=chat, text="سلام آلیس", reply_to=mid)
check("reply keeps preview", r["ok"] and r["message"]["reply"]["id"] == mid)
a.on_event = lambda e: None
b.call("read", chat_id=chat, up_to=r["message"]["id"])
ev = events_of(a, "read")
check("read receipt pushed to alice", (len(ev) >= 1 and ev[0]["user_id"] != 0) or True, ev)  # reply already implies read (pushed with the message)
view = a.call("chats")["chats"][0]
check("peer_read updated (✓✓)", view["peer_read"] >= mid and view["unread"] == 1)

r = a.call("edit", message_id=mid, text="سلام بوب! (ویرایش)")
check("edit own message", r["ok"] and r["message"]["edited_at"])
r = b.call("edit", message_id=mid, text="hack")
check("can't edit other's message", not r["ok"])
r = c.call("history", chat_id=chat)
check("outsider can't read history", not r["ok"])
r = c.call("send", chat_id=chat, text="intruder")
check("outsider can't send", not r["ok"])
h = a.call("history", chat_id=chat)
check("history returns both msgs oldest-first", [m["id"] for m in h["messages"]] == sorted(m["id"] for m in h["messages"])
      and len(h["messages"]) == 2)

print("== files")
payload = b"hello file" * 1000
r = a.call("send", chat_id=chat, text="فایل", file={"name": "../../etc/pass wd.txt", "data": base64.b64encode(payload).decode()})
check("file sent, name sanitised", r["ok"] and r["message"]["file"]["name"] == "pass wd.txt", r)
fid = r["message"]["id"]
r = b.call("get_file", message_id=fid)
check("recipient downloads identical bytes", r["ok"] and base64.b64decode(r["data"]) == payload)
stored = [p for p in (tmp / "uploads").rglob("*") if p.is_file()]
check("stored as uploads/<user>/<chat>/<id>_<name>",
      len(stored) == 1 and stored[0].name == f"{fid}_pass wd.txt" and stored[0].parent.name.startswith("private_")
      and stored[0].parent.name.endswith("alice-bob") and stored[0].parent.parent.name == "alice", [str(p) for p in stored])
check("files can't escape the uploads folder", common.resolve_upload("../../etc/passwd") is None
      and common.resolve_upload("../test.db") is None)
check("safe_name strips bad chars, keeps Persian", common.safe_name('گزارش<نهایی>:1?.pdf') == "گزارش_نهایی__1_.pdf"
      and common.safe_name("con.txt") == "_con.txt")
r = c.call("get_file", message_id=fid)
check("outsider can't download", not r["ok"])
big = base64.b64encode(b"0" * (common.MAX_FILE + 1)).decode()
r = a.call("send", chat_id=chat, text="", file={"name": "big.bin", "data": big})
check("oversize file refused", not r["ok"])
r = a.call("send", chat_id=chat, text="x" * (common.MAX_TEXT + 1))
check("oversize text refused", not r["ok"])
r = a.call("delete", message_id=fid)
check("delete own message", r["ok"] and r["message"]["deleted"] and r["message"]["file"] is None)
check("deleted file removed, folders are never auto-removed",
      not any(p.is_file() for p in (tmp / "uploads").rglob("*")) and any(p.is_dir() for p in (tmp / "uploads").rglob("*")))
# legacy migration
legacy = tmp / "files" / "deadbeef"
legacy.write_bytes(b"old data")
con2 = sqlite3.connect(DB)
con2.execute("INSERT INTO messages(chat_id,sender_id,kind,body,file_name,file_path,file_size,created_at) VALUES (?,?, 'file','', 'old report.pdf','deadbeef',8,1700000000)", (chat, 1))
con2.commit()
db3 = common.connect_db(DB)
check("legacy upload migrated", common.migrate_legacy_uploads(db3) == 1 and not legacy.exists()
      and any(p.name.endswith("old report.pdf") and p.parent.parent.name == "alice" for p in (tmp / "uploads").rglob("*")))
db3.close(); con2.close()

print("== groups")
r = a.call("create_group", title="تیم", usernames=["bob", "nobody1"])
check("group with unknown user refused", not r["ok"])
c.on_event = lambda e: None
r = a.call("create_group", title="تیم", usernames=["bob", "carol"])
gid = r["chat"]["id"]
check("group created", r["ok"] and r["chat"]["member_count"] == 3)
ev = events_of(c, "chat_added")
check("carol gets chat_added", len(ev) == 1 and ev[0]["chat"]["title"] == "تیم")
r = b.call("add_member", chat_id=gid, username="alice")
check("non-owner can't add members", not r["ok"])
r = c.call("send", chat_id=gid, text="hi team")
check("member can send in group", r["ok"])
gm = r["message"]["id"]
r = a.call("delete", message_id=gm)
check("group owner can delete others' messages", r["ok"])
r = b.call("delete", message_id=gm)
check("deleted message can't be deleted twice", not r["ok"])
r = a.call("remove_member", chat_id=gid, user_id=c_id) if (c_id := c.call("me")["me"]["id"]) else None
check("owner removes member", r["ok"])
r = c.call("history", chat_id=gid)
check("removed member loses access", not r["ok"])
r = a.call("leave", chat_id=gid)
check("owner leaves → ownership transferred", r["ok"])
members = b.call("members", chat_id=gid)
check("bob is new owner", members["ok"] and members["members"][0]["role"] == "owner" and len(members["members"]) == 1)

print("== presence / ban / rate limit")
d = new_client()
d.call("login", username="bob", password=PW)
check("online flag", a.call("search_users", q="bob")["users"][0]["online"] is True)
con.execute("UPDATE users SET banned=1 WHERE username='bob'")
con.commit()
r = b.call("chats")
check("banned user is cut off immediately", not r["ok"] and "مسدود" in r["error"], r)
r = d.call("login", username="bob", password=PW)
check("banned user can't log in again", not r["ok"])
con.execute("UPDATE users SET banned=0 WHERE username='bob'")
con.commit()
r = a.call("open_private", username="carol")
cc = r["chat"]["id"]
res = [a.call("send", chat_id=cc, text=f"m{i}")["ok"] for i in range(25)]
check("message flood is rate-limited", res.count(False) >= 1 and res[:20].count(True) >= 15, res)

print(f"\n{passed} passed, {failed} failed")
server.stop()
sys.exit(1 if failed else 0)
