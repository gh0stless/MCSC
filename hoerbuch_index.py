"""Hörbuch-Index (/media/audio/Sammlung-Hörbücher*) – läuft im Container werkbank; danach nur Änderungen.
NUR LESEN auf den Medien. DB: /werkbank/db/hoerbuch_index.sqlite, Stand für den PC in db/stand/. Pfade im PC-Format (//server/media/audio\\…).
  Tabelle files  – eine Zeile je Datei (Audio mit Laufzeit/Bitrate/Tags; Bilder/NFO nur Pfad+Größe)
  Sicht   books  – eine Zeile je Titel-Ordner (Autor\\Titel): Dateien, Laufzeit, ASIN, Sprecher, Serie, Jahr, Cover, Auffälligkeiten
  Sicht   authors – eine Zeile je Autor-Ordner: Anzahl Titel, folder.jpg, artist.nfo
Aufruf:
  python3 hoerbuch_index.py            inkrementell (neue/geänderte/zuvor unlesbare Dateien)
  python3 hoerbuch_index.py --voll     alles neu
  python3 hoerbuch_index.py --nur "Sammlung-Hörbücher-Sachbuch"   nur eine Sammlung (Test)
Cover/Text je Datei: cover_art ('original' | 'platzhalter' = unser weißes Autor/Titel-Bild aus cover_placeholder.py | 'defekt' | NULL)
und cover_px ('BxH') – bei Audio für das eingebettete Bild, bei cover.jpg/folder.jpg für die Bilddatei selbst; lyrics_lrc/lyrics_sync
(.lrc daneben, mit Zeitmarken), lyrics_uslt (Text im Tag). .lrc-Spalten werden bei jedem Lauf für alle Dateien neu gesetzt.
Spalte v = Leseversion; Zeilen älterer Version werden neu gelesen."""
import io, os, re, sys, time, sqlite3, threading, concurrent.futures as cf
from werkbank import pc_pfad, DB_DIR, HOERBUCH_PRAEFIX
import mutagen
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from PIL import Image
V = 2
BILDER = {"cover.jpg", "folder.jpg"}
def platzhalter(im):
    """Hörbuch-Platzhalter (cover_placeholder.py): 600x600 weiß, Rand frei, grauer 80-px-Strich (170) mittig.
    Geprüft an erzeugten Bildern und 400 Server-cover.jpg (Logo-Cover KKH/TK zählen als original)."""
    if im.size != (600, 600): return False
    im = im.convert("L"); px = im.load()
    if any(px[x, y] < 240 for x in (3, 30, 570, 596) for y in range(0, 600, 25)): return False
    if any(px[x, y] < 240 for y in (3, 30, 570, 596) for x in range(0, 600, 25)): return False
    for y in range(60, 540):
        if all(150 <= px[x, y] <= 195 for x in (265, 280, 300, 320, 335)) and px[250, y] >= 235 and px[350, y] >= 235: return True
    return False
def bild(r, data_or_path):
    try:
        im = Image.open(io.BytesIO(data_or_path) if isinstance(data_or_path, bytes) else data_or_path)
        r["cover_px"] = "%dx%d" % im.size; r["cover_art"] = "platzhalter" if platzhalter(im) else "original"
    except Exception: r["cover_art"] = "defekt"

ROOT = "/media/audio"
DB = os.path.join(DB_DIR, "hoerbuch_index.sqlite")
FULL = "--voll" in sys.argv
ONLY = sys.argv[sys.argv.index("--nur") + 1] if "--nur" in sys.argv else None
AUDIO = (".mp3", ".m4b", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".wav")
COLS = ["path", "coll", "author", "book", "sub", "dir", "name", "ext", "size", "mtime", "dur", "bitrate", "channels", "samplerate",
        "artist", "albumartist", "album", "title", "track", "disc", "year", "genre", "publisher", "comment",
        "asin", "narrator", "series", "series_part", "tagquelle", "mb_album", "discogs", "cover", "id3v1", "ape", "err", "scanned",
        "cover_art", "cover_px", "lyrics_lrc", "lyrics_sync", "lyrics_uslt", "v"]
TX = {"asin": "TXXX:ASIN", "narrator": "TXXX:NARRATOR", "series": "TXXX:SERIES", "series_part": "TXXX:SERIES-PART",
      "tagquelle": "TXXX:Tag-Quelle", "mb_album": "TXXX:MusicBrainz Album Id", "discogs": "TXXX:DISCOGS_RELEASE_ID"}
FR = {"artist": "TPE1", "albumartist": "TPE2", "album": "TALB", "title": "TIT2", "track": "TRCK", "disc": "TPOS",
      "year": "TDRC", "genre": "TCON", "publisher": "TPUB"}

def txt(v): return "; ".join(str(x) for x in v.text) if hasattr(v, "text") else str(v)
def read(path, st):
    rel = os.path.relpath(path, ROOT).replace("\\", "/").split("/")
    r = dict.fromkeys(COLS)
    r.update(path=pc_pfad(path), coll=rel[0], author=rel[1] if len(rel) > 2 else None, book=rel[2] if len(rel) > 3 else None,
             sub="/".join(rel[3:-1]) or None, dir=pc_pfad(os.path.dirname(path)), name=rel[-1], ext=os.path.splitext(path)[1].lower(),
             size=st.st_size, mtime=st.st_mtime, scanned=time.time(), v=V)
    if r["name"].lower() in BILDER: bild(r, path); return r
    if r["ext"] == ".lrc":
        try: r["lyrics_sync"] = int(bool(re.search(r"^\[\d+:\d\d", open(path, encoding="utf-8", errors="replace").read(), re.M)))
        except Exception as e: r["err"] = f"lrc: {e}"[:200]
        return r
    if r["ext"] not in AUDIO: return r
    try:
        if r["ext"] == ".mp3":
            a = MP3(path)
            with open(path, "rb") as fh:
                fh.seek(-128, 2); r["id3v1"] = int(fh.read(3) == b"TAG")
                fh.seek(-160, 2); r["ape"] = int(b"APETAGEX" in fh.read(160))
        else: a = mutagen.File(path)
        i = a.info
        r["dur"] = i.length; r["bitrate"] = (getattr(i, "bitrate", 0) or 0) // 1000
        r["channels"] = getattr(i, "channels", None); r["samplerate"] = getattr(i, "sample_rate", None)
    except Exception as e: r["err"] = f"audio: {e}"[:200]
    if r["ext"] == ".mp3":
        try:
            t = ID3(path)
            for k, f in {**FR, **TX}.items():
                v = t.get(f)
                if v is not None: r[k] = txt(v)
            c = t.getall("COMM"); r["comment"] = txt(c[0])[:300] if c else None
            pics = t.getall("APIC"); r["cover"] = len(pics)
            if pics: bild(r, next((p for p in pics if p.type == 3), pics[0]).data)   # Vorderseite bevorzugt
            r["lyrics_uslt"] = int(any(str(u.text).strip() for u in t.getall("USLT")))
        except Exception as e: r["err"] = ((r["err"] or "") + f" tag: {e}")[:200]
    return r

con = sqlite3.connect(DB)
con.execute(f"CREATE TABLE IF NOT EXISTS files ({', '.join(c + (' PRIMARY KEY' if c == 'path' else '') for c in COLS)})")
vorhanden = {r[1] for r in con.execute("PRAGMA table_info(files)")}
for c in COLS:
    if c not in vorhanden: con.execute(f"ALTER TABLE files ADD COLUMN {c}")   # neue Spalten in bestehender DB
for c in ("dir", "coll", "author", "book", "asin", "ext", "cover_art"):
    con.execute(f"CREATE INDEX IF NOT EXISTS ix_{c} ON files({c})")
con.executescript("""
DROP VIEW IF EXISTS books;
CREATE VIEW books AS
SELECT coll, author, book, MIN(dir) AS dir,
       SUM(dur IS NOT NULL) AS audio_dateien, ROUND(SUM(dur)/3600.0, 2) AS stunden, ROUND(AVG(bitrate)) AS kbps,
       GROUP_CONCAT(DISTINCT ext) AS formate,
       COUNT(DISTINCT asin) AS asin_anzahl, MAX(asin) AS asin, MAX(narrator) AS sprecher, MAX(series) AS serie, MAX(series_part) AS teil,
       MAX(substr(year,1,4)) AS jahr, MAX(publisher) AS verlag, MAX(genre) AS genre, MAX(album) AS album_tag,
       COUNT(DISTINCT album) AS album_varianten, COUNT(DISTINCT albumartist) AS albuminterpret_varianten,
       SUM(name = 'cover.jpg') AS cover_jpg, MAX(CASE WHEN name = 'cover.jpg' THEN cover_art END) AS cover_jpg_art,
       SUM(cover > 0) AS mit_cover_eingebettet,
       SUM(dur IS NOT NULL AND cover_art = 'original') AS eingebettet_original, SUM(dur IS NOT NULL AND cover_art = 'platzhalter') AS eingebettet_platzhalter,
       SUM(lyrics_lrc = 1) AS mit_lrc,
       SUM(ext = '.mp3' AND (track IS NULL OR track = '')) AS ohne_track, SUM(id3v1 = 1) AS id3v1, SUM(ape = 1) AS ape,
       SUM(err IS NOT NULL) AS fehler, COUNT(DISTINCT sub) AS unterordner
FROM files WHERE book IS NOT NULL GROUP BY coll, author, book;
DROP VIEW IF EXISTS authors;
CREATE VIEW authors AS
SELECT coll, author, COUNT(DISTINCT book) AS titel,
       MAX(book IS NULL AND name = 'folder.jpg') AS foto, MAX(CASE WHEN book IS NULL AND name = 'folder.jpg' THEN cover_art END) AS foto_art,
       MAX(book IS NULL AND name = 'artist.nfo') AS nfo,
       ROUND(SUM(dur)/3600.0, 1) AS stunden
FROM files WHERE author IS NOT NULL GROUP BY coll, author;
""")
known = {} if FULL else {p: (s, m) for p, s, m in con.execute(
    "SELECT path, size, mtime FROM files WHERE (err IS NULL OR err NOT LIKE '%Errno%') AND v = ?", (V,))}   # ältere Leseversion -> neu lesen
roots = [d for d in os.listdir(ROOT) if d.startswith(HOERBUCH_PRAEFIX)]
if ONLY: roots = [d for d in roots if d == ONLY]
print("in DB:", len(known), "| Sammlungen:", roots, flush=True)
todo, seen, n_dirs, t0 = [], set(), 0, time.time()
lesefehler = []   # os.walk verschluckt Fehler sonst still -> abgebrochener Server-Zugriff sähe aus wie gelöschte Titel
for r0 in roots:
    for dp, dn, fs in os.walk(os.path.join(ROOT, r0), onerror=lesefehler.append):
        dn[:] = [d for d in dn if d not in ("$RECYCLE.BIN", "#recycle", "@eaDir", ".@__thumb")]
        n_dirs += 1
        for f in fs:
            p = os.path.join(dp, f)
            try: st = os.stat(p)
            except OSError: continue
            key = pc_pfad(p); seen.add(key); k = known.get(key)
            if not k or k[0] != st.st_size or abs(k[1] - st.st_mtime) > 1: todo.append((p, st))
        if n_dirs % 2000 == 0: print(f"  {n_dirs} Ordner, {len(seen)} Dateien, {len(todo)} zu lesen ({time.time()-t0:.0f} s)", flush=True)
if not seen: sys.exit("Server lieferte keine Dateien – Abbruch, DB unverändert.")
scope = tuple(pc_pfad(os.path.join(ROOT, r0)) + "\\" for r0 in roots)
gone = [p for p, in con.execute("SELECT path FROM files") if p.startswith(scope) and p not in seen]   # auch ältere Leseversion
print(f"Dateien: {len(seen)} | neu/geändert: {len(todo)} | verschwunden: {len(gone)}", flush=True)
# Schutz (wie Musik-Index nach dem 10.11.2026): bei Lesefehlern oder ungewöhnlich vielen „verschwundenen“ Dateien nichts austragen
if lesefehler or len(gone) > max(500, 0.05 * len(seen)):
    print(f"WARNUNG: {len(lesefehler)} Lesefehler (z. B. {lesefehler[0] if lesefehler else '-'}) bzw. {len(gone)} verschwunden – "
          "es wird NICHTS ausgetragen. Server-Zugriff prüfen und neu starten.", flush=True)
    gone = []
buf, done = [], 0
def flush():
    global buf
    con.executemany(f"INSERT OR REPLACE INTO files ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})", [[x[c] for c in COLS] for x in buf])
    con.commit(); buf = []
with cf.ThreadPoolExecutor(8) as ex:
    for x in ex.map(lambda a: read(*a), todo):
        buf.append(x); done += 1
        if len(buf) >= 500: flush()
        if done % 2000 == 0: print(f"  gelesen {done}/{len(todo)} ({time.time()-t0:.0f} s)", flush=True)
flush()
con.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone]); con.commit()
# .lrc für alle Audiodateien aus dem aktuellen Bestand (eine später hinzugekommene .lrc ändert die Audiodatei nicht)
con.execute("""UPDATE files SET
    lyrics_lrc  = EXISTS (SELECT 1 FROM files l WHERE l.path = substr(files.path, 1, length(files.path) - length(files.ext)) || '.lrc'),
    lyrics_sync = (SELECT l.lyrics_sync FROM files l WHERE l.path = substr(files.path, 1, length(files.path) - length(files.ext)) || '.lrc')
    WHERE dur IS NOT NULL"""); con.commit()
n, e = con.execute("SELECT COUNT(*), SUM(err LIKE '%Errno%') FROM files WHERE dur IS NOT NULL OR err IS NOT NULL").fetchone()
b, = con.execute("SELECT COUNT(*) FROM books").fetchone()
print(f"FERTIG in {time.time()-t0:.0f} s | Audio: {n} | Titel: {b} | nicht lesbar: {e or 0} | DB: {DB}", flush=True)
if e: print("HINWEIS: Dateien nicht lesbar (Errno) – siehe Spalte err.", flush=True)
