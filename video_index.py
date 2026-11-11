"""Video-Index (/media/filme, /serien, /video) – läuft im Container werkbank; danach nur Änderungen.
NUR LESEN auf den Medien. DB: /werkbank/db/video_index.sqlite, Stand für den PC in db/stand/. Pfade im PC-Format (//server/media/filme\\…, siehe MCSC_SMB_PREFIX).
  Tabelle files   – eine Zeile je Datei.
                    Videos per ffprobe: Laufzeit, Codec/Profil, Auflösung, Seitenverhältnis, Bittiefe, Farbraum, HDR, Zeilensprung,
                    Bitraten, Spur-Zusammenfassung, Kapitel(-namen), Cover, Container-Tags.
                    NFO: Typ, Titel, Originaltitel, Jahr, IDs, Sammlung, Genres, Regie, Darsteller, Studio, Freigabe, Bewertung, Plot, Staffel/Folge.
                    Bilder: Breite/Höhe. Externe Untertitel: Sprache + Flags aus dem Dateinamen.
                    art: 'haupt' | 'extra' (in featurettes\\, behind the scenes\\ … ) | 'trickplay' | NULL
  Tabelle streams – eine Zeile je Spur (Video/Ton/Untertitel): Codec, Sprache, Name, Kanäle/Layout, Bitrate, Abtastrate,
                    Flags (default/forced/Kommentar/Hörgeschädigte/Sehbehinderte), Größe (exakt aus MKV-Statistik, sonst geschätzt).
  Tabelle jf      – Jellyfin-Sicht je Datei (aus nur-lesender Kopie der Jellyfin-DB, jellyfin_kopie.sqlite): Titel, Genres, Studios,
                    Tags, Freigabe, Bewertungen, Inhalt, Serie/Staffel/Folge, IDs, Regie, Darsteller, gesehen/Favorit.
  Sicht   titles  – eine Zeile je Titel-Ordner (Bibliothek\\[Kategorie\\]Titel).
Aufruf:
  python3 video_index.py            inkrementell
  python3 video_index.py --voll     alles neu
Spalte v = Leseversion; Zeilen älterer Version werden neu gelesen (V=2 seit 09.11.2026: Spuren, Tags, volle NFO, Bilder, Jellyfin)."""
import os, sys, json, time, sqlite3, ntpath, subprocess, concurrent.futures as cf
from werkbank import pc_pfad, DB_DIR, SMB_PREFIX
import xml.etree.ElementTree as ET
V = 2
ROOTS = ["/media/filme", "/media/serien", "/media/video"]
DB = os.path.join(DB_DIR, "video_index.sqlite"); JF_KOPIE = os.path.join(DB_DIR, "jellyfin_kopie.sqlite")
JF_DB = "/jellyfin/jellyfin.db"   # nur-lesend eingebunden
# Jellyfin-Bibliothekspfade (im Jellyfin-Container) -> Index-Pfade; anpassbar per MCSC_JF_PFADE="/data/movies/=filme,/data/tvshows/=serien,..."
JF_PFAD = {a: SMB_PREFIX + b + "\\" for a, b in (x.split("=") for x in os.environ.get(
    "MCSC_JF_PFADE", "/data/movies/=filme,/data/tvshows/=serien,/data/video/=video").split(","))}
FULL = "--voll" in sys.argv
if "--test" in sys.argv:   # Probe: nur ein Unterordner, eigene Test-DB
    ROOTS = [sys.argv[sys.argv.index("--test") + 1]]; DB = DB.replace(".sqlite", "_test.sqlite")
VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mpg", ".mpeg", ".vob", ".wmv", ".mov", ".webm", ".flv", ".iso", ".divx", ".ogm"}
SUBS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt", ".smi"}
BILD = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
EXTRA_DIRS = {"featurettes", "behind the scenes", "deleted scenes", "interviews", "scenes", "samples", "shorts", "trailers", "clips", "other", "extras", "backdrops", "theme-music"}
COLS = ["path", "lib", "titel", "dir", "name", "ext", "size", "mtime", "art",
        "dur", "vcodec", "vprofil", "breite", "hoehe", "dar", "fps", "scan", "bittiefe", "pix_fmt", "farbraum", "primaries", "transfer", "hdr",
        "bitrate", "vbitrate", "container_titel", "tags", "cover",
        "audio_n", "audio", "audio_sprachen", "kanaele_max", "sub_n", "sub_sprachen", "kapitel", "kapitel_namen",
        "nfo_typ", "nfo_titel", "nfo_originaltitel", "nfo_jahr", "imdb", "tmdb", "nfo_set", "nfo_genres", "nfo_regie", "nfo_darsteller",
        "nfo_studio", "nfo_freigabe", "nfo_bewertung", "nfo_plot", "nfo_staffel", "nfo_folge",
        "bild_breite", "bild_hoehe", "sub_ext_sprache", "sub_ext_flags", "err", "scanned", "v"]
SCOLS = ["path", "idx", "typ", "codec", "profil", "sprache", "name", "kanaele", "layout", "bitrate", "samplerate", "bittiefe",
         "standard", "forced", "kommentar", "hoergeschaedigt", "sehbehindert", "bytes", "bytes_exakt"]

def titel_ordner(path, root):
    """Bibliothek + Titel-Ordner: filme\\<Titel>, serien\\<Serie>, video\\<Kategorie>\\<Titel>."""
    parts = os.path.relpath(path, root).split(os.sep)
    lib = os.path.basename(root)
    if lib == "video": return lib, os.path.join(*parts[:2]) if len(parts) > 2 else parts[0]
    return lib, parts[0] if len(parts) > 1 else ""

def tag(d, key):
    """Tag ohne Rücksicht auf Groß/Klein und Sprachsuffix (MKV: BPS-eng, NUMBER_OF_BYTES-eng)."""
    for k, v in (d or {}).items():
        if k.upper() == key or k.upper().startswith(key + "-"): return v
    return None

def bit_tiefe(s):
    if s.get("bits_per_raw_sample"): return int(s["bits_per_raw_sample"])
    pf = s.get("pix_fmt") or ""
    return 12 if "12" in pf else 10 if "10" in pf else 8 if pf else None

def read(path, st, root):
    r = dict.fromkeys(COLS); spuren = []
    lib, t = titel_ordner(path, root)
    r.update(path=pc_pfad(path), lib=lib, titel=t.replace("/", "\\"), dir=pc_pfad(os.path.dirname(path)), name=os.path.basename(path),
             ext=os.path.splitext(path)[1].lower(), size=st.st_size, mtime=st.st_mtime, scanned=time.time(), v=V)
    low = [p.lower() for p in os.path.relpath(path, root).split(os.sep)[:-1]]
    if any(p.endswith(".trickplay") for p in low): r["art"] = "trickplay"; return r, spuren
    if r["ext"] in VIDEO:
        r["art"] = "extra" if any(p in EXTRA_DIRS for p in low) else "haupt"
        if r["ext"] == ".iso": return r, spuren
        try:
            j = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", path],
                                          capture_output=True, timeout=120).stdout or b"{}")
            f = j.get("format", {}); ss = j.get("streams", [])
            v = next((s for s in ss if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), {})
            a = [s for s in ss if s.get("codec_type") == "audio"]; u = [s for s in ss if s.get("codec_type") == "subtitle"]
            lang = lambda s: s.get("tags", {}).get("language", "und")
            r["dur"] = float(f["duration"]) if f.get("duration") else None
            r["bitrate"] = int(f["bit_rate"]) // 1000 if f.get("bit_rate") else None
            ft = f.get("tags", {})
            r["container_titel"] = tag(ft, "TITLE")
            r["tags"] = json.dumps(ft, ensure_ascii=False) if ft else None
            r["vcodec"] = v.get("codec_name"); r["vprofil"] = v.get("profile"); r["breite"] = v.get("width"); r["hoehe"] = v.get("height")
            r["dar"] = v.get("display_aspect_ratio") if v.get("display_aspect_ratio") not in (None, "0:1") else None
            if v.get("avg_frame_rate", "0/0") != "0/0":
                n, d = v["avg_frame_rate"].split("/"); r["fps"] = round(int(n) / int(d), 3) if int(d) else None
            r["scan"] = v.get("field_order"); r["pix_fmt"] = v.get("pix_fmt"); r["bittiefe"] = bit_tiefe(v) if v else None
            r["farbraum"] = v.get("color_space"); r["primaries"] = v.get("color_primaries"); r["transfer"] = v.get("color_transfer")
            vb = v.get("bit_rate") or tag(v.get("tags"), "BPS")
            r["vbitrate"] = int(vb) // 1000 if vb and str(vb).isdigit() else None
            tr = v.get("color_transfer", "")
            r["hdr"] = "DV" if any(sd.get("side_data_type", "").startswith("DOVI") for sd in v.get("side_data_list", [])) else \
                       "HDR10" if tr == "smpte2084" else "HLG" if tr == "arib-std-b67" else None
            r["cover"] = int(any(s.get("disposition", {}).get("attached_pic") for s in ss) or
                             any(s.get("codec_type") == "attachment" and str(s.get("tags", {}).get("mimetype", "")).startswith("image") for s in ss))
            r["audio_n"] = len(a); r["sub_n"] = len(u)
            r["audio"] = "; ".join(f'{lang(s)}:{s.get("codec_name")} {s.get("channels", "?")}ch' for s in a)
            r["audio_sprachen"] = ",".join(sorted({lang(s) for s in a})); r["sub_sprachen"] = ",".join(sorted({lang(s) for s in u}))
            r["kanaele_max"] = max((s.get("channels", 0) for s in a), default=None)
            ch = j.get("chapters", []); r["kapitel"] = len(ch)
            namen = [c.get("tags", {}).get("title", "") for c in ch]
            if any(n and not n.lower().startswith(("chapter", "kapitel")) and not n.replace(":", "").replace(".", "").isdigit() for n in namen):
                r["kapitel_namen"] = " | ".join(namen)[:2000]
            for s in ss:
                typ = s.get("codec_type")
                if typ not in ("video", "audio", "subtitle") or s.get("disposition", {}).get("attached_pic"): continue
                dp = s.get("disposition", {}); stg = s.get("tags", {})
                br = s.get("bit_rate") or tag(stg, "BPS"); br = int(br) if br and str(br).isdigit() else None
                nb = tag(stg, "NUMBER_OF_BYTES"); exakt = bool(nb and str(nb).isdigit())
                byt = int(nb) if exakt else (int(br * r["dur"] / 8) if br and r["dur"] else None)
                spuren.append(dict(path=r["path"], idx=s.get("index"), typ=typ, codec=s.get("codec_name"), profil=s.get("profile"),
                                   sprache=stg.get("language"), name=tag(stg, "TITLE") or tag(stg, "HANDLER_NAME"),
                                   kanaele=s.get("channels"), layout=s.get("channel_layout"), bitrate=br // 1000 if br else None,
                                   samplerate=int(s["sample_rate"]) if s.get("sample_rate") else None,
                                   bittiefe=bit_tiefe(s) if typ == "video" else (s.get("bits_per_raw_sample") and int(s["bits_per_raw_sample"])) or None,
                                   standard=dp.get("default"), forced=dp.get("forced"), kommentar=dp.get("comment"),
                                   hoergeschaedigt=dp.get("hearing_impaired"), sehbehindert=dp.get("visual_impaired"),
                                   bytes=byt, bytes_exakt=int(exakt)))
            if not ss: r["err"] = "ffprobe: keine Spuren"
        except Exception as e: r["err"] = f"ffprobe: {e}"[:200]
    elif r["ext"] == ".nfo":
        try:
            x = ET.parse(path).getroot(); g = lambda k: (x.findtext(k) or "").strip() or None
            alle = lambda k: [e.text.strip() for e in x.findall(k) if e.text and e.text.strip()]
            r["nfo_typ"] = x.tag; r["nfo_titel"] = g("title"); r["nfo_originaltitel"] = g("originaltitle")
            r["nfo_jahr"] = g("year") or (g("premiered") or g("aired") or "")[:4] or None
            for uid in x.findall("uniqueid"):
                if uid.get("type") in ("imdb", "tmdb"): r[uid.get("type")] = (uid.text or "").strip() or None
            r["imdb"] = r["imdb"] or g("imdbid") or g("imdb_id"); r["tmdb"] = r["tmdb"] or g("tmdbid")
            s = x.find("set"); r["nfo_set"] = (s.findtext("name") or s.text or "").strip() or None if s is not None else None
            r["nfo_genres"] = "|".join(alle("genre")) or None; r["nfo_regie"] = "|".join(alle("director")) or None
            r["nfo_studio"] = "|".join(alle("studio")) or None; r["nfo_freigabe"] = g("mpaa")
            r["nfo_bewertung"] = g("rating") or (x.findtext("ratings/rating/value") or "").strip() or None
            r["nfo_plot"] = g("plot"); r["nfo_staffel"] = g("season"); r["nfo_folge"] = g("episode")
            ds = [(a_.findtext("name") or "").strip() + (f' ({a_.findtext("role").strip()})' if (a_.findtext("role") or "").strip() else "")
                  for a_ in x.findall("actor")[:15]]
            r["nfo_darsteller"] = "|".join(d for d in ds if d) or None
        except Exception as e: r["err"] = f"nfo: {e}"[:200]
    elif r["ext"] in BILD:
        try:
            from PIL import Image
            with Image.open(path) as im: r["bild_breite"], r["bild_hoehe"] = im.size
        except Exception as e: r["err"] = f"bild: {e}"[:200]
    elif r["ext"] in SUBS:   # Film.de.forced.srt, Film.en.sdh.srt, Film.ger.idx
        teile = [p.lower() for p in r["name"].split(".")[1:-1]]
        flags = [p for p in teile if p in ("forced", "sdh", "hi", "cc", "default", "foreign")]
        spr = [p for p in teile if p not in flags and p.isalpha() and 2 <= len(p) <= 3]
        r["sub_ext_sprache"] = spr[-1] if spr else None; r["sub_ext_flags"] = ",".join(flags) or None
    return r, spuren

def jellyfin(con):
    """Kopie der Jellyfin-DB ziehen (sqlite-Backup-API aus der nur-lesend eingebundenen DB) und Tabelle jf bauen."""
    tmp = JF_KOPIE + ".part"
    try:
        if os.path.exists(tmp): os.remove(tmp)
        q = sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True); k = sqlite3.connect(tmp)
        with k: q.backup(k)
        q.close(); ok = k.execute("PRAGMA quick_check").fetchone()[0] == "ok"; k.close()
        if not ok: raise RuntimeError("Kopie defekt")
        os.replace(tmp, JF_KOPIE)
    except Exception as e:
        print(f"Jellyfin: Kopie fehlgeschlagen ({e}) – Tabelle jf bleibt wie sie ist", flush=True)
        if os.path.exists(tmp): os.remove(tmp)
        return
    con.execute("ATTACH DATABASE ? AS j", (JF_KOPIE,))
    pfad = "CASE " + " ".join(f"WHEN b.Path LIKE '{a}%' THEN '{z}' || replace(substr(b.Path, {len(a) + 1}), '/', '\\')" for a, z in JF_PFAD.items()) + " END"
    con.executescript(f"""
      DROP TABLE IF EXISTS jf;
      CREATE TABLE jf AS SELECT {pfad} AS path, b.Id AS jf_id, replace(b.Type, 'MediaBrowser.Controller.Entities.', '') AS jf_typ,
        b.Name AS name, b.OriginalTitle AS originaltitel, b.ProductionYear AS jahr, b.Genres AS genres, b.Studios AS studios, b.Tags AS tags,
        b.OfficialRating AS freigabe, b.CommunityRating AS bewertung, b.CriticRating AS kritik, b.Overview AS inhalt, b.Tagline AS tagline,
        b.SeriesName AS serie, b.ParentIndexNumber AS staffel, b.IndexNumber AS folge, b.ExtraType AS extra_typ,
        b.RunTimeTicks / 10000000.0 AS dur, b.Width AS breite, b.Height AS hoehe, b.DateCreated AS hinzugefuegt,
        (SELECT ProviderValue FROM j.BaseItemProviders WHERE ItemId = b.Id AND ProviderId = 'Imdb') AS imdb,
        (SELECT ProviderValue FROM j.BaseItemProviders WHERE ItemId = b.Id AND ProviderId = 'Tmdb') AS tmdb,
        (SELECT ProviderValue FROM j.BaseItemProviders WHERE ItemId = b.Id AND ProviderId = 'Tvdb') AS tvdb,
        (SELECT ProviderValue FROM j.BaseItemProviders WHERE ItemId = b.Id AND ProviderId = 'TmdbCollection') AS tmdb_sammlung,
        (SELECT group_concat(Name, '|') FROM (SELECT p.Name FROM j.PeopleBaseItemMap m JOIN j.Peoples p ON p.Id = m.PeopleId
            WHERE m.ItemId = b.Id AND p.PersonType = 'Director' ORDER BY m.ListOrder)) AS regie,
        (SELECT group_concat(x, '|') FROM (SELECT p.Name || CASE WHEN m.Role > '' THEN ' (' || m.Role || ')' ELSE '' END AS x
            FROM j.PeopleBaseItemMap m JOIN j.Peoples p ON p.Id = m.PeopleId
            WHERE m.ItemId = b.Id AND p.PersonType IN ('Actor', 'GuestStar') ORDER BY m.ListOrder LIMIT 15)) AS darsteller,
        (SELECT MAX(Played) FROM j.UserData WHERE ItemId = b.Id) AS gesehen,
        (SELECT MAX(PlayCount) FROM j.UserData WHERE ItemId = b.Id) AS abspielzahl,
        (SELECT MAX(LastPlayedDate) FROM j.UserData WHERE ItemId = b.Id) AS zuletzt,
        (SELECT MAX(IsFavorite) FROM j.UserData WHERE ItemId = b.Id) AS favorit
      FROM j.BaseItems b WHERE b.IsFolder = 0 AND b.IsVirtualItem = 0 AND ({' OR '.join(f"b.Path LIKE '{a}%'" for a in JF_PFAD)});
      CREATE INDEX ix_jf_path ON jf(path);""")
    con.commit(); con.execute("DETACH DATABASE j")
    n, m = con.execute("SELECT COUNT(*), SUM(path IN (SELECT path FROM files)) FROM jf").fetchone()
    print(f"Jellyfin: {n} Einträge, {m} davon einer Datei im Index zugeordnet", flush=True)

con = sqlite3.connect(DB)
con.execute(f"CREATE TABLE IF NOT EXISTS files ({', '.join(c + (' PRIMARY KEY' if c == 'path' else '') for c in COLS)})")
vorhanden = {r[1] for r in con.execute("PRAGMA table_info(files)")}
for c in COLS:
    if c not in vorhanden: con.execute(f"ALTER TABLE files ADD COLUMN {c}")
con.execute(f"CREATE TABLE IF NOT EXISTS streams ({', '.join(SCOLS)}, PRIMARY KEY (path, idx))")
for c in ("lib", "titel", "dir", "art", "ext", "imdb"): con.execute(f"CREATE INDEX IF NOT EXISTS ix_{c} ON files({c})")
for c in ("typ", "sprache", "codec"): con.execute(f"CREATE INDEX IF NOT EXISTS ix_s_{c} ON streams({c})")
con.execute("DROP VIEW IF EXISTS titles")
con.execute("""CREATE VIEW titles AS SELECT lib, titel,
    SUM(art='haupt') AS haupt_n, SUM(art='extra') AS extras_n,
    ROUND(SUM(CASE WHEN art='haupt' THEN dur END)/60, 1) AS haupt_min,
    MAX(CASE WHEN art='haupt' THEN hoehe END) AS hoehe_max,
    GROUP_CONCAT(DISTINCT CASE WHEN art='haupt' THEN vcodec END) AS vcodecs,
    MAX(CASE WHEN art='haupt' THEN hdr END) AS hdr,
    GROUP_CONCAT(DISTINCT CASE WHEN art='haupt' THEN audio_sprachen END) AS ton_sprachen,
    ROUND(SUM(CASE WHEN art='haupt' THEN size END)/1e9, 2) AS gb_haupt,
    ROUND(SUM(CASE WHEN art='extra' THEN size END)/1e9, 2) AS gb_extras,
    ROUND(SUM(size)/1e9, 2) AS gb,
    SUM(ext IN ('.srt','.ass','.ssa','.sub','.idx','.sup','.vtt','.smi')) AS ext_untertitel,
    MAX(ext='.nfo') AS nfo, MAX(imdb) AS imdb, MAX(tmdb) AS tmdb, MAX(nfo_set) AS sammlung, MAX(nfo_genres) AS genres,
    MAX(LOWER(name) IN ('poster.jpg','folder.jpg','cover.jpg','poster.png')) AS poster,
    SUM(err IS NOT NULL) AS fehler
  FROM files WHERE art IS NOT 'trickplay' GROUP BY lib, titel""")
if "--nur-jellyfin" in sys.argv:   # nur Jellyfin-Kopie holen + Tabelle jf neu bauen, keine Dateien lesen
    jellyfin(con); con.commit(); print("FERTIG (nur Jellyfin)"); sys.exit()
known ={} if FULL else {p: (s, m) for p, s, m in con.execute(
    "SELECT path, size, mtime FROM files WHERE (err IS NULL OR err NOT LIKE '%Errno%') AND v = ?", (V,))}
print("in DB:", len(known), "| lese Verzeichnisse ...", flush=True)

todo, seen, n_dirs, t0 = [], set(), 0, time.time()
lesefehler = []   # os.walk verschluckt Fehler sonst still -> abgebrochener Server-Zugriff sähe aus wie gelöschte Videos
for root in ROOTS:
    for dp, dn, fs in os.walk(root, onerror=lesefehler.append):
        dn[:] = [d for d in dn if d not in ("$RECYCLE.BIN", "#recycle", "@eaDir", ".@__thumb") and not d.endswith(".trickplay")]
        n_dirs += 1
        for f in fs:
            p = os.path.join(dp, f)
            try: st = os.stat(p)
            except OSError: continue
            key = pc_pfad(p); seen.add(key); k = known.get(key)
            if not k or k[0] != st.st_size or abs(k[1] - st.st_mtime) > 1: todo.append((p, st, root))
        if n_dirs % 2000 == 0: print(f"  {n_dirs} Ordner, {len(seen)} Dateien, {len(todo)} zu lesen ({time.time()-t0:.0f} s)", flush=True)
if not seen: sys.exit("Server lieferte keine Dateien – Abbruch, DB unverändert.")
gone = [p for p, in con.execute("SELECT path FROM files") if p not in seen]
print(f"Dateien: {len(seen)} | neu/geändert: {len(todo)} | verschwunden: {len(gone)}", flush=True)
# Schutz (wie Musik-Index nach dem 10.11.2026): bei Lesefehlern oder ungewöhnlich vielen „verschwundenen“ Dateien nichts austragen
if lesefehler or len(gone) > max(200, 0.05 * len(seen)):
    print(f"WARNUNG: {len(lesefehler)} Lesefehler (z. B. {lesefehler[0] if lesefehler else '-'}) bzw. {len(gone)} verschwunden – "
          "es wird NICHTS ausgetragen. Server-Zugriff prüfen und neu starten.", flush=True)
    gone = []
buf, done = [], 0
def flush():
    global buf
    con.executemany("DELETE FROM streams WHERE path = ?", [(r["path"],) for r, _ in buf])
    con.executemany(f"INSERT OR REPLACE INTO files ({', '.join(COLS)}) VALUES ({', '.join('?' * len(COLS))})", [[r[c] for c in COLS] for r, _ in buf])
    con.executemany(f"INSERT OR REPLACE INTO streams ({', '.join(SCOLS)}) VALUES ({', '.join('?' * len(SCOLS))})",
                    [[s[c] for c in SCOLS] for _, sp in buf for s in sp])
    con.commit(); buf = []
with cf.ThreadPoolExecutor(8) as ex:
    for res in ex.map(lambda x: read(*x), todo):
        buf.append(res); done += 1
        if len(buf) >= 200: flush()
        if done % 500 == 0: print(f"  gelesen {done}/{len(todo)} ({time.time()-t0:.0f} s)", flush=True)
flush()
con.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in gone])
con.executemany("DELETE FROM streams WHERE path = ?", [(p,) for p in gone]); con.commit()
if "--test" not in sys.argv: jellyfin(con)
n = dict(con.execute("SELECT lib, COUNT(*) FROM files WHERE art='haupt' GROUP BY lib").fetchall())
s = con.execute("SELECT COUNT(*) FROM streams").fetchone()[0]
print(f"FERTIG in {time.time()-t0:.0f} s | DB: {DB} | Hauptvideos je Bibliothek: {n} | Spuren: {s}", flush=True)
