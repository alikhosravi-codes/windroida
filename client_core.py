"""Network client used by the messenger UI (no Tk imports here, so it can be tested headless)."""
import hashlib
import json
import socket
import ssl
import threading
import queue
import itertools

from common import BASE_DIR, DEFAULT_HOST, DEFAULT_PORT, encode_frame, recv_frame

CONFIG_PATH = BASE_DIR / "client_config.json"


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


class CertChanged(Exception):
    pass


class Client:
    """Thread-based client. Replies/events are delivered as callables on `inbox`
    (the UI drains it from the Tk main loop), so no widget is ever touched from a worker thread."""

    def __init__(self):
        self.sock = None
        self.inbox = queue.Queue()
        self.on_event = None          # callable(event_dict), run via inbox
        self.on_disconnect = None     # callable(), run via inbox
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.connected = False

    # ------------------------------------------------------------ connect
    def connect(self, host=DEFAULT_HOST, port=DEFAULT_PORT, tls=False, pinned_fingerprint=None, timeout=8):
        """Returns the server certificate fingerprint when tls=True (for trust-on-first-use pinning)."""
        self.close()
        raw = socket.create_connection((host, int(port)), timeout=timeout)
        fingerprint = None
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE     # identity is checked by fingerprint pinning below
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            raw = ctx.wrap_socket(raw)
            fingerprint = hashlib.sha256(raw.getpeercert(binary_form=True)).hexdigest()
            if pinned_fingerprint and pinned_fingerprint != fingerprint:
                raw.close()
                raise CertChanged("گواهی سرور عوض شده! ممکنه کسی وسط راه باشه.")
        raw.settimeout(None)
        self.sock = raw
        self.connected = True
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._pinger, daemon=True).start()
        return fingerprint

    def close(self):
        self.connected = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    # --------------------------------------------------------- internals
    def _reader(self):
        sock = self.sock
        try:
            while True:
                msg = recv_frame(sock)
                if "event" in msg:
                    if self.on_event:
                        self.inbox.put(lambda m=msg: self.on_event(m))
                    continue
                with self._lock:
                    waiter = self._pending.pop(msg.get("id"), None)
                if waiter:
                    waiter(msg)
        except Exception:
            pass
        finally:
            if sock is self.sock:
                self.connected = False
                with self._lock:
                    pending, self._pending = list(self._pending.values()), {}
                for w in pending:
                    w({"ok": False, "error": "ارتباط با سرور قطع شد"})
                if self.on_disconnect:
                    self.inbox.put(self.on_disconnect)

    def _pinger(self):
        import time
        while self.connected:
            time.sleep(25)
            if self.connected:
                self.request("ping")

    # --------------------------------------------------------------- API
    def request(self, op, callback=None, **fields):
        """Send a request. callback(resp) is queued onto `inbox` (UI thread)."""
        rid = next(self._ids)

        def deliver(resp):
            if callback:
                self.inbox.put(lambda: callback(resp))

        if not self.connected:
            deliver({"ok": False, "error": "به سرور وصل نیستی"})
            return rid
        with self._lock:
            self._pending[rid] = deliver
        try:
            data = encode_frame({"id": rid, "op": op, **fields})
            with self._send_lock:
                self.sock.sendall(data)
        except Exception as e:
            with self._lock:
                self._pending.pop(rid, None)
            deliver({"ok": False, "error": "ارسال ناموفق: " + str(e)})
        return rid

    def call(self, op, timeout=15, **fields):
        """Blocking variant (for tests / scripts; never call from the Tk thread)."""
        done = threading.Event()
        box = {}

        def waiter(resp):
            box["r"] = resp
            done.set()

        rid = next(self._ids)
        if not self.connected:
            return {"ok": False, "error": "به سرور وصل نیستی"}
        with self._lock:
            self._pending[rid] = waiter
        with self._send_lock:
            self.sock.sendall(encode_frame({"id": rid, "op": op, **fields}))
        if not done.wait(timeout):
            return {"ok": False, "error": "timeout"}
        return box["r"]
