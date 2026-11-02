"""One-click launcher (double-click start.bat).

Starts everything: server + web UI (https), the admin datasheet, Cloud Drive (فضای ابری), and opens the web UI
in your browser. Friends on your Wi-Fi/LAN just open the "Friends" address printed below in Chrome.
Missing Python libraries (Pillow, cryptography, and Cloud Drive's Flask/waitress) are installed automatically.

Options:  --local        only this PC (don't open to the network)
          --http         plain http instead of https (voice recording/calls then only work on this PC)
          --no-datasheet / --no-install / --no-drive / --no-ghab
          --browser      also open the web UI in the browser (off by default)
"""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

here = Path(__file__).resolve().parent
os.environ["WINDROIDA_LAUNCHER"] = "1"
sys.path.insert(0, str(here))
from server import lan_ip  # noqa: E402

PORT, WEB_PORT, DRIVE_PORT = 8765, 8081, int(os.environ.get("WINDROIDA_DRIVE_PORT") or 8082)
GHAB_PORT = int(os.environ.get("WINDROIDA_GHAB_PORT") or 8084)
flags = set(a for a in sys.argv[1:] if a.startswith("--"))


def spawn(script, *args):
    return subprocess.Popen([sys.executable, str(here / script), *args], cwd=str(here))


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.3).close()
        return True
    except OSError:
        return False


REQUIREMENTS = [("PIL", "Pillow", "profile photos"), ("cryptography", "cryptography", "https certificate")]


def ensure_libs():
    """Try to import each library; if missing pip-install it (several fallbacks). Never fatal."""
    import importlib
    missing = []
    for mod, pkg, why in REQUIREMENTS:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append((mod, pkg, why))
    if not missing:
        return
    print("  Installing needed libraries: " + ", ".join(p for _, p, _ in missing) + " ...", flush=True)
    for mod, pkg, why in missing:
        done = False
        wheels = here / "wheels"        # offline cache made by download_wheels.bat: works without any internet
        srcs = ([["--no-index", "--find-links", str(wheels)]] if wheels.is_dir() else []) + [[]]
        for src, extra in [(a, b) for a in srcs for b in ([], ["--user"], ["--break-system-packages"], ["--user", "--break-system-packages"])]:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", pkg, *src, *extra],
                               capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                done = True
                break
        try:
            importlib.invalidate_caches()
            importlib.import_module(mod)
        except ImportError:
            done = False
        print(f"    {'OK  ' if done else 'FAIL'} {pkg}" + ("" if done else f"  (no {why}; run: pip install {pkg})"), flush=True)
    print()


DRIVE_REQUIREMENTS = [("flask", "Flask", "فضای ابری - لازم"), ("waitress", "waitress", "فضای ابری - سرور تولید"),
                      ("argon2", "argon2-cffi", "فضای ابری - هش رمز قوی‌تر")]


def ensure_drive_libs():
    """Same idea as ensure_libs() but for Cloud Drive's own requirements, installed into this SAME Python (no venv,
    so one `start.bat` run gets everything). Flask is required; waitress/argon2-cffi are optional (Cloud Drive
    falls back on its own if they're missing). Returns True iff Cloud Drive can actually be started."""
    import importlib
    missing = []
    for mod, pkg, why in DRIVE_REQUIREMENTS:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append((mod, pkg, why))
    if missing:
        print("  Installing Cloud Drive libraries: " + ", ".join(p for _, p, _ in missing) + " ...", flush=True)
        for mod, pkg, why in missing:
            done = False
            wheels = here / "wheels"
            srcs = ([["--no-index", "--find-links", str(wheels)]] if wheels.is_dir() else []) + [[]]
            for src, extra in [(a, b) for a in srcs for b in ([], ["--user"], ["--break-system-packages"], ["--user", "--break-system-packages"])]:
                r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", pkg, *src, *extra],
                                   capture_output=True, text=True, timeout=180)
                if r.returncode == 0:
                    done = True
                    break
            try:
                importlib.invalidate_caches()
                importlib.import_module(mod)
            except ImportError:
                done = False
            print(f"    {'OK  ' if done else 'FAIL'} {pkg}" + ("" if done else f"  (no {why})"), flush=True)
        print()
    try:
        importlib.import_module("flask")
        return True
    except ImportError:
        return False


def spawn_drive(drive_secret, local):
    """Launch cloud-drive/wsgi.py as another subprocess of this same Python (its own venv/run.bat/run.sh are for
    running it standalone; start.bat instead folds it in, same as the datasheet). Never fatal: if anything about
    Cloud Drive fails, the messenger itself still starts normally."""
    env = dict(os.environ)
    env["CD_PORT"] = str(DRIVE_PORT)
    env["CD_HOST"] = "127.0.0.1" if local else "0.0.0.0"
    env["CD_SSO_SECRET"] = drive_secret
    env["CD_HSTS"] = "0"
    env["CD_PASSWORD_AUTH"] = "0"      # folded into Windroida: only the SSO link gets anyone in, no separate
                                        # username/password (and so no separate admin login either - see datasheet)
    crt, key = here / "data" / "server.crt", here / "data" / "server.key"
    if crt.is_file() and key.is_file():          # reuse Windroida's own certificate: same browser warning, real TLS
        env["CD_SSL_CERT"], env["CD_SSL_KEY"], env["CD_COOKIE_SECURE"] = str(crt), str(key), "1"
    return subprocess.Popen([sys.executable, str(here / "cloud-drive" / "wsgi.py")], cwd=str(here / "cloud-drive"), env=env)


def _drive_bg(result, do_install, local):
    """Runs on its own thread: install (optional), read the shared secret server.py already wrote, spawn. Writes
    into `result` (a plain dict the main thread already holds a reference to) so the main thread can find and stop
    the process later, but never waits on this thread itself."""
    try:
        ok = ensure_drive_libs() if do_install else _have_flask()
        if not ok:
            result["status"] = "no-flask"
            return
        secret = (here / "data" / "drive_sso.key").read_text(encoding="utf-8").strip()
        if not secret:
            result["status"] = "no-secret"
            return
        result["proc"] = spawn_drive(secret, local)
        result["respawn"] = lambda: spawn_drive(secret, local)
        result["status"] = "started"
    except Exception:
        result["status"] = "error"


GHAB_REQUIREMENTS = [("flask", "Flask", "قاب - لازم"), ("imageio_ffmpeg", "imageio-ffmpeg", "قاب - فشرده‌سازی ویدیو (اختیاری)")]


def _pip_install(mod, pkg, why):
    import importlib
    try:
        importlib.import_module(mod)
        return True
    except ImportError:
        pass
    wheels = here / "wheels"
    srcs = ([["--no-index", "--find-links", str(wheels)]] if wheels.is_dir() else []) + [[]]
    for src, extra in [(a, b) for a in srcs for b in ([], ["--user"], ["--break-system-packages"], ["--user", "--break-system-packages"])]:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", pkg, *src, *extra],
                           capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            break
    try:
        importlib.invalidate_caches()
        importlib.import_module(mod)
        return True
    except ImportError:
        return False


def spawn_ghab(secret, local):
    """Ghab (قاب) as another subprocess of this same Python, exactly like Cloud Drive: no signup/login/admin page of its
    own (GHAB_PASSWORD_AUTH=0), entered only through the Windroida menu link, same https certificate. Never fatal."""
    env = dict(os.environ)
    env["GHAB_PORT"] = str(GHAB_PORT)
    env["GHAB_SSO_SECRET"] = secret
    env["GHAB_PASSWORD_AUTH"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    crt, key = here / "data" / "server.crt", here / "data" / "server.key"
    if crt.is_file() and key.is_file():
        env["GHAB_CERT"], env["GHAB_KEY"] = str(crt), str(key)
    return subprocess.Popen([sys.executable, str(here / "ghab" / "app.py")], cwd=str(here / "ghab"), env=env)


def _ghab_bg(result, do_install, local):
    try:
        ok = _pip_install("flask", "Flask", "قاب") if do_install else _have_flask()
        if not ok:
            result["status"] = "no-flask"
            return
        if do_install:
            _pip_install("imageio_ffmpeg", "imageio-ffmpeg", "فشرده‌سازی ویدیو")   # optional: Ghab works without it
        secret = (here / "data" / "ghab_sso.key").read_text(encoding="utf-8").strip()
        if not secret:
            result["status"] = "no-secret"
            return
        result["proc"] = spawn_ghab(secret, local)
        result["respawn"] = lambda: spawn_ghab(secret, local)
        result["status"] = "started"
    except Exception:
        result["status"] = "error"


_stopping = threading.Event()


def _supervise(named_results):
    """Cloud Drive and Ghab run as their own processes. If one dies (a crash, ffmpeg going wild, the process being killed) it is started
    again after a few seconds - at most 8 times in 10 minutes, so a service that can never start doesn't loop forever."""
    crashes = {name: [] for name, _ in named_results}
    while not _stopping.wait(3):
        for name, res in named_results:
            p, again = res.get("proc"), res.get("respawn")
            if p is None or again is None or p.poll() is None:
                continue
            t = time.time()
            crashes[name] = [x for x in crashes[name] if x > t - 600] + [t]
            if len(crashes[name]) > 8:
                res["proc"] = None
                print(f"\n  {name} keeps crashing; giving up on it (the messenger is unaffected). Details: its log file / console.\n", flush=True)
                continue
            print(f"\n  {name} stopped (code {p.returncode}); starting it again ...\n", flush=True)
            try:
                res["proc"] = again()
            except Exception:
                res["proc"] = None


def _have_flask():
    try:
        import flask  # noqa: F401
        return True
    except ImportError:
        return False


def save_wheels_in_background():
    """With internet: quietly copy the libraries into ./wheels (exactly for THIS Python), so the whole folder can be moved to
    another PC/server and start.bat installs them from there without any internet. Skipped if wheels/ already has files."""
    try:
        wheels = here / "wheels"
        if wheels.is_dir() and any(wheels.glob("*.whl")):
            return
        wheels.mkdir(exist_ok=True)
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}      # no black window on Windows
        subprocess.Popen([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "-q", "-r",
                          str(here / "requirements.txt"), "-d", str(wheels)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kw)
    except Exception:
        pass


def real_scheme(wanted):
    """The server falls back to http if it could not make a certificate; ask it which one it really speaks."""
    if wanted == "http":
        return "http"
    import ssl
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection(("127.0.0.1", WEB_PORT), timeout=2) as raw:
            ctx.wrap_socket(raw).close()
        return "https"
    except OSError:
        return "http"


def have_tk():
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


def main():
    if "--no-install" not in flags:
        ensure_libs()
        save_wheels_in_background()
    if port_open(WEB_PORT) or port_open(PORT):
        print("\n  Windroida seems to be running already (port %d/%d is busy)." % (PORT, WEB_PORT))
        print("  Close the other Windroida window first, then try again.\n")
        return 1
    want_drive = "--no-drive" not in flags
    drive_already_running = want_drive and port_open(DRIVE_PORT)
    server_args = ["--web", "--web-port", str(WEB_PORT), "--port", str(PORT)]
    if "--local" not in flags:
        server_args += ["--host", "0.0.0.0"]
    scheme = "http" if "--http" in flags else "https"
    if scheme == "http":
        server_args.append("--http")
    procs = [spawn("server.py", *server_args)]
    time.sleep(0.3)
    for _ in range(60):                                   # wait until the server answers
        if procs[0].poll() is not None:
            print("\n  The server could not start (see the message above).\n")
            return 1
        if port_open(WEB_PORT):
            break
        time.sleep(0.2)

    # Cloud Drive is optional and its (rare) pip install can be slow on a bad connection, so it's set up in the
    # background: the messenger itself must never wait on it. drive_result gets filled in once that's done.
    drive_result = {"proc": None, "status": "running-separately" if drive_already_running else None}
    if want_drive and not drive_already_running:
        threading.Thread(target=_drive_bg, args=(drive_result, "--no-install" not in flags, "--local" in flags), daemon=True).start()

    want_ghab = "--no-ghab" not in flags and (here / "ghab" / "app.py").is_file()
    ghab_already_running = want_ghab and port_open(GHAB_PORT)
    ghab_result = {"proc": None, "status": "running-separately" if ghab_already_running else None}
    if want_ghab and not ghab_already_running:
        threading.Thread(target=_ghab_bg, args=(ghab_result, "--no-install" not in flags, "--local" in flags), daemon=True).start()

    threading.Thread(target=_supervise, args=([("Cloud Drive", drive_result), ("Ghab", ghab_result)],), daemon=True).start()

    scheme = real_scheme(scheme)
    print("\n" + "=" * 58)
    print("  Windroida is running")
    print(f"    Web UI (this PC):  {scheme}://127.0.0.1:{WEB_PORT}")
    if "--local" not in flags:
        print(f"    Friends (Wi-Fi):   {scheme}://{lan_ip()}:{WEB_PORT}")
    if scheme == "https":
        print("  First visit: the browser warns about the self-signed certificate -> Advanced -> Continue.")
        print("  (https is what lets browsers use the microphone for voice messages and calls.)")
    elif "--http" not in flags:
        print("  https is OFF (certificate could not be created: pip install cryptography). Voice only works on this PC.")
    try:                                              # fingerprint of the server certificate: the Android app asks you to confirm it once
        import hashlib, ssl as _ssl
        _pem = (here / "data" / "server.crt")
        if _pem.exists():
            _fp = hashlib.sha256(_ssl.PEM_cert_to_DER_cert(_pem.read_text())).hexdigest().upper()
            print("  Android app certificate fingerprint (SHA-256):")
            print("    " + ":".join(_fp[i:i + 2] for i in range(0, len(_fp), 2)))
    except Exception:
        pass
    if drive_already_running:
        print(f"    Cloud Drive (فضای ابری):  http://127.0.0.1:{DRIVE_PORT}  (از قبل جدا روشن بود)")
    elif want_drive:
        print(f"    Cloud Drive (فضای ابری):  داره تو پس‌زمینه آماده می‌شه... (از منوی ویندروید → ☁ فضای ابری)")
    elif want_drive:
        print("    Cloud Drive (فضای ابری): راه نیفتاد (Flask نصب نشد یا پوشهٔ cloud-drive پیدا نشد)")
    if ghab_already_running:
        print(f"    Ghab (قاب):  port {GHAB_PORT} از قبل جدا روشن بود")
    elif want_ghab:
        print("    Ghab (قاب):  داره تو پس‌زمینه آماده می‌شه...  (از منوی ویندروید)")
    print("  Close this window to stop everything.")
    print("=" * 58 + "\n", flush=True)

    if have_tk():
        if "--no-datasheet" not in flags:
            procs.append(spawn("datasheet.py"))
    else:
        print("  (tkinter is not installed, so the desktop windows are skipped; the web UI works.)\n")
    if "--browser" in flags:
        webbrowser.open(f"{scheme}://127.0.0.1:{WEB_PORT}")

    try:
        crashes = []                                       # supervisor: if the server process dies, start it again (max 8 times in 10 minutes)
        while True:
            while procs[0].poll() is None:
                time.sleep(0.5)
            code = procs[0].returncode
            t = time.time()
            crashes = [x for x in crashes if x > t - 600] + [t]
            if code in (0, None) or len(crashes) > 8:
                break
            print(f"\n  The server stopped unexpectedly (code {code}); restarting in 3 s ... (details: data/windroida.log)\n", flush=True)
            time.sleep(3)
            procs[0] = spawn("server.py", *server_args)
    finally:
        _stopping.set()
        for p in procs:
            if p.poll() is None:
                p.terminate()
        gp = ghab_result.get("proc")
        if gp is not None and gp.poll() is None:
            gp.terminate()
        dp = drive_result.get("proc")                  # may have been started just now by the background thread
        if dp is not None and dp.poll() is None:
            dp.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
