"""AeroCal - avvisi Google Calendar dalla system tray.

Un aeroplanino di carta attraversa lo schermo (+ toast di Windows)
5 minuti prima di ogni evento del calendario.

Sicurezza:
- l'URL ICS segreto e' cifrato con DPAPI (legato all'account Windows dell'utente)
- solo HTTPS verso calendar.google.com, certificato verificato
- i testi provenienti dal calendario vengono sanificati prima delle notifiche
- nessun contenuto del calendario viene scritto nei log

Uso:
    pythonw main.py            avvia l'utility nella tray
    python  main.py --setup    (ri)configura l'URL ICS
    python  main.py --fly "titolo"   test dell'animazione
"""

import base64
import ctypes
import ctypes.wintypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import winreg
from datetime import datetime, timedelta
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape

APP_NAME = "AeroCal"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(APP_DIR, "config.dat")
LOG_PATH = os.path.join(APP_DIR, "aerocal.log")

LEAD_MINUTES = 5          # minuti di preavviso
POLL_SECONDS = 120        # ogni quanto scaricare l'ICS
TICK_SECONDS = 5          # ogni quanto controllare l'orologio
MAX_ICS_BYTES = 20 * 1024 * 1024
ALLOWED_HOSTS = {"calendar.google.com"}
MAX_TITLE_LEN = 120


# --------------------------------------------------------------------------
# log minimale (solo errori tecnici, mai contenuti del calendario)
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# DPAPI: cifratura legata all'account Windows dell'utente
# --------------------------------------------------------------------------

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, encrypt: bool) -> bytes:
    blob_in = _DataBlob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)),
                                               ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    func = (ctypes.windll.crypt32.CryptProtectData if encrypt
            else ctypes.windll.crypt32.CryptUnprotectData)
    if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI failure")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def save_config(cfg: dict) -> None:
    os.makedirs(APP_DIR, exist_ok=True)
    raw = json.dumps(cfg).encode("utf-8")
    with open(CONFIG_PATH, "wb") as f:
        f.write(base64.b64encode(_dpapi(raw, encrypt=True)))


def load_config() -> dict | None:
    try:
        with open(CONFIG_PATH, "rb") as f:
            raw = _dpapi(base64.b64decode(f.read()), encrypt=False)
        return json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# validazione e download dell'ICS
# --------------------------------------------------------------------------

def validate_ics_url(url: str) -> str | None:
    """Ritorna un messaggio di errore, o None se l'URL e' accettabile."""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return "URL non valido."
    if p.scheme != "https":
        return "L'URL deve iniziare con https://"
    if p.hostname not in ALLOWED_HOSTS:
        return "Per sicurezza sono accettati solo URL di calendar.google.com"
    if not p.path.endswith(".ics"):
        return "L'URL deve essere l'indirizzo segreto in formato iCal (finisce con .ics)"
    if "/public/" in p.path:
        return ("Questo e' l'indirizzo PUBBLICO, che non funziona se il calendario "
                "non e' pubblico.\nNella sezione 'Integra il calendario' scorri piu' "
                "in basso e copia l'INDIRIZZO SEGRETO in formato iCal "
                "(contiene '/private-...').")
    return None


def fetch_ics(url: str) -> bytes:
    import requests
    with requests.get(url, timeout=(10, 30), stream=True,
                      headers={"User-Agent": f"{APP_NAME}/1.0"}) as r:
        r.raise_for_status()
        chunks, total = [], 0
        for chunk in r.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_ICS_BYTES:
                raise ValueError("ICS troppo grande")
            chunks.append(chunk)
        return b"".join(chunks)


def parse_upcoming(ics_bytes: bytes, window_hours: int = 24) -> list[dict]:
    """Eventi (non tutto-il-giorno) delle prossime `window_hours` ore, ora locale."""
    import icalendar
    import recurring_ical_events
    from tzlocal import get_localzone

    tz = get_localzone()
    now = datetime.now(tz)
    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = []
    for ev in recurring_ical_events.of(cal).between(now - timedelta(hours=1),
                                                    now + timedelta(hours=window_hours)):
        dtstart = ev.get("DTSTART")
        if dtstart is None or not isinstance(dtstart.dt, datetime):
            continue  # ignora eventi tutto-il-giorno
        start = dtstart.dt
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        start = start.astimezone(tz)
        title = clean_title(str(ev.get("SUMMARY", "Evento")))
        uid = str(ev.get("UID", title))
        events.append({"uid": uid, "start": start, "title": title})
    events.sort(key=lambda e: e["start"])
    return events


def clean_title(text: str) -> str:
    text = "".join(ch for ch in text if ch.isprintable())
    text = " ".join(text.split())
    return text[:MAX_TITLE_LEN] or "Evento"


# --------------------------------------------------------------------------
# notifiche
# --------------------------------------------------------------------------

def _ps_safe(text: str) -> str:
    """winotify incolla il testo in una here-string PowerShell espandibile,
    dove $(...) verrebbe ESEGUITO: qui si neutralizzano i metacaratteri.
    """
    return text.replace("`", "'").replace("$", "＄")  # ＄ fullwidth, innocuo


def show_toast(title: str, body: str) -> None:
    try:
        from winotify import Notification, audio
        toast = Notification(app_id=APP_NAME,
                             title=xml_escape(_ps_safe(title)),
                             msg=xml_escape(_ps_safe(body)))
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception as e:
        log(f"toast error: {type(e).__name__}")


def _self_cmd(*args: str) -> list[str]:
    """Comando per rilanciare l'app: exe PyInstaller oppure script Python."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return [exe, os.path.abspath(__file__), *args]


def launch_airplane(text: str) -> None:
    """Lancia l'animazione in un processo separato (tkinter isolato)."""
    try:
        subprocess.Popen(_self_cmd("--fly", text),
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as e:
        log(f"airplane launch error: {type(e).__name__}")


def notify_event(ev: dict) -> None:
    when = ev["start"].strftime("%H:%M")
    show_toast(f"Tra {LEAD_MINUTES} minuti: {ev['title']}",
               f"Inizio alle {when} - preparati alla call!")
    launch_airplane(f"{ev['title']}  ({when})")


# --------------------------------------------------------------------------
# animazione: aereo pubblicitario che traina lo striscione con il testo
# --------------------------------------------------------------------------

FLY_SECONDS = 24.0        # durata di un singolo passaggio
FLY_REPEATS = 2           # quante volte l'aereo attraversa lo schermo
SCALE = 2.0


def fly_animation(text: str) -> None:
    import tkinter as tk
    import tkinter.font as tkfont

    S = SCALE
    text = clean_title(text).upper()
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    transparent = "#ff00fe"
    root.attributes("-transparentcolor", transparent)

    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    band_h = int(220 * S)
    y0 = int(sh * 0.10)
    root.geometry(f"{sw}x{band_h}+0+{y0}")
    canvas = tk.Canvas(root, width=sw, height=band_h, bg=transparent,
                       highlightthickness=0)
    canvas.pack()

    RED, DARK, CREAM, ROPE = "#d94f4f", "#9c3232", "#fff6e8", "#8a7b66"
    banner_font = tkfont.Font(family="Segoe UI", size=int(15 * S), weight="bold")
    banner_w = banner_font.measure(text) + int(44 * S)
    banner_h = 46 * S
    gap = 46 * S      # corda tra coda e striscione
    total_w = 120 * S + gap + banner_w   # aereo + corda + striscione

    def off(pts, x, y):
        return [c for px, py in pts for c in (x + px * S, y + py * S)]

    # l'aereo punta verso destra, origine sul centro della fusoliera
    fus = [(46, -2), (38, -8), (10, -11), (-24, -8), (-34, -3),
           (-34, 3), (-24, 8), (10, 11), (38, 8), (46, 2)]
    wing = [(16, -4), (2, -4), (-14, 16), (-2, 16)]
    wing_top = [(12, -9), (0, -9), (-10, -24), (0, -24)]
    tailfin = [(-26, -6), (-40, -26), (-32, -26), (-18, -8)]
    tailplane = [(-24, -6), (-40, -10), (-40, -6), (-26, -2)]

    items = {
        "tailplane": canvas.create_polygon(0, 0, fill=DARK, outline=DARK),
        "tailfin": canvas.create_polygon(0, 0, fill=RED, outline=DARK, width=2),
        "wing_top": canvas.create_polygon(0, 0, fill=CREAM, outline=DARK, width=2),
        "fus": canvas.create_polygon(0, 0, fill=RED, outline=DARK, width=3, smooth=True),
        "stripe": canvas.create_line(0, 0, 0, 0, fill=CREAM, width=6),
        "cockpit": canvas.create_oval(0, 0, 0, 0, fill="#bfe3f2", outline=DARK, width=2),
        "wing": canvas.create_polygon(0, 0, fill=CREAM, outline=DARK, width=2),
        "nose": canvas.create_oval(0, 0, 0, 0, fill=DARK, outline=DARK),
        "prop": canvas.create_line(0, 0, 0, 0, fill="#6b6b6b", width=6, capstyle="round"),
        "rope1": canvas.create_line(0, 0, 0, 0, fill=ROPE, width=3),
        "rope2": canvas.create_line(0, 0, 0, 0, fill=ROPE, width=3),
        "ribbon1": canvas.create_polygon(0, 0, fill=RED, outline=DARK, width=1),
        "ribbon2": canvas.create_polygon(0, 0, fill="#e8a3a3", outline=DARK, width=1),
        "banner": canvas.create_polygon(0, 0, fill="#fffdf7", outline=RED, width=5,
                                        joinstyle="round"),
        "inner_top": canvas.create_line(0, 0, 0, 0, fill="#e5b8b8", width=2),
        "inner_bot": canvas.create_line(0, 0, 0, 0, fill="#e5b8b8", width=2),
        "pole": canvas.create_line(0, 0, 0, 0, fill=RED, width=7, capstyle="round"),
    }

    # una lettera per item: cosi' il testo ondeggia insieme alla stoffa
    char_centers, cum = [], 0.0
    for ch in text:
        w = banner_font.measure(ch)
        char_centers.append(cum + w / 2)
        cum += w
    text_w = cum
    char_sh = [canvas.create_text(0, 0, text=ch, font=banner_font, fill="#e2d3c2")
               for ch in text]
    char_fg = [canvas.create_text(0, 0, text=ch, font=banner_font, fill="#b33939")
               for ch in text]

    start_t = time.monotonic()

    def step():
        el = time.monotonic() - start_t
        t = el / FLY_SECONDS
        if t >= FLY_REPEATS:
            root.destroy()
            return
        t = t % 1.0           # ogni passaggio riparte da sinistra
        # il muso entra subito da sinistra; il passaggio finisce appena lo
        # striscione esce a destra, cosi' la ripetizione e' quasi istantanea
        x = -60 * S + (sw + total_w + 20 * S) * t
        y = band_h * 0.45 + math.sin(el * 1.1) * 12 * S

        canvas.coords(items["fus"], *off(fus, x, y))
        canvas.coords(items["wing"], *off(wing, x, y))
        canvas.coords(items["wing_top"], *off(wing_top, x, y))
        canvas.coords(items["tailfin"], *off(tailfin, x, y))
        canvas.coords(items["tailplane"], *off(tailplane, x, y))
        canvas.coords(items["stripe"], x - 30 * S, y + 1 * S, x + 40 * S, y + 1 * S)
        canvas.coords(items["cockpit"], x + 14 * S, y - 13 * S, x + 30 * S, y - 3 * S)
        canvas.coords(items["nose"], x + 42 * S, y - 5 * S, x + 52 * S, y + 5 * S)

        # elica: linea verticale che "gira" (lunghezza pulsante)
        pr = (20 * abs(math.sin(el * 24)) + 4) * S
        canvas.coords(items["prop"], x + 49 * S, y - pr, x + 49 * S, y + pr)

        # striscione trainato: stoffa che ondeggia come un vero banner aereo
        bx1 = x - 34 * S - gap             # bordo destro (attaccato alle corde)
        bx0 = bx1 - banner_w               # bordo sinistro (estremita' libera)
        by = y + 8 * S
        ncol = 12
        tops, bots, dys = [], [], []
        for i in range(ncol + 1):
            u = i / ncol                   # 0 = estremita' libera, 1 = attacco
            xi = bx0 + banner_w * u
            amp = (2 + 8 * (1 - u)) * S    # l'onda cresce verso la coda libera
            dy = math.sin(el * 3.0 - u * 5.0) * amp
            dys.append(dy)
            tops.append((xi, by - banner_h / 2 + dy))
            bots.append((xi, by + banner_h / 2 + dy))
        notch = (bx0 + 15 * S, by + dys[0])            # coda a rondine
        poly = tops + list(reversed(bots)) + [notch]
        canvas.coords(items["banner"], *[c for p in poly for c in p])

        # doppio bordo interno decorativo
        inset = 7 * S
        canvas.coords(items["inner_top"],
                      *[c for (px, py) in tops[1:ncol] for c in (px, py + inset)])
        canvas.coords(items["inner_bot"],
                      *[c for (px, py) in bots[1:ncol] for c in (px, py - inset)])

        # asta rigida sul bordo destro + doppia corda di traino a V
        canvas.coords(items["pole"], bx1, tops[-1][1], bx1, bots[-1][1])
        canvas.coords(items["rope1"], x - 34 * S, y, bx1, tops[-1][1] + 4 * S)
        canvas.coords(items["rope2"], x - 34 * S, y, bx1, bots[-1][1] - 4 * S)

        # nastrini svolazzanti agli angoli della coda
        c1, c2 = tops[0], bots[0]
        fl1 = math.sin(el * 3.4 + 0.7) * 8 * S
        fl2 = math.sin(el * 3.4 + 2.1) * 8 * S
        canvas.coords(items["ribbon1"], c1[0], c1[1],
                      c1[0] - 30 * S, c1[1] - 4 * S + fl1, c1[0] - 6 * S, c1[1] + 8 * S)
        canvas.coords(items["ribbon2"], c2[0], c2[1],
                      c2[0] - 30 * S, c2[1] + 4 * S + fl2, c2[0] - 6 * S, c2[1] - 8 * S)

        # ogni lettera segue l'onda della stoffa (posizione + inclinazione)
        def fabric_dy(u):
            return math.sin(el * 3.0 - u * 5.0) * (2 + 8 * (1 - u)) * S

        left = (bx0 + bx1 + 12 * S) / 2 - text_w / 2
        for i in range(len(char_fg)):
            cx = left + char_centers[i]
            u = min(1.0, max(0.0, (cx - bx0) / banner_w))
            dy = fabric_dy(u) * 0.9
            slope = (fabric_dy(min(1.0, u + 0.03)) * 0.9 - dy) / (0.03 * banner_w)
            ang = -math.degrees(math.atan(slope))
            cy = by + dy
            canvas.coords(char_sh[i], cx + 2 * S, cy + 2 * S)
            canvas.coords(char_fg[i], cx, cy)
            canvas.itemconfigure(char_sh[i], angle=ang)
            canvas.itemconfigure(char_fg[i], angle=ang)

        root.after(16, step)

    step()
    root.mainloop()


# --------------------------------------------------------------------------
# finestra con l'elenco dei prossimi eventi
# --------------------------------------------------------------------------

def list_window(status: str, lines: list[str]) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title(f"{APP_NAME} - prossimi eventi")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    tk.Label(root, text="Prossimi eventi (24 ore)", font=("Segoe UI", 12, "bold"),
             padx=16, pady=10).pack(anchor="w")
    body = "\n".join(lines) if lines else "Nessun evento nelle prossime 24 ore."
    tk.Label(root, text=body, justify="left", font=("Segoe UI", 11),
             padx=16).pack(anchor="w")
    if status:
        tk.Label(root, text=f"Stato: {status}", justify="left",
                 font=("Segoe UI", 9), fg="#777777", padx=16, pady=8).pack(anchor="w")
    tk.Button(root, text="Chiudi", command=root.destroy, width=12).pack(pady=(4, 12))

    # in basso a destra, sopra la tray
    root.update_idletasks()
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"+{sw - w - 24}+{sh - h - 90}")
    root.bind("<Escape>", lambda e: root.destroy())
    root.mainloop()


# --------------------------------------------------------------------------
# dialogo di configurazione (URL ICS segreto)
# --------------------------------------------------------------------------

def setup_dialog() -> bool:
    """Chiede l'URL ICS, lo valida con un download di prova e lo salva cifrato."""
    import tkinter as tk
    from tkinter import messagebox

    result = {"ok": False}

    root = tk.Tk()
    root.title(f"{APP_NAME} - configurazione")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    tk.Label(root, justify="left", padx=14, pady=8, text=(
        "Incolla l'indirizzo segreto in formato iCal del tuo Google Calendar.\n\n"
        "Dove trovarlo:  Google Calendar (web) > Impostazioni >\n"
        "il tuo calendario > 'Integra il calendario' >\n"
        "'Indirizzo segreto in formato iCal'.\n\n"
        "L'URL verra' salvato cifrato (DPAPI) solo su questo PC.\n"
        "Non condividerlo con nessuno: chi lo possiede puo' leggere il calendario."
    )).pack()

    entry = tk.Entry(root, width=72, show="*")
    entry.pack(padx=14, pady=4)
    entry.focus_set()

    shown = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="mostra URL", variable=shown,
                   command=lambda: entry.config(show="" if shown.get() else "*")).pack()

    def on_ok():
        url = entry.get().strip()
        err = validate_ics_url(url)
        if err:
            messagebox.showerror(APP_NAME, err, parent=root)
            return
        try:
            data = fetch_ics(url)
            parse_upcoming(data)
        except Exception:
            messagebox.showerror(APP_NAME,
                                 "Download o lettura del calendario falliti.\n"
                                 "Controlla l'URL e la connessione.", parent=root)
            return
        save_config({"ics_url": url, "lead_minutes": LEAD_MINUTES})
        result["ok"] = True
        root.destroy()

    tk.Button(root, text="Salva e avvia", command=on_ok, width=18).pack(pady=10)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()
    return result["ok"]


# --------------------------------------------------------------------------
# avvio automatico con Windows (chiave Run dell'utente corrente)
# --------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _autostart_cmd() -> str:
    return " ".join(f'"{part}"' for part in _self_cmd())


def is_autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0] == _autostart_cmd()
    except OSError:
        return False


def toggle_autostart() -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if is_autostart():
            winreg.DeleteValue(k, APP_NAME)
        else:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _autostart_cmd())


# --------------------------------------------------------------------------
# motore: polling del calendario + scheduler degli avvisi
# --------------------------------------------------------------------------

class Engine:
    def __init__(self):
        self.lock = threading.Lock()
        self.upcoming: list[dict] = []
        self.notified: set[tuple[str, str]] = set()
        self.stop_flag = threading.Event()
        self.last_tick = time.time()
        self.last_poll = 0.0
        self.status = "in attesa del primo aggiornamento"

    def poll_once(self) -> None:
        cfg = load_config()
        if not cfg:
            self.status = "configurazione mancante"
            return
        try:
            events = parse_upcoming(fetch_ics(cfg["ics_url"]))
            with self.lock:
                self.upcoming = events
            self.status = f"ok, ultimo aggiornamento {datetime.now():%H:%M}"
        except Exception as e:
            self.status = "errore di aggiornamento (vedi log)"
            log(f"poll error: {type(e).__name__}")

    def tick(self) -> None:
        from tzlocal import get_localzone
        now = datetime.now(get_localzone())
        lead = timedelta(minutes=LEAD_MINUTES)
        with self.lock:
            events = list(self.upcoming)
        for ev in events:
            key = (ev["uid"], ev["start"].isoformat())
            if key in self.notified:
                continue
            if ev["start"] - lead <= now < ev["start"]:
                self.notified.add(key)
                notify_event(ev)
        # pulizia deduplicazione (eventi ormai passati da piu' di un giorno)
        cutoff = (now - timedelta(days=1)).isoformat()
        self.notified = {k for k in self.notified if k[1] > cutoff}

    def run(self) -> None:
        while not self.stop_flag.is_set():
            now = time.time()
            woke_from_sleep = now - self.last_tick > 60
            self.last_tick = now
            if woke_from_sleep or now - self.last_poll >= POLL_SECONDS:
                self.last_poll = now
                self.poll_once()
            self.tick()
            self.stop_flag.wait(TICK_SECONDS)


# --------------------------------------------------------------------------
# icona nella system tray
# --------------------------------------------------------------------------

def make_icon_image():
    """Stesso aereo dell'animazione: fusoliera rossa, ali, coda ed elica."""
    from PIL import Image, ImageDraw
    RED, DARK = (217, 79, 79, 255), (156, 50, 50, 255)
    CREAM, SKYBLUE = (255, 246, 232, 255), (191, 227, 242, 255)
    GRAY = (107, 107, 107, 255)

    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # piano di coda e deriva
    d.polygon([(48, 122), (14, 96), (40, 128)], fill=DARK)
    d.polygon([(52, 120), (24, 48), (58, 48), (82, 118)], fill=RED, outline=DARK)
    # ala superiore (dietro la fusoliera)
    d.polygon([(122, 118), (90, 118), (66, 40), (98, 40)], fill=CREAM, outline=DARK)
    # fusoliera
    d.rounded_rectangle([(30, 110), (216, 154)], radius=22, fill=RED,
                        outline=DARK, width=6)
    d.line([(52, 134), (200, 134)], fill=CREAM, width=8)
    # abitacolo e ala inferiore
    d.ellipse([(126, 88), (170, 122)], fill=SKYBLUE, outline=DARK, width=4)
    d.polygon([(130, 146), (98, 146), (74, 220), (106, 220)], fill=CREAM,
              outline=DARK)
    # muso ed elica
    d.ellipse([(202, 114), (232, 150)], fill=DARK)
    d.line([(224, 78), (224, 186)], fill=GRAY, width=11)
    d.ellipse([(216, 124), (232, 140)], fill=GRAY)
    return img


def run_tray(engine: Engine) -> None:
    import pystray
    from pystray import MenuItem as Item

    def show_upcoming(icon, item):
        from tzlocal import get_localzone
        now = datetime.now(get_localzone())
        with engine.lock:
            events = list(engine.upcoming)
        lines = []
        for e in events:
            d = e["start"].date()
            if d == now.date():
                day = "oggi"
            elif d == (now + timedelta(days=1)).date():
                day = "domani"
            else:
                day = f"{e['start']:%d/%m}"
            lines.append(f"{day} {e['start']:%H:%M}   {e['title']}")
        subprocess.Popen(_self_cmd("--list", engine.status, *lines),
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def test_plane(icon, item):
        launch_airplane("Volo di prova!")

    def change_url(icon, item):
        subprocess.Popen(_self_cmd("--setup"),
                         creationflags=subprocess.CREATE_NO_WINDOW)

    def on_quit(icon, item):
        engine.stop_flag.set()
        icon.stop()

    menu = pystray.Menu(
        Item("Prossimi eventi", show_upcoming, default=True),
        Item("Testa aeroplanino", test_plane),
        Item("Cambia URL ICS", change_url),
        Item("Avvio automatico", lambda i, it: toggle_autostart(),
             checked=lambda it: is_autostart()),
        pystray.Menu.SEPARATOR,
        Item("Esci", on_quit),
    )
    icon = pystray.Icon(APP_NAME, make_icon_image(), APP_NAME, menu)
    icon.run()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def already_running() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, f"{APP_NAME}_single_instance")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def main() -> None:
    args = sys.argv[1:]
    if args[:1] == ["--fly"]:
        fly_animation(args[1] if len(args) > 1 else "Volo di prova!")
        return
    if args[:1] == ["--setup"]:
        setup_dialog()
        return
    if args[:1] == ["--list"]:
        list_window(args[1] if len(args) > 1 else "", args[2:])
        return

    if already_running():
        return
    if load_config() is None and not setup_dialog():
        return

    engine = Engine()
    worker = threading.Thread(target=engine.run, daemon=True)
    worker.start()
    show_toast(APP_NAME, "In ascolto: ti avviso 5 minuti prima di ogni evento. ✈")
    run_tray(engine)


if __name__ == "__main__":
    main()
