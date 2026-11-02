"""Datasheet's Ghab tabs without a GUI: tkinter is replaced by a stub, so only the data/delete/ban/link logic runs. Run: python test_ghab_sheet.py"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

here = Path(__file__).resolve().parent
# --- stub tkinter + ui_kit (no display here)
tk = types.ModuleType("tkinter")
tk.Tk = type("Tk", (), {})
tk.Toplevel = tk.Frame = tk.Label = tk.Text = tk.Menu = tk.StringVar = tk.BooleanVar = object
tkf, tkm, tkt = types.ModuleType("tkinter.filedialog"), types.ModuleType("tkinter.messagebox"), types.ModuleType("tkinter.ttk")
sys.modules.update({"tkinter": tk, "tkinter.filedialog": tkf, "tkinter.messagebox": tkm, "tkinter.ttk": tkt,
                    "tkinter.simpledialog": types.ModuleType("tkinter.simpledialog")})
tk.filedialog, tk.messagebox, tk.ttk = tkf, tkm, tkt
ui = types.ModuleType("ui_kit")
for n in ("T", "Dialog", "FlatButton", "font", "gregorian_to_jalali", "init_fonts", "make_entry", "set_theme"):
    setattr(ui, n, mock.MagicMock())
ui.fa = lambda x: str(x)
sys.modules["ui_kit"] = ui
sys.path.insert(0, str(here))
import datasheet  # noqa: E402

ok = bad = 0


def check(name, cond):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("FAIL", name)


root = Path(tempfile.mkdtemp())
data = root / "ghab" / "data"
(data / "users" / "ali" / "posts").mkdir(parents=True)
(data / "users" / "sara").mkdir(parents=True)
for f in ("ali/posts/a.jpg", "ali/posts/a_p.jpg", "sara/x.jpg"):
    (data / "users" / f).write_bytes(b"x" * 2048)
# build a Ghab database with the real migrations
sys.path.insert(0, str(here / "ghab"))
os.environ["GHAB_DATA"] = str(data)
from server.db import migrate  # noqa: E402
migrate()
con = sqlite3.connect(data / "ghab.db")
con.executescript("""
insert into users(id,username,pass,name,created,wd_name) values(1,'ali','x','Ali',1700000000,'Ali');
insert into users(id,username,pass,name,created) values(2,'sara','x','Sara',1700000100);
insert into posts(id,user_id,file,kind,caption,created,poster) values(1,1,'ali/posts/a.jpg','image','hello',1700000200,'ali/posts/a_p.jpg');
insert into comments(id,post_id,user_id,text,created) values(1,1,2,'nice',1700000300);
insert into likes(user_id,post_id) values(2,1);
insert into follows(follower,followee) values(2,1);
insert into messages(id,sender,receiver,text,created) values(1,2,1,'secret dm',1700000400);
insert into stories(id,user_id,file,kind,created) values(1,2,'sara/x.jpg','image',1700000500);
""")
con.commit()
con.close()

D = datasheet.Datasheet
sheet = object.__new__(D)
sheet._ghab_data_root = lambda: data
msgs = []
sheet.toast = lambda m, error=False: msgs.append((m, error))
sheet.confirm = lambda t: True
sheet.last_signature = None
sheet.refresh = lambda: None
wdb = sqlite3.connect(":memory:")
wdb.row_factory = sqlite3.Row
wdb.execute("create table users(username text)")
wdb.execute("insert into users values('Sara_W')")
sheet.db = wdb


def load(tab):
    rows = D.load_ghab_table(sheet, tab)
    sheet.rows = rows
    return rows


def select(tab, idx):
    rows = load(tab)
    sheet.tree = types.SimpleNamespace(selection=lambda: [rows[idx]["iid"]])
    sheet.tab = tab
    return rows


for tab in datasheet.GHAB_TABS:
    r = load(tab)
    check(f"{tab} loads", len(r) >= 1 or tab == "ghabreports")
    for row in r:
        check(f"{tab} columns match", len(row["display"]) == len(datasheet.COLUMNS[tab]) == len(row["export"]) == len(row["sort"]))
users = load("ghab")
check("users: 2 rows, linked/old", [u["display"][2] for u in users] == ["Ali", "— (حساب قدیمی)"])
check("posts size shown", load("ghabposts")[0]["display"][4] != "—")
check("dm readable", "secret dm" in load("ghabdm")[0]["display"][3])
check("follows/likes iid unique", len({r["iid"] for r in load("ghabfollows")}) == 1)

# ban / unban
select("ghab", 1)
D.act_ghab_ban(sheet)
check("banned", sqlite3.connect(data / "ghab.db").execute("select banned from users where id=2").fetchone()[0] == 1)
select("ghab", 1)
D.act_ghab_ban(sheet)
check("unbanned", sqlite3.connect(data / "ghab.db").execute("select banned from users where id=2").fetchone()[0] == 0)

# link old account to a Windroida user (must exist there)
select("ghab", 1)
with mock.patch.dict(sys.modules, {"tkinter.simpledialog": types.SimpleNamespace(askstring=lambda *a, **k: "nobody")}):
    D.act_ghab_link(sheet)
check("link unknown windroida user refused", sqlite3.connect(data / "ghab.db").execute("select wd_name from users where id=2").fetchone()[0] is None)
select("ghab", 1)
with mock.patch.dict(sys.modules, {"tkinter.simpledialog": types.SimpleNamespace(askstring=lambda *a, **k: "sara_w")}):
    D.act_ghab_link(sheet)
check("linked (exact windroida name)", sqlite3.connect(data / "ghab.db").execute("select wd_name from users where id=2").fetchone()[0] == "Sara_W")
select("ghab", 0)   # already linked
n0 = len(msgs)
D.act_ghab_link(sheet)
check("already linked refused", msgs[-1][1] is True)

# delete a post: row + both files go
select("ghabposts", 0)
sheet.tab = "ghabposts"
D.act_ghab_delete(sheet)
con = sqlite3.connect(data / "ghab.db")
check("post row gone", con.execute("select count(*) from posts").fetchone()[0] == 0)
check("comment/like cascaded", con.execute("select count(*) from comments").fetchone()[0] == 0 and con.execute("select count(*) from likes").fetchone()[0] == 0)
check("post files gone", not (data / "users/ali/posts/a.jpg").exists() and not (data / "users/ali/posts/a_p.jpg").exists())
check("other user's files kept", (data / "users/sara/x.jpg").exists())

# path traversal in a stored file name never escapes the users folder
secret = root / "outside.txt"
secret.write_text("keep me")
con.execute("insert into posts(id,user_id,file,kind,created) values(9,1,'../../../outside.txt','image',1)")
con.commit()
con.close()
select("ghabposts", 0)
sheet.tab = "ghabposts"
D.act_ghab_delete(sheet)
check("traversal path not deleted", secret.exists())
check("traversal open refused", D._ghab_file(sheet, "../../../outside.txt") is None)

# delete a user: whole folder goes, nobody else's
select("ghab", 1)
sheet.tab = "ghab"
D.act_ghab_delete(sheet)
check("user folder removed", not (data / "users/sara").exists())
check("other folder kept", (data / "users/ali").exists())

# unsupported
sheet.tab = "ghablikes"
D.act_ghab_delete(sheet)
check("likes not deletable", msgs[-1][1] is True)
shutil.rmtree(root, ignore_errors=True)
print(f"ghab sheet: {ok} ok, {bad} failed")
sys.exit(1 if bad else 0)
