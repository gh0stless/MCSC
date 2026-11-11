"""Gemeinsame Helfer für alle Werkbank-Skripte (MCSC-Dienstcontainer „werkbank“).

Einbindungen im Container:
  /media        <- /mnt/user/media                      (lesen + schreiben)
  /werkbank     <- /mnt/user/appdata/werkbank           (skripte/, db/, berichte/, logs/)
  /jellyfin     <- /mnt/cache/appdata/Jellyfin/data     (nur lesen)
  /host/lock    <- /var/lock   (nur lesen, Sperrdatei von backup-wd.sh)
  /host/run     <- /run        (nur lesen, mover.pid)
"""
import os, time, fcntl, datetime

WB = "/werkbank"
DB_DIR = os.path.join(WB, "db")
BERICHTE = os.path.join(WB, "berichte")
LOGS = os.path.join(WB, "logs")
SMB_PREFIX = os.environ.get("MCSC_SMB_PREFIX", "//server/media/")   # so stehen die Pfade in den PC-Indizes (Freigabe am PC)
MUSIK_PRAEFIX = os.environ.get("MCSC_MUSIK_PRAEFIX", "Sammlung-MP3")          # Ordner unter /media/audio für den Musik-Index
HOERBUCH_PRAEFIX = os.environ.get("MCSC_HOERBUCH_PRAEFIX", "Sammlung-Hörbücher")  # … für den Hörbuch-Index
JELLYFIN_URL = os.environ.get("MCSC_JELLYFIN_URL", "http://jellyfin:8096")


def smb_pfad(p):
    """/media/filme/X/Y.mkv -> //server/media/filme/X/Y.mkv (für Berichte, am PC klickbar)"""
    return SMB_PREFIX + p[len("/media/"):] if p.startswith("/media/") else p


def lokal_pfad(p):
    """//server/media/filme\\X\\Y.mkv -> /media/filme/X/Y.mkv"""
    p = p.replace("\\", "/")
    return "/media/" + p[len(SMB_PREFIX):] if p.lower().startswith(SMB_PREFIX.lower()) else p


def pc_pfad(p):
    """Index-Schlüssel wie am PC (so stehen die Pfade in allen Index-DBs):
    /media/audio/Sammlung-X/a/b.mp3 -> //server/media/audio\\Sammlung-X\\a\\b.mp3"""
    if not p.startswith("/media/"):
        return p
    lib, _, rest = p[len("/media/"):].partition("/")
    return SMB_PREFIX + lib + ("\\" + rest.replace("/", "\\") if rest else "")


def stand_sichern(db, log=print):
    """Konsistente Kopie einer DB nach db/stand/ (die holt der PC; die Arbeits-DB kann mitten im Lauf sein)."""
    import sqlite3
    ziel_dir = os.path.join(DB_DIR, "stand"); os.makedirs(ziel_dir, exist_ok=True)
    ziel = os.path.join(ziel_dir, os.path.basename(db)); tmp = ziel + ".part"
    if os.path.exists(tmp): os.remove(tmp)
    q = sqlite3.connect(db); z = sqlite3.connect(tmp)
    with z: q.backup(z)
    q.close(); z.close(); os.replace(tmp, ziel)
    log(f"Stand gesichert: {ziel}")


def unruhe():
    """Grund, warum gerade nicht gearbeitet werden soll – oder None"""
    if os.path.exists("/host/run/mover.pid"):
        return "Mover läuft"
    try:
        with open("/host/lock/backup-wd.lock") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                return "WD-Backup läuft"
    except FileNotFoundError:
        pass
    return None


def warten_auf_ruhe(log=print, takt=60):
    gemeldet = None
    while (g := unruhe()):
        if g != gemeldet:
            log(f"Pause: {g} – warte"); gemeldet = g
        time.sleep(takt)
    if gemeldet:
        log("weiter")


class Log:
    def __init__(self, name):
        os.makedirs(LOGS, exist_ok=True)
        self.datei = os.path.join(LOGS, name + ".log")

    def __call__(self, s):
        z = f"{datetime.datetime.now():%F %T} {s}"
        print(z, flush=True)
        with open(self.datei, "a", encoding="utf-8") as f:
            f.write(z + "\n")


def niedrige_prioritaet():
    """CPU nice 19 + Platten-Priorität idle für diesen Prozess und seine Kinder"""
    try: os.nice(19)
    except OSError: pass
    try: os.system(f"ionice -c3 -p {os.getpid()} >/dev/null 2>&1")
    except Exception: pass
