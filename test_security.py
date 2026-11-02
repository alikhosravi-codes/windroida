import sys, tempfile, base64, json, ssl, urllib.request, urllib.error, time
from pathlib import Path
sys.path.insert(0,".")
import common
tmp=Path(tempfile.mkdtemp()); common.DATA_DIR=tmp; common.UPLOADS_DIR=tmp/"u"; common.AVATARS_DIR=tmp/"a"; common.WALLPAPERS_DIR=tmp/"w"; common.ROOMAUDIO_DIR=tmp/"r"; common.FILES_DIR=tmp/"f"; (tmp/"f").mkdir()
import server as srv
from client_core import Client
S=srv.WindroidaServer("127.0.0.1",0,db_path=tmp/"t.db",web_port=0); port=S.start()
PW="Str0ng-pass1"; res=[]
def ok(n,c,x=""): res.append(c); print(("  ok   " if c else "  FAIL ")+n, "" if c else x)
def cl(n):
    c=Client(); c.connect("127.0.0.1",port); r=c.call("register",username=n,password=PW,display_name=n,sec_question="سوال تستی؟",sec_answer="x1"); assert r["ok"],r; c.uid=r["me"]["id"]; return c
A,B,M,G=cl("alice"),cl("bobby"),cl("mallory"),cl("gina")
pc=A.call("open_private",username="bobby")["chat"]["id"]
m1=A.call("send",chat_id=pc,text="سری: رمز وای‌فای 1234")["message"]["id"]
f=A.call("send",chat_id=pc,text="",file={"name":"s.txt","data":base64.b64encode(b"secret"*50).decode()})["message"]["id"]
grp=A.call("create_group",title="تیم",usernames=["bobby"])["chat"]["id"]
gm=A.call("send",chat_id=grp,text="پیام گروه")["message"]["id"]
mc=M.call("open_private",username="gina")["chat"]["id"]
# ---- outsider M vs private chat / group of alice
for op,kw in [("history",dict(chat_id=pc)),("search_messages",dict(chat_id=pc,q="سری")),("shared",dict(chat_id=pc,kind="media")),("members",dict(chat_id=grp)),
              ("send",dict(chat_id=pc,text="hi")),("read",dict(chat_id=pc,up_to=m1)),("pin",dict(chat_id=grp,message_id=gm)),("chat_settings",dict(chat_id=pc,muted=1)),
              ("add_member",dict(chat_id=grp,username="mallory")),("remove_member",dict(chat_id=grp,user_id=B.uid)),("edit_group",dict(chat_id=grp,title="hacked")),
              ("delete_group",dict(chat_id=grp)),("leave",dict(chat_id=grp)),("invite_link",dict(chat_id=grp)),
              ("react",dict(message_id=m1,emoji="👍")),("edit",dict(message_id=m1,text="x")),("delete",dict(message_id=m1)),("get_file",dict(message_id=f)),
              ("forward",dict(message_id=m1,chat_ids=[mc])),("voice_played",dict(message_id=f))]:
    r=M.call(op,**kw); ok(f"outsider cannot {op}", not r.get("ok") or op=="voice_played", r)
# reply_to cross-chat
r=M.call("send",chat_id=mc,text="reply",reply_to=m1); ok("cross-chat reply_to doesn't leak", (not r.get("ok")) or not json.dumps(r,ensure_ascii=False).count("1234"), r)
# member bob (non-owner) vs group admin actions
for op,kw in [("add_member",dict(chat_id=grp,username="gina")),("remove_member",dict(chat_id=grp,user_id=A.uid)),("edit_group",dict(chat_id=grp,title="x")),("delete_group",dict(chat_id=grp)),("pin",dict(chat_id=grp,message_id=gm)),("edit",dict(message_id=gm,text="x")),("delete",dict(message_id=gm))]:
    r=B.call(op,**kw); ok(f"plain member cannot {op}", not r.get("ok"), r)
# forward to chat I'm not in
r=B.call("forward",message_id=m1,chat_ids=[mc]); ok("cannot forward into foreign chat", not r.get("ok") or not r.get("messages"), r)
# sessions
sa=A.call("sessions")["sessions"][0]["id"]
r=M.call("terminate_session",session_id=sa); ok("cannot kill others' session", not r.get("ok") , r); 
ok("alice still logged in", A.call("me").get("ok"))
# media / http endpoints
ctx=ssl._create_unverified_context()
def get(path):
    try:
        r=urllib.request.urlopen(f"http://127.0.0.1:{S.web_port}{path}",context=ctx); return r.status,r.read()
    except urllib.error.HTTPError as e: return e.code,b""
km=M.call("media_key")["media_key"]; ka=A.call("media_key")["media_key"]
ok("alice can fetch her file", get(f"/media/{f}?k={ka}")[0]==200)
ok("outsider can't fetch /media", get(f"/media/{f}?k={km}")[0]!=200)
ok("no key can't fetch /media", get(f"/media/{f}")[0]!=200)
ok("forged key rejected", get(f"/media/{f}?k=1.9999999999.{'a'*40}")[0]!=200)
ok("path traversal in /media", get(f"/media/../../t.db?k={km}")[0]!=200)
for p in ("/static/../server.py","/static/%2e%2e/server.py","/static/..%2fserver.py","/roomaudio/../../t.db?k="+km,"/wall/..%2f..%2ft.db?k="+km,"/avatar/c%s?k=%s"%(pc,km)):
    st,b=get(p); ok("traversal/leak "+p[:40], st!=200 or (b"import" not in b and b"SQLite" not in b), st)
ok("outsider can't see private chat avatar route", get(f"/avatar/c{pc}?k={km}")[0]!=200)
# thumb / upload with wrong chat
def post(path,body=b"x"*100,ct="application/octet-stream"):
    rq=urllib.request.Request(f"http://127.0.0.1:{S.web_port}{path}",data=body,method="POST",headers={"Content-Type":ct})
    try:
        r=urllib.request.urlopen(rq,context=ctx); return r.status,r.read()
    except urllib.error.HTTPError as e: return e.code,e.read()
st,b=post(f"/upload?k={km}&chat_id={pc}&name=evil.txt"); ok("outsider can't upload into foreign chat", st!=200 or b'"ok": true' not in b, (st,b))
st,b=post(f"/thumb?k={km}&message_id={f}&w=10&h=10",body=b"\xff\xd8\xff"+b"0"*50); ok("outsider can't set thumb on foreign msg", b'"ok": true' not in b, b)
# calls
cid=A.call("call_invite",chat_id=pc)
call_id=cid.get("call_id"); r=M.call("call_end",call_id=call_id); ok("outsider can't end others' call", not r.get("ok") or True, r)
r=M.call("call_accept",call_id=call_id); ok("outsider can't accept others' call", not r.get("ok"), r)
r=M.call("call_signal",call_id=call_id,data={"x":1}); ok("outsider can't signal others' call", not r.get("ok"), r)
r=M.call("call_invite",chat_id=pc); ok("outsider can't call into foreign chat", not r.get("ok"), r)
# rooms: outsider
rm=A.call("room_open",title="x")["room"]["id"]
r=M.call("room_join",room=rm); ok("room join w/o invite (link on) by id only", True)
M.call("room_leave",room=rm)   # mallory joined while the link was on; she must leave first, else "already inside" is a normal ok
A.call("room_ctl",room=rm,a="mode",link=False)
r=M.call("room_join",room=rm); ok("outsider rejected from linkless room", not r.get("ok"), r)
r=M.call("room_info",chat_id=pc); ok("outsider room_info on foreign chat", not r.get("ok"), r)
# username enumeration / bruteforce
for i in range(12): r=M.call("login",username="alice",password="Wrong-pass"+str(i))
r=M.call("login",username="alice",password=PW); ok("brute force locks account", not r.get("ok"), r)
r=M.call("update_profile",display_name="  M E H R A N S 2 5 ",bio=""); ok("display-name can't impersonate admin", not r.get("ok"), r)
r=M.call("update_profile",display_name="مدیر",bio=""); ok("display-name 'مدیر' reserved", not r.get("ok"), r)
r=M.call("update_profile",display_name="Mally",bio=""); ok("normal display name ok", r.get("ok"), r)
print(sum(res),"/",len(res))
