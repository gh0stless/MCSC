"""Musik-Index (MP3-Sammlungen unter /media/audio/Sammlung-MP3*) – läuft im Container werkbank.
NUR LESEN auf den Medien. DB: /werkbank/db/musik_index.sqlite (Tabelle files, eine Zeile je Datei), Stand für den PC in db/stand/.
Pfade stehen im PC-Format (//server/media/audio\\Sammlung…\\x.mp3), damit die PC-Skripte sie direkt öffnen können.
Aufruf:  python3 musik_index.py            (inkrementell: neue/geänderte Dateien lesen, verschwundene austragen)
         python3 musik_index.py --voll     (alles neu lesen)
Abfrage-Beispiel: SELECT dir, COUNT(*), SUM(dur)/60 FROM files WHERE ext='.mp3' AND albumartist LIKE 'Air' GROUP BY dir
Cover/Liedtext je MP3: cover (Anzahl eingebetteter Bilder), cover_art ('original' | 'platzhalter' = unser generiertes Textcover | NULL),
cover_px ('BxH'), ordner_cover (folder/cover/front.jpg|png im Ordner), lyrics_lrc (.lrc daneben), lyrics_sync (1 = mit Zeitmarken,
0 = nur Text), lyrics_txt (.txt daneben), lyrics_uslt (Text im Tag). Ordner-Cover und Liedtext-Spalten werden bei jedem Lauf für alle
MP3 neu gesetzt (eine später hinzugekommene .lrc ändert die MP3 nicht). Spalte v = Leseversion; Zeilen älterer Version werden neu gelesen."""
import io, os, re, sys, time, sqlite3, ntpath, threading, concurrent.futures as cf
from werkbank import pc_pfad, DB_DIR, MUSIK_PRAEFIX
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from PIL import Image
V = 2
PALETTE = [(38, 50, 72), (60, 40, 70), (35, 64, 58), (72, 46, 38), (44, 44, 52), (30, 58, 80), (70, 60, 30)]   # placeholder_covers*.py
def platzhalter(im):
    """Unser Platzhalter: 600x600, einfarbig aus PALETTE, heller Rahmen bei 24 px (geprüft an allen 138 erzeugten Bildern)."""
    if im.size != (600, 600): return False
    im = im.convert("RGB"); near = lambda p, q, tol: all(abs(a - b) <= tol for a, b in zip(p, q))
    for bg in PALETTE:
        if all(near(im.getpixel(xy), bg, 6) for xy in ((5, 5), (594, 5), (5, 594), (594, 594), (300, 10))):
            rand = tuple(min(255, c + 60) for c in bg)
            return near(im.getpixel((25, 300)), rand, 12) and near(im.getpixel((300, 575)), rand, 12)
    return False
ORDNER_COVER = {"folder.jpg", "folder.png", "cover.jpg", "cover.png", "front.jpg", "front.png"}

ROOT = "/media/audio"
DB = os.path.join(DB_DIR, "musik_index.sqlite")
FULL = "--voll" in sys.argv
COLS = ["path", "coll", "dir", "name", "ext", "size", "mtime", "dur", "bitrate", "vbr",
        "artist", "albumartist", "album", "title", "track", "disc", "year", "genre", "label", "catno",
        "compilation", "mb_album", "mb_rg", "mb_albumartist", "mb_track", "discogs", "tagquelle",
        "cover", "lyrics_lrc", "err", "scanned",
        "cover_art", "cover_px", "ordner_cover", "lyrics_sync", "lyrics_txt", "lyrics_uslt", "v"]
TAGS = {"artist": "TPE1", "albumartist": "TPE2", "album": "TALB", "title": "TIT2", "track": "TRCK", "disc": "TPOS",
        "year": "TDRC", "genre": "TCON", "label": "TPUB", "compilation": "TCMP",
        "catno": "TXXX:CATALOGNUMBER", "mb_album": "TXXX:MusicBrainz Album Id", "mb_rg": "TXXX:MusicBrainz Release Group Id",
        "mb_albumartist": "TXXX:MusicBrainz Album Artist Id", "mb_track": "TXXX:MusicBrainz Release Track Id",
        "discogs": "TXXX:DISCOGS_RELEASE_ID", "tagquelle": "TXXX:Tag-Quelle"}

def read(path, st):
    rel = os.path.relpath(path, ROOT)
    r = dict.fromkeys(COLS)
    r.update(path=pc_pfad(path), coll=rel.split(os.sep)[0], dir=pc_pfad(os.path.dirname(path)), name=os.path.basename(path),
             ext=os.path.splitext(path)[1].lower(), size=st.st_size, mtime=st.st_mtime, scanned=time.time(), v=V)
    if r["ext"] == ".lrc":                                            # Zeitmarken? (wird am Ende auf die MP3 übertragen)
        try: r["lyrics_sync"] = int(bool(re.search(r"^\[\d+:\d\d", open(path, encoding="utf-8", errors="replace").read(), re.M)))
        except Exception as e: r["err"] = f"lrc: {e}"[:200]
    if r["ext"] == ".mp3":
        try:
            a = MP3(path); r["dur"] = a.info.length; r["bitrate"] = a.info.bitrate // 1000
            r["vbr"] = int(getattr(a.info, "bitrate_mode", 0) not in (0, 1))
        except Exception as e: r["err"] = f"audio: {e}"[:200]
        try:
            t = ID3(path)
            for k, f in TAGS.items():
                v = t.get(f)
                if v is not None: r[k] = "; ".join(str(x) for x in v.text) if hasattr(v, "text") else str(v)
            pics = t.getall("APIC"); r["cover"] = len(pics)
            r["lyrics_uslt"] = int(any(str(u.text).strip() for u in t.getall("USLT")))
            if pics:
                pic = next((p for p in pics if p.type == 3), pics[0])   # Vorderseite bevorzugt
                try:
                    im = Image.open(io.BytesIO(pic.data)); r["cover_px"] = "%dx%d" % im.size
                    r["cover_art"] = "platzhalter" if platzhalter(im) else "original"
                except Exception: r["cover_art"] = "defekt"
        except Exception as e: r["err"] = ((r["err"] or "") + f" tag: {e}")[:200]
    return r

con = sqlite3.connect(DB)
con.execute(f"CREATE TABLE IF NOT EXISTS files ({', '.join(c + (' PRIMARY KEY' if c == 'path' else '') for c in COLS)})")
vorhanden = {r[1] for r in con.execute("PRAGMA table_info(files)")}
for c in COLS:
    if c not in vorhanden: con.execute(f"ALTER TABLE files ADD COLUMN {c}")   # neue Spalten in bestehender DB
for c in ("dir", "albumartist", "album", "catno", "mb_album", "dur", "coll", "cover_art"):
    con.execute(f"CREATE INDEX IF NOT EXISTS ix_{c} ON files({c})")
# Zeilen älterer Leseversion gelten als unbekannt -> werden neu gelesen
known = {} if FULL else {p: (s, m) for p, s, m in con.execute(
    "SELECT path, size, mtime FROM files WHERE (err IS NULL OR err NOT LIKE '%Errno%') AND v = ?", (V,))}
print("in DB:", len(known), "| lese Verzeichnisse ...", flush=True)

todo, seen, n_dirs = [], set(), 0
t0 = time.time()
lesefehler = []   # os.walk verschluckt Fehler sonst still -> ein abgebrochener Server-Zugriff sähe aus wie gelöschte Sammlungen
for dp, dn, fs in os.walk(ROOT, onerror=lesefehler.append):
    dn[:] = [d for d in dn if d not in ("$RECYCLE.BIN", "#recycle", "@eaDir", ".@__thumb")]
    if os.path.normpath(dp) == os.path.normpath(ROOT): dn[:] = [d for d in dn if d.startswith(MUSIK_PRAEFIX) or d == "_Doppelt"]
    n_dirs += 1
    for f in fs:
        p = os.path.join(dp, f)
        try: st = os.stat(p)
        except OSError: continue
        key = pc_pfad(p); seen.add(key)
        k = known.get(key)
        if not k or k[0] != st.st_size or abs(k[1] - st.st_mtime) > 1: todo.append((p, st))
    if n_dirs % 2000 == 0: print(f"  {n_dirs} Ordner, {len(seen)} Dateien, {len(todo)} zu lesen ({time.time()-t0:.0f} s)", flush=True)
if not seen: sys.exit("Server lieferte keine Dateien – Abbruch, DB unverändert.")
gone = [p for p, in con.execute("SELECT path FROM files") if p not in seen]   # alle Zeilen, auch ältere Leseversion
print(f"Dateien: {len(seen)} | neu/geändert: {len(todo)} | verschwunden: {len(gone)}", flush=True)
# Schutz (Lauf 10.11.2026: Zugriff brach nach Sammlung-MP3-Alben ab -> 65.197 Zeilen fälschlich ausgetragen):
# bei Lesefehlern oder ungewöhnlich vielen „verschwundenen“ Dateien nichts austragen, nur Neues/Geändertes schreiben.
if lesefehler or len(gone) > max(500, 0.05 * len(seen)):
    print(f"WARNUNG: {len(lesefehler)} Lesefehler (z. B. {lesefehler[0] if lesefehler else '-'}) bzw. {len(gone)} verschwunden – "
          "es wird NICHTS ausgetragen. Server-Zugriff prüfen und neu starten.", flush=True)
    gone = []

lock = threading.Lock(); done = 0; buf = []
def flush():
    global buf
    con.executemany(f"INSERT OR REPLACE INTO files ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})", [[r[c] for c in COLS] for r in buf])
    con.commit(); buf = []
with cf.ThreadPoolExecutor(8) as ex:
    for r in ex.map(lambda x: read(*x), todo):
        buf.append(r); done += 1
        if len(buf) >= 500: flush()
        if done % 2000 == 0: print(f"  gelesen {done}/{len(todo)} ({time.time()-t0:.0f} s)", flush=True)
flush()
con.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone]); con.commit()
# Ordner-Cover und Liedtexte für ALLE MP3 aus dem aktuellen Bestand (unabhängig davon, ob die MP3 neu gelesen wurde)
con.execute("CREATE TEMP TABLE oc (dir PRIMARY KEY)")
con.executemany("INSERT OR IGNORE INTO oc VALUES (?)", [(ntpath.dirname(p),) for p in seen if ntpath.basename(p).lower() in ORDNER_COVER])
con.execute("""UPDATE files SET ordner_cover = (dir IN (SELECT dir FROM oc)),
    lyrics_lrc  = EXISTS (SELECT 1 FROM files l WHERE l.path = substr(files.path, 1, length(files.path) - 4) || '.lrc'),
    lyrics_sync = (SELECT l.lyrics_sync FROM files l WHERE l.path = substr(files.path, 1, length(files.path) - 4) || '.lrc'),
    lyrics_txt  = EXISTS (SELECT 1 FROM files l WHERE l.path = substr(files.path, 1, length(files.path) - 4) || '.txt')
    WHERE ext = '.mp3'"""); con.commit()
mp3, = con.execute("SELECT COUNT(*) FROM files WHERE ext='.mp3'").fetchone()
print(f"FERTIG in {time.time()-t0:.0f} s | DB: {DB} | MP3: {mp3}", flush=True)
