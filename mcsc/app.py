"""MCSC – Media Collections Service Container: Web-Oberfläche der Werkbank (nur LAN, nur lesend).
Stufe 1: Übersicht, Logs, Berichte.  Start im Container:  python3 /werkbank/skripte/mcsc/app.py
Zum Testen am PC:  set MCSC_WB=\\\\server\\appdata\\werkbank  &&  python app.py   -> http://localhost:8095"""
import os, re, csv, time, sqlite3, datetime, urllib.request
from flask import Flask, render_template, request, send_file, abort

WB = os.environ.get("MCSC_WB", "/werkbank")
LOGS, BERICHTE, DB = (os.path.join(WB, x) for x in ("logs", "berichte", "db"))
CRONTAB = os.path.join(WB, "skripte", "crontab")
HOST_LOG = os.environ.get("MCSC_HOSTLOG", "/host/log")              # /var/log des Servers (backup-wd.log)
HOST_STEMPEL = os.environ.get("MCSC_STEMPEL", "/host/backup-wd")    # /boot/config/backup-wd (*.letztes)
HOST_LOCK = os.environ.get("MCSC_LOCK", "/host/lock")
HOST_RUN = os.environ.get("MCSC_RUN", "/host/run")

JOBS = [("Nachtlauf", "index_lauf.log"), ("Musik-Index", "musik_index.log"), ("Hörbuch-Index", "hoerbuch_index.log"),
        ("Video-Index", "video_index.log"), ("Defekt-Check", "datei_check.log"), ("MKV-Tagging", "mkv_taggen.log"),
        ("Umbenennen", "umbenennen.log")]
FEHLER = re.compile(r"FEHLER|Traceback|NOTBREMSE|ABBRUCH|rc=[1-9]")
FAZIT = re.compile(r"FERTIG|=== Ende|Ende:|umbenannt:|Zeitgrenze|defekt bisher|Pause:|weiter$|=== Backup")
ZEIT = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")

app = Flask(__name__)


# ---------- Helfer ----------
def ende_lesen(pfad, bytes_=200_000):
    """letzte Zeilen einer Datei (liest nur das Ende)"""
    try:
        with open(pfad, "rb") as f:
            f.seek(0, 2); n = f.tell(); f.seek(max(0, n - bytes_))
            zeilen = f.read().decode("utf-8", "replace").splitlines()
            return zeilen[1:] if n > bytes_ else zeilen
    except OSError:
        return []


def db_ro(pfad):
    return sqlite3.connect("file:" + urllib.request.pathname2url(pfad) + "?mode=ro", uri=True, timeout=2)


def vor(ts):
    """Zeitpunkt -> 'vor 5 min' / 'gestern 22:05' / '10.11. 22:05'"""
    if not ts: return "–"
    t = datetime.datetime.fromtimestamp(ts); jetzt = datetime.datetime.now(); d = jetzt - t
    if d.total_seconds() < 90: return "gerade eben"
    if d.total_seconds() < 3600: return f"vor {int(d.total_seconds() // 60)} min"
    if t.date() == jetzt.date(): return f"heute {t:%H:%M}"
    if t.date() == (jetzt - datetime.timedelta(days=1)).date(): return f"gestern {t:%H:%M}"
    return f"{t:%d.%m. %H:%M}"


def groesse(n):
    for e in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or e == "TB": return f"{n:.0f} {e}" if e in ("B", "KB") else f"{n:.1f} {e}"
        n /= 1024


app.jinja_env.filters.update(vor=vor, groesse=groesse)


def unruhe():
    try:
        import fcntl
    except ImportError:
        return None, "unbekannt (Test am PC)"
    if os.path.exists(os.path.join(HOST_RUN, "mover.pid")): return "gelb", "Mover läuft"
    try:
        with open(os.path.join(HOST_LOCK, "backup-wd.lock")) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB); fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                return "gelb", "WD-Backup läuft"
    except FileNotFoundError:
        pass
    return "gruen", "Ruhe – Nachtjobs dürfen arbeiten"


def job_status(name, datei):
    pfad = os.path.join(LOGS, datei)
    if not os.path.exists(pfad): return dict(name=name, datei=datei, da=False)
    zeilen = ende_lesen(pfad)
    start = max((i for i, z in enumerate(zeilen) if "===" in z and "Ende" not in z), default=0)
    block = zeilen[start:]
    mtime = os.path.getmtime(pfad)
    fehler = [z for z in block if FEHLER.search(z)]
    teil = [ZEIT.sub("", z).split("|")[0].strip() for z in block if re.search(r"^\S+ \S+ \w+: rc=", z)]
    fazit = " · ".join(teil) if teil else next((z for z in reversed(block) if FAZIT.search(z)), block[-1] if block else "")
    aktiv = time.time() - mtime < 180
    pause = "Pause:" in (block[-1] if block else "")
    farbe = "rot" if fehler else "gelb" if pause else "blau" if aktiv else "gruen"
    zustand = "Fehler" if fehler else "pausiert" if pause else "läuft" if aktiv else "fertig"
    return dict(name=name, datei=datei, da=True, mtime=mtime, farbe=farbe, zustand=zustand,
                fazit=ZEIT.sub("", fazit).strip(), fehler=fehler[-3:], n_fehler=len(fehler))


def defekt_zahlen():
    try:
        c = db_ro(os.path.join(DB, "datei_check.sqlite"))
        z = dict(c.execute("select status, count(*) from pruefung where path not like '/werkbank/test/%' group by status").fetchall()); c.close()
        return z
    except sqlite3.Error:
        return None


def backup_status():
    zeilen = [z for z in ende_lesen(os.path.join(HOST_LOG, "backup-wd.log"), 400_000)
              if " === Backup" in z or " FEHLER" in z or " --- " in z or " ABBRUCH" in z]
    letzte = zeilen[-1] if zeilen else ""
    laeuft = bool(letzte) and "ABBRUCH" not in letzte and "gestartet" in (next((z for z in reversed(zeilen) if "=== Backup" in z), ""))
    if "ABBRUCH" in letzte:
        zeilen.append(letzte.replace("ABBRUCH", "=== Backup abgebrochen:"))
    abgl = [z for z in ende_lesen(os.path.join(HOST_LOG, "wd-abgleich.log"), 400_000) if ZEIT.match(z)]
    abgleich = ZEIT.sub("", abgl[-1]).strip() if abgl else ""
    abgleich_laeuft = bool(abgl) and not any(x in abgl[-1] for x in ("=== Abgleich erfolgreich", "=== Abgleich mit Fehlern", "ABBRUCH"))
    stempel = []
    if os.path.isdir(HOST_STEMPEL):
        for f in sorted(os.listdir(HOST_STEMPEL)):
            if f.endswith(".letztes"):
                try: stempel.append((f[:-8], int(open(os.path.join(HOST_STEMPEL, f)).read().strip())))
                except (OSError, ValueError): pass
    ergebnis = next((z for z in reversed(zeilen) if "=== Backup" in z and "gestartet" not in z), "")
    m = ZEIT.match(ergebnis)
    erg_zeit = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() if m else None
    return dict(da=bool(zeilen), laeuft=laeuft, aktuell=ZEIT.sub("", letzte).strip(), erg_zeit=erg_zeit,
                abgleich=abgleich, abgleich_laeuft=abgleich_laeuft,
                ergebnis=ZEIT.sub("", ergebnis).strip().strip("= "), stempel=stempel)


def zeitplan():
    plan = []
    for z in ende_lesen(CRONTAB):
        z = z.strip()
        if not z or z.startswith("#"): continue
        teile = z.split(None, 5)
        if len(teile) < 6: continue
        mi, st, tag, mon, wt, befehl = teile
        wann = f"täglich {int(st):02d}:{int(mi):02d}" if tag == mon == wt == "*" and mi.isdigit() and st.isdigit() else " ".join(teile[:5])
        jobs = [os.path.basename(b.split()[1]) + (" " + " ".join(b.split()[2:]) if len(b.split()) > 2 else "")
                for b in befehl.split(";") if b.strip().startswith("python3")]
        plan.append((wann, jobs or [befehl]))
    return plan


def datenbanken():
    erg = []
    for f in sorted(os.listdir(DB)) if os.path.isdir(DB) else []:
        p = os.path.join(DB, f)
        if f.endswith(".sqlite"): erg.append((f, os.path.getsize(p), os.path.getmtime(p)))
    return erg


# ---------- Seiten ----------
@app.route("/")
def uebersicht():
    return render_template("uebersicht.html", seite="uebersicht", ruhe=unruhe(), jobs=[job_status(*j) for j in JOBS],
                           defekt=defekt_zahlen(), backup=backup_status(), plan=zeitplan(), dbs=datenbanken(),
                           jetzt=datetime.datetime.now())


@app.route("/logs")
def logs():
    dateien = sorted(f for f in os.listdir(LOGS) if f.endswith(".log")) if os.path.isdir(LOGS) else []
    host = {"host:" + f: os.path.join(HOST_LOG, f) for f in ("backup-wd.log", "wd-abgleich.log") if os.path.exists(os.path.join(HOST_LOG, f))}
    pfade = {f: os.path.join(LOGS, f) for f in dateien} | host
    dateien += list(host)
    wahl = request.args.get("datei", "index_lauf.log")
    n = min(int(request.args.get("n", 300) or 300), 5000)
    q = request.args.get("q", "").strip(); nur_fehler = bool(request.args.get("fehler"))
    zeilen = ende_lesen(pfade[wahl], 2_000_000) if wahl in pfade else []
    if q: zeilen = [z for z in zeilen if q.lower() in z.lower()]
    if nur_fehler: zeilen = [z for z in zeilen if FEHLER.search(z)]
    zeilen = list(reversed(zeilen[-n:]))
    return render_template("logs.html", seite="logs", dateien=dateien, wahl=wahl, n=n, q=q, nur_fehler=nur_fehler,
                           zeilen=[(z, bool(FEHLER.search(z))) for z in zeilen],
                           mtime=os.path.getmtime(pfade[wahl]) if wahl in pfade else None)


@app.route("/berichte")
def berichte():
    liste = []
    if os.path.isdir(BERICHTE):
        for f in os.listdir(BERICHTE):
            p = os.path.join(BERICHTE, f)
            if os.path.isfile(p): liste.append((f, os.path.getsize(p), os.path.getmtime(p)))
    liste.sort(key=lambda x: -x[2])
    return render_template("berichte.html", seite="berichte", liste=liste)


def bericht_pfad(name):
    name = os.path.basename(name); p = os.path.join(BERICHTE, name)
    if not os.path.isfile(p): abort(404)
    return p


@app.route("/bericht/<name>")
def bericht(name):
    p = bericht_pfad(name); q = request.args.get("q", "").strip().lower()
    text = open(p, encoding="utf-8-sig", errors="replace").read()
    kopf = text.split("\n", 1)[0]
    trenner = "\t" if "\t" in kopf else ";" if ";" in kopf else ","
    zeilen = list(csv.reader(text.splitlines(), delimiter=trenner))
    kopf, daten = (zeilen[0], zeilen[1:]) if zeilen else ([], [])
    if q: daten = [z for z in daten if any(q in x.lower() for x in z)]
    gesamt = len(daten)
    return render_template("bericht.html", seite="berichte", name=name, kopf=kopf, daten=daten[:2000], gesamt=gesamt, q=q)


@app.route("/bericht/<name>/download")
def bericht_download(name):
    return send_file(bericht_pfad(name), as_attachment=True)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("MCSC_PORT", 8095)), debug=bool(os.environ.get("MCSC_DEBUG")))
