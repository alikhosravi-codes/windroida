"""Ghab (قاب) folded into Windroida: SSO-only, no signup/login/reset/admin. Run:  python test_ghab.py"""
import hashlib
import hmac
import os
import shutil
import sys
import tempfile
import time

TMP = tempfile.mkdtemp()
SECRET = "ab12" * 16
os.environ.update(GHAB_DATA=TMP, GHAB_PASSWORD_AUTH="0", GHAB_SSO_SECRET=SECRET, GHAB_COMPRESS="0")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghab"))

from server import create_app  # noqa: E402
from server.db import migrate  # noqa: E402

ok = bad = 0


def check(name, cond):
    global ok, bad
    if cond:
        ok += 1
    else:
        bad += 1
        print("FAIL", name)


def token(user, secret=SECRET, ttl=60, nonce=None):
    exp = int(time.time()) + ttl
    nonce = nonce or os.urandom(6).hex()
    sig = hmac.new(secret.encode(), f"{user}:{exp}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return f"{user}.{exp}.{nonce}.{sig}"


app = create_app()
app.config["TESTING"] = True


def client():
    return app.test_client()


c = client()
check("auth_mode off", c.get("/api/auth_mode").get_json() == {"password_auth": False})
check("register blocked", c.post("/api/register", json={"username": "hacker1", "password": "123456", "sec_q": 0, "sec_a": "xx"}).status_code == 403)
check("login blocked", c.post("/api/login", json={"username": "a", "password": "b"}).status_code == 403)
check("reset question blocked", c.get("/api/reset/question?username=a").status_code == 403)
check("reset blocked", c.post("/api/reset", json={"username": "a", "answer": "x", "password": "123456"}).status_code == 403)
check("admin page 404", c.get("/admin").status_code == 404)
check("admin api 404", c.get("/admin/api/overview").status_code == 404)
check("admin login 404", c.post("/admin/api/login", json={"password": "wrong-password"}).status_code == 404)
check("me needs login", c.get("/api/me").status_code == 401)
check("media needs login", c.get("/media/x/y.jpg").status_code == 401)

# valid SSO
r = c.get("/sso?t=" + token("Ali_Reza"))
check("sso redirect", r.status_code == 302 and r.headers["Location"].endswith("/"))
me = c.get("/api/me").get_json()
check("me ok lower name", me.get("username") == "ali_reza")
check("sso_only flag", me.get("sso_only") is True and me.get("has_sec") is True)
check("cookie name", "ghab_session" in r.headers.get("Set-Cookie", ""))
check("password change blocked", c.post("/api/me/password", json={"current": "x", "password": "123456"}).status_code == 403)
check("security blocked", c.post("/api/me/security", json={"current": "x", "sec_q": 0, "sec_a": "zz"}).status_code == 403)

# replay, tampering, expiry, wrong secret
t = token("Zed_One")
check("first use", client().get("/sso?t=" + t).status_code == 302)
check("replay rejected", client().get("/sso?t=" + t).status_code == 400)
t = token("Zed_Two")
p = t.split(".")
check("tampered name", client().get("/sso?t=" + ".".join(["Admin_X"] + p[1:])).status_code == 400)
check("tampered expiry", client().get("/sso?t=" + ".".join([p[0], str(int(p[1]) + 9999)] + p[2:])).status_code == 400)
check("expired", client().get("/sso?t=" + token("Zed_Old", ttl=-5)).status_code == 400)
check("wrong secret", client().get("/sso?t=" + token("Zed_Bad", secret="ff" * 32)).status_code == 400)
check("garbage", client().get("/sso?t=abc").status_code == 400)
check("no token", client().get("/sso").status_code == 400)
check("bad name chars", client().get("/sso?t=" + token("../etc")).status_code == 400)

# Case collisions, reserved names, old standalone accounts: never merged
c2 = client()
c2.get("/sso?t=" + token("Sara_1"))
c3 = client()
c3.get("/sso?t=" + token("sara_1"))
a, b = c2.get("/api/me").get_json(), c3.get("/api/me").get_json()
check("Sara_1 vs sara_1 distinct", a["id"] != b["id"] and a["username"] != b["username"])
c4 = client()
c4.get("/sso?t=" + token("con"))
check("reserved name", c4.get("/api/me").get_json()["username"] == "u_con")
c4b = client()
c4b.get("/sso?t=" + token("con"))
check("same wd name -> same account", c4b.get("/api/me").get_json()["id"] == c4.get("/api/me").get_json()["id"])
from server import config  # noqa: E402
import sqlite3  # noqa: E402
con = sqlite3.connect(config.DB_PATH)
con.execute("insert into users(username,pass,name,created) values('oldguy','x','Old',0)")
con.commit()
c5 = client()
c5.get("/sso?t=" + token("OldGuy"))
m5 = c5.get("/api/me").get_json()
check("old standalone account not taken over", m5["username"] != "oldguy" and m5["username"].startswith("oldguy"))

# banned
con.execute("update users set banned=1 where wd_name='Zed_One'")
con.commit()
check("banned refused", client().get("/sso?t=" + token("Zed_One")).status_code == 403)
con.close()

# ---- security hardening: Origin check, CSP, no HSTS knob here
cs = client()
cs.get("/sso?t=" + token("Orig_Test"))
r = cs.post("/api/logout", headers={"Origin": "https://evil.example:8082"})
check("foreign Origin refused on POST", r.status_code == 403)
r = cs.post("/api/impressions", json={"ids": []}, headers={"Origin": "http://localhost"})
check("same Origin accepted", r.status_code == 200)
r = cs.post("/api/impressions", json={"ids": []})
check("no Origin accepted (native clients)", r.status_code == 200)
check("CSP header", "frame-ancestors 'none'" in cs.get("/").headers.get("Content-Security-Policy", ""))
check("nosniff", cs.get("/").headers.get("X-Content-Type-Options") == "nosniff")

# ---- algorithm: author diversity + trending pool + new posts get a chance
con = sqlite3.connect(config.DB_PATH)
ids = {n: con.execute("select id from users where wd_name=?", (n,)).fetchone()[0] for n in ("Ali_Reza", "Sara_1", "sara_1", "Orig_Test")}
me_id = ids["Orig_Test"]
now = time.time()
spam, other1, other2 = ids["Ali_Reza"], ids["Sara_1"], ids["sara_1"]
for u in (spam, other1, other2):
    con.execute("insert or ignore into follows(follower,followee) values(?,?)", (me_id, u))
nid = 1000
for i in range(12):
    con.execute("insert into posts(id,user_id,file,kind,caption,created) values(?,?,?,?,?,?)", (nid, spam, "x/y.jpg", "image", "spam", now - 60 * i)); nid += 1
for u in (other1, other2):
    con.execute("insert into posts(id,user_id,file,kind,caption,created) values(?,?,?,?,?,?)", (nid, u, "x/y.jpg", "image", "other", now - 600)); nid += 1
con.commit()
feed = cs.get("/api/feed?limit=8").get_json()
authors = [p["user"]["id"] if isinstance(p.get("user"), dict) else p.get("user_id") for p in feed]
check("feed returns posts", len(feed) >= 6)
check("feed not flooded by one author", len(set(authors[:6])) >= 3 and authors[:6].count(spam) <= 4)
# seen posts go to the bottom but stay in the feed
f1 = cs.get("/api/feed?limit=30").get_json()
top3 = [p["id"] for p in f1[:3]]
c2 = sqlite3.connect(config.DB_PATH)
for pid in top3:
    c2.execute("insert or ignore into impressions(post_id,user_id,created) values(?,?,?)", (pid, me_id, time.time()))
c2.commit()
c2.close()
f2 = [p["id"] for p in cs.get("/api/feed?limit=30").get_json()]
check("seen posts still in feed", all(i in f2 for i in top3))
check("seen posts at the bottom", set(f2[-3:]) == set(top3))
check("unseen come first", not any(i in top3 for i in f2[:len(f2) - 3]))
check("feed size unchanged by seen", len(f2) == len(f1))
# trending pool: an old, popular post beyond the 200 newest must show up in explore
for i in range(230):
    con.execute("insert into posts(id,user_id,file,kind,caption,created) values(?,?,?,?,?,?)", (2000 + i, spam, "x/y.jpg", "image", "junk", now - 3600 + i)); 
con.execute("insert into posts(id,user_id,file,kind,caption,created) values(5000,?,?,?,?,?)", (other1, "x/y.jpg", "image", "gem", now - 5 * 86400))
for u in (spam, other2, other1):
    con.execute("insert or ignore into likes(user_id,post_id) values(?,5000)", (u,))
con.commit()
con.close()
ex = [p["id"] for p in cs.get("/api/explore").get_json()]
check("explore shows an older popular post", 5000 in ex)


# very old posts stay candidates (older = lower, never dropped); share list = only people I follow
con = sqlite3.connect(config.DB_PATH)
for i in range(400):
    con.execute("insert into posts(id,user_id,file,kind,caption,created) values(?,?,?,?,?,?)", (50000 + i, other1, "x/y.mp4", "video", "old", now - 86400 * 400 - i)); 
con.commit(); con.close()
allp = []
for _ in range(60):
    b = cs.get("/api/reels?limit=30&exclude=" + ",".join(map(str, allp))).get_json()
    if not b: break
    allp += [p["id"] for p in b]
check("reels: all 400 old videos reachable", len([i for i in allp if i >= 50000]) == 400)
sh = cs.get("/api/share/suggest").get_json()
names = {u["username"] for u in sh}
con = sqlite3.connect(config.DB_PATH)
fol_names = {con.execute("select username from users where id=?", (u,)).fetchone()[0] for u in (spam, other1, other2)}
con.close()
check("share list only followees", names and names <= fol_names)
check("share search only followees", all(u["username"] in fol_names for u in cs.get("/api/share/suggest?q=a").get_json()))

# local datasheet gate: loopback + file key only
from server.routes.local import KEY  # noqa: E402
lc = client()
check("local: no key -> 404", lc.get("/_local/status").status_code == 404)
check("local: wrong key -> 404", lc.get("/_local/status", headers={"X-Local-Key": "x" * 64}).status_code == 404)
check("local: right key ok", lc.get("/_local/status", headers={"X-Local-Key": KEY}).status_code == 200)
check("local: proxied header refused", lc.get("/_local/status", headers={"X-Local-Key": KEY, "X-Forwarded-For": "1.2.3.4"}).status_code == 404)
check("local: foreign address refused", lc.get("/_local/status", headers={"X-Local-Key": KEY}, environ_overrides={"REMOTE_ADDR": "8.8.8.8"}).status_code == 404)
check("local: compress runs", lc.post("/_local/compress", headers={"X-Local-Key": KEY}).status_code == 200)
check("web admin gone", c.get("/admin").status_code == 404 and c.get("/static/admin.js").status_code == 404)

# a Cloud-Drive-style token (other key) never opens Ghab: covered by wrong secret above
print(f"ghab: {ok} ok, {bad} failed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if bad else 0)
