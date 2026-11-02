"""Web Push (VAPID) without extra libraries: only `cryptography`, which Windroida already uses for its certificate.

The push carries NO message text (privacy: Google/Apple/Mozilla push services only see "something arrived"); the service worker
shows a generic "پیام جدید" notification and the app loads the real messages when you open it.
Endpoints are only accepted for the known push services, so a user can't make the server call arbitrary addresses (SSRF)."""
import base64
import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ALLOWED_SUFFIXES = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com", "web.push.apple.com")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def allowed_endpoint(url) -> bool:
    try:
        u = urllib.parse.urlparse(str(url))
    except ValueError:
        return False
    h = (u.hostname or "").lower()
    return u.scheme == "https" and len(str(url)) < 700 and any(h == s or h.endswith("." + s) for s in ALLOWED_SUFFIXES)


def load_vapid(data_dir):
    """returns (private_key_object, public_key_b64url); creates data/vapid.json on first use"""
    p = data_dir / "vapid.json"
    try:
        d = json.loads(p.read_text())
        key = serialization.load_pem_private_key(d["priv"].encode(), None)
    except Exception:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        pub = b64u(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        p.write_text(json.dumps({"priv": pem, "pub": pub}))
        try:
            p.chmod(0o600)
        except OSError:
            pass
        return key, pub
    return key, d["pub"]


def make_jwt(key, endpoint, subject="mailto:admin@windroida.local") -> str:
    u = urllib.parse.urlparse(endpoint)
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": subject}).encode())
    msg = f"{head}.{claims}".encode()
    r, s = decode_dss_signature(key.sign(msg, ec.ECDSA(hashes.SHA256())))
    return f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


class Pusher:
    """background sender: never blocks the chat server; dead subscriptions (404/410) are reported to `on_gone`"""

    def __init__(self, key, pub, on_gone, check=allowed_endpoint):
        self.key, self.pub, self.on_gone, self.check = key, pub, on_gone, check
        self.q = queue.Queue(maxsize=500)
        for _ in range(2):
            threading.Thread(target=self._loop, daemon=True).start()

    def send(self, endpoint):
        try:
            self.q.put_nowait(endpoint)
        except queue.Full:
            pass

    def deliver(self, endpoint) -> int:
        if not self.check(endpoint):
            return 0
        req = urllib.request.Request(endpoint, data=b"", method="POST", headers={
            "Authorization": f"vapid t={make_jwt(self.key, endpoint)}, k={self.pub}",
            "TTL": "86400", "Urgency": "high", "Content-Length": "0"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            return -1

    def _loop(self):
        while True:
            ep = self.q.get()
            try:
                code = self.deliver(ep)
                if code in (404, 410):
                    self.on_gone(ep)
            except Exception:
                pass
