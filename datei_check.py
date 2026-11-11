#!/usr/bin/env python3
"""Defekt-Check für Videodateien – läuft im Container werkbank, liest nur (ändert keine Medien).

Je Datei:
  1. Komplett durchlesen ohne Dekodieren (ffmpeg -c copy -f null): abgeschnittene Dateien,
     kaputte Containerstruktur, Lesefehler der Platte.
  2. Stichproben dekodieren: 5 Stellen à 10 s, Bild + alle Tonspuren; misst je Tonspur den
     Spitzenpegel -> durchgehend stille Spuren fallen auf.

Ergebnis: Tabelle in /werkbank/db/datei_check.sqlite, Bericht /werkbank/berichte/datei_check_<Datum>.csv
Inkrementell: unveränderte Dateien (Größe + mtime) werden übersprungen, außer mit --neu.
Wartet automatisch, solange WD-Backup oder Mover laufen.

Aufruf (im Container):
  python3 /werkbank/skripte/datei_check.py                 alles unter filme/serien/video
  python3 /werkbank/skripte/datei_check.py --test DATEI…   nur diese Dateien (immer neu prüfen)
  python3 /werkbank/skripte/datei_check.py --nur 20        die ersten 20 offenen
  python3 /werkbank/skripte/datei_check.py --bericht       nur Bericht aus der DB schreiben
Optionen: --neu (alles neu prüfen), --worker N (Standard 2), --bis HH:MM (danach keine neue Datei mehr anfangen)
"""
import os, re, sys, csv, json, time, sqlite3, subprocess, datetime, concurrent.futures as cf
import werkbank as wb

DB = os.path.join(wb.DB_DIR, "datei_check.sqlite")
WURZELN = ["/media/filme", "/media/serien", "/media/video"]
EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".webm", ".flv", ".ogm", ".vob", ".divx"}
PROBEN = (0.05, 0.25, 0.50, 0.75, 0.95)
LAENGE = 10
STILL_DB = -70.0                     # Spitzenpegel darunter in ALLEN Proben -> Spur gilt als stumm
SCHWER = re.compile(r"ended prematurely|truncat|invalid data|input/output error|i/o error|ebml|corrupt|"
                    r"moov atom not found|partial file|error reading|could not find codec|no frame|"
                    r"decode_slice_header error|concealing|error while decoding|missing picture|"
                    r"invalid nal|non-existing pps|header damaged|exceeds max length", re.I)

log = wb.Log("datei_check")
ARGS = sys.argv[1:]
def opt(name, default=None, typ=str):
    return typ(ARGS[ARGS.index(name) + 1]) if name in ARGS else default


def db():
    os.makedirs(wb.DB_DIR, exist_ok=True)
    c = sqlite3.connect(DB, timeout=60)
    c.execute("""create table if not exists pruefung (
        path text primary key, size integer, mtime real, geprueft text, status text, dauer_s real,
        lesen_mbs real, laufzeit_s real, fehler_lesen text, fehler_proben text, ton text, details text)""")
    return c


def sauber(zeilen):
    """Adressen wie [matroska @ 0x55..] entfernen, Duplikate zusammenfassen"""
    aus = {}
    for z in zeilen:
        z = re.sub(r"\s*@ 0x[0-9a-f]+", "", z.strip())
        z = re.sub(r"pos \d+ \(0x[0-9a-f]+\)", "pos …", z)
        if z: aus[z] = aus.get(z, 0) + 1
    return [f"{z} (×{n})" if n > 1 else z for z, n in aus.items()]


def probe_info(pfad):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=index,codec_type,codec_name:stream_disposition=attached_pic",
                        "-of", "json", pfad], capture_output=True, text=True, timeout=300)
    d = json.loads(r.stdout or "{}")
    st = d.get("streams", [])
    video = [s for s in st if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audio = [s for s in st if s.get("codec_type") == "audio"]
    dauer = float(d.get("format", {}).get("duration") or 0)
    return dauer, len(video), len(audio), r.stderr.strip()


def durchlesen(pfad, groesse):
    t0 = time.time()
    to = max(900, groesse / (10 * 2**20))            # mind. 10 MB/s, sonst Abbruch
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-i", pfad, "-map", "0:V?", "-map", "0:a?",
                            "-map", "0:s?", "-c", "copy", "-f", "null", "-"], capture_output=True, text=True, timeout=to,
                           errors="replace")
        zeilen = r.stderr.splitlines()
        if r.returncode != 0 and not zeilen: zeilen = [f"ffmpeg Rückgabe {r.returncode}"]
    except subprocess.TimeoutExpired:
        zeilen = [f"Zeitüberschreitung beim Lesen (>{to / 60:.0f} min)"]
    dt = time.time() - t0
    return sauber(zeilen), dt, groesse / 2**20 / dt if dt > 0 else 0


def stichproben(pfad, dauer, n_video, n_audio):
    fehler, pegel = [], {n: [] for n in range(n_audio)}
    for anteil in PROBEN:
        start = max(0.0, min(dauer * anteil, dauer - LAENGE - 1)) if dauer > LAENGE + 2 else 0.0
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "repeat+level+info", "-ss", f"{start:.1f}", "-i", pfad, "-t", str(LAENGE)]
        if n_video: cmd += ["-map", "0:V:0"]
        if n_audio:
            cmd += ["-filter_complex", ";".join(f"[0:a:{n}]volumedetect[a{n}]" for n in range(n_audio))]
            for n in range(n_audio): cmd += ["-map", f"[a{n}]"]
        cmd += ["-f", "null", "-"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, errors="replace")
        except subprocess.TimeoutExpired:
            fehler.append(f"{start:.0f}s: Zeitüberschreitung"); continue
        for z in r.stderr.splitlines():
            m = re.search(r"Parsed_volumedetect_(\d+).*max_volume:\s*(-?[\d.]+|-inf) dB", z)
            if m:
                n = int(m.group(1)); v = float("-inf") if m.group(2) == "-inf" else float(m.group(2))
                if n in pegel: pegel[n].append(v)
            elif z.startswith(("[error]", "[fatal]")) or (z.startswith("[warning]") and SCHWER.search(z)):
                fehler.append(f"{start:.0f}s: " + z.split("] ", 1)[-1])
    ton = []
    for n, werte in pegel.items():
        if not werte: ton.append(f"a:{n} nicht dekodierbar")
        elif max(werte) < STILL_DB: ton.append(f"a:{n} stumm (max {max(werte):.0f} dB)")
    return sauber(fehler), ton


def pruefen(pfad):
    st = os.stat(pfad)
    t0 = time.time()
    try:
        dauer, nv, na, probefehler = probe_info(pfad)
    except Exception as e:
        return dict(path=pfad, size=st.st_size, mtime=st.st_mtime, status="defekt", fehler_lesen=f"ffprobe: {e}",
                    fehler_proben="", ton="", dauer_s=time.time() - t0, lesen_mbs=0, laufzeit_s=0, details="")
    lesen, dt, mbs = durchlesen(pfad, st.st_size)
    proben, ton = stichproben(pfad, dauer, nv, na) if (nv or na) else ([], ["keine Bild-/Tonspur"])
    if probefehler: lesen = sauber([probefehler] + lesen)
    schwer = any(SCHWER.search(z) for z in lesen + proben) or any("stumm" in t or "nicht dekodierbar" in t or "keine" in t for t in ton)
    status = "defekt" if schwer else "warnung" if (lesen or proben) else "ok"
    return dict(path=pfad, size=st.st_size, mtime=st.st_mtime, status=status, fehler_lesen=" | ".join(lesen)[:2000],
                fehler_proben=" | ".join(proben)[:2000], ton=" | ".join(ton), dauer_s=round(time.time() - t0, 1),
                lesen_mbs=round(mbs, 1), laufzeit_s=round(dauer), details=f"video={nv} audio={na}")


def kandidaten(c):
    if "--test" in ARGS:
        return [wb.lokal_pfad(a) for a in ARGS[ARGS.index("--test") + 1:] if not a.startswith("--")]
    bekannt = {p: (s, m) for p, s, m in c.execute("select path, size, mtime from pruefung")}
    offen = []
    for w in WURZELN:
        for d, _, dateien in os.walk(w):
            for f in dateien:
                if os.path.splitext(f)[1].lower() not in EXT: continue
                p = os.path.join(d, f)
                try: st = os.stat(p)
                except OSError: continue
                if "--neu" not in ARGS and bekannt.get(p) == (st.st_size, st.st_mtime): continue
                offen.append(p)
    offen.sort()
    return offen[:opt("--nur", None, int)] if "--nur" in ARGS else offen


def bericht(c):
    os.makedirs(wb.BERICHTE, exist_ok=True)
    out = os.path.join(wb.BERICHTE, f"datei_check_{datetime.date.today()}.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Status", "Pfad", "Ton", "Fehler beim Lesen", "Fehler in Stichproben", "Laufzeit min", "Größe MB", "geprüft"])
        for r in c.execute("""select status, path, ton, fehler_lesen, fehler_proben, laufzeit_s, size, geprueft from pruefung
                              where status != 'ok' order by case status when 'defekt' then 0 else 1 end, path"""):
            w.writerow([r[0], wb.smb_pfad(r[1]), r[2], r[3], r[4], round((r[5] or 0) / 60), round((r[6] or 0) / 2**20), r[7]])
    zahl = dict(c.execute("select status, count(*) from pruefung group by status").fetchall())
    log(f"Bericht: {out}  Stand: {zahl}")
    return out


def main():
    c = db()
    if "--bericht" in ARGS:
        bericht(c); return
    wb.niedrige_prioritaet()
    bis = opt("--bis")
    grenze = None
    if bis:   # nächstes HH:MM nach dem Start (22:00 + --bis 07:00 -> morgen 07:00)
        h, m = map(int, bis.split(":"))
        jetzt = datetime.datetime.now()
        grenze = jetzt.replace(hour=h, minute=m, second=0, microsecond=0)
        if grenze <= jetzt: grenze += datetime.timedelta(days=1)
    liste = kandidaten(c)
    log(f"=== Start: {len(liste)} Dateien, {opt('--worker', 2, int)} parallel{f', neue Dateien bis {bis}' if bis else ''} ===")
    t0, fertig, n_def = time.time(), 0, 0
    with cf.ThreadPoolExecutor(opt("--worker", 2, int)) as ex:
        laufend = {}
        it = iter(liste)
        def nachlegen():
            if grenze and datetime.datetime.now() >= grenze: return False
            wb.warten_auf_ruhe(log)
            p = next(it, None)
            if p is None: return False
            laufend[ex.submit(pruefen, p)] = p; return True
        for _ in range(opt("--worker", 2, int)): nachlegen()
        while laufend:
            erledigt, _ = cf.wait(laufend, return_when=cf.FIRST_COMPLETED)
            for fu in erledigt:
                p = laufend.pop(fu)
                try: e = fu.result()
                except Exception as ex_:
                    e = dict(path=p, size=0, mtime=0, status="defekt", fehler_lesen=f"Prüfung abgebrochen: {ex_}",
                             fehler_proben="", ton="", dauer_s=0, lesen_mbs=0, laufzeit_s=0, details="")
                e["geprueft"] = datetime.datetime.now().isoformat(timespec="seconds")
                c.execute("insert or replace into pruefung values (:path,:size,:mtime,:geprueft,:status,:dauer_s,:lesen_mbs,:laufzeit_s,:fehler_lesen,:fehler_proben,:ton,:details)", e)
                c.commit(); fertig += 1; n_def += e["status"] == "defekt"
                if e["status"] != "ok":
                    log(f"{e['status'].upper():7} {wb.smb_pfad(p)}  {e['ton']}  {e['fehler_lesen'][:200]}  {e['fehler_proben'][:200]}")
                if fertig % 25 == 0 or fertig == len(liste):
                    rest = (time.time() - t0) / fertig * (len(liste) - fertig)
                    log(f"  {fertig}/{len(liste)}  defekt bisher {n_def}  (~{rest / 3600:.1f} h Rest)")
                nachlegen()
    log(f"=== Ende: {fertig} geprüft in {(time.time() - t0) / 3600:.1f} h, defekt {n_def} ===")
    bericht(c)


if __name__ == "__main__":
    main()
