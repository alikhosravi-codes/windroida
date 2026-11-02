"""Small Tk toolkit shared by the messenger and the datasheet: themes, flat widgets, dates."""
import datetime
import time
import tkinter as tk
import tkinter.font as tkfont
import unicodedata

THEMES = {
    "dark": dict(bg="#0e1621", side="#17212b", hover="#202b36", sel="#2b5278", sel_text="#ffffff",
                 bubble_out="#2b5278", bubble_in="#182533", text="#f5f5f5", muted="#7f91a4",
                 accent="#6ab3f3", btn="#3a7bd5", entry="#242f3d", border="#0b121a", danger="#ec7676",
                 ok="#5ec27f", warn="#e0a458", stripe="#1b2733", head="#1f2c3a", out_muted="#a5c4e4"),
    "light": dict(bg="#dfe6ec", side="#ffffff", hover="#f1f3f5", sel="#3390ec", sel_text="#ffffff",
                  bubble_out="#d6f5c2", bubble_in="#ffffff", text="#111111", muted="#707579",
                  accent="#3390ec", btn="#3390ec", entry="#f1f3f4", border="#d5dbe0", danger="#e53935",
                  ok="#2e9e55", warn="#d98c1f", stripe="#f6f8fa", head="#eef1f4", out_muted="#5f9a49"),
}
T = dict(THEMES["dark"])
FAM = "TkDefaultFont"
AVATAR_COLORS = ["#e17076", "#7bc862", "#e5ca77", "#65aadd", "#a695e7", "#ee7aae", "#6ec9cb", "#faa774"]


def set_theme(name):
    T.clear()
    T.update(THEMES[name])


def init_fonts(root):
    global FAM
    fams = set(tkfont.families(root))
    for cand in ("Vazirmatn", "Vazir", "Segoe UI", "Tahoma", "Noto Sans Arabic", "DejaVu Sans", "Arial"):
        if cand in fams:
            FAM = cand
            break
    return FAM


def font(size=10, weight="normal", slant="roman"):
    return (FAM, size, weight, slant) if slant != "roman" else (FAM, size, weight)


def shade(color, factor):
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    f = (lambda c: max(0, min(255, int(c * factor))))
    return "#%02x%02x%02x" % (f(r), f(g), f(b))


# ---------------------------------------------------------------- dates / text
FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور", "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]


def fa(text):
    return str(text).translate(FA_DIGITS)


def gregorian_to_jalali(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    if gy > 1600:
        jy, gy = 979, gy - 1600
    else:
        jy, gy = 0, gy - 621
    gy2 = gy + 1 if gm > 2 else gy
    days = 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 - 80 + gd + g_d_m[gm - 1]
    jy += 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def fmt_time(ts):
    return fa(time.strftime("%H:%M", time.localtime(ts)))


def fmt_date(ts, short=False):
    d = datetime.date.fromtimestamp(ts)
    jy, jm, jd = gregorian_to_jalali(d.year, d.month, d.day)
    return fa(f"{jd} {MONTHS[jm - 1]}" + ("" if short else f" {jy}"))


def day_key(ts):
    return datetime.date.fromtimestamp(ts).toordinal()


def fmt_day_label(ts):
    diff = datetime.date.today().toordinal() - day_key(ts)
    return {0: "امروز", 1: "دیروز"}.get(diff, fmt_date(ts))


def fmt_list_time(ts):
    diff = datetime.date.today().toordinal() - day_key(ts)
    if diff == 0:
        return fmt_time(ts)
    if diff == 1:
        return "دیروز"
    return fmt_date(ts, short=True)


def fmt_last_seen(ts):
    if not ts:
        return "مدت‌ها پیش"
    diff = datetime.date.today().toordinal() - day_key(ts)
    if diff == 0:
        return "امروز " + fmt_time(ts)
    if diff == 1:
        return "دیروز " + fmt_time(ts)
    return fmt_date(ts)


def fmt_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def is_rtl(text):
    for ch in text:
        d = unicodedata.bidirectional(ch)
        if d in ("R", "AL"):
            return True
        if d == "L":
            return False
    return False


# -------------------------------------------------------------------- widgets
class FlatButton(tk.Label):
    def __init__(self, parent, text, command=None, kind="primary", **kw):
        self.kind = kind
        self.command = command
        self.enabled = True
        base = {"primary": T["btn"], "danger": T["danger"], "ghost": parent.cget("bg"),
                "soft": T["entry"]}[kind]
        fg = {"primary": "#ffffff", "danger": "#ffffff", "ghost": T["accent"], "soft": T["text"]}[kind]
        self.base, self.hover = base, (shade(base, 1.15) if kind in ("primary", "danger") else T["hover"])
        kw.setdefault("padx", 16)
        kw.setdefault("pady", 8)
        kw.setdefault("font", font(10, "bold"))
        super().__init__(parent, text=text, bg=base, fg=fg, cursor="hand2", **kw)
        self.fg = fg
        self.bind("<Button-1>", self._click)
        self.bind("<Enter>", lambda e: self.enabled and self.configure(bg=self.hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self.base if self.enabled else T["border"]))

    def _click(self, _e=None):
        if self.enabled and self.command:
            self.command()

    def set_enabled(self, value):
        self.enabled = value
        self.configure(bg=self.base if value else T["border"], cursor="hand2" if value else "arrow",
                       fg=self.fg if value else T["muted"])


def make_entry(parent, show=None, textvariable=None, width=None, justify="left"):
    kw = dict(bg=T["entry"], fg=T["text"], insertbackground=T["text"], relief="flat", bd=0,
              highlightthickness=1, highlightbackground=T["border"], highlightcolor=T["accent"],
              font=font(11), justify=justify, disabledbackground=T["entry"], disabledforeground=T["muted"])
    if show:
        kw["show"] = show
    if textvariable is not None:
        kw["textvariable"] = textvariable
    if width:
        kw["width"] = width
    return tk.Entry(parent, **kw)


def avatar(parent, size, text, key, bg=None):
    c = tk.Canvas(parent, width=size, height=size, bg=bg or parent.cget("bg"), highlightthickness=0, bd=0)
    color = AVATAR_COLORS[abs(int(key or 0)) % len(AVATAR_COLORS)]
    c.create_oval(1, 1, size - 1, size - 1, fill=color, outline=color)
    initial = (text or "?").strip()[:1].upper() or "?"
    c.create_text(size / 2, size / 2, text=initial, fill="#ffffff", font=font(max(9, size // 3), "bold"))
    return c


class Scrollable(tk.Frame):
    """Vertically scrollable frame (canvas based). Mouse wheel handled by bind_wheel()."""

    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self._scroll_target = self
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, yscrollincrement=18)
        self.bar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview, width=10, bd=0,
                                relief="flat", troughcolor=bg, bg=T["border"], activebackground=T["muted"])
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))

    def clear(self):
        for w in self.inner.winfo_children():
            w.destroy()

    def scroll_bottom(self):
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def near_bottom(self):
        return self.canvas.yview()[1] > 0.93

    def wheel(self, steps):
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(steps, "units")


def bind_wheel(root):
    """One global wheel handler: scrolls the Scrollable under the pointer."""
    def target(event):
        try:
            w = root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return None
        while w is not None:
            t = getattr(w, "_scroll_target", None)
            if t is not None:
                return t
            w = getattr(w, "master", None)
        return None

    def on_wheel(event):
        t = target(event)
        if t:
            delta = getattr(event, "delta", 0)
            if event.num == 4:
                steps = -3
            elif event.num == 5:
                steps = 3
            else:
                steps = -3 if delta > 0 else 3
            t.wheel(steps)

    root.bind_all("<MouseWheel>", on_wheel)
    root.bind_all("<Button-4>", on_wheel)
    root.bind_all("<Button-5>", on_wheel)


class Dialog(tk.Toplevel):
    def __init__(self, app, title, width=400):
        super().__init__(app)
        self.app = app
        self.title(title)
        self.configure(bg=T["side"])
        self.transient(app)
        self.resizable(False, False)
        self.body = tk.Frame(self, bg=T["side"], padx=24, pady=20, width=width)
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(30, self._place)

    def _place(self):
        try:
            self.update_idletasks()
            x = self.app.winfo_rootx() + (self.app.winfo_width() - self.winfo_width()) // 2
            y = self.app.winfo_rooty() + (self.app.winfo_height() - self.winfo_height()) // 3
            self.geometry(f"+{max(0, x)}+{max(0, y)}")
            self.grab_set()
        except tk.TclError:
            pass

    def label(self, text, muted=True, size=9, **kw):
        l = tk.Label(self.body, text=text, bg=T["side"], fg=T["muted"] if muted else T["text"],
                     font=font(size), anchor="w", justify="left", **kw)
        l.pack(fill="x", pady=(10, 3))
        return l

    def entry(self, label, show=None, value="", justify="left"):
        self.label(label)
        e = make_entry(self.body, show=show, justify=justify)
        e.pack(fill="x", ipady=6)
        if value:
            e.insert(0, value)
        return e

    def error_label(self):
        self.err = tk.Label(self.body, text="", bg=T["side"], fg=T["danger"], font=font(9), wraplength=340,
                            justify="left", anchor="w")
        self.err.pack(fill="x", pady=(10, 0))
        return self.err

    def set_error(self, text, ok=False):
        self.err.configure(text=text, fg=T["ok"] if ok else T["danger"])
