"""Watch / listen-together rooms (v2).

* A room lives in memory (and is logged to the `rooms_log` table so the datasheet can show it).
* Three kinds: a room of a private chat (both people see it, either can invite more), a room of a group
  (all group members see it) and a FREE room (anyone can create one; invisible to everybody except the people the
  creator - or a member - invites, or who got the secret link).
* Everybody opens the SAME file from their own device (or a song from the server library); the server only relays
  tiny control messages, room chat and voice, and owns ONE timeline for everybody:
      position now = pos + (server_now - at) * rate / 1000      (pos is valid at server time `at`, in ms)
* Connection drops (phone thinks the app was closed) don't kick anybody: a user stays in the room for
  ROOM_GRACE seconds and picks the room up again on reconnect.
"""
import os
import re
import secrets
import threading
import time
from collections import deque

MAX_QUEUE = 100
MAX_MEMBERS = int(os.environ.get("WINDROIDA_ROOM_MAX", "0"))      # 0 = no limit (the server's bandwidth is the limit)
MAX_CAMS = 4                       # cameras on at the same time per room (video goes through the server: ~5 Mbps upload)
MAX_SPEAKERS = 8                   # talking at the same instant (like Telegram voice chats); listeners/members are unlimited
CHAT_KEEP = 100
ROOM_GRACE = int(os.environ.get("WINDROIDA_ROOM_GRACE", "90"))
HOLD_MAX = 15                      # seconds everybody waits for a buffering friend, at most
KINDS = ("video", "audio")
REACTS = ("❤️", "😂", "😮", "👏", "🔥", "😢", "👍", "🎉")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‎‏‪-‮⁦-⁩]")


def clean(text, n):
    return " ".join(_CTRL.sub("", str(text or "")).split())[:n]


def make_rooms(Fail, common, admin_user):
    def ms():
        return int(time.time() * 1000)

    class RoomsMixin:
        # ================================================================ helpers
        def _rooms_init(self):
            self.rooms = {}
            self.rooms_lock = threading.RLock()
            self._rooms_cleaned = False

        def _room_hits(self, key, limit, window):
            q = self.ip_hits[key]
            t = time.time()
            while q and q[0] < t - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(t)
            return True

        def _room_guard(self, uid, bucket, limit, window, msg="آروم‌تر"):
            if not self._room_hits(("room", bucket, uid), limit, window):
                raise Fail(msg)

        def _room_members_view(self, room):
            return [{"id": uid, "name": m["name"], "have": m["have"], "mic": m["mic"], "buf": m["buf"], "away": m["away"], "hq": m["hq"], "cam": m["cam"]}
                    for uid, m in room["members"].items()]

        def _room_meta(self, room):
            return {"id": room["id"], "kind": room["kind"], "chat_id": room["chat_id"], "title": room["title"],
                    "owner": room["owner"], "host": room["host"], "ctl": room["ctl"], "invite": room["invite"],
                    "link": room["link"], "wait": room["wait"], "repeat": room["repeat"]}

        def _room_state_view(self, room):
            return {"room": room["id"], "meta": self._room_meta(room), "cur": room["cur"], "playing": room["playing"],
                    "held": room["held"], "pos": room["pos"], "at": room["at"], "rate": room["rate"],
                    "queue": room["queue"], "v": room["v"], "now": ms(), "host": room["host"], "ctl": room["ctl"],
                    "repeat": room["repeat"], "chat_id": room["chat_id"]}

        def _room_send(self, room, event, exclude_uid=None, relay=False, video=False):
            with self.sessions_lock:
                targets = []
                for uid, m in room["members"].items():
                    if uid == exclude_uid:
                        continue
                    targets += [c for c in self.sessions.get(uid, []) if c.alive and getattr(c, "room_id", None) == room["id"]]
            for c in targets:
                (c.relay(event, video=True) if video else c.relay(event) if relay else c.send(event))

        def _room_broadcast_members(self, room, exclude_uid=None):
            self._room_send(room, {"event": "room_members", "room": room["id"], "members": self._room_members_view(room),
                                   "host": room["host"], "meta": self._room_meta(room)}, exclude_uid=exclude_uid)

        def _room_broadcast_state(self, room, by=0, act=""):
            self._room_send(room, {"event": "room_state", "room": room["id"], "state": self._room_state_view(room), "by": by, "act": act})

        @staticmethod
        def _room_cur_name(room):
            for it in room["queue"]:
                if it["id"] == room["cur"]:
                    return it["name"]
            return ""

        def _room_pos(self, room):
            return room["pos"] + ((ms() - room["at"]) * room["rate"] / 1000.0 if room["playing"] else 0)

        def _room_log(self, room, closed=False):
            """Keep the datasheet's picture of the room up to date (own short connection: may run on any thread)."""
            try:
                db = common.connect_db(self.db_path)
            except Exception:
                return
            try:
                names = ", ".join(m["name"] for m in room["members"].values())
                with db:
                    db.execute(
                        "INSERT INTO rooms_log(id, kind, chat_id, title, owner_id, owner_name, created_at, last_at, closed_at, peak, members, cur, songs, invites) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, last_at=excluded.last_at, "
                        "closed_at=excluded.closed_at, peak=MAX(peak, excluded.peak), members=excluded.members, cur=excluded.cur, "
                        "songs=excluded.songs, invites=excluded.invites",
                        (room["id"], room["kind"], room["chat_id"], room["title"], room["owner"], room["owner_name"], room["created"],
                         int(time.time()), int(time.time()) if closed else None, len(room["members"]), names, self._room_cur_name(room),
                         room["songs"], room["invites"]))
            except Exception:
                pass
            finally:
                db.close()

        def _room_announce(self, room):
            """Rooms of a chat/group: tell the chat's members (join banner). Free rooms stay invisible."""
            if room["chat_id"] is None:
                return
            try:
                db = common.connect_db(self.db_path)
                try:
                    ids = self._member_ids(db, room["chat_id"])
                finally:
                    db.close()
            except Exception:
                return
            live = bool(room["members"])
            self.push(ids, {"event": "room_live", "chat_id": room["chat_id"], "room": room["id"], "live": live,
                            "count": len(room["members"]), "cur": self._room_cur_name(room)})

        def _room_close(self, room):
            with self.rooms_lock:
                self.rooms.pop(room["id"], None)
            self._room_log(room, closed=True)
            self._room_announce(room)

        def _room_get(self, rid):
            with self.rooms_lock:
                return self.rooms.get(rid) if isinstance(rid, str) else None

        def _room_of(self, conn, req, member=True):
            room = self._room_get(req.get("room"))
            if room is None or (member and conn.uid not in room["members"]):
                raise Fail("اتاقی فعال نیست")
            return room

        def _room_may_join(self, db, conn, room):
            if conn.uid in room["banned"]:
                raise Fail("از این اتاق حذف شدی")
            if conn.uid in room["members"] or conn.uid in room["invited"] or conn.uid == room["owner"]:
                return
            if room["chat_id"] is not None:
                if db.execute("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (room["chat_id"], conn.uid)).fetchone():
                    return
            if room["link"] and room["kind"] == "free":
                return                      # the link carries the room's secret id
            raise Fail("برای این اتاق دعوت نشدی")

        def _room_enter(self, conn, room, created=False):
            """Put conn's user into the room (leaving any other room first) and return the full snapshot."""
            old = getattr(conn, "room_id", None)
            if old and old != room["id"]:
                prev = self._room_get(old)
                if prev:
                    conn.room_id = None
                    self._room_leave_uid(prev, conn.uid, kick_other_conns=False)
            name = conn.db.execute("SELECT display_name FROM users WHERE id=?", (conn.uid,)).fetchone()["display_name"]
            with self.rooms_lock:
                if conn.uid not in room["members"] and MAX_MEMBERS and len(room["members"]) >= MAX_MEMBERS:
                    raise Fail("اتاق پره")
                prev_m = room["members"].get(conn.uid)
                room["members"][conn.uid] = {"name": name, "have": prev_m["have"] if prev_m else False, "mic": False,
                                             "buf": False, "away": False,
                                             "hq": prev_m["hq"] if prev_m else False, "cam": False}
                room["invited"].discard(conn.uid)
                room["empty_since"] = None
            conn.room_id = room["id"]
            self._room_broadcast_members(room, exclude_uid=conn.uid)
            self._room_announce(room)
            self._room_log(room)
            return {"room": self._room_meta(room), "state": self._room_state_view(room), "members": self._room_members_view(room),
                    "chat": list(room["chat"]), "created": created, "me": conn.uid}

        def _room_leave_uid(self, room, uid, kick_other_conns=True):
            with self.rooms_lock:
                if uid not in room["members"]:
                    return
                room["members"].pop(uid, None)
                if kick_other_conns:
                    with self.sessions_lock:
                        for c in self.sessions.get(uid, []):
                            if getattr(c, "room_id", None) == room["id"]:
                                c.room_id = None
                empty = not room["members"]
                if not empty and room["host"] == uid:
                    room["host"] = next(iter(room["members"]))
                    room["v"] += 1
            if empty:
                return self._room_close(room)
            self._room_broadcast_members(room)
            self._room_buf_check(room)
            self._room_broadcast_state(room)
            self._room_announce(room)
            self._room_log(room)

        def _room_drop(self, conn):
            """A connection closed. The user keeps their seat for ROOM_GRACE seconds (mobile networks blink, phones
            suspend pages) and simply rejoins when the app comes back."""
            rid = getattr(conn, "room_id", None)
            if rid is None or conn.uid is None:
                return
            room = self._room_get(rid)
            if room is None:
                return
            with self.sessions_lock:
                if any(c is not conn and c.alive and getattr(c, "room_id", None) == rid for c in self.sessions.get(conn.uid, [])):
                    return
            m = room["members"].get(conn.uid)
            if not m:
                return
            m["away"], m["mic"], m["buf"], m["cam"] = True, False, False, False
            self._room_broadcast_members(room)
            self._room_buf_check(room)
            t = threading.Timer(ROOM_GRACE, self._room_expire, (rid, conn.uid))
            t.daemon = True
            t.start()

        def _room_expire(self, rid, uid):
            room = self._room_get(rid)
            if room and room["members"].get(uid, {}).get("away"):
                self._room_leave_uid(room, uid)

        # ---- buffering hold: everybody waits (max HOLD_MAX s) for a friend whose player is still loading
        def _room_buf_check(self, room):
            with self.rooms_lock:
                waiting = [m for m in room["members"].values() if m["buf"] and not m["away"]]
                act = None
                if room["wait"] and room["playing"] and waiting:
                    room["pos"], room["at"], room["playing"], room["held"] = self._room_pos(room), ms(), False, True
                    room["v"] += 1
                    act = "hold"
                    t = threading.Timer(HOLD_MAX, self._room_force_resume, (room["id"], room["v"]))
                    t.daemon = True
                    t.start()
                elif room["held"] and not waiting:
                    room["at"], room["playing"], room["held"] = ms(), True, False
                    room["v"] += 1
                    act = "resume"
            if act:
                self._room_broadcast_state(room, act=act)

        def _room_force_resume(self, rid, v):
            room = self._room_get(rid)
            if not room:
                return
            with self.rooms_lock:
                if not room["held"] or room["v"] != v:
                    return
                for m in room["members"].values():
                    m["buf"] = False
            self._room_broadcast_members(room)
            self._room_buf_check(room)

        # ================================================================ ops
        def op_room_time(self, conn, req):
            return {"t": ms()}

        def op_room_sync(self, conn, req):
            """heartbeat (also re-calibrates the client clock) - returns the authoritative timeline"""
            room = self._room_of(conn, req)
            m = room["members"][conn.uid]
            if m["away"]:
                m["away"] = False
                self._room_broadcast_members(room)
            return {"state": self._room_state_view(room), "members": self._room_members_view(room)}

        def op_room_info(self, conn, req):
            cid, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
            with self.rooms_lock:
                room = next((r for r in self.rooms.values() if r["chat_id"] == cid), None)
            if not room:
                return {"live": False}
            return {"live": True, "room": room["id"], "count": len(room["members"]), "cur": self._room_cur_name(room)}

        def op_room_mine(self, conn, req):
            """rooms this user may join: rooms of their chats/groups that are running + rooms they were invited to"""
            out = []
            my_chats = {r["chat_id"] for r in conn.db.execute("SELECT chat_id FROM members WHERE user_id=?", (conn.uid,))}
            with self.rooms_lock:
                for r in self.rooms.values():
                    if r["chat_id"] in my_chats or conn.uid in r["invited"] or conn.uid in r["members"]:
                        out.append({"room": r["id"], "title": r["title"], "kind": r["kind"], "count": len(r["members"]),
                                    "cur": self._room_cur_name(r), "invited": conn.uid in r["invited"], "joined": conn.uid in r["members"]})
            return {"rooms": out}

        def op_room_open(self, conn, req):
            """Create (or get) the room of a chat, or create a FREE room (no chat_id), and join it."""
            if req.get("chat_id") is not None:
                cid, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
                c = conn.db.execute("SELECT kind, channel, title FROM chats WHERE id=?", (cid,)).fetchone()
                if c is None or c["channel"]:
                    raise Fail("تو کانال اتاق تماشا نیست")
                with self.rooms_lock:
                    room = next((r for r in self.rooms.values() if r["chat_id"] == cid), None)
                if room:
                    self._room_may_join(conn.db, conn, room)
                    return self._room_enter(conn, room)
                kind = "private" if c["kind"] == "private" else "group"
                title = "اتاق " + clean(c["title"] or "چت", 30) if kind == "group" else "اتاق تماشا"
            else:
                cid, kind = None, "free"
                title = clean(req.get("title"), 40) or "اتاق من"
            self._room_guard(conn.uid, "create", 60, 3600, "تعداد ساخت اتاق زیاد شد؛ بعداً امتحان کن")
            name = conn.db.execute("SELECT display_name FROM users WHERE id=?", (conn.uid,)).fetchone()["display_name"]
            room = {"id": secrets.token_urlsafe(9), "kind": kind, "chat_id": cid, "title": title, "owner": conn.uid, "owner_name": name,
                    "host": conn.uid, "ctl": "all", "invite": "all", "link": kind == "free", "wait": True, "repeat": "off",
                    "cur": None, "playing": False, "held": False, "pos": 0.0, "at": ms(), "rate": 1.0, "queue": [], "v": 1,
                    "members": {}, "invited": set(), "banned": set(), "chat": deque(maxlen=CHAT_KEEP), "next_id": 1,
                    "created": int(time.time()), "songs": 0, "invites": 0, "empty_since": None}
            with self.rooms_lock:
                if cid is not None:                       # lost a race with someone else opening the same chat's room
                    other = next((r for r in self.rooms.values() if r["chat_id"] == cid), None)
                    if other:
                        return self._room_enter(conn, other)
                self.rooms[room["id"]] = room
            return self._room_enter(conn, room, created=True)

        def op_room_join(self, conn, req):
            room = self._room_get(req.get("room"))
            if room is None:
                raise Fail("این اتاق دیگه وجود نداره")
            self._room_guard(conn.uid, "join", 40, 600)
            self._room_may_join(conn.db, conn, room)
            return self._room_enter(conn, room)

        def op_room_leave(self, conn, req):
            room = self._room_of(conn, req)
            conn.room_id = None
            self._room_leave_uid(room, conn.uid)
            return {}

        def op_room_invite(self, conn, req):
            room = self._room_of(conn, req)
            if room["invite"] == "host" and room["host"] != conn.uid:
                raise Fail("فقط میزبان می‌تونه دعوت کنه")
            self._room_guard(conn.uid, "invite", 25, 3600, "تعداد دعوت‌ها زیاد شد؛ بعداً امتحان کن")
            uname = str(req.get("username", "")).strip().lstrip("@")
            if not common.USERNAME_RE.fullmatch(uname):
                raise Fail("نام کاربری نامعتبره")
            u = conn.db.execute("SELECT id, display_name, banned FROM users WHERE username=? COLLATE NOCASE", (uname,)).fetchone()
            if not u or u["banned"]:
                raise Fail("کاربری با این نام پیدا نشد")
            if u["id"] == conn.uid:
                raise Fail("خودتی 🙂")
            if u["id"] in room["members"]:
                raise Fail("همین الان تو اتاقه")
            if u["id"] in room["banned"]:
                raise Fail("این کاربر از اتاق حذف شده")
            room["invited"].add(u["id"])
            room["invites"] += 1
            me = room["members"][conn.uid]["name"]
            self.push([u["id"]], {"event": "room_invite", "room": room["id"], "title": room["title"], "kind": room["kind"],
                                  "from": {"id": conn.uid, "name": me}})
            self._room_log(room)
            return {"invited": u["display_name"]}

        def op_room_kick(self, conn, req):
            room = self._room_of(conn, req)
            if room["host"] != conn.uid:
                raise Fail("فقط میزبان می‌تونه کسی رو حذف کنه")
            try:
                uid = int(req.get("user"))
            except (TypeError, ValueError):
                raise Fail("کاربر نامعتبر")
            if uid == conn.uid or uid not in room["members"]:
                raise Fail("این کاربر تو اتاق نیست")
            room["banned"].add(uid)
            self.push([uid], {"event": "room_kicked", "room": room["id"]})
            with self.sessions_lock:
                for c in self.sessions.get(uid, []):
                    if getattr(c, "room_id", None) == room["id"]:
                        c.room_id = None
            self._room_leave_uid(room, uid, kick_other_conns=False)
            return {}

        def op_room_flags(self, conn, req):
            room = self._room_of(conn, req)
            self._room_guard(conn.uid, "flags", 60, 10)
            m = room["members"][conn.uid]
            m["away"] = False
            if req.get("cam") and not m["cam"] and sum(1 for x in room["members"].values() if x["cam"]) >= MAX_CAMS:
                raise Fail("حداکثر ۴ دوربین هم‌زمان تو اتاق روشن می‌تونه باشه")
            for k in ("have", "mic", "buf", "hq", "cam"):
                if k in req:
                    m[k] = bool(req[k])
            self._room_broadcast_members(room)
            if "buf" in req:
                self._room_buf_check(room)
            return {}

        def op_room_ctl(self, conn, req):
            room = self._room_of(conn, req)
            self._room_guard(conn.uid, "ctl", 40, 10)
            a = str(req.get("a", ""))
            uid = conn.uid
            can = room["ctl"] == "all" or room["host"] == uid
            added = []
            with self.rooms_lock:
                if a == "ended":           # any client may report the end of the current item (idempotent)
                    if req.get("item") != room["cur"] or not room["playing"]:
                        return {}
                    self._room_advance(room, +1, auto=True)
                elif a == "mode":
                    if room["host"] != uid:
                        raise Fail("فقط میزبان می‌تونه این رو عوض کنه")
                    if req.get("ctl") in ("host", "all"):
                        room["ctl"] = req["ctl"]
                    if req.get("repeat") in ("off", "all", "one"):
                        room["repeat"] = req["repeat"]
                    if req.get("invite") in ("host", "all"):
                        room["invite"] = req["invite"]
                    if "link" in req and room["kind"] == "free":
                        room["link"] = bool(req["link"])
                    if "wait" in req:
                        room["wait"] = bool(req["wait"])
                    if "title" in req:
                        room["title"] = clean(req["title"], 40) or room["title"]
                elif a == "add":
                    items = req.get("items") if isinstance(req.get("items"), list) else [req.get("item")]
                    for it in items[:30]:
                        if not isinstance(it, dict) or len(room["queue"]) >= MAX_QUEUE:
                            continue
                        if it.get("lib"):          # a song from the server: library, or MY OWN upload
                            rel = it["lib"]
                            p = common.resolve_roomaudio(rel) if isinstance(rel, str) else None
                            of = common.song_owner_folder(rel) if p else None
                            if p is None:
                                continue
                            if of is not None:
                                row = conn.db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
                                if of != common.song_folder(row["username"]):
                                    continue       # nobody can queue somebody else's private upload
                            item = {"id": room["next_id"], "name": clean(p.stem, 120), "size": p.stat().st_size, "dur": 0,
                                    "kind": "audio", "by": uid, "lib": rel}
                        else:
                            name = clean(it.get("name"), 120)
                            try:
                                size = int(it.get("size", 0))
                                dur = float(it.get("dur", 0) or 0)
                            except (TypeError, ValueError):
                                continue
                            kind = it.get("kind") if it.get("kind") in KINDS else "video"
                            if not name or size <= 0:
                                continue
                            item = {"id": room["next_id"], "name": name, "size": size, "dur": round(max(0, min(dur, 86400)), 2),
                                    "kind": kind, "by": uid}
                        room["next_id"] += 1
                        room["queue"].append(item)
                        added.append(item["id"])
                    if not added:
                        raise Fail("فایل نامعتبر")
                    room["songs"] += len(added)
                    if room["cur"] is None:
                        room["cur"] = added[0]
                        room["pos"], room["at"], room["playing"], room["held"] = 0.0, ms(), False, False
                elif not can:
                    raise Fail("الان فقط میزبان کنترل می‌کنه")
                elif a in ("play", "pause", "seek"):
                    try:
                        pos = float(req.get("pos", 0))
                    except (TypeError, ValueError):
                        raise Fail("زمان نامعتبر")
                    if room["cur"] is None:
                        raise Fail("هنوز چیزی تو صف نیست")
                    room["pos"], room["at"], room["held"] = max(0.0, min(pos, 86400.0)), ms(), False
                    if a != "seek":
                        room["playing"] = a == "play"
                elif a == "rate":
                    try:
                        r = float(req.get("rate", 1))
                    except (TypeError, ValueError):
                        raise Fail("سرعت نامعتبر")
                    room["pos"], room["at"] = self._room_pos(room), ms()
                    room["rate"] = max(0.5, min(r, 2.0))
                elif a == "play_item":
                    if not any(i["id"] == req.get("item") for i in room["queue"]):
                        raise Fail("این مورد تو صف نیست")
                    room["cur"], room["pos"], room["at"], room["playing"], room["held"] = req["item"], 0.0, ms(), True, False
                elif a in ("next", "prev"):
                    self._room_advance(room, 1 if a == "next" else -1)
                elif a == "remove":
                    idx = next((i for i, it in enumerate(room["queue"]) if it["id"] == req.get("item")), None)
                    if idx is None:
                        raise Fail("این مورد تو صف نیست")
                    self._room_remove_idx(room, idx)
                elif a == "clear":
                    room["queue"].clear()
                    room["cur"], room["pos"], room["at"], room["playing"], room["held"] = None, 0.0, ms(), False, False
                else:
                    raise Fail("درخواست نامعتبر")
                room["v"] += 1
            self._room_broadcast_state(room, by=uid, act=a)
            if a in ("add", "play_item", "next", "prev", "ended", "remove", "clear", "mode"):
                self._room_announce(room)
                self._room_log(room)
            if a == "mode":
                self._room_broadcast_members(room)
            return {"added": added} if a == "add" else {}

        def _room_remove_idx(self, room, idx):
            gone = room["queue"].pop(idx)
            if room["cur"] == gone["id"]:
                if room["queue"]:
                    room["cur"] = room["queue"][min(idx, len(room["queue"]) - 1)]["id"]
                    room["pos"], room["at"] = 0.0, ms()
                else:
                    room["cur"], room["pos"], room["at"], room["playing"], room["held"] = None, 0.0, ms(), False, False

        def _rooms_drop_lib(self, rel):
            """a library/uploaded song was deleted: take it out of every queue"""
            with self.rooms_lock:
                rooms = list(self.rooms.values())
            for room in rooms:
                with self.rooms_lock:
                    idxs = [i for i, it in enumerate(room["queue"]) if it.get("lib") == rel]
                    for i in reversed(idxs):
                        self._room_remove_idx(room, i)
                    if idxs:
                        room["v"] += 1
                if idxs:
                    self._room_broadcast_state(room, act="remove")

        def _room_advance(self, room, step, auto=False):
            q = room["queue"]
            if not q:
                return
            idx = next((i for i, it in enumerate(q) if it["id"] == room["cur"]), -1)
            if auto and room["repeat"] == "one":
                nxt = idx
            else:
                nxt = idx + step
                if nxt >= len(q):
                    if auto and room["repeat"] != "all":
                        room["playing"], room["pos"], room["at"] = False, 0.0, ms()
                        return
                    nxt = 0
                nxt = max(0, nxt)
            room["cur"], room["pos"], room["at"], room["playing"], room["held"] = q[nxt]["id"], 0.0, ms(), True, False

        def op_room_chat(self, conn, req):
            room = self._room_of(conn, req)
            text = clean(req.get("text"), 500)
            if not text:
                raise Fail("پیام خالیه")
            self._room_guard(conn.uid, "chat", 8, 10)
            m = {"event": "room_chat", "room": room["id"], "from": conn.uid,
                 "name": room["members"][conn.uid]["name"], "text": text, "t": ms()}
            room["chat"].append({k: m[k] for k in ("from", "name", "text", "t")})
            self._room_send(room, m)
            return {}

        def op_room_react(self, conn, req):
            room = self._room_of(conn, req)
            e = req.get("emoji")
            if e not in REACTS or not self._room_hits(("room", "react", conn.uid), 12, 5):
                return {}
            self._room_send(room, {"event": "room_react", "room": room["id"], "from": conn.uid, "emoji": e})
            return {}

        def op_room_audio(self, conn, req):
            """voice, fire-and-forget: {op:'room_audio', room, d:<b64>, sr, c:'opus'|'pcm'}. Needs the mic flag; frame-rate capped."""
            room = self._room_get(req.get("room"))
            if room is None or conn.uid not in room["members"] or not room["members"][conn.uid]["mic"]:
                return {}
            d = req.get("d")
            if not isinstance(d, str) or len(d) > 20000:
                return {}
            t = time.time()
            w, n = getattr(conn, "_ra", (t, 0))
            if t - w > 1.0:
                w, n = t, 0
            if n >= 90:                           # ~90 frames/s is far above any real speaker
                return {}
            conn._ra = (w, n + 1)
            now = time.time()
            with self.rooms_lock:
                sp = room.setdefault("speakers", {})
                for k in [k for k, v in sp.items() if now - v > 1.5]:
                    del sp[k]
                if conn.uid not in sp and len(sp) >= MAX_SPEAKERS:
                    return {}                     # too many people talking at once: the newest waits
                sp[conn.uid] = now
            try:
                sr = max(8000, min(int(req.get("sr") or 16000), 48000))
            except (TypeError, ValueError):
                sr = 16000
            codec = "opus" if req.get("c") == "opus" else "pcm"
            self._room_send(room, {"event": "room_audio", "room": room["id"], "from": conn.uid, "d": d, "sr": sr, "c": codec},
                            exclude_uid=conn.uid, relay=True)
            return {}

        def op_room_video(self, conn, req):
            """camera frame (small JPEG, base64), fire-and-forget. d == "" -> camera off. Needs the cam flag; ~10 fps max."""
            room = self._room_get(req.get("room"))
            if room is None or conn.uid not in room["members"] or not room["members"][conn.uid]["cam"]:
                return {}
            d = req.get("d")
            if not isinstance(d, str) or len(d) > 60000:
                return {}
            t = time.time()
            w, n = getattr(conn, "_rv", (t, 0))
            if t - w > 1.0:
                w, n = t, 0
            if n >= 12:
                return {}
            conn._rv = (w, n + 1)
            self._room_send(room, {"event": "room_video", "room": room["id"], "from": conn.uid, "d": d}, exclude_uid=conn.uid, video=True)
            return {}

        # ================================================================ room songs (server library + private uploads)
        def _roomaudio_allowed(self, uid, username, rel):
            """May this user stream this song? Library: everybody. Private upload: its owner, or anybody currently sitting
            in a room whose queue contains it."""
            of = common.song_owner_folder(rel)
            if of is None:
                return True
            if of == common.song_folder(username):
                return True
            with self.rooms_lock:
                return any(uid in r["members"] and any(it.get("lib") == rel for it in r["queue"]) for r in self.rooms.values())

        def sweep_song_uploads(self, now_ts=None):
            """delete private uploads older than SONG_TTL_HOURS, except songs that are in a live room's queue. Library never touched."""
            ttl = common.SONG_TTL_HOURS
            if ttl <= 0:
                return 0
            now_ts = now_ts or time.time()
            with self.rooms_lock:
                busy = {it.get("lib") for r in self.rooms.values() for it in r["queue"] if it.get("lib")}
            n = 0
            base = common.ROOMAUDIO_DIR / common.UPLOADS_FOLDER
            try:
                files = [p for p in base.rglob("*") if p.is_file()]
            except OSError:
                return 0
            for p in files:
                try:
                    rel = p.relative_to(common.ROOMAUDIO_DIR).as_posix()
                    if rel in busy or now_ts - p.stat().st_mtime < ttl * 3600:
                        continue
                    p.unlink()
                    n += 1
                except OSError:
                    pass
            return n

        def _housekeeping(self):
            """drop empty rate-limit buckets (memory), shrink the SQLite WAL"""
            t = time.time()
            for k in list(self.ip_hits.keys()):
                q = self.ip_hits.get(k)
                if q is not None and (not q or q[-1] < t - 7200):
                    self.ip_hits.pop(k, None)
            for d in (self.send_hits,):
                for k in list(d.keys()):
                    q = d.get(k)
                    if q is not None and (not q or q[-1] < t - 7200):
                        d.pop(k, None)
            db = common.connect_db(self.db_path)
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                db.close()

        def _song_sweeper(self):
            while True:
                time.sleep(600)
                try:
                    self.sweep_song_uploads()
                    self.sweep_deleted_files()
                    self._housekeeping()
                except Exception:
                    pass

        def op_roomaudio_list(self, conn, req):
            u = conn.db.execute("SELECT username FROM users WHERE id=?", (conn.uid,)).fetchone()
            n, size = common.user_song_usage(u["username"])
            return {"songs": common.list_roomaudio(u["username"]),
                    "usage": {"count": n, "bytes": size, "max_count": common.SONG_QUOTA_COUNT, "max_bytes": common.SONG_QUOTA_BYTES}}

        def op_roomaudio_delete(self, conn, req):
            rel = req.get("rel")
            u = conn.db.execute("SELECT username FROM users WHERE id=?", (conn.uid,)).fetchone()
            p = common.resolve_roomaudio(rel) if isinstance(rel, str) else None
            if p is None:
                raise Fail("آهنگ پیدا نشد")
            of = common.song_owner_folder(rel)
            is_admin = u["username"].lower() == admin_user
            if not (of == common.song_folder(u["username"]) or is_admin):
                raise Fail("فقط آهنگ‌های خودت رو می‌تونی پاک کنی")
            try:
                p.unlink()
            except OSError:
                raise Fail("پاک نشد")
            self._rooms_drop_lib(rel)
            return {}

    return RoomsMixin
