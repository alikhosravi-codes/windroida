"""Shared helpers: wire protocol, validation, password hashing, DB schema."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import struct
import time
import unicodedata
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
FILES_DIR = DATA_DIR / "files"          # legacy flat storage (auto-migrated)
UPLOADS_DIR = DATA_DIR / "uploads"      # clean structure: uploads/<user>/<chat>/<id>_<name>
ROOMAUDIO_DIR = DATA_DIR / "roomaudio"    # songs for watch/listen rooms: drop files (or sub-folders = playlists) here
WALLPAPERS_DIR = DATA_DIR / "wallpapers"  # chat backgrounds the admin drops in; every user can pick from them
AVATARS_DIR = DATA_DIR / "avatars"      # profile photos live OUTSIDE uploads/, so cleaning files never touches them
DB_PATH = DATA_DIR / "windroida.db"


def _adopt_old_database():
    """The app used to be called Pygram: keep existing users/messages by renaming its database once."""
    old = DATA_DIR / ("pyg" + "ram.db")
    if old.exists() and not DB_PATH.exists():
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(old) + suffix)
            if src.exists():
                try:
                    src.replace(Path(str(DB_PATH) + suffix))
                except OSError:
                    pass


_adopt_old_database()

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

MAX_FRAME = 12 * 1024 * 1024      # max size of one network frame
MAX_FILE = 5 * 1024 * 1024        # max attachment size over the TCP/base64 protocol (desktop app)
MAX_UPLOAD = 200 * 1024 * 1024    # max attachment size over HTTP upload (web app, streamed to disk)
SESSION_TTL = 365 * 24 * 3600     # "stay logged in" sessions (sliding window)
MAX_TEXT = 4000                   # max message length
MAX_AVATAR = 8 * 1024 * 1024      # max profile photo upload (it is re-encoded to ~512px anyway)
REACTIONS = ["👍", "❤️", "🔥", "🎉", "😂", "😮", "😢", "🙏", "👎", "💯", "🤔", "👏"]

USERNAME_RE = re.compile(r"^(?=.*[A-Za-z0-9])[A-Za-z0-9_]{3,32}$")      # free like Telegram: letters, digits, _ (3-32)

SECURITY_QUESTIONS = [          # only shown for names that do not exist (decoy, so nobody can find out which usernames exist); same style as the real choices
    "یه کلمه یا جمله مخفی که فقط خودم می‌دونم",
    "اسم اولین کتابی که یادمه و خوندمش؟",
    "اسم کوچه یا جایی که برای من خاطره خاصی داره؟",
    "یه عبارت شخصی که هیچ‌وقت جایی نگفتم؟",
]


# --------------------------------------------------------------------------
# Wire protocol: 4-byte big-endian length + UTF-8 JSON
# --------------------------------------------------------------------------
def encode_frame(obj) -> bytes:
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("frame too large")
    return struct.pack(">I", len(data)) + data


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock):
    (length,) = struct.unpack(">I", _recv_exact(sock, 4))
    if length > MAX_FRAME:
        raise ConnectionError("frame too large")
    return json.loads(_recv_exact(sock, length).decode("utf-8"))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def normalize_answer(text: str) -> str:
    """Make security answers forgiving: case, spaces, Arabic/Persian letters."""
    t = (text or "").strip().lower()
    t = t.replace("ي", "ی").replace("ك", "ک").replace("‌", " ")
    t = re.sub(r"\s+", " ", t)
    return t


def check_password_rules(password: str, username: str = ""):
    """Return an error message (Persian) or None."""
    if not isinstance(password, str) or len(password) < 8:
        return "رمز باید حداقل ۸ کاراکتر باشه"
    if len(password) > 128:
        return "رمز خیلی طولانیه"
    if username and username.lower() in password.lower():
        return "رمز نباید شامل نام کاربری باشه"
    if password.isdigit() or password.isalpha():
        return "رمز باید ترکیبی از حرف و عدد (یا نماد) باشه"
    if password.lower() in {"12345678a", "password1", "qwerty123", "abc12345", "a1234567"}:
        return "این رمز خیلی رایجه، یکی قوی‌تر بذار"
    return None


# --------------------------------------------------------------------------
# Hashing (scrypt, per-secret random salt)
# --------------------------------------------------------------------------
def hash_secret(secret: str, salt: bytes = None):
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(secret.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt, digest


def verify_secret(secret: str, salt: bytes, expected: bytes) -> bool:
    _, digest = hash_secret(secret, salt)
    return hmac.compare_digest(digest, expected)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT NOT NULL,
    bio           TEXT NOT NULL DEFAULT '',
    pw_salt       BLOB NOT NULL,
    pw_hash       BLOB NOT NULL,
    sec_question  TEXT NOT NULL,
    sec_salt      BLOB NOT NULL,
    sec_hash      BLOB NOT NULL,
    created_at    INTEGER NOT NULL,
    last_seen     INTEGER,
    banned        INTEGER NOT NULL DEFAULT 0,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('private','group')),
    title      TEXT,
    pair_key   TEXT UNIQUE,
    owner_id   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    chat_id      INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role         TEXT NOT NULL DEFAULT 'member',
    joined_at    INTEGER NOT NULL,
    last_read_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    sender_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind       TEXT NOT NULL DEFAULT 'text' CHECK (kind IN ('text','file','system')),
    body       TEXT NOT NULL DEFAULT '',
    reply_to   INTEGER,
    file_name  TEXT,
    file_path  TEXT,
    file_size  INTEGER,
    created_at INTEGER NOT NULL,
    edited_at  INTEGER,
    deleted    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash BLOB NOT NULL UNIQUE,
    device     TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    last_used  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reactions (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    emoji      TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS voice_plays (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    played_at  INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS push_subs (
    endpoint TEXT PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (user_id, blocked_id)
);
CREATE TABLE IF NOT EXISTS purge_files (
    rel TEXT PRIMARY KEY,
    due INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rooms_log (
    id         TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    chat_id    INTEGER,
    title      TEXT NOT NULL DEFAULT '',
    owner_id   INTEGER,
    owner_name TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    last_at    INTEGER NOT NULL DEFAULT 0,
    closed_at  INTEGER,
    peak       INTEGER NOT NULL DEFAULT 0,
    members    TEXT NOT NULL DEFAULT '',
    cur        TEXT NOT NULL DEFAULT '',
    songs      INTEGER NOT NULL DEFAULT 0,
    invites    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_members_user ON members(user_id);
"""


# columns added after the first release (old databases are upgraded in place, nothing is lost)
MIGRATIONS = [
    ("users", "avatar", "TEXT"),                               # path of the current profile photo under avatars/
    ("users", "avatar_at", "INTEGER NOT NULL DEFAULT 0"),      # version (ms) of the photo; 0 = none
    ("chats", "avatar", "TEXT"),
    ("chats", "avatar_at", "INTEGER NOT NULL DEFAULT 0"),
    ("chats", "description", "TEXT NOT NULL DEFAULT ''"),
    ("chats", "pinned_id", "INTEGER"),                         # pinned message
    ("members", "muted", "INTEGER NOT NULL DEFAULT 0"),
    ("members", "fav", "INTEGER NOT NULL DEFAULT 0"),          # chat pinned to the top of the list
    ("chats", "username", "TEXT"),                              # public @id of a group (unique, optional)
    ("chats", "invite", "TEXT"),                                # secret token of the invite link
    ("chats", "channel", "INTEGER NOT NULL DEFAULT 0"),        # 1 = announcement channel (only the admin posts)
    ("users", "priv_seen", "TEXT NOT NULL DEFAULT 'all'"),     # who sees my last seen / online: all | contacts | nobody
    ("users", "read_receipts", "INTEGER NOT NULL DEFAULT 1"),  # 0 = I don't send (and don't see) "read" ticks
    ("messages", "meta", "TEXT"),                              # JSON: voice {dur, wave}, forwarded-from, ...
]


def _migrate(conn):
    for table, col, decl in MIGRATIONS:
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:       # another process (server/datasheet) added it a moment ago
                pass
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chats_username ON chats(username COLLATE NOCASE) "
                     "WHERE username IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_chats_invite ON chats(invite) WHERE invite IS NOT NULL")
    except sqlite3.OperationalError:
        pass
    conn.commit()


def connect_db(path=DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    WALLPAPERS_DIR.mkdir(parents=True, exist_ok=True)
    ROOMAUDIO_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


MIN_FREE_MB = int(os.environ.get("WINDROIDA_MIN_FREE_MB", "300") or 300)   # refuse uploads when the disk has less free space than this


def disk_ok(extra: int = 0) -> bool:
    try:
        import shutil as _sh
        return _sh.disk_usage(str(DATA_DIR)).free - extra > MIN_FREE_MB * 1048576
    except OSError:
        return True


def now() -> int:
    return int(time.time())


# --------------------------------------------------------------------------
# Upload storage: data/uploads/<kind>_<chatid>_<label>/<YYYY-MM>/<messageid>_<filename>
# --------------------------------------------------------------------------
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_name(name, limit=80, default="file") -> str:
    """Filesystem-safe (Windows/Linux/macOS) but human-readable, Persian letters are kept."""
    name = _FORBIDDEN.sub("_", unicodedata.normalize("NFC", str(name or ""))).strip(" .")
    base, ext = os.path.splitext(name)
    ext = ext[:12]
    if base.lower() in _RESERVED:
        base = "_" + base
    base = base[:max(1, limit - len(ext))].strip(" .")
    return (base or default) + ext


def chat_label(db, chat_id):
    c = db.execute("SELECT kind, title FROM chats WHERE id=?", (chat_id,)).fetchone()
    if not c:
        return "chat", str(chat_id)
    if c["kind"] == "group":
        return "group", c["title"] or "group"
    names = [r[0] for r in db.execute("SELECT u.username FROM members m JOIN users u ON u.id=m.user_id "
                                      "WHERE m.chat_id=? ORDER BY u.username", (chat_id,))]
    return "private", "-".join(names) or "chat"


def upload_relpath(db, chat_id, message_id, filename, sender_id) -> str:
    """uploads/<username>/<chat folder>/<messageid>_<filename>  (one folder per user)."""
    kind, label = chat_label(db, chat_id)
    row = db.execute("SELECT username FROM users WHERE id=?", (sender_id,)).fetchone()
    owner = safe_name(row["username"], 30, "user") if row else "_deleted-user"
    chat = f"{kind}_{chat_id}_{safe_name(label, 40, 'chat').replace(' ', '-')}"
    return f"{owner}/{chat}/{message_id}_{safe_name(filename)}"


def resolve_upload(rel):
    """Absolute path of a stored upload (new or legacy layout), or None. Blocks path traversal."""
    if not rel:
        return None
    for base in (UPLOADS_DIR, FILES_DIR):
        p = (base / rel).resolve()
        try:
            p.relative_to(base.resolve())
        except ValueError:
            continue
        if p.is_file():
            return p
    return None


def thumb_rel(rel):
    return (rel or "") + ".thumb.jpg"


def file_in_use(db, rel, except_mid=None):
    """True if some other (not deleted) message still points at this stored file (forwards share one file)."""
    if not rel:
        return False
    row = db.execute("SELECT 1 FROM messages WHERE file_path=? AND id IS NOT ? LIMIT 1", (rel, except_mid)).fetchone()
    return row is not None


def remove_upload(rel, db=None, except_mid=None):
    """Delete a stored file - unless another message (a forward) still uses it."""
    if db is not None and file_in_use(db, rel, except_mid):
        return
    p = resolve_upload(rel)
    if not p:
        return
    for q in (p, resolve_upload(thumb_rel(rel))):
        try:
            if q:
                q.unlink()
        except OSError:
            pass


def migrate_legacy_uploads(db) -> int:
    """Move files stored in an older layout (data/files/<uuid> ...) into uploads/<user>/<chat>/. Never deletes."""
    moved = 0
    rows = db.execute("SELECT id, chat_id, sender_id, file_name, file_path FROM messages "
                      "WHERE file_path IS NOT NULL").fetchall()
    for r in rows:
        rel = upload_relpath(db, r["chat_id"], r["id"], r["file_name"], r["sender_id"])
        if r["file_path"] == rel:
            continue
        if db.execute("SELECT COUNT(*) FROM messages WHERE file_path=?", (r["file_path"],)).fetchone()[0] > 1:
            continue                      # a forwarded file is shared by several messages: leave it where it is
        src = resolve_upload(r["file_path"])
        dest = UPLOADS_DIR / rel
        if src is None or dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            src.replace(dest)
        except OSError:
            continue
        with db:
            db.execute("UPDATE messages SET file_path=? WHERE id=?", (rel, r["id"]))
        moved += 1
    return moved


# --------------------------------------------------------------------------
# Media types: what the browser may play/show inline (everything else is download-only)
# --------------------------------------------------------------------------
_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".avif": "image/avif",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".flac": "audio/flac", ".weba": "audio/webm",
}


def media_info(name):
    """-> (mime, category, inline_ok). category is 'image' | 'video' | 'audio' | 'file'.
    SVG/HTML/PDF and anything unknown are never served inline (they could run scripts on our origin)."""
    ext = os.path.splitext(str(name or ""))[1].lower()
    mime = _EXT_MIME.get(ext)
    if not mime:
        return "application/octet-stream", "file", False
    return mime, mime.split("/")[0], True


# --------------------------------------------------------------------------
# Profile photos: data/avatars/<username>/<YYYYmmdd-HHMMSS>.jpg (+ _s.jpg thumbnail). Old photos are KEPT.
# With Pillow every upload is decoded and re-encoded (square crop, EXIF/metadata stripped, no active content).
# Without Pillow only plain JPEG/PNG files are accepted, as they are.
# --------------------------------------------------------------------------
try:
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 50_000_000          # refuse decompression bombs
    HAVE_PIL = True
except Exception:                                # pragma: no cover - depends on the machine
    Image = ImageOps = None
    HAVE_PIL = False

BIG_PX, SMALL_PX = 512, 128


def sniff_image(raw: bytes):
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return None


def process_avatar(raw: bytes):
    """-> (big_bytes, small_bytes, ext). Raises ValueError with a Persian message for bad images."""
    if not raw:
        raise ValueError("عکس خالیه")
    if len(raw) > MAX_AVATAR:
        raise ValueError(f"حجم عکس بیشتر از {MAX_AVATAR // (1024 * 1024)} مگابایته")
    if not HAVE_PIL:
        ext = sniff_image(raw)
        if not ext or len(raw) > 1_500_000:
            raise ValueError("فقط عکس JPEG/PNG تا ۱.۵ مگابایت قبوله (برای پشتیبانی از همه فرمت‌ها Pillow نصب کن)")
        return raw, raw, ext
    import io
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        side = min(BIG_PX, min(im.size))
        big = ImageOps.fit(im, (side, side), Image.LANCZOS, centering=(0.5, 0.5))
        small = big.resize((min(SMALL_PX, side), min(SMALL_PX, side)), Image.LANCZOS)
        out = []
        for img in (big, small):
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88, optimize=True, progressive=True)
            out.append(buf.getvalue())
        return out[0], out[1], ".jpg"
    except Exception:
        raise ValueError("این فایل عکس معتبر نیست")


def _userwalls_dir():
    return DATA_DIR / "userwalls"        # every user's own chat background: userwalls/<user>/<time>.jpg (old ones are kept)


def store_user_wall(username: str, raw: bytes) -> int:
    """Save one user's chat background in HIS folder. Returns its version (milliseconds)."""
    if not raw:
        raise ValueError("عکس خالیه")
    if len(raw) > MAX_AVATAR:
        raise ValueError(f"حجم عکس بیشتر از {MAX_AVATAR // (1024 * 1024)} مگابایته")
    if HAVE_PIL:
        import io
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
            im = ImageOps.exif_transpose(im).convert("RGB")
            k = min(1.0, 1600 / max(im.size))
            if k < 1:
                im = im.resize((max(1, round(im.size[0] * k)), max(1, round(im.size[1] * k))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82, optimize=True, progressive=True)
            data = buf.getvalue()
        except Exception:
            raise ValueError("این فایل عکس معتبر نیست")
    else:
        if not sniff_image(raw) or len(raw) > 1_500_000:
            raise ValueError("فقط عکس JPEG/PNG تا ۱.۵ مگابایت قبوله")
        data = raw
    if len(user_wall_versions(username)) >= MAX_USER_WALLS:
        raise ValueError(f"حداکثر {MAX_USER_WALLS} پس‌زمینه می‌تونی داشته باشی؛ اول یکی رو حذف کن")
    d = _userwalls_dir() / avatar_folder_for_user(username)
    d.mkdir(parents=True, exist_ok=True)
    v = int(time.time() * 1000)
    while (d / f"{v}.img").exists():
        v += 1
    (d / f"{v}.img").write_bytes(data)
    return v


MAX_USER_WALLS = 12      # every user may keep up to 12 backgrounds; nothing is removed automatically


def user_wall_versions(username: str) -> list:
    """versions (ms) of all backgrounds of this user, newest first."""
    d = _userwalls_dir() / avatar_folder_for_user(username)
    if not d.is_dir():
        return []
    return sorted((int(f.stem) for f in d.iterdir() if f.suffix == ".img" and f.stem.isdigit()), reverse=True)


def current_user_wall(username: str, v: int = 0):
    """(path, version): the background with version v, or (v=0) the newest one; (None, 0) when there is none."""
    d = _userwalls_dir() / avatar_folder_for_user(username)
    vs = user_wall_versions(username)
    if v:
        f = d / f"{int(v)}.img"
        return (f, int(v)) if int(v) in vs else (None, 0)
    return (d / f"{vs[0]}.img", vs[0]) if vs else (None, 0)


def retire_user_wall(username: str, v: int = 0) -> int:
    """The user deleted one background (v) or all of them (v=0): the files are really deleted (they vanish from the datasheet too).
    Older copies that earlier versions parked in <folder>/_removed/ go as well; an empty folder is removed."""
    d = _userwalls_dir() / avatar_folder_for_user(username)
    n = 0
    if d.is_dir():
        for f in list(d.iterdir()):
            if f.suffix == ".img" and (not v or f.stem == str(int(v))):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        rm = d / "_removed"
        if rm.is_dir():
            for f in list(rm.iterdir()):
                try:
                    f.unlink()
                except OSError:
                    pass
            try:
                rm.rmdir()
            except OSError:
                pass
        try:
            d.rmdir()                      # only succeeds when nothing is left inside
        except OSError:
            pass
    return n


def small_avatar_rel(rel: str) -> str:
    stem, ext = os.path.splitext(rel)
    return f"{stem}_s{ext}"


def store_avatar(folder: str, raw: bytes) -> str:
    """Process and save a photo into avatars/<folder>/. Returns the relative path of the big image."""
    big, small, ext = process_avatar(raw)
    d = AVATARS_DIR / folder
    d.mkdir(parents=True, exist_ok=True)
    stamp, n = time.strftime("%Y%m%d-%H%M%S"), 0
    while (d / (f"{stamp}{'' if not n else '-' + str(n)}{ext}")).exists():
        n += 1
    name = f"{stamp}{'' if not n else '-' + str(n)}{ext}"
    (d / name).write_bytes(big)
    (d / small_avatar_rel(name)).write_bytes(small)
    return f"{folder}/{name}"


def resolve_avatar(rel):
    """Absolute path of a stored avatar, or None (also blocks path traversal)."""
    if not rel:
        return None
    p = (AVATARS_DIR / rel).resolve()
    try:
        p.relative_to(AVATARS_DIR.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


_PIC_RE = re.compile(r"^[0-9][0-9\-]*\.(jpg|png|webp)$")


def avatar_pics(folder: str):
    """Names of the kept profile photos of one user/group, newest first (removed ones live in _removed/)."""
    d = AVATARS_DIR / folder
    if not d.is_dir():
        return []
    return sorted((f.name for f in d.iterdir() if f.is_file() and _PIC_RE.match(f.name) and not f.stem.endswith("_s")),
                  reverse=True)


def valid_pic_name(name: str) -> bool:
    return bool(_PIC_RE.match(name or "")) and not os.path.splitext(name)[0].endswith("_s")


def retire_avatar(rel):
    """User removed the photo: move it (and its small copy) into <folder>/_removed/ - kept on disk, never deleted,
    but no longer shown in the photo gallery."""
    big = resolve_avatar(rel)
    if not big:
        return
    dest = big.parent / "_removed"
    dest.mkdir(exist_ok=True)
    for p in (big, resolve_avatar(small_avatar_rel(rel))):
        if p:
            try:
                p.replace(dest / p.name)
            except OSError:
                pass


def avatar_folder_for_user(username: str) -> str:
    return safe_name(username, 32, "user")


def avatar_folder_for_chat(chat_id: int, title: str) -> str:
    g = AVATARS_DIR / "_groups"
    if g.is_dir():                           # keep using the folder the group already has (even if it was renamed)
        for d in sorted(g.glob(f"{chat_id}_*")):
            if d.is_dir():
                return f"_groups/{d.name}"
    return f"_groups/{chat_id}_{safe_name(title or 'group', 30, 'group').replace(' ', '-')}"


# --------------------------------------------------------------------------
# Chat wallpapers: data/wallpapers/<name>.jpg|png|webp (put them there by hand or with the datasheet).
WALL_EXTS = (".jpg", ".jpeg", ".png", ".webp")
WALL_MAX = 12 * 1048576


def valid_wall_name(name: str) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 120 and "/" not in name and "\\" not in name
            and not name.startswith(".") and name.lower().endswith(WALL_EXTS))


def list_wallpapers():
    """[(name, mtime, size)] sorted by name"""
    out = []
    try:
        for p in sorted(WALLPAPERS_DIR.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and valid_wall_name(p.name):
                st = p.stat()
                if 0 < st.st_size <= WALL_MAX:
                    out.append((p.name, int(st.st_mtime), st.st_size))
    except OSError:
        pass
    return out[:80]


def resolve_wallpaper(name: str, small: bool = False):
    """Path of a wallpaper (or its cached 360px preview), None when missing/invalid."""
    if not valid_wall_name(name):
        return None
    p = WALLPAPERS_DIR / name
    if not p.is_file():
        return None
    if not small:
        return p
    t = WALLPAPERS_DIR / "_thumbs" / (name + ".jpg")
    try:
        if t.is_file() and t.stat().st_mtime >= p.stat().st_mtime:
            return t
        from PIL import Image
        t.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((360, 360))
            im.save(t, "JPEG", quality=70)
        return t
    except Exception:
        return p


# --------------------------------------------------------------------------
# Room songs: data/roomaudio/[playlist folder/]song.mp3 ; songs uploaded from inside a room go to roomaudio/_uploads/<user>/
AUDIO_EXTS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".weba")
SONG_MAX = 80 * 1048576
UPLOADS_FOLDER = "_uploads"
GROUP_FILE_TTL_HOURS = float(os.environ.get("WINDROIDA_GROUP_FILE_TTL_HOURS", "4") or 4)   # files of a deleted group are removed this long after deletion
SONG_TTL_HOURS = float(os.environ.get("WINDROIDA_SONG_TTL_HOURS", "0") or 0)   # default 0 = never auto-delete uploads (only the owner/admin deletes them)


def valid_song_rel(rel: str) -> bool:
    if not isinstance(rel, str) or not rel or len(rel) > 260 or "\\" in rel or rel.startswith("/"):
        return False
    parts = rel.split("/")
    return (len(parts) <= 3 and all(p and p not in (".", "..") and not p.startswith(".") for p in parts)
            and parts[-1].lower().endswith(AUDIO_EXTS))


def resolve_roomaudio(rel: str):
    if not valid_song_rel(rel):
        return None
    p = ROOMAUDIO_DIR / rel
    try:
        p.resolve().relative_to(ROOMAUDIO_DIR.resolve())
    except ValueError:
        return None
    if not p.is_file():
        return None
    return p


def song_folder(username: str) -> str:
    """Per-user private folder for songs uploaded from rooms: _uploads/<username>"""
    return f"{UPLOADS_FOLDER}/" + re.sub(r"[^A-Za-z0-9_]", "_", username or "")[:40]


def song_owner_folder(rel: str):
    """'_uploads/alice/x.mp3' -> '_uploads/alice' ; None for library songs"""
    parts = rel.split("/")
    return "/".join(parts[:2]) if len(parts) >= 3 and parts[0] == UPLOADS_FOLDER else None


def list_roomaudio(username=None):
    """Songs visible to `username`: the admin's library + that user's OWN uploads (other people's uploads stay private
    until someone puts them into a room queue). username=None -> everything (datasheet)."""
    mine = song_folder(username) if username else None
    out = []
    try:
        for p in ROOMAUDIO_DIR.rglob("*"):
            if len(out) >= 800:
                break
            if not p.is_file():
                continue
            rel = p.relative_to(ROOMAUDIO_DIR).as_posix()
            if not valid_song_rel(rel):
                continue
            of = song_owner_folder(rel)
            if username is not None and of is not None and of != mine:
                continue
            st = p.stat()
            if 0 < st.st_size <= SONG_MAX:
                folder = rel.rsplit("/", 1)[0] if "/" in rel else ""
                out.append({"rel": rel, "name": p.stem, "folder": folder, "size": st.st_size, "mtime": int(st.st_mtime),
                            "mine": of is not None and of == mine})
    except OSError:
        pass
    out.sort(key=lambda x: (x["folder"].lower(), x["name"].lower()))
    return out


def user_song_usage(username: str):
    d = ROOMAUDIO_DIR / song_folder(username)
    n = size = 0
    if d.is_dir():
        for p in d.iterdir():
            if p.is_file():
                n += 1
                size += p.stat().st_size
    return n, size


SONG_QUOTA_BYTES = 300 * 1048576
SONG_QUOTA_COUNT = 60


def _looks_like_audio(raw: bytes, ext: str) -> bool:
    h = raw[:16]
    if h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):      # mp3 / aac (ADTS)
        return True
    return (h[:4] in (b"OggS", b"fLaC", b"RIFF") or h[4:8] == b"ftyp" or h[:4] == b"\x1a\x45\xdf\xa3")


def store_roomaudio(username: str, filename: str, raw: bytes) -> str:
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", os.path.basename(filename or "song")).strip(" .") or "song"
    stem, ext = os.path.splitext(base)
    if ext.lower() not in AUDIO_EXTS:
        raise ValueError("فقط فایل صوتی (mp3, m4a, ogg, wav, flac…)")
    if not _looks_like_audio(raw, ext.lower()):
        raise ValueError("این فایل صوتی معتبر به نظر نمی‌رسه")
    n, size = user_song_usage(username)
    if n >= SONG_QUOTA_COUNT or size + len(raw) > SONG_QUOTA_BYTES:
        raise ValueError("سهمیه آهنگ‌هات پره (۶۰ آهنگ یا ۳۰۰ مگابایت)؛ چندتا از قبلی‌ها رو پاک کن")
    d = ROOMAUDIO_DIR / song_folder(username)
    d.mkdir(parents=True, exist_ok=True)
    dest, k = d / (stem[:90] + ext), 1
    while dest.exists():
        dest = d / f"{stem[:90]}_{k}{ext}"
        k += 1
    dest.write_bytes(raw)
    return dest.relative_to(ROOMAUDIO_DIR).as_posix()
