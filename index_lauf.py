"""Alle Server-Indizes nacheinander inkrementell aktualisieren (löst den früheren PC-Nachtlauf nacht_db_lauf ab).
  1. musik_index.py    -> db/musik_index.sqlite    (MP3-Sammlungen)
  2. hoerbuch_index.py -> db/hoerbuch_index.sqlite (Hörbücher)
  3. video_index.py    -> db/video_index.sqlite    (Filme, Serien, Videos + Jellyfin-Sicht)
Nach jedem erfolgreichen Lauf: konsistente Kopie nach db/stand/ – die holt der PC (werkbank_db.py im Repo).
Wartet vorher, solange WD-Backup oder Mover laufen. Protokoll: logs/index_lauf.log, je Index logs/<name>.log.
Aufruf: python3 index_lauf.py [musik|hoerbuch|video ...]"""
import os, sys, time, subprocess
from werkbank import Log, warten_auf_ruhe, niedrige_prioritaet, stand_sichern, DB_DIR, LOGS

HIER = os.path.dirname(os.path.abspath(__file__))
LAEUFE = {"musik": ("musik_index.py", "musik_index.sqlite"),
          "hoerbuch": ("hoerbuch_index.py", "hoerbuch_index.sqlite"),
          "video": ("video_index.py", "video_index.sqlite")}
log = Log("index_lauf")
auswahl = [a for a in sys.argv[1:] if a in LAEUFE] or list(LAEUFE)

niedrige_prioritaet()
log(f"=== Start: {', '.join(auswahl)}")
for name in auswahl:
    skript, db = LAEUFE[name]
    warten_auf_ruhe(log)
    t0 = time.time(); letzte = ""
    with open(os.path.join(LOGS, skript.replace(".py", ".log")), "w", encoding="utf-8") as einzel:
        p = subprocess.Popen([sys.executable, "-u", os.path.join(HIER, skript)], cwd=HIER,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        for z in p.stdout:
            einzel.write(z); einzel.flush(); letzte = z.strip() or letzte
        p.wait()
    log(f"{name}: rc={p.returncode} in {(time.time() - t0) / 60:.0f} min | {letzte}")
    if p.returncode == 0:
        stand_sichern(os.path.join(DB_DIR, db), log)
        if name == "video" and os.path.exists(os.path.join(DB_DIR, "jellyfin_kopie.sqlite")):
            stand_sichern(os.path.join(DB_DIR, "jellyfin_kopie.sqlite"), log)
log("=== Ende")
