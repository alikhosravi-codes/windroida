"""Windroida server: threaded TCP server, SQLite storage, push events.

Run:  python server.py [--host 0.0.0.0] [--port 8765] [--http]
"""
import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import shutil
import threading
import time
import uuid
from pathlib import Path
from collections import defaultdict, deque

import common
from rooms import make_rooms
from social import make_social
from common import (DATA_DIR, MAX_FILE, MAX_TEXT, REACTIONS, SECURITY_QUESTIONS, SESSION_TTL, USERNAME_RE,
                    avatar_folder_for_chat, avatar_folder_for_user, check_password_rules, connect_db, encode_frame,
                    hash_secret, media_info, avatar_pics, valid_pic_name, retire_avatar, migrate_legacy_uploads, normalize_answer, now, recv_frame,
                    remove_upload, resolve_upload, store_avatar, thumb_rel, upload_relpath, verify_secret)

RESERVED = {"admin", "administrator", "root", "system", "support", "windroida", "telegram", "null"}
MAX_FAILS = 5
LOCK_SECONDS = 300
CALL_RING_SECONDS = 45
CALL_GRACE_SECONDS = int(os.environ.get("WINDROIDA_CALL_GRACE", "60"))          # a dropped call participant has this long to come back before the call ends
ADMIN_USER = os.environ.get("WINDROIDA_ADMIN", "mehrans25").strip().lower()   # only this user posts in the channel
CHANNEL_TITLE = "اطلاعیه‌ها"
MEDIA_KEY_TTL = 24 * 3600
DEFAULT_ICE = [{"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.l.google.com:19302",
                         "stun:stun1.l.google.com:19302", "stun:global.stun.twilio.com:3478"]}]
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Fail(Exception):
    """Expected, user-facing error."""


def clean_line(text, limit, field="مقدار"):
    text = CTRL_RE.sub("", str(text or "")).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        raise Fail(f"{field} خیلی طولانیه (حداکثر {limit} کاراکتر)")
    return text


_LOG_LOCK = threading.Lock()


def log(*a):
    line = time.strftime("%H:%M:%S") + " " + " ".join(str(x) for x in a)
    print(line, flush=True)
    try:                                   # keep a rotating log file (data/windroida.log, 3 x ~1 MB) so a crash can be diagnosed later
        p = common.DATA_DIR / "windroida.log"
        with _LOG_LOCK:
            if p.exists() and p.stat().st_size > 1_000_000:
                for i in (2, 1):
                    o = p.with_name(f"windroida.log.{i}")
                    if o.exists():
                        o.replace(p.with_name(f"windroida.log.{i + 1}"))
                p.replace(p.with_name("windroida.log.1"))
            with open(p, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d ") + line + "\n")
    except Exception:
        pass


RoomsMixin = make_rooms(Fail, common, ADMIN_USER)
SocialMixin = make_social(Fail, common)


class Conn:
    def __init__(self, server, sock, addr):
        self.server, self.sock, self.addr = server, sock, addr
        self.ip = addr[0]
        self.uid = None
        self.db = None
        self.caps = set()           # what this client can do (the web UI announces {"call"})
        self.session_id = None      # row in `sessions` when logged in with a persistent session
        self.send_lock = threading.Lock()
        self.alive = True
        self.vis_at = time.time()
        self.vis = True             # is the app visible on screen? (web UI reports it; hidden -> a push wakes the phone)

    def send(self, obj):
        if not self.alive:
            return
        try:
            data = encode_frame(obj)
            with self.send_lock:
                self.sock.sendall(data)
        except Exception:
            self.alive = False

    def relay(self, obj, video=False):
        """Queue a call frame (audio/video). A slow receiver never blocks the sender: old frames are dropped,
        audio always goes out before video."""
        if getattr(self, "_rq", None) is None:
            self._rq, self._rvq, self._rev = deque(maxlen=60), deque(maxlen=2), threading.Event()
            threading.Thread(target=self._relay_loop, daemon=True).start()
        (self._rvq if video else self._rq).append(obj)
        self._rev.set()

    def _relay_loop(self):
        while self.alive:
            self._rev.wait(5)
            self._rev.clear()
            while self.alive and (self._rq or self._rvq):
                try:
                    obj = self._rq.popleft() if self._rq else self._rvq.popleft()
                except IndexError:
                    break
                self.send(obj)

    def close(self):
        self.alive = False
        try:
            if getattr(self, "_rev", None):
                self._rev.set()
        except Exception:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


class WindroidaServer(RoomsMixin, SocialMixin):
    def __init__(self, host="127.0.0.1", port=common.DEFAULT_PORT, tls=False, db_path=None, web_port=None):
        self.host, self.port, self.tls = host, port, tls
        self.web_port, self.web = web_port, None
        self.db_path = db_path or common.DB_PATH
        self.sessions = defaultdict(list)       # uid -> [Conn]
        self.sessions_lock = threading.Lock()
        self.ip_hits = defaultdict(deque)       # ip -> timestamps of auth attempts
        self.send_hits = defaultdict(deque)     # uid -> timestamps of sent messages
        self.conns_lock = threading.Lock()
        self.conns_total = 0
        self.conns_by_ip = defaultdict(int)     # raw TCP connections currently open, before any login (flood guard)
        self.notify_q = defaultdict(lambda: deque(maxlen=50))   # uid -> recent {seq, chat_id, title, text} for the Android app's long-poll
        self.notify_seq = defaultdict(int)
        self.notify_cv = threading.Condition()
        self.listener = None
        self.ssl_ctx = None
        self._stop = threading.Event()
        self._rooms_init()
        self.calls = {}                         # call_id -> call dict (voice calls)
        self.user_call = {}                     # uid -> call_id
        self.calls_lock = threading.Lock()
        self._learn_lock = threading.Lock()
        try:
            self.ice_servers = json.loads(os.environ.get("WINDROIDA_ICE", "")) or DEFAULT_ICE
        except ValueError:
            self.ice_servers = DEFAULT_ICE
        self._secret = self._load_secret()
        self.drive_port = int(os.environ.get("WINDROIDA_DRIVE_PORT") or 8082)
        self.ghab_port = int(os.environ.get("WINDROIDA_GHAB_PORT") or 8084)
        self._ghab_secret = self._load_drive_secret("ghab_sso.key")   # its own key: a Cloud Drive link can never open Ghab
        self._drive_secret = self._load_drive_secret()  # written to data/drive_sso.key right away, so start.py
                                                          # can hand it to Cloud Drive on this same first launch
        boot = connect_db(self.db_path)         # make sure schema exists
        try:
            qc = boot.execute("PRAGMA quick_check").fetchone()[0]
            if qc != "ok":
                log("WARNING: database integrity check failed:", qc, "- stop the server and restore data/windroida.db from a copy")
        except sqlite3.Error as e:
            log("WARNING: database check error:", repr(e))
        self._ensure_channel(boot)
        n = migrate_legacy_uploads(boot)
        if n:
            log(f"moved {n} old upload(s) into data/uploads/")
        with boot:                              # rooms live in RAM only: whatever was open before a restart is over
            boot.execute("UPDATE rooms_log SET closed_at=? WHERE closed_at IS NULL", (now(),))
        boot.close()

    # ------------------------------------------------------------------ run
    def start(self):
        if self.tls:
            self.ssl_ctx = self._make_ssl_context()
            self.tls = self.ssl_ctx is not None
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.host, self.port))
        self.listener.listen(64)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._song_sweeper, daemon=True).start()
        log(f"server listening on {self.host}:{self.port} (tls={'on' if self.tls else 'off'})")
        if self.web_port is not None:
            from webgw import WebGateway
            self.web = WebGateway(self, self.host, self.web_port)
            self.web_port = self.web.start()
        return self.port

    def serve_forever(self):
        if self.listener is None:
            self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        self.stop()

    def stop(self):
        self._stop.set()
        if self.web:
            self.web.stop()
        try:
            self.listener.close()
        except Exception:
            pass
        with self.sessions_lock:
            conns = [c for lst in self.sessions.values() for c in lst]
        for c in conns:
            c.close()

    def _cert_names(self):
        names = {"localhost", "127.0.0.1", lan_ip(), socket.gethostname()}
        names |= {n.strip() for n in os.environ.get("WINDROIDA_CERT_NAMES", "").split(",") if n.strip()}
        f = DATA_DIR / "cert_names.txt"            # public IP / domain names (also filled in automatically, see learn_host)
        if f.exists():
            names |= {n.strip() for n in f.read_text(encoding="utf-8").split() if n.strip() and not n.startswith("#")}
        return sorted(names)

    def _make_ssl_context(self):
        """Self-signed certificate valid for localhost, this PC's name, its LAN IP and the public IP people use.
        Made with the `cryptography` package (installed by start.bat); openssl is the fallback.
        The certificate is also a 'CA' so it can be installed on a phone (Android: Install a certificate -> CA)
        and then Chrome trusts it: no red warning, the app can be installed. Returns None (-> http) on failure."""
        crt, key, meta = DATA_DIR / "server.crt", DATA_DIR / "server.key", DATA_DIR / "server.names"
        self._crt_path, self._key_path, self._meta_path = crt, key, meta
        names = self._cert_names()
        text = meta.read_text(encoding="utf-8") if meta.exists() else ""
        have = [x for x in text.split() if not x.startswith("#")]
        # an existing certificate is KEPT even when this PC gets a new address: a new certificate means a new fingerprint and the phone
        # app (which trusts one fingerprint) would refuse to connect. Delete data/server.crt by hand to make a fresh one.
        if not (crt.exists() and key.exists()) or "#v2" not in text:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # keep the names of earlier certificates too (a changing LAN/VPN address must not make a NEW certificate on every start:
            # that breaks the phone app's pinned fingerprint). The list only grows, up to 12 names.
            merged = sorted(set(names) | set(have))
            if len(merged) <= 12:
                names = merged
            if not (self._cert_cryptography(crt, key, names) or self._cert_openssl(crt, key, names)):
                log("could not create a certificate (pip install cryptography) -> falling back to plain http")
                return None
            meta.write_text("#v2\n" + "\n".join(names), encoding="utf-8")
            try:
                os.chmod(key, 0o600)
            except OSError:
                pass
            log("generated self-signed certificate in data/ for:", ", ".join(names))
        self.cert_names = names
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            ctx.num_tickets = 0        # no TLS 1.3 session tickets: they are written right after the handshake, racing with the
        except (AttributeError, ValueError):   # first request on the same socket (rare stalls seen in tests)
            pass
        ctx.load_cert_chain(str(crt), str(key))
        return ctx

    def learn_host(self, host, client_ip=None):
        """Someone opened the site through an IPv4 address the certificate does not cover (e.g. the public/static IP):
        add it, make a new certificate and use it for new connections. (Phones must install the new one once.)"""
        if not self.ssl_ctx or host in getattr(self, "cert_names", ()) or len(getattr(self, "cert_names", ())) > 12:
            return
        if getattr(self, "keep_cert", True):
            return                                  # never replace the certificate behind the phones' backs
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
            src = ipaddress.ip_address(client_ip or "")
        except ValueError:
            return
        if not (src.is_private or src.is_loopback):
            return          # SECURITY: a stranger on the internet must never be able to make the server replace its certificate
        if ip.version != 4 or ip.is_unspecified or ip.is_multicast:
            return
        with self._learn_lock:
            if host in self.cert_names:
                return
            f = DATA_DIR / "cert_names.txt"
            old = f.read_text(encoding="utf-8") if f.exists() else ""
            f.write_text(old.rstrip() + "\n" + host + "\n", encoding="utf-8")
            names = self._cert_names()
            if self._cert_cryptography(self._crt_path, self._key_path, names) or \
                    self._cert_openssl(self._crt_path, self._key_path, names):
                self._meta_path.write_text("#v2\n" + "\n".join(names), encoding="utf-8")
                self.cert_names = names
                try:
                    self.ssl_ctx.load_cert_chain(str(self._crt_path), str(self._key_path))
                except (ssl.SSLError, OSError) as e:
                    log("reloading certificate failed:", repr(e))
                log("new certificate now also covers", host)

    @staticmethod
    def _cert_cryptography(crt, key, names):
        try:
            import datetime
            import ipaddress
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError:
            return False
        try:
            k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            san = []
            for n in names:
                try:
                    san.append(x509.IPAddress(ipaddress.ip_address(n)))
                except ValueError:
                    san.append(x509.DNSName(n))
            nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Windroida")])
            now_ = datetime.datetime.now(datetime.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm).public_key(k.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now_ - datetime.timedelta(days=1))
                    .not_valid_after(now_ + datetime.timedelta(days=3650))
                    .add_extension(x509.SubjectAlternativeName(san), critical=False)
                    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                    .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=True, key_cert_sign=True,
                                                 content_commitment=False, data_encipherment=False, key_agreement=False,
                                                 crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                    .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                    .sign(k, hashes.SHA256()))
            key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                            serialization.NoEncryption()))
            crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            return True
        except Exception as e:
            log("cryptography cert failed:", repr(e))
            return False

    @staticmethod
    def _cert_openssl(crt, key, names):
        import ipaddress
        san = []
        for n in names:
            try:
                ipaddress.ip_address(n)
                san.append(f"IP:{n}")
            except ValueError:
                san.append(f"DNS:{n}")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(crt), "-days", "3650", "-subj", "/CN=Windroida",
                            "-addext", "subjectAltName=" + ",".join(san),
                            "-addext", "basicConstraints=critical,CA:TRUE",
                            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign"], check=True, capture_output=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            return False

    def _ensure_channel(self, db):
        """The announcement channel: every user is a member, only ADMIN_USER can post."""
        with db:
            row = db.execute("SELECT id FROM chats WHERE channel=1 LIMIT 1").fetchone()
            if row is None:
                cur = db.execute("INSERT INTO chats(kind, title, owner_id, created_at, channel, description) "
                                 "VALUES ('group',?,NULL,?,1,?)",
                                 (CHANNEL_TITLE, now(), "اطلاعیه‌های رسمی؛ فقط مدیر می‌تونه پیام بذاره"))
                cid = cur.lastrowid
            else:
                cid = row["id"]
            db.execute("INSERT OR IGNORE INTO members(chat_id, user_id, joined_at) "
                       "SELECT ?, id, ? FROM users WHERE banned=0", (cid, now()))
        return cid

    def _can_post(self, db, chat_id, uid):
        c = db.execute("SELECT channel FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not c or not c["channel"]:
            return True
        u = db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        return bool(u and u["username"].lower() == ADMIN_USER)

    MAX_CONNS_TOTAL = 2000      # raw TCP connections (open sockets that never log in cost a thread each -> flood guard)
    MAX_CONNS_PER_IP = 40

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                sock, addr = self.listener.accept()
            except OSError:
                break
            ip = addr[0] if addr else ""
            with self.conns_lock:
                over = self.conns_total >= self.MAX_CONNS_TOTAL or self.conns_by_ip[ip] >= self.MAX_CONNS_PER_IP
                if not over:
                    self.conns_total += 1
                    self.conns_by_ip[ip] += 1
            if over:         # too many open connections already (this IP, or overall): refuse before spending a thread/DB handle
                try:
                    sock.close()
                except Exception:
                    pass
                continue
            threading.Thread(target=self._client_thread, args=(sock, addr), daemon=True).start()

    def _conn_done(self, ip):
        with self.conns_lock:
            self.conns_total = max(0, self.conns_total - 1)
            self.conns_by_ip[ip] = max(0, self.conns_by_ip[ip] - 1)
            if self.conns_by_ip[ip] == 0:
                del self.conns_by_ip[ip]

    # ------------------------------------------------------ per-connection
    def _client_thread(self, sock, addr):
        ip = addr[0] if addr else ""
        try:
            sock.settimeout(120)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.ssl_ctx:
                sock = self.ssl_ctx.wrap_socket(sock, server_side=True)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            self._conn_done(ip)
            return
        conn = Conn(self, sock, addr)
        conn.db = connect_db(self.db_path)
        try:
            while conn.alive:
                req = recv_frame(sock)
                self._dispatch(conn, req)
        except (ConnectionError, OSError, ValueError, ssl.SSLError):
            pass
        except Exception as e:  # never let one client kill the server
            log("client error:", repr(e))
        finally:
            self._drop(conn)
            self._conn_done(ip)

    def _drop(self, conn):
        conn.alive = False
        uid = conn.uid
        try:
            self._room_drop(conn)
        except Exception:
            pass
        if uid is not None:
            with self.calls_lock:
                cid = self.user_call.get(uid)
                call = self.calls.get(cid) if cid else None
            if call and (call["caller_conn"] is conn or call["callee_conn"] is conn):
                try:
                    if call["state"] == "active":
                        self._call_grace(call, uid)          # mobile networks blink: wait for the user to come back
                    else:
                        self._finish_call(cid, "disconnected", conn.db)
                except Exception:
                    pass
        if uid is not None:
            with self.sessions_lock:
                lst = self.sessions.get(uid, [])
                if conn in lst:
                    lst.remove(conn)
                offline = not lst
                if offline:
                    self.sessions.pop(uid, None)
            if offline:
                try:
                    with conn.db:
                        conn.db.execute("UPDATE users SET last_seen=? WHERE id=?", (now(), uid))
                    self._broadcast_presence(conn.db, uid, False)
                except Exception:
                    pass
        try:
            conn.db.close()
        except Exception:
            pass
        conn.close()

    def _dispatch(self, conn, req):
        if not isinstance(req, dict):
            return
        rid, op = req.get("id"), str(req.get("op", ""))
        handler = getattr(self, "op_" + op, None)
        if handler is None or op.startswith("_"):
            return conn.send({"id": rid, "ok": False, "error": "درخواست نامعتبر"})
        public = {"register", "login", "resume", "reset_question", "reset_password", "ping"}
        try:
            if op in ("call_audio", "call_video", "room_audio", "room_video"):   # hot path: many small frames/second, skip the per-request DB check
                if conn.uid is None:
                    return
                try:
                    getattr(self, "op_" + op)(conn, req)
                except Fail:
                    pass
                return
            if op not in public:
                if conn.uid is None:
                    raise Fail("اول وارد شو")
                if not self._room_hits(("op", conn.uid), 200, 10):                      # a client gone wild must not hog the server
                    raise Fail("درخواست‌های خیلی زیاد؛ چند ثانیه صبر کن")
                if op in ("search_all", "search_messages", "search_users", "invite_info", "join", "shared") \
                        and not self._room_hits(("heavy", conn.uid), 40, 60):
                    raise Fail("خیلی سریع جستجو می‌کنی، یه لحظه صبر کن")
                row = conn.db.execute("SELECT banned FROM users WHERE id=?", (conn.uid,)).fetchone()
                if row is None or row["banned"]:
                    conn.send({"event": "banned"})
                    raise Fail("این حساب مسدود شده")
            result = handler(conn, req) or {}
            if rid is not None:                      # fire-and-forget ops (call audio) send no id -> no reply
                conn.send({"id": rid, "ok": True, **result})
        except Fail as e:
            if rid is not None:
                conn.send({"id": rid, "ok": False, "error": str(e)})
        except sqlite3.Error as e:
            log("db error:", repr(e))
            conn.send({"id": rid, "ok": False, "error": "خطای دیتابیس"})
        except Exception as e:
            log("handler error:", op, repr(e))
            conn.send({"id": rid, "ok": False, "error": "خطای داخلی سرور"})
        if op == "logout":
            conn.close()

    # ------------------------------------------------------------- helpers
    def is_online(self, uid):
        with self.sessions_lock:
            return bool(self.sessions.get(uid))

    def push(self, uids, event, exclude=None):
        with self.sessions_lock:
            targets = [c for u in set(uids) for c in self.sessions.get(u, []) if c is not exclude]
        for c in targets:
            c.send(event)

    def _throttle_ip(self, ip, limit=30, window=60, bucket="auth"):
        q = self.ip_hits[(ip, bucket)]
        t = time.time()
        while q and q[0] < t - window:
            q.popleft()
        if len(q) >= limit:
            raise Fail("تلاش‌های زیاد؛ یه دقیقه صبر کن")
        q.append(t)

    def _user_public(self, row):
        keys = row.keys()
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"],
                "bio": row["bio"], "online": self.is_online(row["id"]), "last_seen": row["last_seen"],
                "avatar_at": (row["avatar_at"] if "avatar_at" in keys and row["avatar"] else 0)}

    def _msg(self, db, r):
        deleted = bool(r["deleted"])
        d = {"id": r["id"], "chat_id": r["chat_id"], "sender_id": r["sender_id"],
             "sender_name": r["sender_name"] if "sender_name" in r.keys() else None,
             "kind": r["kind"], "body": "" if deleted else r["body"], "reply_to": r["reply_to"],
             "created_at": r["created_at"], "edited_at": r["edited_at"], "deleted": deleted,
             "file": None, "reply": None, "fwd": None, "reactions": []}
        meta = {}
        if "meta" in r.keys() and r["meta"]:
            try:
                meta = json.loads(r["meta"])
            except ValueError:
                meta = {}
        if not deleted:
            d["fwd"] = meta.get("fwd")
            d["reactions"] = self._reactions(db, r["id"])
        if r["kind"] == "file" and not deleted:
            d["file"] = {"name": r["file_name"], "size": r["file_size"], "kind": media_info(r["file_name"])[1]}
            if meta.get("voice"):
                d["file"].update(voice=True, dur=meta.get("dur"), wave=meta.get("wave"),
                                 played=[x[0] for x in db.execute(
                                     "SELECT user_id FROM voice_plays WHERE message_id=?", (r["id"],))])
            elif d["file"]["kind"] == "image" and not str(r["file_name"]).lower().endswith(".gif"):
                d["file"].update(thumb=True, tw=meta.get("tw"), th=meta.get("th"))     # made on demand by the server
            elif d["file"]["kind"] == "video" and resolve_upload(thumb_rel(r["file_path"])):
                d["file"].update(thumb=True, tw=meta.get("tw"), th=meta.get("th"))
        if r["reply_to"]:
            q = db.execute("SELECT m.id, m.body, m.kind, m.file_name, m.deleted, u.display_name AS sender_name "
                           "FROM messages m LEFT JOIN users u ON u.id=m.sender_id WHERE m.id=?",
                           (r["reply_to"],)).fetchone()
            if q:
                text = "پیام حذف شده" if q["deleted"] else (q["body"] or ("📎 " + (q["file_name"] or "فایل")))
                d["reply"] = {"id": q["id"], "sender_name": q["sender_name"], "text": text[:120]}
        return d

    def _reactions(self, db, mid):
        out = {}
        for r in db.execute("SELECT emoji, user_id FROM reactions WHERE message_id=? ORDER BY created_at", (mid,)):
            out.setdefault(r["emoji"], []).append(r["user_id"])
        return [{"e": e, "users": u} for e, u in out.items()]

    def _msg_by_id(self, db, mid):
        r = db.execute("SELECT m.*, u.display_name AS sender_name FROM messages m "
                       "LEFT JOIN users u ON u.id=m.sender_id WHERE m.id=?", (mid,)).fetchone()
        return self._msg(db, r) if r else None

    def _member_ids(self, db, chat_id):
        return [r["user_id"] for r in db.execute("SELECT user_id FROM members WHERE chat_id=?", (chat_id,))]

    def _require_member(self, db, chat_id, uid):
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            raise Fail("چت نامعتبر")
        row = db.execute("SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
        if not row:
            raise Fail("به این چت دسترسی نداری")
        return chat_id, row

    def _chat_view(self, db, chat_id, uid):
        c = db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        me = db.execute("SELECT * FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
        if not c or not me:
            return None
        last = db.execute("SELECT m.*, u.display_name AS sender_name FROM messages m LEFT JOIN users u "
                          "ON u.id=m.sender_id WHERE m.chat_id=? AND m.deleted=0 ORDER BY m.id DESC LIMIT 1", (chat_id,)).fetchone()
        unread = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id=? AND id>? AND deleted=0 "
                            "AND kind!='system' AND IFNULL(sender_id,0)!=?",
                            (chat_id, me["last_read_id"], uid)).fetchone()[0]
        my_rr = db.execute("SELECT read_receipts FROM users WHERE id=?", (uid,)).fetchone()
        peer_read = 0
        if my_rr is None or my_rr[0]:          # both sides must have read receipts on (like Telegram)
            peer_read = db.execute("SELECT IFNULL(MAX(m.last_read_id),0) FROM members m JOIN users u ON u.id=m.user_id "
                                   "WHERE m.chat_id=? AND m.user_id!=? AND u.read_receipts=1", (chat_id, uid)).fetchone()[0]
        count = db.execute("SELECT COUNT(*) FROM members WHERE chat_id=?", (chat_id,)).fetchone()[0]
        view = {"id": c["id"], "kind": c["kind"], "title": c["title"], "owner_id": c["owner_id"],
                "unread": unread, "last": self._msg(db, last) if last else None, "peer_read": peer_read,
                "member_count": count, "created_at": c["created_at"], "peer": None, "saved": False,
                "avatar_at": c["avatar_at"] if c["avatar"] else 0, "description": c["description"] or "",
                "channel": bool(c["channel"]), "username": c["username"],
                "invite": c["invite"] if me["role"] == "owner" else None, "can_post": self._can_post(db, chat_id, uid),
                "my_read": me["last_read_id"], "my_role": me["role"], "muted": bool(me["muted"]), "fav": bool(me["fav"]), "pin_msg": None}
        if c["pinned_id"]:
            pm = db.execute("SELECT m.*, u.display_name AS sender_name FROM messages m LEFT JOIN users u "
                            "ON u.id=m.sender_id WHERE m.id=? AND m.deleted=0", (c["pinned_id"],)).fetchone()
            if pm:
                view["pin_msg"] = {"id": pm["id"], "sender_name": pm["sender_name"],
                                   "text": (pm["body"] or ("📎 " + (pm["file_name"] or "فایل")))[:120]}
        if c["kind"] == "private":
            other = db.execute("SELECT u.* FROM members m JOIN users u ON u.id=m.user_id "
                               "WHERE m.chat_id=? AND m.user_id!=?", (chat_id, uid)).fetchone()
            if other:
                view["title"] = other["display_name"]
                view["peer"] = self._user_for(db, other, uid)
            elif count == 1:                                   # chat with yourself = "Saved messages"
                view["saved"] = True
                view["title"] = "پیام‌های ذخیره‌شده"
                view["peer_read"] = db.execute("SELECT IFNULL(MAX(id),0) FROM messages WHERE chat_id=?",
                                               (chat_id,)).fetchone()[0]
        return view

    def _push_chat_update(self, db, chat_id):
        """Every member gets his own fresh view of the chat (title, photo, pinned message, ...)."""
        for u in self._member_ids(db, chat_id):
            view = self._chat_view(db, chat_id, u)
            if view:
                self.push([u], {"event": "chat_updated", "chat": view})

    def _system_message(self, db, chat_id, text):
        with db:
            cur = db.execute("INSERT INTO messages(chat_id, sender_id, kind, body, created_at) "
                             "VALUES (?,NULL,'system',?,?)", (chat_id, text, now()))
        msg = self._msg_by_id(db, cur.lastrowid)
        self.push(self._member_ids(db, chat_id), {"event": "message", "message": msg})
        return msg

    # -------------------------------------------------------- auth: public
    def op_ping(self, conn, req):
        return {"pong": now()}

    def _new_session(self, db, uid, device=""):
        token = secrets.token_urlsafe(32)
        device = CTRL_RE.sub("", str(device or ""))[:80]
        with db:
            db.execute("DELETE FROM sessions WHERE last_used<?", (now() - SESSION_TTL,))
            cur = db.execute("INSERT INTO sessions(user_id, token_hash, device, created_at, last_used) "
                             "VALUES (?,?,?,?,?)", (uid, hashlib.sha256(token.encode()).digest(), device, now(), now()))
        return token, cur.lastrowid

    def _login_ok(self, conn, row, device=None):
        conn.uid = row["id"]
        with self.sessions_lock:
            first = not self.sessions[row["id"]]
            self.sessions[row["id"]].append(conn)
        with conn.db:
            conn.db.execute("UPDATE users SET last_seen=? WHERE id=?", (now(), row["id"]))
        if first:
            self._broadcast_presence(conn.db, row["id"], True)
        fresh = conn.db.execute("SELECT * FROM users WHERE id=?", (row["id"],)).fetchone()
        out = {"me": self._me_view(fresh)}
        if device is not None:                      # fresh login/register -> remember this device
            token, conn.session_id = self._new_session(conn.db, row["id"], device)
            out["session"] = token
        return out

    def op_register(self, conn, req):
        self._throttle_ip(conn.ip, limit=10, window=600, bucket="register")
        username = str(req.get("username", "")).strip()
        if not USERNAME_RE.match(username):
            raise Fail("نام کاربری باید ۳ تا ۳۲ کاراکتر و فقط شامل حرف انگلیسی، عدد یا _ باشه")
        if username.lower() in RESERVED:
            raise Fail("این نام کاربری رزرو شده")
        password = req.get("password", "")
        err = check_password_rules(password, username)
        if err:
            raise Fail(err)
        display = clean_line(req.get("display_name") or username, 40, "نام نمایشی") or username
        self._check_display_name(display, username)
        question = clean_line(req.get("sec_question"), 100, "سوال امنیتی")
        answer = normalize_answer(req.get("sec_answer", ""))
        if len(question) < 5:
            raise Fail("یه سوال امنیتی انتخاب کن")
        if len(answer) < 2:
            raise Fail("جواب سوال امنیتی خیلی کوتاهه")
        if len(answer) > 100:
            raise Fail("جواب سوال امنیتی خیلی طولانیه")
        if conn.db.execute("SELECT 1 FROM chats WHERE username=? COLLATE NOCASE", (username,)).fetchone():
            raise Fail("این نام کاربری قبلاً گرفته شده")
        ps, ph = hash_secret(password)
        ss, sh = hash_secret(answer)
        try:
            with conn.db:
                cur = conn.db.execute(
                    "INSERT INTO users(username, display_name, pw_salt, pw_hash, sec_question, sec_salt, sec_hash,"
                    " created_at) VALUES (?,?,?,?,?,?,?,?)", (username, display, ps, ph, question, ss, sh, now()))
        except sqlite3.IntegrityError:
            raise Fail("این نام کاربری قبلاً گرفته شده")
        row = conn.db.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        try:
            self._ensure_channel(conn.db)
        except sqlite3.Error:
            pass
        log("registered", username)
        return self._login_ok(conn, row, device=req.get("device", ""))

    def _check_lock(self, row):
        if row["locked_until"] > now():
            raise Fail(f"حساب موقتاً قفل شده؛ {row['locked_until'] - now()} ثانیه دیگه تلاش کن")

    def _register_failure(self, db, row):
        fails = row["failed_logins"] + 1
        if fails >= MAX_FAILS:
            with db:
                db.execute("UPDATE users SET failed_logins=0, locked_until=? WHERE id=?",
                           (now() + LOCK_SECONDS, row["id"]))
            raise Fail("چند بار اشتباه زدی؛ حساب ۵ دقیقه قفل شد")
        with db:
            db.execute("UPDATE users SET failed_logins=? WHERE id=?", (fails, row["id"]))

    def op_login(self, conn, req):
        self._throttle_ip(conn.ip)
        username = str(req.get("username", "")).strip()
        password = str(req.get("password", ""))
        row = conn.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone() \
            if USERNAME_RE.match(username) else None
        generic = Fail("نام کاربری یا رمز اشتباهه")
        if row is None:
            hash_secret(password[:128])  # equalise timing, no user enumeration
            raise generic
        self._check_lock(row)
        if not verify_secret(password[:128], row["pw_salt"], row["pw_hash"]):
            self._register_failure(conn.db, row)
            raise generic
        if row["banned"]:
            raise Fail("این حساب مسدود شده")
        with conn.db:
            conn.db.execute("UPDATE users SET failed_logins=0 WHERE id=?", (row["id"],))
        return self._login_ok(conn, row, device=req.get("device", ""))

    def op_resume(self, conn, req):
        """Log back in with the saved session token (this is what keeps you signed in, like Telegram)."""
        self._throttle_ip(conn.ip, limit=60, window=60, bucket="resume")
        token = str(req.get("token") or "")
        if conn.uid is not None:
            return self.op_me(conn, req)
        expired = Fail("نشست تموم شده؛ دوباره وارد شو")
        if not 20 <= len(token) <= 100:
            raise expired
        row = conn.db.execute("SELECT s.id AS sid, s.last_used, u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                              "WHERE s.token_hash=?", (hashlib.sha256(token.encode()).digest(),)).fetchone()
        if row is None:
            raise expired
        if row["last_used"] < now() - SESSION_TTL:
            with conn.db:
                conn.db.execute("DELETE FROM sessions WHERE id=?", (row["sid"],))
            raise expired
        if row["banned"]:
            raise Fail("این حساب مسدود شده")
        if now() - row["last_used"] > 3600:
            with conn.db:
                conn.db.execute("UPDATE sessions SET last_used=? WHERE id=?", (now(), row["sid"]))
        conn.session_id = row["sid"]
        return self._login_ok(conn, row)

    def op_reset_question(self, conn, req):
        self._throttle_ip(conn.ip)
        username = str(req.get("username", "")).strip()
        row = conn.db.execute("SELECT sec_question FROM users WHERE username=?", (username,)).fetchone() \
            if USERNAME_RE.match(username) else None
        if row:
            return {"question": row["sec_question"]}
        idx = int(hashlib.sha256(username.lower().encode()).hexdigest(), 16) % len(SECURITY_QUESTIONS)
        return {"question": SECURITY_QUESTIONS[idx]}   # same shape for unknown users

    def op_reset_password(self, conn, req):
        self._throttle_ip(conn.ip, limit=10, bucket="reset")
        username = str(req.get("username", "")).strip()
        answer = normalize_answer(req.get("answer", ""))
        new_pw = req.get("new_password", "")
        row = conn.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone() \
            if USERNAME_RE.match(username) else None
        generic = Fail("جواب امنیتی اشتباهه")
        if row is None:
            hash_secret(answer[:100])
            raise generic
        self._check_lock(row)
        if not verify_secret(answer[:100], row["sec_salt"], row["sec_hash"]):
            self._register_failure(conn.db, row)
            raise generic
        err = check_password_rules(new_pw, username)
        if err:
            raise Fail(err)
        salt, digest = hash_secret(new_pw)
        with conn.db:
            conn.db.execute("UPDATE users SET pw_salt=?, pw_hash=?, failed_logins=0, locked_until=0 WHERE id=?",
                            (salt, digest, row["id"]))
            conn.db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        log("password reset for", username)
        return {}

    # --------------------------------------------------- account (authed)
    # ---------------------------------------------------------- web push (notifications while the app is closed)
    def _pusher_get(self):
        if getattr(self, "_pusher", None) is None:
            import webpush
            key, pub = webpush.load_vapid(common.DATA_DIR)
            def gone(ep):
                d = connect_db(self.db_path)
                try:
                    with d:
                        d.execute("DELETE FROM push_subs WHERE endpoint=?", (ep,))
                finally:
                    d.close()
            self._pusher = webpush.Pusher(key, pub, gone)
        return self._pusher

    def _push_wake(self, db, chat_id, sender_uid, members):
        """members whose app is closed/hidden get a content-free push (muted chats and the sender are skipped)"""
        for r in db.execute("SELECT user_id, muted FROM members WHERE chat_id=?", (chat_id,)).fetchall():
            uid = r["user_id"]
            if uid == sender_uid or r["muted"]:
                continue
            with self.sessions_lock:
                if any(c.alive and c.vis and time.time() - c.vis_at < 50 for c in self.sessions.get(uid, [])):
                    continue
            if not self._room_hits(("wake", uid), 1, 15):
                continue
            subs = db.execute("SELECT endpoint FROM push_subs WHERE user_id=?", (uid,)).fetchall()
            if subs:
                p = self._pusher_get()
                for s_ in subs:
                    p.send(s_["endpoint"])

    # ------------------------------------------------ notifications for the Android app (long-poll, no Google needed)
    def _notify_add(self, db, chat_id, sender_uid, msg):
        chat = db.execute("SELECT kind, title FROM chats WHERE id=?", (chat_id,)).fetchone()
        if not chat or msg.get("kind") == "system":
            return
        f = msg.get("file")
        text = (("🎤 پیام صوتی" if f.get("voice") else "📎 " + str(f.get("name"))) if f else str(msg.get("body") or ""))[:120]
        sender = msg.get("sender_name") or ""
        title = sender if chat["kind"] == "private" else (chat["title"] or "گروه")
        if chat["kind"] != "private" and sender:
            text = sender + ": " + text
        with self.notify_cv:
            for r in db.execute("SELECT user_id, muted FROM members WHERE chat_id=?", (chat_id,)).fetchall():
                if r["user_id"] == sender_uid or r["muted"]:
                    continue
                self.notify_seq[r["user_id"]] += 1
                self.notify_q[r["user_id"]].append({"seq": self.notify_seq[r["user_id"]], "chat_id": chat_id, "title": title, "text": text})
            self.notify_cv.notify_all()

    def notify_poll(self, token, since, ip, timeout=25):
        """POST /notify {token, since}: blocks up to `timeout` s until a new message arrived for this session's user.
        since=-1 just returns the current sequence number. Returns (http status, dict)."""
        if not self._room_hits(("poll", ip), 40, 60):
            return 429, {"ok": False, "error": "slow down"}
        token = str(token or "")
        if not 20 <= len(token) <= 100:
            return 403, {"ok": False, "error": "bad token"}
        db = connect_db(self.db_path)
        try:
            row = db.execute("SELECT s.last_used, u.id AS uid, u.banned FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                             (hashlib.sha256(token.encode()).digest(),)).fetchone()
        finally:
            db.close()
        if row is None or row["banned"] or row["last_used"] < now() - SESSION_TTL:
            return 403, {"ok": False, "error": "session"}
        uid = row["uid"]
        try:
            since = int(since)
        except (TypeError, ValueError):
            since = -1
        end = time.time() + timeout
        with self.notify_cv:
            if since < 0:
                return 200, {"ok": True, "seq": self.notify_seq[uid], "items": []}
            while self.notify_seq[uid] <= since:
                left = end - time.time()
                if left <= 0:
                    break
                self.notify_cv.wait(left)
            items = [x for x in self.notify_q[uid] if x["seq"] > since]
            return 200, {"ok": True, "seq": self.notify_seq[uid], "items": items}

    def op_push_key(self, conn, req):
        return {"key": self._pusher_get().pub}

    def op_push_subscribe(self, conn, req):
        import webpush
        ep = str(req.get("endpoint") or "")
        if not webpush.allowed_endpoint(ep):
            raise Fail("این سرویس اعلان پشتیبانی نمی‌شه")
        n = conn.db.execute("SELECT COUNT(*) FROM push_subs WHERE user_id=?", (conn.uid,)).fetchone()[0]
        if n >= 10:
            conn.db.execute("DELETE FROM push_subs WHERE endpoint IN (SELECT endpoint FROM push_subs WHERE user_id=? ORDER BY created_at LIMIT 5)", (conn.uid,))
        with conn.db:
            conn.db.execute("INSERT OR REPLACE INTO push_subs(endpoint, user_id, created_at) VALUES (?,?,?)", (ep, conn.uid, now()))
        return {}

    def op_push_test(self, conn, req):
        """diagnose notifications: send now and report what the push service answered (201 = accepted, -1 = this server can't reach it);
        with delay=true send once more after 10 s so you can hide the app and see the real notification"""
        if not self._room_hits(("pushtest", conn.uid), 6, 60):
            raise Fail("آروم‌تر؛ چند ثانیه صبر کن")
        subs = [r["endpoint"] for r in conn.db.execute("SELECT endpoint FROM push_subs WHERE user_id=?", (conn.uid,)).fetchall()]
        p = self._pusher_get()
        codes = [p.deliver(ep) for ep in subs]
        if req.get("delay") and subs:
            threading.Timer(10, lambda: [p.send(ep) for ep in subs]).start()
        return {"subs": len(subs), "codes": codes}

    def op_push_unsubscribe(self, conn, req):
        with conn.db:
            conn.db.execute("DELETE FROM push_subs WHERE endpoint=? AND user_id=?", (str(req.get("endpoint") or ""), conn.uid))
        return {}

    def op_visible(self, conn, req):
        conn.vis = bool(req.get("v"))
        conn.vis_at = time.time()
        return {}

    def op_logout(self, conn, req):
        if conn.session_id:
            with conn.db:
                conn.db.execute("DELETE FROM sessions WHERE id=?", (conn.session_id,))
            conn.session_id = None
        return {}

    def op_me(self, conn, req):
        row = conn.db.execute("SELECT * FROM users WHERE id=?", (conn.uid,)).fetchone()
        return {"me": self._me_view(row)}

    @staticmethod
    def _check_display_name(display, username):
        """Nobody may pose as the admin / official accounts through the display name (look-alike letters and spaces stripped)."""
        import unicodedata
        norm = "".join(ch for ch in unicodedata.normalize("NFKC", display).lower() if ch.isalnum())
        mine = "".join(ch for ch in username.lower() if ch.isalnum())
        official = {"".join(ch for ch in n if ch.isalnum()) for n in (ADMIN_USER, *RESERVED, "مدیر", "ادمین", "پشتیبانی", "مدیریت", "ویندروید")}
        if norm in official and mine != norm:
            raise Fail("این نام نمایشی برای حساب‌های رسمی رزرو شده")

    def op_update_profile(self, conn, req):
        display = clean_line(req.get("display_name"), 40, "نام نمایشی")
        bio = clean_line(req.get("bio"), 140, "بیو")
        if not display:
            raise Fail("نام نمایشی نمی‌تونه خالی باشه")
        self._check_display_name(display, conn.db.execute("SELECT username FROM users WHERE id=?", (conn.uid,)).fetchone()["username"])
        with conn.db:
            conn.db.execute("UPDATE users SET display_name=?, bio=? WHERE id=?", (display, bio, conn.uid))
        self._broadcast_profile(conn.db, conn.uid)
        return self.op_me(conn, req)

    def set_user_avatar(self, db, uid, raw):
        """Called by the HTTP avatar endpoint. The old photo files stay on disk (avatars/<user>/...)."""
        row = db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        try:
            rel = store_avatar(avatar_folder_for_user(row["username"]), raw)
        except ValueError as e:
            raise Fail(str(e))
        at = int(time.time() * 1000)
        with db:
            db.execute("UPDATE users SET avatar=?, avatar_at=? WHERE id=?", (rel, at, uid))
        self._broadcast_profile(db, uid)
        return at

    def op_userwall_remove(self, conn, req):
        row = conn.db.execute("SELECT username FROM users WHERE id=?", (conn.uid,)).fetchone()
        try:
            v = int(req.get("v") or 0)
        except (TypeError, ValueError):
            v = 0
        common.retire_user_wall(row["username"], v)
        vs = common.user_wall_versions(row["username"])
        return {"wall_v": vs[0] if vs else 0, "walls": vs}

    def set_user_wall(self, db, uid, raw):
        row = db.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        try:
            return common.store_user_wall(row["username"], raw)
        except ValueError as e:
            raise Fail(str(e))

    def set_chat_avatar(self, db, uid, chat_id, raw):
        chat_id, me = self._require_member(db, chat_id, uid)
        chat = db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        allowed = me["role"] == "owner" or (chat["channel"] and self._can_post(db, chat_id, uid))
        if chat["kind"] != "group" or not allowed:
            raise Fail("فقط سازنده گروه (یا مدیر کانال) می‌تونه عکس رو عوض کنه")
        try:
            rel = store_avatar(avatar_folder_for_chat(chat_id, chat["title"]), raw)
        except ValueError as e:
            raise Fail(str(e))
        at = int(time.time() * 1000)
        with db:
            db.execute("UPDATE chats SET avatar=?, avatar_at=? WHERE id=?", (rel, at, chat_id))
        self._push_chat_update(db, chat_id)
        return at

    def op_remove_avatar(self, conn, req):
        """Detach the current photo (the file itself is kept in avatars/ so nothing is ever lost)."""
        if req.get("chat_id"):
            chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
            ch = conn.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            if not (me["role"] == "owner" or (ch["channel"] and self._can_post(conn.db, chat_id, conn.uid))):
                raise Fail("فقط سازنده گروه می‌تونه عکس گروه رو عوض کنه")
            retire_avatar(ch["avatar"])
            with conn.db:
                conn.db.execute("UPDATE chats SET avatar=NULL, avatar_at=0 WHERE id=?", (chat_id,))
            self._push_chat_update(conn.db, chat_id)
        else:
            cur_av = conn.db.execute("SELECT avatar FROM users WHERE id=?", (conn.uid,)).fetchone()["avatar"]
            retire_avatar(cur_av)
            with conn.db:
                conn.db.execute("UPDATE users SET avatar=NULL, avatar_at=0 WHERE id=?", (conn.uid,))
            self._broadcast_profile(conn.db, conn.uid)
        return {}

    def op_wallpaper_list(self, conn, req):
        return {"walls": [{"name": n, "v": m, "size": s} for n, m, s in common.list_wallpapers()]}

    def op_avatar_list(self, conn, req):
        """All kept profile photos of a user or group (for the swipe-through gallery), newest first."""
        if req.get("chat_id"):
            chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
            c = conn.db.execute("SELECT title FROM chats WHERE id=?", (chat_id,)).fetchone()
            return {"pics": avatar_pics(avatar_folder_for_chat(chat_id, c["title"] or "")), "chat_id": chat_id}
        try:
            uid = int(req.get("user_id"))
        except (TypeError, ValueError):
            raise Fail("کاربر نامعتبر")
        u = conn.db.execute("SELECT username, banned FROM users WHERE id=?", (uid,)).fetchone()
        if not u or u["banned"]:
            raise Fail("کاربر پیدا نشد")
        return {"pics": avatar_pics(avatar_folder_for_user(u["username"])), "user_id": uid}

    # --------------------------------------- group @id and invite links
    def _new_invite(self, db, chat_id):
        for _ in range(5):
            tok = secrets.token_urlsafe(9)
            try:
                with db:
                    db.execute("UPDATE chats SET invite=? WHERE id=?", (tok, chat_id))
                return tok
            except sqlite3.IntegrityError:
                continue
        raise Fail("ساخت لینک ناموفق بود")

    def op_invite_link(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        chat = self._group_owner_check(conn, chat_id)
        if me["role"] != "owner":
            raise Fail("فقط سازنده گروه به لینک دعوت دسترسی داره")
        tok = chat["invite"]
        if not tok or req.get("reset"):
            tok = self._new_invite(conn.db, chat_id)
        return {"invite": tok}

    def op_invite_info(self, conn, req):
        c = self._chat_by_token_or_name(conn.db, req)
        mine = conn.db.execute("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (c["id"], conn.uid)).fetchone()
        n = conn.db.execute("SELECT COUNT(*) FROM members WHERE chat_id=?", (c["id"],)).fetchone()[0]
        return {"chat": {"id": c["id"], "title": c["title"], "description": c["description"] or "", "member_count": n,
                         "username": c["username"], "member": bool(mine)}}

    def _chat_by_token_or_name(self, db, req):
        tok, name = str(req.get("token") or ""), str(req.get("username") or "").lstrip("@")
        c = None
        if tok and len(tok) < 40:
            c = db.execute("SELECT * FROM chats WHERE invite=? AND kind='group' AND channel=0", (tok,)).fetchone()
        elif name and USERNAME_RE.match(name):
            c = db.execute("SELECT * FROM chats WHERE username=? COLLATE NOCASE AND kind='group' AND channel=0",
                           (name,)).fetchone()
        if not c:
            raise Fail("این لینک یا آیدی معتبر نیست (یا گروه حذف شده)")
        return c

    def op_join(self, conn, req):
        c = self._chat_by_token_or_name(conn.db, req)
        if not conn.db.execute("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (c["id"], conn.uid)).fetchone():
            n = conn.db.execute("SELECT COUNT(*) FROM members WHERE chat_id=?", (c["id"],)).fetchone()[0]
            if n >= 2000:
                raise Fail("ظرفیت گروه پره")
            with conn.db:
                conn.db.execute("INSERT INTO members(chat_id, user_id, joined_at) VALUES (?,?,?)",
                                (c["id"], conn.uid, now()))
            name = conn.db.execute("SELECT display_name FROM users WHERE id=?", (conn.uid,)).fetchone()[0]
            self._system_message(conn.db, c["id"], f"{name} با لینک به گروه پیوست")
            self.push(self._member_ids(conn.db, c["id"]), {"event": "members_changed", "chat_id": c["id"]})
        return {"chat": self._chat_view(conn.db, c["id"], conn.uid)}

    # ------------------------------------------------ devices (sessions)
    def op_sessions(self, conn, req):
        rows = conn.db.execute("SELECT id, device, created_at, last_used FROM sessions WHERE user_id=? "
                               "ORDER BY last_used DESC", (conn.uid,)).fetchall()
        return {"sessions": [dict(id=r["id"], device=r["device"], created_at=r["created_at"],
                                  last_used=r["last_used"], current=r["id"] == conn.session_id) for r in rows]}

    def op_terminate_session(self, conn, req):
        try:
            sid = int(req.get("session_id"))
        except (TypeError, ValueError):
            raise Fail("نشست نامعتبر")
        with conn.db:
            n = conn.db.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (sid, conn.uid)).rowcount
        if not n:
            raise Fail("نشست پیدا نشد")
        with self.sessions_lock:
            victims = [c for c in self.sessions.get(conn.uid, []) if c.session_id == sid and c is not conn]
        for c in victims:
            c.send({"event": "session_ended"})
            c.close()
        return {}

    def op_terminate_others(self, conn, req):
        with conn.db:
            conn.db.execute("DELETE FROM sessions WHERE user_id=? AND id!=?", (conn.uid, conn.session_id or 0))
        with self.sessions_lock:
            victims = [c for c in self.sessions.get(conn.uid, []) if c is not conn and c.session_id != conn.session_id]
        for c in victims:
            c.send({"event": "session_ended"})
            c.close()
        return {}

    def op_change_password(self, conn, req):
        row = conn.db.execute("SELECT * FROM users WHERE id=?", (conn.uid,)).fetchone()
        self._check_lock(row)
        if not verify_secret(str(req.get("old_password", ""))[:128], row["pw_salt"], row["pw_hash"]):
            self._register_failure(conn.db, row)
            raise Fail("رمز فعلی اشتباهه")
        err = check_password_rules(req.get("new_password", ""), row["username"])
        if err:
            raise Fail(err)
        salt, digest = hash_secret(req["new_password"])
        with conn.db:
            conn.db.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE id=?", (salt, digest, conn.uid))
            conn.db.execute("DELETE FROM sessions WHERE user_id=? AND id IS NOT ?", (conn.uid, conn.session_id))
        return {}

    def op_search_users(self, conn, req):
        q = str(req.get("q", "")).strip().lstrip("@")
        if not q:
            return {"users": []}
        if not USERNAME_RE.match(q):
            return {"users": []}
        rows = conn.db.execute("SELECT * FROM users WHERE id!=? AND banned=0 AND username=? COLLATE NOCASE",
                               (conn.uid, q)).fetchall()      # exact username only: nobody can browse the user list
        gr = conn.db.execute("SELECT c.*, (SELECT COUNT(*) FROM members m WHERE m.chat_id=c.id) AS n FROM chats c "
                             "WHERE c.username=? COLLATE NOCASE AND c.kind='group' AND c.channel=0", (q,)).fetchall()
        groups = [{"id": g["id"], "title": g["title"], "username": g["username"], "member_count": g["n"],
                   "description": g["description"] or ""} for g in gr]
        return {"users": [self._user_for(conn.db, r, conn.uid) for r in rows], "groups": groups}

    # ------------------------------------------------------------- chats
    def op_chats(self, conn, req):
        ids = [r[0] for r in conn.db.execute(
            "SELECT c.id FROM chats c JOIN members m ON m.chat_id=c.id WHERE m.user_id=? AND "
            "(c.kind='group' OR c.pair_key=? OR EXISTS (SELECT 1 FROM messages WHERE chat_id=c.id))",
            (conn.uid, f"{conn.uid}:{conn.uid}"))]
        views = [v for v in (self._chat_view(conn.db, i, conn.uid) for i in ids) if v]
        views.sort(key=lambda v: (v["fav"], v["last"]["created_at"] if v["last"] else v["created_at"],
                                  v["last"]["id"] if v["last"] else 0), reverse=True)
        return {"chats": views}

    def op_open_private(self, conn, req):
        username = str(req.get("username", "")).strip().lstrip("@")
        target = conn.db.execute("SELECT * FROM users WHERE username=? AND banned=0", (username,)).fetchone() \
            if USERNAME_RE.match(username) else None
        if not target:
            raise Fail("کاربری با این نام پیدا نشد")
        a, b = sorted((conn.uid, target["id"]))                   # a == b -> "Saved messages"
        key = f"{a}:{b}"
        row = conn.db.execute("SELECT id FROM chats WHERE pair_key=?", (key,)).fetchone()
        if row:
            chat_id = row["id"]
        else:
            with conn.db:
                cur = conn.db.execute("INSERT INTO chats(kind, pair_key, created_at) VALUES ('private',?,?)",
                                      (key, now()))
                chat_id = cur.lastrowid
                for u in {a, b}:
                    conn.db.execute("INSERT INTO members(chat_id, user_id, joined_at) VALUES (?,?,?)",
                                    (chat_id, u, now()))
        return {"chat": self._chat_view(conn.db, chat_id, conn.uid)}

    def op_create_group(self, conn, req):
        title = clean_line(req.get("title"), 40, "اسم گروه")
        if not title:
            raise Fail("اسم گروه رو بنویس")
        names = req.get("usernames") or []
        if not isinstance(names, list) or len(names) > 50:
            raise Fail("لیست اعضا نامعتبره")
        ids, missing = [], []
        for n in names:
            n = str(n).strip().lstrip("@")
            if not n:
                continue
            row = conn.db.execute("SELECT id FROM users WHERE username=? AND banned=0", (n,)).fetchone() \
                if USERNAME_RE.match(n) else None
            if row is None:
                missing.append(n)
            elif row["id"] != conn.uid and row["id"] not in ids:
                ids.append(row["id"])
        if missing:
            raise Fail("این کاربرا پیدا نشدن: " + "، ".join(missing))
        with conn.db:
            cur = conn.db.execute("INSERT INTO chats(kind, title, owner_id, created_at) VALUES ('group',?,?,?)",
                                  (title, conn.uid, now()))
            chat_id = cur.lastrowid
            conn.db.execute("INSERT INTO members(chat_id, user_id, role, joined_at) VALUES (?,?, 'owner', ?)",
                            (chat_id, conn.uid, now()))
            for u in ids:
                conn.db.execute("INSERT INTO members(chat_id, user_id, joined_at) VALUES (?,?,?)",
                                (chat_id, u, now()))
        me = conn.db.execute("SELECT display_name FROM users WHERE id=?", (conn.uid,)).fetchone()
        for u in ids:
            self.push([u], {"event": "chat_added", "chat": self._chat_view(conn.db, chat_id, u)})
        self._system_message(conn.db, chat_id, f"{me['display_name']} گروه «{title}» رو ساخت")
        return {"chat": self._chat_view(conn.db, chat_id, conn.uid)}

    def op_members(self, conn, req):
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        rows = conn.db.execute("SELECT u.*, m.role FROM members m JOIN users u ON u.id=m.user_id "
                               "WHERE m.chat_id=? ORDER BY m.joined_at", (chat_id,)).fetchall()
        return {"members": [dict(self._user_for(conn.db, r, conn.uid), role=r["role"]) for r in rows]}

    def _group_owner_check(self, conn, chat_id):
        chat = conn.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat["kind"] != "group" or chat["channel"]:
            raise Fail("این کار فقط تو گروه ممکنه")
        return chat

    def op_add_member(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        chat = self._group_owner_check(conn, chat_id)
        if me["role"] not in ("owner", "admin"):
            raise Fail("فقط سازنده یا ادمین گروه می‌تونه عضو اضافه کنه")
        name = str(req.get("username", "")).strip().lstrip("@")
        target = conn.db.execute("SELECT * FROM users WHERE username=? AND banned=0", (name,)).fetchone() \
            if USERNAME_RE.match(name) else None
        if not target:
            raise Fail("کاربری با این نام پیدا نشد")
        if conn.db.execute("SELECT 1 FROM members WHERE chat_id=? AND user_id=?", (chat_id, target["id"])).fetchone():
            raise Fail("این کاربر قبلاً عضو گروهه")
        with conn.db:
            conn.db.execute("INSERT INTO members(chat_id, user_id, joined_at) VALUES (?,?,?)",
                            (chat_id, target["id"], now()))
        self.push([target["id"]], {"event": "chat_added", "chat": self._chat_view(conn.db, chat_id, target["id"])})
        self._system_message(conn.db, chat_id, f"{target['display_name']} به گروه اضافه شد")
        self.push(self._member_ids(conn.db, chat_id), {"event": "members_changed", "chat_id": chat_id})
        return {}

    def op_set_role(self, conn, req):
        """owner promotes a member to admin (add/remove members, pin, edit group info) or demotes him again"""
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        self._group_owner_check(conn, chat_id)
        if me["role"] != "owner":
            raise Fail("فقط سازنده گروه می‌تونه ادمین تعیین کنه")
        try:
            uid = int(req.get("user_id"))
        except (TypeError, ValueError):
            raise Fail("کاربر نامعتبر")
        role = "admin" if req.get("admin") else "member"
        t = conn.db.execute("SELECT role FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
        if not t or t["role"] == "owner":
            raise Fail("این کاربر عضو عادی گروه نیست")
        with conn.db:
            conn.db.execute("UPDATE members SET role=? WHERE chat_id=? AND user_id=?", (role, chat_id, uid))
        name = conn.db.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone()[0]
        self._system_message(conn.db, chat_id, f"{name} " + ("ادمین شد" if role == "admin" else "دیگه ادمین نیست"))
        self.push(self._member_ids(conn.db, chat_id), {"event": "members_changed", "chat_id": chat_id})
        for m in self._member_ids(conn.db, chat_id):
            self.push([m], {"event": "chat_updated", "chat": self._chat_view(conn.db, chat_id, m)})
        return {}

    def op_remove_member(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        self._group_owner_check(conn, chat_id)
        if me["role"] not in ("owner", "admin"):
            raise Fail("فقط سازنده یا ادمین گروه می‌تونه عضو حذف کنه")
        try:
            uid = int(req.get("user_id"))
        except (TypeError, ValueError):
            raise Fail("کاربر نامعتبر")
        if uid == conn.uid:
            raise Fail("برای خروج از «ترک گروه» استفاده کن")
        target = conn.db.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone()
        trole = conn.db.execute("SELECT role FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
        if trole and trole["role"] == "owner" or (trole and trole["role"] == "admin" and me["role"] != "owner"):
            raise Fail("ادمین‌ها رو فقط سازنده گروه می‌تونه حذف کنه")
        with conn.db:
            n = conn.db.execute("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, uid)).rowcount
        if not n:
            raise Fail("این کاربر عضو گروه نیست")
        self.push([uid], {"event": "chat_removed", "chat_id": chat_id})
        self._system_message(conn.db, chat_id, f"{target['display_name']} از گروه حذف شد")
        self.push(self._member_ids(conn.db, chat_id), {"event": "members_changed", "chat_id": chat_id})
        return {}

    def op_leave(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        self._group_owner_check(conn, chat_id)
        name = conn.db.execute("SELECT display_name FROM users WHERE id=?", (conn.uid,)).fetchone()[0]
        with conn.db:
            conn.db.execute("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat_id, conn.uid))
            rest = conn.db.execute("SELECT user_id FROM members WHERE chat_id=? ORDER BY joined_at LIMIT 1",
                                   (chat_id,)).fetchone()
            if rest is None:
                conn.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
            elif me["role"] == "owner":
                conn.db.execute("UPDATE members SET role='owner' WHERE chat_id=? AND user_id=?",
                                (chat_id, rest["user_id"]))
                conn.db.execute("UPDATE chats SET owner_id=? WHERE id=?", (rest["user_id"], chat_id))
        if rest is not None:
            self._system_message(conn.db, chat_id, f"{name} گروه رو ترک کرد")
            self.push(self._member_ids(conn.db, chat_id), {"event": "members_changed", "chat_id": chat_id})
        self.push([conn.uid], {"event": "chat_removed", "chat_id": chat_id}, exclude=conn)
        return {}

    def sweep_deleted_files(self):
        """remove files of deleted groups once their delay is over (unless a forward in another chat still uses the file)"""
        db = connect_db(self.db_path)
        try:
            n = 0
            for r in db.execute("SELECT rel FROM purge_files WHERE due<=?", (now(),)).fetchall():
                if not common.file_in_use(db, r["rel"]):
                    common.remove_upload(r["rel"])
                    n += 1
                with db:
                    db.execute("DELETE FROM purge_files WHERE rel=?", (r["rel"],))
            return n
        finally:
            db.close()

    def op_delete_group(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        self._group_owner_check(conn, chat_id)
        if me["role"] != "owner":
            raise Fail("فقط سازنده گروه می‌تونه گروه رو حذف کنه")
        members = self._member_ids(conn.db, chat_id)
        due = now() + int(common.GROUP_FILE_TTL_HOURS * 3600)
        with conn.db:                    # the group's files are removed GROUP_FILE_TTL_HOURS (4 h) later by the sweeper
            for r in conn.db.execute("SELECT DISTINCT file_path FROM messages WHERE chat_id=? AND file_path IS NOT NULL", (chat_id,)).fetchall():
                conn.db.execute("INSERT OR REPLACE INTO purge_files(rel, due) VALUES (?,?)", (r["file_path"], due))
            conn.db.execute("DELETE FROM chats WHERE id=?", (chat_id,))
        self.push(members, {"event": "chat_removed", "chat_id": chat_id, "deleted": True}, exclude=conn)
        return {}

    # ----------------------------------------------------------- messages
    def op_history(self, conn, req):
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        try:
            limit = max(1, min(int(req.get("limit") or 40), 100))
            before = int(req["before_id"]) if req.get("before_id") else None
            after = int(req["after_id"]) if req.get("after_id") else None
            around = int(req["around"]) if req.get("around") else None
        except (TypeError, ValueError):
            raise Fail("پارامتر نامعتبر")
        base = ("SELECT m.*, u.display_name AS sender_name FROM messages m LEFT JOIN users u ON u.id=m.sender_id "
                "WHERE m.chat_id=?")
        db = conn.db
        newer = False
        if around:                                           # a window of messages centred on one message
            half = max(5, limit // 2)
            older = db.execute(base + " AND m.id<=? ORDER BY m.id DESC LIMIT ?", (chat_id, around, half + 1)).fetchall()
            nxt = db.execute(base + " AND m.id>? ORDER BY m.id ASC LIMIT ?", (chat_id, around, half + 1)).fetchall()
            has_more, newer = len(older) > half, len(nxt) > half
            rows = list(reversed(older[:half])) + nxt[:half]
        elif after:                                          # scrolling down towards the newest messages
            nxt = db.execute(base + " AND m.id>? ORDER BY m.id ASC LIMIT ?", (chat_id, after, limit + 1)).fetchall()
            has_more, newer, rows = False, len(nxt) > limit, nxt[:limit]
        else:
            sql, args = base, [chat_id]
            if before:
                sql += " AND m.id<?"
                args.append(before)
            rows = db.execute(sql + " ORDER BY m.id DESC LIMIT ?", args + [limit + 1]).fetchall()
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
        return {"messages": [self._msg(db, r) for r in rows], "has_more": has_more, "has_newer": newer}

    def op_search_messages(self, conn, req):
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        q = str(req.get("q") or "").strip()[:100]
        if not q:
            return {"messages": []}
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.db.execute(
            "SELECT m.*, u.display_name AS sender_name FROM messages m LEFT JOIN users u ON u.id=m.sender_id "
            "WHERE m.chat_id=? AND m.deleted=0 AND m.kind!='system' AND (m.body LIKE ? ESCAPE '\\' "
            "OR m.file_name LIKE ? ESCAPE '\\') ORDER BY m.id DESC LIMIT 40", (chat_id, like, like)).fetchall()
        return {"messages": [self._msg(conn.db, r) for r in rows]}

    def op_search_all(self, conn, req):
        """search the text of every chat I am a member of (the main search box)"""
        q = str(req.get("q") or "").strip()[:100]
        if len(q) < 2:
            return {"results": []}
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = conn.db.execute(
            "SELECT m.id, m.chat_id, m.body, m.file_name, m.created_at, u.display_name AS sender_name FROM messages m "
            "JOIN members mb ON mb.chat_id=m.chat_id AND mb.user_id=? LEFT JOIN users u ON u.id=m.sender_id "
            "WHERE m.deleted=0 AND m.kind!='system' AND (m.body LIKE ? ESCAPE '\\' OR m.file_name LIKE ? ESCAPE '\\') "
            "ORDER BY m.id DESC LIMIT 30", (conn.uid, like, like)).fetchall()
        return {"results": [{"id": r["id"], "chat_id": r["chat_id"], "sender": r["sender_name"] or "",
                             "text": (r["body"] or r["file_name"] or "")[:160], "at": r["created_at"]} for r in rows]}

    def op_shared(self, conn, req):
        """Shared media/files/voice/links of a chat (the 'Shared media' tabs in the chat info)."""
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        kind = str(req.get("kind") or "media")
        try:
            before = int(req["before_id"]) if req.get("before_id") else None
        except (TypeError, ValueError):
            raise Fail("پارامتر نامعتبر")
        sql = ("SELECT m.*, u.display_name AS sender_name FROM messages m LEFT JOIN users u ON u.id=m.sender_id "
               "WHERE m.chat_id=? AND m.deleted=0 AND ")
        sql += "m.kind='file'" if kind != "link" else "m.kind='text' AND (m.body LIKE '%http://%' OR m.body LIKE '%https://%')"
        args = [chat_id]
        if before:
            sql += " AND m.id<?"
            args.append(before)
        out = []
        for r in conn.db.execute(sql + " ORDER BY m.id DESC LIMIT 600", args):
            if kind != "link":
                m = self._msg(conn.db, r)
                f = m["file"] or {}
                ok = {"media": f.get("kind") in ("image", "video") and not f.get("voice"),
                      "voice": bool(f.get("voice")),
                      "audio": f.get("kind") == "audio" and not f.get("voice"),
                      "file": f.get("kind") == "file"}.get(kind, False)
                if not ok:
                    continue
            else:
                m = self._msg(conn.db, r)
            out.append(m)
            if len(out) >= 60:
                break
        return {"messages": out}

    def _check_send_rate(self, uid):
        q = self.send_hits[uid]
        t = time.time()
        while q and q[0] < t - 10:
            q.popleft()
        if len(q) >= 20:
            raise Fail("خیلی سریع پیام می‌فرستی، یه لحظه صبر کن")
        q.append(t)

    @staticmethod
    def _safe_filename(name):
        name = os.path.basename(str(name or "file").replace("\\", "/"))
        name = CTRL_RE.sub("", name).strip().strip(".") or "file"
        return name[:100]

    def _store_message(self, db, uid, chat_id, text, reply_to, fname=None, raw=None, src=None, meta=None, ref=None):
        """Insert a text/file message. A file comes either as bytes (`raw`) or as a finished temp file (`src`)."""
        if not fname and not text:
            raise Fail("پیام خالیه")
        if not self._can_post(db, chat_id, uid):
            raise Fail("فقط مدیر می‌تونه تو این کانال پیام بذاره")
        self._check_not_blocked_private(db, chat_id, uid)
        if reply_to:
            if not isinstance(reply_to, int):
                raise Fail("پیامی که جوابش رو می‌دی نامعتبره")
            if not db.execute("SELECT 1 FROM messages WHERE id=? AND chat_id=?", (reply_to, chat_id)).fetchone():
                raise Fail("پیامی که جوابش رو می‌دی پیدا نشد")
        fsize = None
        if fname:
            fsize = len(raw) if raw is not None else Path(src if ref is None else resolve_upload(ref)).stat().st_size
        saved = None
        try:
            with db:       # message row + file on disk succeed or fail together
                cur = db.execute(
                    "INSERT INTO messages(chat_id, sender_id, kind, body, reply_to, file_name, file_size,"
                    " created_at, meta) VALUES (?,?,?,?,?,?,?,?,?)",
                    (chat_id, uid, "file" if fname else "text", text, reply_to or None, fname, fsize, now(),
                     json.dumps(meta, ensure_ascii=False) if meta else None))
                mid = cur.lastrowid
                if fname and ref is not None:        # forward: point at the same stored file, no copy
                    db.execute("UPDATE messages SET file_path=? WHERE id=?", (ref, mid))
                elif fname:
                    rel = upload_relpath(db, chat_id, mid, fname, uid)
                    dest = common.UPLOADS_DIR / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if raw is not None:
                        dest.write_bytes(raw)
                    else:
                        shutil.move(str(src), str(dest))
                    saved = rel
                    db.execute("UPDATE messages SET file_path=? WHERE id=?", (rel, mid))
                db.execute("UPDATE members SET last_read_id=MAX(last_read_id, ?) WHERE chat_id=? AND user_id=?",
                           (mid, chat_id, uid))
        except Exception as e:
            if saved:
                remove_upload(saved)
            if isinstance(e, OSError):
                raise Fail("ذخیره فایل روی سرور ناموفق بود")
            raise
        return self._msg_by_id(db, mid)

    def _announce(self, db, chat_id, msg, uid, exclude=None):
        members = self._member_ids(db, chat_id)
        self.push(members, {"event": "message", "message": msg}, exclude=exclude)
        try:
            self._notify_add(db, chat_id, uid, msg)
        except Exception as e:
            log("notify queue error:", repr(e))
        try:
            self._push_wake(db, chat_id, uid, members)
        except Exception as e:
            log("push wake error:", repr(e))
        # replying implies you have seen everything before it -> peers get the double tick
        self.push(members, {"event": "read", "chat_id": chat_id, "user_id": uid, "up_to": msg["id"]},
                  exclude=exclude)

    @staticmethod
    def _voice_meta(name, voice):
        """Validate the voice-message info sent by the client -> meta dict (or None for normal files)."""
        if not voice:
            return None
        if media_info(name)[1] != "audio":
            raise Fail("پیام صوتی باید فایل صوتی باشه")
        try:
            dur = round(float(voice.get("dur")), 1) if isinstance(voice, dict) else 0.0
        except (TypeError, ValueError):
            dur = 0.0
        wave = voice.get("wave") if isinstance(voice, dict) else None
        if isinstance(wave, str):
            wave = [int(x) for x in wave.split(",") if x.strip().isdigit()]
        if not isinstance(wave, list):
            wave = []
        wave = [max(0, min(31, int(x))) for x in wave[:64] if isinstance(x, (int, float))]
        return {"voice": True, "dur": max(0.0, min(dur, 3600.0)), "wave": wave}

    def op_send(self, conn, req):
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        self._check_send_rate(conn.uid)
        text = CTRL_RE.sub("", str(req.get("text") or "").replace("\r\n", "\n").strip())
        if len(text) > MAX_TEXT:
            raise Fail(f"پیام خیلی طولانیه (حداکثر {MAX_TEXT} کاراکتر)")
        file = req.get("file")
        fname, raw = None, None
        if file:
            if not isinstance(file, dict):
                raise Fail("فایل نامعتبر")
            try:
                raw = base64.b64decode(str(file.get("data", "")), validate=True)
            except (binascii.Error, ValueError):
                raise Fail("فایل نامعتبر")
            if not raw:
                raise Fail("فایل خالیه")
            if len(raw) > MAX_FILE:
                raise Fail(f"حجم فایل بیشتر از {MAX_FILE // (1024 * 1024)} مگابایته")
            fname = self._safe_filename(file.get("name"))
        meta = self._voice_meta(fname, file.get("voice")) if file else None
        msg = self._store_message(conn.db, conn.uid, chat_id, text, req.get("reply_to"), fname, raw=raw, meta=meta)
        self._announce(conn.db, chat_id, msg, conn.uid, exclude=conn)
        return {"message": msg}

    def create_file_message(self, db, uid, chat_id, text, reply_to, name, src, voice=None):
        """Used by the HTTP upload endpoint (large files are streamed to a temp file first)."""
        user = db.execute("SELECT banned FROM users WHERE id=?", (uid,)).fetchone()
        if user is None or user["banned"]:
            raise Fail("این حساب مسدود شده")
        chat_id, _ = self._require_member(db, chat_id, uid)
        self._check_send_rate(uid)
        text = CTRL_RE.sub("", str(text or "").replace("\r\n", "\n").strip())
        if len(text) > MAX_TEXT:
            raise Fail(f"پیام خیلی طولانیه (حداکثر {MAX_TEXT} کاراکتر)")
        fname = self._safe_filename(name)
        msg = self._store_message(db, uid, chat_id, text, reply_to, fname, src=src, meta=self._voice_meta(fname, voice))
        self._announce(db, chat_id, msg, uid)
        return msg

    # ----------------------------------------------- reactions / forward / pin
    def op_react(self, conn, req):
        try:
            mid = int(req.get("message_id"))
        except (TypeError, ValueError):
            raise Fail("پیام نامعتبر")
        emoji = str(req.get("emoji") or "")
        row = conn.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row or row["deleted"] or row["kind"] == "system":
            raise Fail("پیام پیدا نشد")
        self._require_member(conn.db, row["chat_id"], conn.uid)
        if emoji not in REACTIONS:
            raise Fail("این ری‌اکشن مجاز نیست")
        old = conn.db.execute("SELECT emoji FROM reactions WHERE message_id=? AND user_id=?", (mid, conn.uid)).fetchone()
        with conn.db:
            if old and old["emoji"] == emoji:
                conn.db.execute("DELETE FROM reactions WHERE message_id=? AND user_id=?", (mid, conn.uid))
            else:
                conn.db.execute("INSERT OR REPLACE INTO reactions(message_id, user_id, emoji, created_at) "
                                "VALUES (?,?,?,?)", (mid, conn.uid, emoji, now()))
        msg = self._msg_by_id(conn.db, mid)
        self.push(self._member_ids(conn.db, row["chat_id"]), {"event": "message_updated", "message": msg}, exclude=conn)
        return {"message": msg}

    def op_forward(self, conn, req):
        try:
            mid = int(req.get("message_id"))
        except (TypeError, ValueError):
            raise Fail("پیام نامعتبر")
        targets = req.get("chat_ids")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 5:
            raise Fail("حداکثر ۵ چت رو انتخاب کن")
        src = conn.db.execute("SELECT m.*, u.display_name AS sender_name FROM messages m LEFT JOIN users u "
                              "ON u.id=m.sender_id WHERE m.id=?", (mid,)).fetchone()
        if not src or src["deleted"] or src["kind"] == "system":
            raise Fail("پیام پیدا نشد")
        self._require_member(conn.db, src["chat_id"], conn.uid)
        try:
            meta = json.loads(src["meta"]) if src["meta"] else {}
        except ValueError:
            meta = {}
        meta["fwd"] = meta.get("fwd") or src["sender_name"] or "—"
        sent = []
        for cid in dict.fromkeys(targets):
            cid, _ = self._require_member(conn.db, cid, conn.uid)
            self._check_send_rate(conn.uid)
            if src["kind"] == "file" and resolve_upload(src["file_path"]) is None:
                raise Fail("فایل این پیام روی سرور نیست")
            msg = self._store_message(conn.db, conn.uid, cid, src["body"], None,
                                      src["file_name"] if src["kind"] == "file" else None,
                                      meta=meta, ref=src["file_path"] if src["kind"] == "file" else None)
            self._announce(conn.db, cid, msg, conn.uid, exclude=conn)
            sent.append(msg)
        return {"messages": sent}

    def image_thumb(self, db, row):
        """Small preview of an image message, made (and cached next to the file) the first time somebody needs it."""
        tp = resolve_upload(thumb_rel(row["file_path"]))
        if tp:
            return tp
        src = resolve_upload(row["file_path"])
        if src is None or src.stat().st_size > 60 * 1024 * 1024:
            return None
        try:
            from PIL import Image, ImageOps
            im = Image.open(src)
            im = ImageOps.exif_transpose(im)
            w, h = im.size
            im.thumbnail((360, 360))
            dest = common.UPLOADS_DIR / thumb_rel(row["file_path"])
            dest.parent.mkdir(parents=True, exist_ok=True)
            im.convert("RGB").save(dest, "JPEG", quality=72)
            try:
                meta = json.loads(row["meta"]) if row["meta"] else {}
            except ValueError:
                meta = {}
            if not meta.get("tw"):
                meta.update(tw=w, th=h)
                with db:
                    db.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta), row["id"]))
            return dest
        except Exception:
            return None

    def op_voice_played(self, conn, req):
        """The listener tells us he played a voice message -> the sender sees it (like Telegram's blue dot)."""
        try:
            mid = int(req.get("message_id"))
        except (TypeError, ValueError):
            raise Fail("پیام نامعتبر")
        row = conn.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row or row["deleted"] or row["kind"] != "file" or '"voice"' not in (row["meta"] or ""):
            return {}
        self._require_member(conn.db, row["chat_id"], conn.uid)
        if row["sender_id"] == conn.uid:
            return {}
        with conn.db:
            new = conn.db.execute("INSERT OR IGNORE INTO voice_plays(message_id, user_id, played_at) VALUES (?,?,?)",
                                  (mid, conn.uid, now())).rowcount
        if new:
            self.push(self._member_ids(conn.db, row["chat_id"]),
                      {"event": "voice_played", "chat_id": row["chat_id"], "message_id": mid, "user_id": conn.uid})
        return {}

    def set_thumb(self, db, uid, mid, raw, w=0, h=0):
        """Preview picture of an image/video message (made by the sender's browser). Stored next to the file."""
        row = db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row or row["deleted"] or row["kind"] != "file" or not row["file_path"]:
            raise Fail("پیام پیدا نشد")
        self._require_member(db, row["chat_id"], uid)
        if row["sender_id"] != uid and resolve_upload(thumb_rel(row["file_path"])):
            raise Fail("پریویو قبلاً ساخته شده")           # any member may add a missing preview, nobody may replace one
        if media_info(row["file_name"])[1] not in ("image", "video"):
            raise Fail("این فایل پریویو نداره")
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(raw))
            im.thumbnail((360, 360))
            out = io.BytesIO()
            im.convert("RGB").save(out, "JPEG", quality=70)
            raw = out.getvalue()
        except Exception:
            if not raw.startswith(b"\xff\xd8"):
                raise Fail("تصویر پریویو نامعتبره")
        dest = common.UPLOADS_DIR / thumb_rel(row["file_path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        try:
            meta = json.loads(row["meta"]) if row["meta"] else {}
        except ValueError:
            meta = {}
        meta.update(tw=max(0, min(int(w or 0), 20000)), th=max(0, min(int(h or 0), 20000)))
        with db:
            db.execute("UPDATE messages SET meta=? WHERE id=?", (json.dumps(meta, ensure_ascii=False), mid))
        msg = self._msg_by_id(db, mid)
        self.push(self._member_ids(db, row["chat_id"]), {"event": "message_updated", "message": msg})
        return msg

    def op_pin(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        chat = conn.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat["channel"]:
            if not self._can_post(conn.db, chat_id, conn.uid):
                raise Fail("فقط مدیر کانال می‌تونه پیام سنجاق کنه")
        elif chat["kind"] == "group" and me["role"] not in ("owner", "admin"):
            raise Fail("فقط سازنده یا ادمین گروه می‌تونه پیام سنجاق کنه")
        mid = req.get("message_id")
        if mid:
            row = conn.db.execute("SELECT * FROM messages WHERE id=? AND chat_id=?", (mid, chat_id)).fetchone()
            if not row or row["deleted"] or row["kind"] == "system":
                raise Fail("پیام پیدا نشد")
        with conn.db:
            conn.db.execute("UPDATE chats SET pinned_id=? WHERE id=?", (int(mid) if mid else None, chat_id))
        if mid:
            name = conn.db.execute("SELECT display_name FROM users WHERE id=?", (conn.uid,)).fetchone()[0]
            self._system_message(conn.db, chat_id, f"{name} یه پیام رو سنجاق کرد")
        self._push_chat_update(conn.db, chat_id)
        return {}

    def op_chat_settings(self, conn, req):
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        with conn.db:
            for key in ("muted", "fav"):
                if key in req:
                    conn.db.execute(f"UPDATE members SET {key}=? WHERE chat_id=? AND user_id=?",
                                    (1 if req[key] else 0, chat_id, conn.uid))
        view = self._chat_view(conn.db, chat_id, conn.uid)
        self.push([conn.uid], {"event": "chat_updated", "chat": view}, exclude=conn)
        return {"chat": view}

    def op_edit_group(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        chat = conn.db.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat["kind"] != "group":
            raise Fail("این کار فقط تو گروه ممکنه")
        if chat["channel"]:
            if not self._can_post(conn.db, chat_id, conn.uid):
                raise Fail("فقط مدیر کانال می‌تونه اطلاعاتش رو عوض کنه")
        elif me["role"] not in ("owner", "admin"):
            raise Fail("فقط سازنده یا ادمین گروه می‌تونه اطلاعات گروه رو عوض کنه")
        title = clean_line(req.get("title", chat["title"]), 40, "اسم گروه")
        desc = clean_line(req.get("description", chat["description"]), 200, "توضیح گروه")
        if not title:
            raise Fail("اسم گروه نمی‌تونه خالی باشه")
        uname = chat["username"]
        if "username" in req and not chat["channel"]:
            u = str(req.get("username") or "").strip().lstrip("@")
            if not u:
                uname = None
            else:
                if not USERNAME_RE.match(u) or len(u) < 4:
                    raise Fail("آیدی گروه باید ۴ تا ۳۲ کاراکتر و فقط حرف انگلیسی، عدد یا _ باشه")
                if u.lower() in RESERVED or conn.db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (u,)).fetchone():
                    raise Fail("این آیدی قبلاً گرفته شده")
                uname = u
        try:
            with conn.db:
                conn.db.execute("UPDATE chats SET title=?, description=?, username=? WHERE id=?",
                                (title, desc, uname, chat_id))
        except sqlite3.IntegrityError:
            raise Fail("این آیدی قبلاً گرفته شده")
        if title != chat["title"] and not chat["channel"]:
            self._system_message(conn.db, chat_id, f"اسم گروه شد «{title}»")
        self._push_chat_update(conn.db, chat_id)
        return {"chat": self._chat_view(conn.db, chat_id, conn.uid)}

    def _own_message(self, conn, req, allow_owner=False):
        try:
            mid = int(req.get("message_id"))
        except (TypeError, ValueError):
            raise Fail("پیام نامعتبر")
        row = conn.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row or row["deleted"] or row["kind"] == "system":
            raise Fail("پیام پیدا نشد")
        _, member = self._require_member(conn.db, row["chat_id"], conn.uid)
        if row["sender_id"] != conn.uid and not (allow_owner and member["role"] in ("owner", "admin")):
            raise Fail("این پیام مال تو نیست")
        return row

    def op_edit(self, conn, req):
        row = self._own_message(conn, req)
        text = CTRL_RE.sub("", str(req.get("text") or "").strip())
        if not text and row["kind"] == "text":
            raise Fail("پیام نمی‌تونه خالی باشه")
        if len(text) > MAX_TEXT:
            raise Fail("پیام خیلی طولانیه")
        with conn.db:
            conn.db.execute("UPDATE messages SET body=?, edited_at=? WHERE id=?", (text, now(), row["id"]))
        msg = self._msg_by_id(conn.db, row["id"])
        self.push(self._member_ids(conn.db, row["chat_id"]), {"event": "message_updated", "message": msg},
                  exclude=conn)
        return {"message": msg}

    def op_delete(self, conn, req):
        row = self._own_message(conn, req, allow_owner=True)
        remove_upload(row["file_path"], conn.db, row["id"])
        with conn.db:
            conn.db.execute("UPDATE messages SET deleted=1, body='', file_path=NULL WHERE id=?", (row["id"],))
            was_pinned = conn.db.execute("UPDATE chats SET pinned_id=NULL WHERE id=? AND pinned_id=?",
                                         (row["chat_id"], row["id"])).rowcount
            conn.db.execute("DELETE FROM reactions WHERE message_id=?", (row["id"],))
        msg = self._msg_by_id(conn.db, row["id"])
        self.push(self._member_ids(conn.db, row["chat_id"]), {"event": "message_updated", "message": msg},
                  exclude=conn)
        self._push_chat_update(conn.db, row["chat_id"])      # the list shows the message before the deleted one
        return {"message": msg}

    def op_read(self, conn, req):
        chat_id, me = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        try:
            up_to = int(req.get("up_to"))
        except (TypeError, ValueError):
            raise Fail("پارامتر نامعتبر")
        if up_to > me["last_read_id"]:
            with conn.db:
                conn.db.execute("UPDATE members SET last_read_id=? WHERE chat_id=? AND user_id=?",
                                (up_to, chat_id, conn.uid))
            rr = conn.db.execute("SELECT read_receipts FROM users WHERE id=?", (conn.uid,)).fetchone()
            if rr is None or rr[0]:                 # reader turned receipts off -> nobody sees "read"
                ok_users = [r[0] for r in conn.db.execute(
                    "SELECT m.user_id FROM members m JOIN users u ON u.id=m.user_id WHERE m.chat_id=? AND u.read_receipts=1",
                    (chat_id,))]
                self.push(ok_users, {"event": "read", "chat_id": chat_id, "user_id": conn.uid, "up_to": up_to}, exclude=conn)
        return {}

    def op_typing(self, conn, req):
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        action = "voice" if req.get("action") == "voice" else "text"
        self.push(self._member_ids(conn.db, chat_id),
                  {"event": "typing", "chat_id": chat_id, "user_id": conn.uid, "action": action}, exclude=conn)
        return {}

    def op_get_file(self, conn, req):
        try:
            mid = int(req.get("message_id"))
        except (TypeError, ValueError):
            raise Fail("پیام نامعتبر")
        row = conn.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        if not row or row["deleted"] or row["kind"] != "file":
            raise Fail("فایل پیدا نشد")
        self._require_member(conn.db, row["chat_id"], conn.uid)
        path = resolve_upload(row["file_path"])
        if path is None:
            raise Fail("فایل روی سرور نیست")
        if path.stat().st_size > 8 * 1024 * 1024:
            raise Fail("این فایل بزرگه؛ از نسخه وب دانلودش کن")
        return {"name": row["file_name"], "data": base64.b64encode(path.read_bytes()).decode("ascii")}

    # ----------------------------------------------------- media access keys
    def _load_secret(self):
        path = Path(self.db_path).parent / "secret.key"
        try:
            data = path.read_bytes()
            if len(data) >= 32:
                return data
        except OSError:
            pass
        data = os.urandom(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data

    DRIVE_TOKEN_TTL = 60   # seconds a "open my cloud drive" link stays valid (one-time; see cloud-drive/app/routes_sso.py)

    def _load_drive_secret(self, fname="drive_sso.key"):
        """The shared secret that lets Windroida sign someone straight into Cloud Drive (ابر), without a second
        password. Written as hex text so it's easy to paste into cloud-drive/.env as CD_SSO_SECRET. Nobody else can
        mint a valid token without this exact value, so it must never be copied anywhere but that one file."""
        path = Path(self.db_path).parent / fname
        try:
            data = path.read_text(encoding="utf-8").strip()
            if len(data) >= 32:
                return data
        except OSError:
            pass
        data = secrets.token_hex(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data, encoding="utf-8")
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data

    def op_drive_token(self, conn, req):
        """A one-time, 60-second link to open Cloud Drive already signed in as this same account (see README:
        'فضای ابری'). The token only works once and only identifies this user's username - cloud-drive looks
        everything else up itself, so this never carries chat data or a password."""
        row = conn.db.execute("SELECT username FROM users WHERE id=?", (conn.uid,)).fetchone()
        username = row["username"]
        secret = getattr(self, "_drive_secret", None) or self._load_drive_secret()
        self._drive_secret = secret
        expiry = now() + self.DRIVE_TOKEN_TTL
        nonce = secrets.token_hex(8)
        sig = hmac.new(secret.encode(), f"{username}:{expiry}:{nonce}".encode(), "sha256").hexdigest()
        return {"token": f"{username}.{expiry}.{nonce}.{sig}", "port": self.drive_port}

    def op_ghab_token(self, conn, req):
        """Same one-time 60-second sign-in link as op_drive_token, but for Ghab (قاب), with Ghab's own secret."""
        row = conn.db.execute("SELECT username FROM users WHERE id=?", (conn.uid,)).fetchone()
        username = row["username"]
        secret = getattr(self, "_ghab_secret", None) or self._load_drive_secret("ghab_sso.key")
        self._ghab_secret = secret
        expiry = now() + self.DRIVE_TOKEN_TTL
        nonce = secrets.token_hex(8)
        sig = hmac.new(secret.encode(), f"{username}:{expiry}:{nonce}".encode(), "sha256").hexdigest()
        return {"token": f"{username}.{expiry}.{nonce}.{sig}", "port": self.ghab_port}

    def make_media_key(self, uid):
        """Signed, expiring key the browser puts in /media/... and /upload URLs (players can't send headers)."""
        msg = f"{uid}.{now() + MEDIA_KEY_TTL}"
        return msg + "." + hmac.new(self._secret, msg.encode(), hashlib.sha256).hexdigest()[:40]

    def check_media_key(self, key):
        try:
            uid, exp, sig = str(key).split(".")
            good = hmac.new(self._secret, f"{uid}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
            if hmac.compare_digest(sig, good) and int(exp) > now():
                return int(uid)
        except (ValueError, TypeError):
            pass
        return None

    def op_media_key(self, conn, req):
        return {"media_key": self.make_media_key(conn.uid)}

    def op_hello(self, conn, req):
        caps = req.get("caps")
        conn.caps = {str(c)[:20] for c in caps[:10]} if isinstance(caps, list) else set()
        me = conn.db.execute("SELECT username FROM users WHERE id=?", (conn.uid,)).fetchone()
        return {"media_key": self.make_media_key(conn.uid), "ice_servers": self.ice_servers,
                "max_upload": common.MAX_UPLOAD, "wall_v": common.current_user_wall(me["username"])[1] if me else 0,
                "walls": common.user_wall_versions(me["username"]) if me else []}

    # ------------------------------------------------------------ voice calls
    # Audio itself flows peer-to-peer (WebRTC); the server only rings, relays offer/answer/ICE and logs the call.
    def _call_of(self, conn, req, need_state=None):
        cid = str(req.get("call_id") or "")
        with self.calls_lock:
            call = self.calls.get(cid)
        if not call or conn.uid not in (call["caller"], call["callee"]):
            raise Fail("تماس دیگه فعال نیست")
        if need_state and call["state"] != need_state:
            raise Fail("تماس دیگه فعال نیست")
        return call

    def op_call_invite(self, conn, req):
        chat_id, _ = self._require_member(conn.db, req.get("chat_id"), conn.uid)
        chat = conn.db.execute("SELECT kind FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat["kind"] != "private":
            raise Fail("تماس فقط تو چت خصوصی ممکنه")
        peer = conn.db.execute("SELECT u.* FROM members m JOIN users u ON u.id=m.user_id "
                               "WHERE m.chat_id=? AND m.user_id!=?", (chat_id, conn.uid)).fetchone()
        if peer is None or peer["banned"]:
            raise Fail("این کاربر در دسترس نیست")
        if self._is_blocked(conn.db, peer["id"], conn.uid) or self._is_blocked(conn.db, conn.uid, peer["id"]):
            raise Fail("تماس ممکن نیست")
        me = conn.db.execute("SELECT * FROM users WHERE id=?", (conn.uid,)).fetchone()
        with self.calls_lock:
            if conn.uid in self.user_call:
                raise Fail("خودت تو یه تماس دیگه‌ای")
            if peer["id"] in self.user_call:
                raise Fail("کاربر تو تماس دیگه‌ایه")
            with self.sessions_lock:
                targets = [c for c in self.sessions.get(peer["id"], []) if "call" in c.caps and c.alive]
            if not targets:
                raise Fail("کاربر الان آنلاین نیست (یا با برنامه‌ای باز کرده که تماس نداره)")
            cid = secrets.token_hex(8)
            call = {"id": cid, "chat_id": chat_id, "caller": conn.uid, "callee": peer["id"], "caller_conn": conn,
                    "callee_conn": None, "state": "ringing", "started": None, "timer": None,
                    "video": bool(req.get("video"))}
            self.calls[cid] = call
            self.user_call[conn.uid] = self.user_call[peer["id"]] = cid
        call["timer"] = threading.Timer(CALL_RING_SECONDS, self._call_timeout, args=(cid,))
        call["timer"].daemon = True
        call["timer"].start()
        event = {"event": "call_incoming", "call_id": cid, "chat_id": chat_id, "video": call["video"],
                 "from": {"id": me["id"], "username": me["username"], "display_name": me["display_name"]}}
        for c in targets:
            c.send(event)
        return {"call_id": cid}

    def op_call_accept(self, conn, req):
        call = self._call_of(conn, req, "ringing")
        if conn.uid != call["callee"]:
            raise Fail("این تماس مال تو نیست")
        with self.calls_lock:
            if call["state"] != "ringing":
                raise Fail("تماس دیگه فعال نیست")
            call.update(state="active", callee_conn=conn, started=time.time())
        if call["timer"]:
            call["timer"].cancel()
        with self.sessions_lock:
            others = [c for c in self.sessions.get(conn.uid, []) if c is not conn]
        for c in others:                       # stop ringing on the user's other devices
            c.send({"event": "call_ended", "call_id": call["id"], "reason": "answered_elsewhere"})
        call["caller_conn"].send({"event": "call_accepted", "call_id": call["id"]})
        return {}

    def op_call_signal(self, conn, req):
        call = self._call_of(conn, req, "active")
        data = req.get("data")
        if not isinstance(data, dict) or len(json.dumps(data)) > 30000:
            raise Fail("سیگنال نامعتبر")
        peer = call["callee_conn"] if conn is call["caller_conn"] else call["caller_conn"]
        if peer is None or not peer.alive:
            raise Fail("طرف مقابل قطع شد")
        peer.send({"event": "call_signal", "call_id": call["id"], "data": data})
        return {}

    def op_call_audio(self, conn, req):
        """Audio relayed THROUGH this server (works with no internet at all, only the link to this PC).
        Fire-and-forget: the client sends {op:"call_audio", call_id, d:<base64 PCM16>, sr}."""
        call = self._call_of(conn, req, "active")
        d = req.get("d")
        if not isinstance(d, str) or len(d) > 40000:
            raise Fail("صدا نامعتبر")
        peer = call["callee_conn"] if conn is call["caller_conn"] else call["caller_conn"]
        if peer is None or not peer.alive:
            raise Fail("طرف مقابل قطع شد")
        try:
            sr = int(req.get("sr") or 16000)
        except (TypeError, ValueError):
            sr = 16000
        peer.relay({"event": "call_audio", "call_id": call["id"], "d": d, "sr": max(8000, min(sr, 48000)),
                    "c": "opus" if req.get("c") == "opus" else "pcm", "hq": 1 if req.get("hq") else 0})
        return {}

    def op_call_video(self, conn, req):
        """Video frames (small JPEGs) relayed through this server. d == "" means 'camera off'. Fire-and-forget."""
        call = self._call_of(conn, req, "active")
        d = req.get("d")
        if not isinstance(d, str) or len(d) > 120000:
            raise Fail("تصویر نامعتبر")
        peer = call["callee_conn"] if conn is call["caller_conn"] else call["caller_conn"]
        if peer is None or not peer.alive:
            raise Fail("طرف مقابل قطع شد")
        peer.relay({"event": "call_video", "call_id": call["id"], "d": d}, video=True)
        return {}

    def _call_grace(self, call, uid):
        """A participant's socket died mid-call: keep the call alive for CALL_GRACE_SECONDS, tell the other side."""
        cid = call["id"]
        peer_conn = call["callee_conn"] if uid == call["caller"] else call["caller_conn"]
        graces = call.setdefault("grace", {})
        if uid in graces:
            graces[uid].cancel()
        t = threading.Timer(CALL_GRACE_SECONDS, self._grace_expired, args=(cid, uid))
        t.daemon = True
        graces[uid] = t
        t.start()
        if peer_conn is not None and peer_conn.alive:
            peer_conn.send({"event": "call_peer", "call_id": cid, "state": "reconnecting"})

    def _grace_expired(self, cid, uid):
        with self.calls_lock:
            call = self.calls.get(cid)
            pending = bool(call and uid in call.get("grace", {}))
        if pending:
            self._finish_call(cid, "disconnected")

    def op_call_rejoin(self, conn, req):
        call = self._call_of(conn, req, "active")
        key = "caller_conn" if conn.uid == call["caller"] else "callee_conn"
        call[key] = conn
        t = call.setdefault("grace", {}).pop(conn.uid, None)
        if t:
            t.cancel()
        peer = call["callee_conn"] if conn.uid == call["caller"] else call["caller_conn"]
        if peer is not None and peer.alive:
            peer.send({"event": "call_peer", "call_id": call["id"], "state": "connected"})
        return {"peer_online": bool(peer is not None and peer.alive)}

    def op_call_end(self, conn, req):
        call = self._call_of(conn, req)
        if call["state"] == "ringing":
            reason = "cancelled" if conn.uid == call["caller"] else "rejected"
        else:
            reason = "hangup"
        self._finish_call(call["id"], reason, conn.db)
        return {}

    def _call_timeout(self, cid):
        with self.calls_lock:
            call = self.calls.get(cid)
            ringing = bool(call and call["state"] == "ringing")
        if ringing:
            self._finish_call(cid, "missed")

    def _finish_call(self, cid, reason, db=None):
        with self.calls_lock:
            call = self.calls.pop(cid, None)
            if call is None:
                return
            for u in (call["caller"], call["callee"]):
                if self.user_call.get(u) == cid:
                    self.user_call.pop(u, None)
        if call["timer"]:
            call["timer"].cancel()
        for t in call.get("grace", {}).values():
            t.cancel()
        self.push([call["caller"], call["callee"]], {"event": "call_ended", "call_id": cid, "reason": reason})
        own = db is None
        db = db or connect_db(self.db_path)
        try:
            name = lambda uid: (db.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone() or ["—"])[0]
            if call["started"]:
                secs = int(time.time() - call["started"])
                text = f"📞 تماس صوتی · {secs // 60:02d}:{secs % 60:02d}".translate(FA_DIGITS)
            elif reason == "rejected":
                text = f"📞 {name(call['callee'])} تماس رو رد کرد"
            elif reason == "cancelled":
                text = f"📞 {name(call['caller'])} تماس رو لغو کرد"
            else:
                text = f"📞 تماس بی‌پاسخ از {name(call['caller'])}"
            self._system_message(db, call["chat_id"], text)
        finally:
            if own:
                db.close()


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    ap = argparse.ArgumentParser(description="Windroida server")
    ap.add_argument("--host", default=common.DEFAULT_HOST,
                    help="127.0.0.1 = only this PC, 0.0.0.0 = whole network")
    ap.add_argument("--port", type=int, default=common.DEFAULT_PORT)
    ap.add_argument("--http", action="store_true", help="plain http/no encryption (default is https)")
    ap.add_argument("--tls", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--web", action="store_true", help="also serve the browser UI (open it in Chrome)")
    ap.add_argument("--web-port", type=int, default=8081)
    args = ap.parse_args()
    srv = WindroidaServer(args.host, args.port, not args.http, web_port=args.web_port if args.web else None)
    srv.start()
    if args.web and not os.environ.get("WINDROIDA_LAUNCHER"):
        scheme = "https" if srv.tls else "http"
        print(f"\n  Web UI:  {scheme}://127.0.0.1:{args.web_port}   |   on your network: {scheme}://{lan_ip()}:{args.web_port}\n", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
