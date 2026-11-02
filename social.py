"""Privacy: last seen (everybody / contacts / nobody), read receipts on/off, block list.

Who is a "contact"?  Anybody you share a private chat with that has at least one message in it
(you wrote to him or he wrote to you).
"""
SEEN_MODES = ("all", "contacts", "nobody")


def make_social(Fail, common):
    now = common.now

    class SocialMixin:
        # ------------------------------------------------------------ contacts / blocks
        def _is_blocked(self, db, by, who):
            """True if `by` has blocked `who`."""
            return db.execute("SELECT 1 FROM blocks WHERE user_id=? AND blocked_id=?", (by, who)).fetchone() is not None

        def _is_contact(self, db, a, b):
            lo, hi = sorted((a, b))
            return db.execute("SELECT 1 FROM chats c WHERE c.pair_key=? AND c.kind='private' AND "
                              "EXISTS (SELECT 1 FROM messages WHERE chat_id=c.id AND kind!='system')",
                              (f"{lo}:{hi}",)).fetchone() is not None

        def _contacts_of(self, db, uid):
            return [r[0] for r in db.execute(
                "SELECT DISTINCT m2.user_id FROM chats c JOIN members m1 ON m1.chat_id=c.id "
                "JOIN members m2 ON m2.chat_id=c.id AND m2.user_id!=m1.user_id "
                "WHERE c.kind='private' AND m1.user_id=? AND "
                "EXISTS (SELECT 1 FROM messages WHERE chat_id=c.id AND kind!='system')", (uid,))]

        # ------------------------------------------------------------ last seen
        @staticmethod
        def _col(row, name, default):
            return row[name] if name in row.keys() and row[name] is not None else default

        def _can_see_seen(self, db, owner, viewer):
            """May `viewer` see whether `owner` is online / when he was last online?"""
            if viewer is None or owner["id"] == viewer:
                return True
            if self._is_blocked(db, owner["id"], viewer):
                return False
            mode = self._col(owner, "priv_seen", "all")
            if mode == "all":
                return True
            if mode == "contacts":
                return self._is_contact(db, owner["id"], viewer)
            return False

        def _user_for(self, db, row, viewer):
            """Public view of a user as `viewer` may see it (presence hidden when the owner chose so)."""
            d = self._user_public(row)
            if not self._can_see_seen(db, row, viewer):
                d["online"] = False
                d["last_seen"] = None
                d["seen_hidden"] = True
            return d

        def _me_view(self, row):
            return dict(self._user_public(row), created_at=row["created_at"],
                        priv_seen=self._col(row, "priv_seen", "all"),
                        read_receipts=bool(self._col(row, "read_receipts", 1)))

        def _broadcast_presence(self, db, uid, online):
            row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                return
            peers = [r[0] for r in db.execute(
                "SELECT DISTINCT m2.user_id FROM members m1 JOIN members m2 ON m1.chat_id=m2.chat_id "
                "WHERE m1.user_id=? AND m2.user_id!=?", (uid, uid))]
            for p in peers:
                if self._can_see_seen(db, row, p):
                    ev = {"event": "presence", "user_id": uid, "online": online, "last_seen": row["last_seen"]}
                else:
                    ev = {"event": "presence", "user_id": uid, "online": False, "last_seen": None, "seen_hidden": True}
                self.push([p], ev)

        def _broadcast_profile(self, db, uid):
            row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not row:
                return
            peers = {r[0] for r in db.execute(
                "SELECT DISTINCT m2.user_id FROM members m1 JOIN members m2 ON m1.chat_id=m2.chat_id "
                "WHERE m1.user_id=?", (uid,))} | {uid}
            for p in peers:
                self.push([p], {"event": "profile", "user": self._user_for(db, row, p)})

        # ------------------------------------------------------------ privacy ops
        def op_privacy_get(self, conn, req):
            row = conn.db.execute("SELECT * FROM users WHERE id=?", (conn.uid,)).fetchone()
            blocked = conn.db.execute(
                "SELECT u.* FROM blocks b JOIN users u ON u.id=b.blocked_id WHERE b.user_id=? ORDER BY b.created_at DESC",
                (conn.uid,)).fetchall()
            return {"priv_seen": self._col(row, "priv_seen", "all"), "read_receipts": bool(self._col(row, "read_receipts", 1)),
                    "blocked": [{"id": u["id"], "username": u["username"], "display_name": u["display_name"],
                                 "avatar_at": u["avatar_at"] if u["avatar"] else 0} for u in blocked]}

        def op_privacy_set(self, conn, req):
            sets, vals = [], []
            if "priv_seen" in req:
                mode = str(req.get("priv_seen"))
                if mode not in SEEN_MODES:
                    raise Fail("مقدار نامعتبر")
                sets.append("priv_seen=?")
                vals.append(mode)
            if "read_receipts" in req:
                sets.append("read_receipts=?")
                vals.append(1 if req.get("read_receipts") else 0)
            if not sets:
                raise Fail("چیزی برای تغییر نیست")
            with conn.db:
                conn.db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*vals, conn.uid))
            self._broadcast_presence(conn.db, conn.uid, self.is_online(conn.uid))
            if "read_receipts" in req:                    # everybody's ticks in our chats change
                for (cid,) in conn.db.execute("SELECT chat_id FROM members WHERE user_id=?", (conn.uid,)).fetchall():
                    self._push_chat_update(conn.db, cid)
            return self.op_privacy_get(conn, req)

        def _target_user(self, db, req, me):
            try:
                tid = int(req.get("user_id"))
            except (TypeError, ValueError):
                raise Fail("کاربر نامعتبر")
            row = db.execute("SELECT * FROM users WHERE id=?", (tid,)).fetchone()
            if not row or tid == me:
                raise Fail("کاربر پیدا نشد")
            return row

        def op_block(self, conn, req):
            t = self._target_user(conn.db, req, conn.uid)
            with conn.db:
                conn.db.execute("INSERT OR IGNORE INTO blocks(user_id, blocked_id, created_at) VALUES (?,?,?)",
                                (conn.uid, t["id"], now()))
            self._broadcast_presence(conn.db, conn.uid, self.is_online(conn.uid))
            return self.op_privacy_get(conn, req)

        def op_unblock(self, conn, req):
            t = self._target_user(conn.db, req, conn.uid)
            with conn.db:
                conn.db.execute("DELETE FROM blocks WHERE user_id=? AND blocked_id=?", (conn.uid, t["id"]))
            self._broadcast_presence(conn.db, conn.uid, self.is_online(conn.uid))
            return self.op_privacy_get(conn, req)

        def _check_not_blocked_private(self, db, chat_id, uid):
            """A blocked user cannot write to the person who blocked him (generic error: no hint)."""
            c = db.execute("SELECT kind FROM chats WHERE id=?", (chat_id,)).fetchone()
            if not c or c["kind"] != "private":
                return
            other = db.execute("SELECT user_id FROM members WHERE chat_id=? AND user_id!=?", (chat_id, uid)).fetchone()
            if other and self._is_blocked(db, other["user_id"], uid):
                raise Fail("پیام ارسال نشد")
            if other and self._is_blocked(db, uid, other["user_id"]):
                raise Fail("این کاربر رو بلاک کردی؛ اول از بلاک درش بیار")

    return SocialMixin
