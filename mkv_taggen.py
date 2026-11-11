"""MKV-Dateien in filme/serien mit Jellyfin-Daten taggen (video/index/Konvention-Tagging.md, Punkte A–L) – Container-Fassung.
Nur Kopfdaten per mkvpropedit, Videodaten bleiben unberührt; mtime wird zurückgesetzt.

Aufruf:  python3 mkv_taggen.py --plan PFAD...             -> nur anzeigen, was geändert würde
         python3 mkv_taggen.py --schreiben [--pruefen] PFAD...   (--pruefen: Spuren-Prüfsumme vorher/nachher)
         python3 mkv_taggen.py --nacht [--bis HH:MM] [--nur N] [--plan]
             -> alle Dateien, die neu sind, sich geändert haben oder deren Jellyfin-Soll sich geändert hat.
                Dateien, bei denen schon alles stimmt, werden nur vermerkt (kein Schreiben).
PFAD als /media/... oder //server/media/... (MCSC_SMB_PREFIX)
Stand je Datei: db/mkv_taggen.sqlite | alte Kopfdaten vor jedem Schreiben: sicherung/tagging/ | Log: logs/mkv_taggen.log"""
import os, re, sys, csv, json, sqlite3, subprocess, hashlib, tempfile, datetime, urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import werkbank as wb

VIDEO_DB = os.path.join(wb.DB_DIR, "video_index.sqlite")
STAND_DB = os.path.join(wb.DB_DIR, "mkv_taggen.sqlite")
JF_DB = "/jellyfin/jellyfin.db"
ENTSCH = os.path.join(wb.WB, "daten", "sprache_entscheidung.csv")
SICH = os.path.join(wb.WB, "sicherung", "tagging")
JF = wb.JELLYFIN_URL
HEUTE = datetime.date.today().isoformat()
MAX_DARSTELLER = 15
log = wb.Log("mkv_taggen")

SPRACHNAME = {"ger": "German", "deu": "German", "eng": "English", "fre": "French", "fra": "French", "ita": "Italian",
              "spa": "Spanish", "rus": "Russian", "jpn": "Japanese", "pol": "Polish", "dan": "Danish", "swe": "Swedish",
              "dut": "Dutch", "nld": "Dutch", "cze": "Czech", "ces": "Czech", "chi": "Chinese", "zho": "Chinese", "kor": "Korean",
              "hin": "Hindi", "tur": "Turkish", "hun": "Hungarian", "por": "Portuguese", "gre": "Greek", "ell": "Greek",
              "fin": "Finnish", "nor": "Norwegian", "nob": "Norwegian", "rum": "Romanian", "ron": "Romanian", "hrv": "Croatian",
              "ara": "Arabic", "heb": "Hebrew", "tha": "Thai", "vie": "Vietnamese", "ukr": "Ukrainian", "bul": "Bulgarian",
              "slv": "Slovenian", "slo": "Slovak", "slk": "Slovak", "srp": "Serbian", "est": "Estonian", "lav": "Latvian",
              "lit": "Lithuanian", "ice": "Icelandic", "isl": "Icelandic", "ind": "Indonesian", "may": "Malay", "msa": "Malay",
              "tam": "Tamil", "tel": "Telugu", "cat": "Catalan", "per": "Persian", "fas": "Persian", "tgl": "Tagalog", "fil": "Filipino"}
KANAL = {1: "Mono", 2: "Stereo", 3: "2.1", 6: "5.1", 7: "6.1", 8: "7.1"}
GEN_SPUR = re.compile(r"^\s*((german|deutsch|english|englisch|unknown|und|ger|deu|eng)\b[\s,-]*)?"
                      r"((stereo|mono|surround|dolby digital|dd|ac-?3|e-?ac-?3|aac|dts|mp3)\b\s*)*(\d\.\d)?\s*$"
                      r"|^\s*(audio ?stream|audio|soundhandler|sound handler|track)\s*\d*\s*$", re.I)
GEN_UT = re.compile(r"^\s*((german|deutsch|english|englisch|ger|deu|eng|unknown|und)(?![a-z])[\s,.:_-]*)?"
                    r"([(\[]?(forced|full|sdh|cc|erzwungen|untertitel|subtitles?|subs?|srt|pgs|ass|ssa|vobsub)[)\]]?[\s_-]*)*$", re.I)
GEN_KAP = re.compile(r"^\s*$|^\s*(chapter|kapitel|szene|scene|capitolo|chapitre|cap[ií]tulo|hoofdstuk|kap\.?)?\s*\(?\d+\)?\.?\s*$"
                     r"|^\s*(\(\d+\))?\s*\d{1,2}:\d\d:\d\d([.:,]\d+)?\s*$", re.I)
MUELL = re.compile(r"https?://|www\.|\.(com|org|net|to|cc|ws)\b|-[A-Za-z0-9]+$|\b(720p|1080p|2160p|x26[45]|h\.?26[45]|web-?dl|bluray|dvdrip|bdrip|german)\b", re.I)
EIGENE = {"TITLE", "ORIGINAL_TITLE", "DATE_RELEASED", "GENRE", "DIRECTOR", "ACTOR", "PRODUCTION_STUDIO", "LAW_RATING",
          "SUMMARY", "IMDB", "TMDB", "TVDB2", "COLLECTION", "PART_NUMBER", "JF_SYNC", "DESCRIPTION", "SYNOPSIS"}


def run(cmd, **kw): return subprocess.run(cmd, capture_output=True, **kw)


def sprach_entscheidungen():
    e = {}
    try:
        for x in csv.DictReader(open(ENTSCH, encoding="utf-8-sig"), delimiter=";"):
            e[(x["Pfad"], int(x["Spur (a:N)"][2:]))] = x["neu"]
    except FileNotFoundError: pass
    return e


# ---------- Soll-Daten ----------
def soll_titel(j):
    if j["jf_typ"] == "TV.Episode":
        se = f"S{j['staffel'] or 0:02d}E{j['folge'] or 0:02d}"
        return f"{j['serie']} – {se} – {j['name']}" if j["name"] else f"{j['serie']} – {se}"
    return f"{j['name']} ({j['jahr']})" if j["jahr"] else j["name"]


def liste(s): return [x.strip() for x in (s or "").split("|") if x.strip()]


def simple(name, wert, kinder=""):
    return f"<Simple><Name>{escape(name)}</Name><String>{escape(str(wert))}</String>{kinder}</Simple>"


def tag(ziel, inhalt, typ):
    return f"<Tag><Targets><TargetTypeValue>{ziel}</TargetTypeValue><TargetType>{typ}</TargetType></Targets>{''.join(inhalt)}</Tag>"


def soll_tags(j, alte, datum=HEUTE):
    ep = j["jf_typ"] == "TV.Episode"
    t = [simple("TITLE", j["name"] or "")]
    if j["originaltitel"] and j["originaltitel"] != j["name"]: t.append(simple("ORIGINAL_TITLE", j["originaltitel"]))
    if j["jahr"]: t.append(simple("DATE_RELEASED", j["jahr"]))
    t += [simple("GENRE", g) for g in liste(j["genres"])]
    t += [simple("DIRECTOR", r) for r in liste(j["regie"])]
    for d in liste(j["darsteller"])[:MAX_DARSTELLER]:
        m = re.match(r"^(.*?)\s*\((.*)\)$", d)
        t.append(simple("ACTOR", m.group(1), simple("CHARACTER", m.group(2))) if m else simple("ACTOR", d))
    t += [simple("PRODUCTION_STUDIO", s) for s in dict.fromkeys(liste(j["studios"]))]
    if j["freigabe"]: t.append(simple("LAW_RATING", j["freigabe"]))
    if j["inhalt"]: t.append(simple("SUMMARY", j["inhalt"]))
    if j["imdb"]: t.append(simple("IMDB", j["imdb"]))
    if j["tmdb"]: t.append(simple("TMDB", ("tv/" if ep else "movie/") + str(j["tmdb"])))
    if j["tvdb"]: t.append(simple("TVDB2", ("episodes/" if ep else "movies/") + str(j["tvdb"])))
    if not ep and j["nfo_set"]: t.append(simple("COLLECTION", j["nfo_set"]))
    if ep and j["folge"] is not None: t.append(simple("PART_NUMBER", j["folge"]))
    t += alte                                   # behaltene alte globale Tags (ENCODER usw.)
    t.append(simple("JF_SYNC", datum))
    teile = []
    if ep:
        teile.append(tag(70, [simple("TITLE", j["serie"])], "COLLECTION"))
        if j["staffel"] is not None: teile.append(tag(60, [simple("PART_NUMBER", j["staffel"])], "SEASON"))
        teile.append(tag(50, t, "EPISODE"))
    else:
        teile.append(tag(50, t, "MOVIE"))
    return '<?xml version="1.0" encoding="UTF-8"?><Tags>' + "".join(teile) + "</Tags>"


def soll_hash(j):
    """Fingerabdruck des Jellyfin-Solls (ohne Datum) – ändert er sich, wird die Datei neu geprüft"""
    return hashlib.md5((soll_titel(j) + soll_tags(j, [], "")).encode()).hexdigest()


def alte_globale_tags(xmltext):
    """globale Simple-Tags behalten, außer Titel/Kommentar-Müll und allem, was wir selbst setzen"""
    behalten, verworfen = [], []
    if not xmltext: return behalten, verworfen
    for t in ET.fromstring(xmltext).findall("Tag"):
        tg = t.find("Targets")
        if tg is not None and any(c.tag.endswith("UID") for c in tg): continue      # Spur-/Kapitel-Tags bleiben ohnehin
        for s in t.findall("Simple"):
            n = (s.findtext("Name") or "").upper(); v = s.findtext("String") or ""
            if n in EIGENE or (n == "COMMENT" and MUELL.search(v)): verworfen.append(f"{n}={v[:60]}")
            else: behalten.append(ET.tostring(s, encoding="unicode"))
    return behalten, verworfen


def tag_menge(xmltext):
    """globale Tags als vergleichbare Menge (ohne JF_SYNC-Datum)"""
    def s_tupel(s):
        return ((s.findtext("Name") or "").upper(), s.findtext("String") or "", tuple(s_tupel(k) for k in s.findall("Simple")))
    erg = []
    if not xmltext: return erg
    for t in ET.fromstring(xmltext).findall("Tag"):
        tg = t.find("Targets")
        if tg is not None and any(c.tag.endswith("UID") for c in tg): continue
        ttv = (tg.findtext("TargetTypeValue") if tg is not None else None) or "50"
        erg += [(ttv,) + s_tupel(s) for s in t.findall("Simple") if (s.findtext("Name") or "").upper() != "JF_SYNC"]
    return sorted(erg)


# ---------- Spuren ----------
def codecname(c):
    c = c or ""
    for a, b in (("E-AC-3", "E-AC3"), ("AC-3", "AC3"), ("DTS-HD Master Audio", "DTS-HD MA"), ("DTS-HD High Resolution", "DTS-HD HRA"),
                 ("TrueHD Atmos", "TrueHD Atmos"), ("TrueHD", "TrueHD"), ("DTS", "DTS"), ("AAC", "AAC"), ("MP3", "MP3"),
                 ("FLAC", "FLAC"), ("Opus", "Opus"), ("Vorbis", "Vorbis"), ("PCM", "PCM")):
        if a in c: return b
    return c


def spur_aenderungen(pc_pfad, info, entsch):
    args, notiz, a = [], [], 0
    for t in info["tracks"]:
        if t["type"] != "audio": continue
        a += 1; p = t["properties"]
        alt_spr = p.get("language", "und"); alt_name = p.get("track_name", "") or ""
        spr = entsch.get((pc_pfad, a - 1), alt_spr)
        sets = []
        if spr != alt_spr: sets += ["--set", f"language={spr}"]; notiz.append(f"a{a} Sprache {alt_spr}->{spr}")
        if GEN_SPUR.match(alt_name):
            neu = " ".join(x for x in (SPRACHNAME.get(spr, spr if spr != "und" else ""), codecname(t.get("codec")),
                                       KANAL.get(p.get("audio_channels"), f"{p.get('audio_channels')}ch")) if x)
            if neu != alt_name: sets += ["--set", f"name={neu}"]; notiz.append(f"a{a} Name '{alt_name}'->'{neu}'")
        if sets: args += ["--edit", f"track:a{a}"] + sets
    s = 0
    for t in info["tracks"]:   # Untertitel: nichtssagende Namen -> "German", "German (Forced)", "English (SDH)"
        if t["type"] != "subtitles": continue
        s += 1; p = t["properties"]
        alt_name = p.get("track_name", "") or ""; spr = p.get("language", "und")
        if not GEN_UT.match(alt_name): continue
        nl = alt_name.lower()
        if spr == "und":
            spr = "ger" if re.search(r"german|deutsch|\bger\b|\bdeu\b", nl) else "eng" if re.search(r"english|englisch|\beng\b", nl) else "und"
        voll = "full" in nl
        forced = not voll and bool(p.get("forced_track") or "forced" in nl or "erzwungen" in nl)
        zusatz = " (Forced)" if forced else " (SDH)" if ("sdh" in nl or re.search(r"(?<![a-z])cc(?![a-z])", nl) or p.get("flag_hearing_impaired")) else ""
        neu = (SPRACHNAME.get(spr, spr if spr != "und" else "Subtitle")) + zusatz
        sets = []
        if neu != alt_name: sets += ["--set", f"name={neu}"]; notiz.append(f"s{s} Name '{alt_name}'->'{neu}'")
        if forced and not p.get("forced_track"): sets += ["--set", "flag-forced=1"]; notiz.append(f"s{s} forced-Flag")
        if voll and p.get("forced_track"): sets += ["--set", "flag-forced=0"]; notiz.append(f"s{s} falsches forced-Flag entfernt")
        if sets: args += ["--edit", f"track:s{s}"] + sets
    return args, notiz


# ---------- Kapitel ----------
def kapitel_xml(pfad, tmp):
    out = os.path.join(tmp, "kap.xml")
    r = run(["mkvextract", pfad, "chapters", out])
    return open(out, encoding="utf-8").read() if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) else ""


def kapitel_neu(xmltext):
    if not xmltext: return None, []
    baum = ET.fromstring(xmltext); n = 0
    for ed in baum.findall("EditionEntry"):
        for i, at in enumerate(ed.findall("ChapterAtom"), 1):
            for disp in at.findall("ChapterDisplay") or [None]:
                if disp is None:
                    disp = ET.SubElement(at, "ChapterDisplay"); ET.SubElement(disp, "ChapterString").text = ""
                s = disp.find("ChapterString"); alt = s.text or ""
                if GEN_KAP.match(alt):
                    neu = f"Kapitel {i:02d}"
                    if alt != neu: s.text = neu; n += 1
                    for l in disp.findall("ChapterLanguage"): l.text = "ger"
    if not n: return None, []
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(baum, encoding="unicode"), [f"{n} Kapitelnamen -> 'Kapitel NN'"]


# ---------- Cover ----------
_SID = None


def serien_id(folge_id):
    """Folge-ID -> Serien-ID direkt aus der Jellyfin-DB (nur lesen)"""
    global _SID
    if _SID is None:
        try:
            k = sqlite3.connect(f"file:{JF_DB}?mode=ro", uri=True)
            _SID = {a.upper(): b for a, b in k.execute("select Id, SeriesId from BaseItems where SeriesId is not null")}
            k.close()
        except sqlite3.Error as e:
            log(f"Jellyfin-DB nicht lesbar ({e}) – Serien-Cover nur aus Dateien"); _SID = {}
    return _SID.get(folge_id.upper())


def cover_quelle(pfad, j):
    d = os.path.dirname(pfad); root = "/media/" + j["lib"]
    namen = ["poster.jpg", "folder.jpg", os.path.splitext(os.path.basename(pfad))[0] + "-poster.jpg"]
    if j["jf_typ"] == "TV.Episode":
        st = j["staffel"] or 0
        namen += [f"season{st:02d}-poster.jpg", "season-specials-poster.jpg" if st == 0 else ""]
    while True:
        for n in namen:
            if n and os.path.isfile(os.path.join(d, n)): return os.path.join(d, n)
        if d.rstrip("/") == root or len(d) <= len(root): break
        d = os.path.dirname(d)
    if j["jf_typ"] == "Movies.Movie": return f"{JF}/Items/{j['jf_id'].replace('-', '')}/Images/Primary"
    sid = serien_id(j["jf_id"])
    return f"{JF}/Items/{sid.replace('-', '')}/Images/Primary" if sid else None


def cover_bauen(quelle, tmp):
    roh = os.path.join(tmp, "roh.jpg"); out = os.path.join(tmp, "cover.jpg")
    if quelle.startswith("http"):
        try: urllib.request.urlretrieve(quelle + "?maxHeight=1200", roh)
        except Exception: return None
    else:
        roh = quelle
    r = run(["ffmpeg", "-v", "error", "-y", "-i", roh, "-vf", "scale='if(gt(iw,ih),min(600,iw),-2)':'if(gt(iw,ih),-2,min(600,ih))'",
             "-q:v", "3", out])
    return out if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000 else None


def spur_hash(pfad):
    r = run(["ffmpeg", "-v", "error", "-i", pfad, "-map", "0:V", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-f", "streamhash", "-hash", "md5", "-"])
    return r.stdout.decode(errors="replace").strip()


# ---------- eine Datei ----------
def bearbeiten(j, entsch, schreiben, pruefen):
    """-> (Ergebnis-Text, geschrieben?)"""
    pfad = wb.lokal_pfad(j["path"])
    st = os.stat(pfad)
    info = json.loads(run(["mkvmerge", "-J", pfad]).stdout)
    with tempfile.TemporaryDirectory() as tmp:
        txml = os.path.join(tmp, "tags.xml")
        run(["mkvextract", pfad, "tags", txml])
        alt_tags = open(txml, encoding="utf-8").read() if os.path.exists(txml) and os.path.getsize(txml) else ""
        alt_kap = kapitel_xml(pfad, tmp)
        args, notiz, aenderung = [], [], False
        titel = soll_titel(j); alt_t = info["container"]["properties"].get("title", "")
        if titel != alt_t:
            args += ["--edit", "info", "--set", f"title={titel}"]; notiz.append(f"Titel '{alt_t}' -> '{titel}'"); aenderung = True
        behalten, verworfen = alte_globale_tags(alt_tags)
        soll = soll_tags(j, behalten)
        if tag_menge(soll) != tag_menge(alt_tags) or "<Name>JF_SYNC</Name>" not in alt_tags:
            neu_tags = os.path.join(tmp, "neu_tags.xml")
            open(neu_tags, "w", encoding="utf-8").write(soll)
            args += ["--tags", f"global:{neu_tags}"]; notiz.append(f"Tags gesetzt (alt verworfen: {', '.join(verworfen) or '-'})"); aenderung = True
        a, n = spur_aenderungen(j["path"], info, entsch); args += a; notiz += n; aenderung |= bool(a)
        kx, n = kapitel_neu(alt_kap)
        if kx:
            kf = os.path.join(tmp, "kap_neu.xml"); open(kf, "w", encoding="utf-8").write(kx)
            args += ["--chapters", kf]; notiz += n; aenderung = True
        if not any((x.get("file_name") or "").lower().startswith("cover") for x in info.get("attachments", [])):
            q = cover_quelle(pfad, j)
            c = cover_bauen(q, tmp) if q else None
            if c:
                args += ["--attachment-name", "cover.jpg", "--attachment-mime-type", "image/jpeg", "--add-attachment", c]
                notiz.append(f"Cover aus {q.split('/')[-1] if q.startswith('http') else os.path.basename(q)} ({os.path.getsize(c) // 1024} KB)")
                aenderung = True
            else: notiz.append("KEIN Cover gefunden")
        if not aenderung: return "stimmt schon" + (" (" + "; ".join(notiz) + ")" if notiz else ""), False
        if not schreiben: return "PLAN: " + " | ".join(notiz), False
        os.makedirs(SICH, exist_ok=True)
        k = hashlib.md5(pfad.encode()).hexdigest()[:10] + "_" + re.sub(r"[^\w.-]", "_", os.path.basename(pfad))[:60]
        json.dump(dict(path=pfad, mtime=st.st_mtime, info=info, tags=alt_tags, kapitel=alt_kap),
                  open(os.path.join(SICH, k + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
        h0 = spur_hash(pfad) if pruefen else None
        r = run(["mkvpropedit", pfad] + args)
        os.utime(pfad, (st.st_atime, st.st_mtime))
        if r.returncode not in (0, 1):
            return "FEHLER mkvpropedit: " + r.stdout.decode(errors="replace")[-300:], False
        warn = " (Warnung: " + r.stdout.decode(errors="replace").strip()[-200:] + ")" if r.returncode == 1 else ""
        if pruefen:
            h1 = spur_hash(pfad)
            notiz.append("Spuren-Prüfsumme GLEICH" if h0 == h1 and h0 else "Spuren-Prüfsumme ABWEICHUNG!")
        return "OK: " + " | ".join(notiz) + warn, True


# ---------- Ablauf ----------
def stand_db():
    s = sqlite3.connect(STAND_DB)
    s.execute("""create table if not exists stand (path text primary key, size int, mtime real, soll_hash text,
                 datum text, ergebnis text)""")
    return s


def grenze_von(bis):
    if not bis: return None
    h, m = map(int, bis.split(":")); jetzt = datetime.datetime.now()
    g = jetzt.replace(hour=h, minute=m, second=0, microsecond=0)
    return g + datetime.timedelta(days=1) if g <= jetzt else g


def main():
    argv = sys.argv[1:]
    nacht, pruefen = "--nacht" in argv, "--pruefen" in argv
    schreiben = "--schreiben" in argv or (nacht and "--plan" not in argv)
    wert = lambda k: argv[argv.index(k) + 1] if k in argv else None
    grenze = grenze_von(wert("--bis")); nur = int(wert("--nur") or 0)
    pfade = [p for i, p in enumerate(argv) if not p.startswith("--") and (i == 0 or argv[i - 1] not in ("--bis", "--nur"))]
    if not nacht and not pfade: sys.exit(__doc__)

    vi = sqlite3.connect(f"file:{VIDEO_DB}?mode=ro", uri=True); vi.row_factory = sqlite3.Row
    zeilen = vi.execute("""select j.*, f.lib, f.nfo_set from files f join jf j on j.path=f.path
                           where f.lib in ('filme','serien') and f.art='haupt' and f.ext='.mkv'
                             and j.jf_typ in ('Movies.Movie','TV.Episode') order by f.lib, f.path""").fetchall()
    vi.close()
    s = stand_db()
    if pfade:
        wahl = {wb.lokal_pfad(p) for p in pfade}
        zeilen = [z for z in zeilen if wb.lokal_pfad(z["path"]) in wahl]
    else:
        bekannt = {p: (g, m, h) for p, g, m, h in s.execute("select path, size, mtime, soll_hash from stand")}
        def offen(z):
            lp = wb.lokal_pfad(z["path"]); b = bekannt.get(lp)
            if not b or b[2] != soll_hash(z): return True
            try: st = os.stat(lp)
            except FileNotFoundError: return False
            return (st.st_size, st.st_mtime) != (b[0], b[1])
        zeilen = [z for z in zeilen if offen(z)]
    if nur: zeilen = zeilen[:nur]
    wb.niedrige_prioritaet()
    entsch = sprach_entscheidungen()
    log(f"=== Lauf {'SCHREIBEN' if schreiben else 'PLAN'}{' (Nacht)' if nacht else ''}: {len(zeilen)} Dateien ===")
    zahl = {"geschrieben": 0, "stimmt schon": 0, "Fehler": 0}; fehler_folge = 0
    for i, z in enumerate(zeilen, 1):
        if grenze and datetime.datetime.now() >= grenze:
            log(f"Zeitgrenze {wert('--bis')} erreicht – Rest ({len(zeilen) - i + 1}) in der nächsten Nacht"); break
        if schreiben: wb.warten_auf_ruhe(log)
        lp = wb.lokal_pfad(z["path"])
        if not os.path.exists(lp): log(f"[{i}] FEHLT {lp}"); continue
        try: erg, geschrieben = bearbeiten(z, entsch, schreiben, pruefen)
        except Exception as e: erg, geschrieben = f"FEHLER {type(e).__name__}: {e}", False
        if erg.startswith("FEHLER"): zahl["Fehler"] += 1; fehler_folge += 1
        else:
            fehler_folge = 0
            if geschrieben: zahl["geschrieben"] += 1
            elif erg.startswith("stimmt"): zahl["stimmt schon"] += 1
        if not erg.startswith("stimmt") or not nacht: log(f"[{i}/{len(zeilen)}] {lp}\n      {erg}")
        if schreiben and not erg.startswith("FEHLER") and not erg.startswith("PLAN"):
            st = os.stat(lp)
            s.execute("insert or replace into stand values (?,?,?,?,?,?)",
                      (lp, st.st_size, st.st_mtime, soll_hash(z), HEUTE, erg[:500])); s.commit()
        if fehler_folge >= 10:
            log("NOTBREMSE: 10 Fehler in Folge – Lauf angehalten"); sys.exit(3)
    log(f"Ende: {zahl}")


if __name__ == "__main__":
    main()
