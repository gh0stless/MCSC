"""Umbenennungsplan auf dem Server ausführen (z. B. video/umbenennen/serien_plan.py -> JSON [{alt, neu, art}], Pfade im PC-Format).
- nur umbenennen (os.rename, gleiche Platte), nie überschreiben; fehlende Quelle / vorhandenes Ziel -> übersprungen + gemeldet
- <Stamm>.trickplay-Ordner wandern mit dem Video mit
- leer gewordene Quellordner werden entfernt (nach oben bis unter die Bibliothek), nicht leere gemeldet
- Video-Index und Defekt-Check bekommen die neuen Pfade (sonst sähe der Nachtlauf tausende „verschwundene“ Dateien)
- Zuordnung alt -> neu als TSV nach berichte/ (für das spätere Spiegeln auf die WD-Backup-Platte)
Aufruf: python3 umbenennen.py <plan.json> [--run]     (ohne --run nur Probe)"""
import os, sys, json, sqlite3, datetime
import werkbank as wb

RUN = "--run" in sys.argv
plan = json.load(open(sys.argv[1], encoding="utf-8"))
log = wb.Log("umbenennen")
STEMPEL = datetime.datetime.now().strftime("%Y-%m-%d_%H%M")


def stamm(p): return os.path.splitext(p)[0]


def bibliothek(p):  # /media/serien/X/... -> /media/serien
    return "/".join(p.split("/")[:3])


log(f"=== {'LAUF' if RUN else 'PROBE'}: {sys.argv[1]} ({len(plan)} Einträge)")
if RUN: wb.warten_auf_ruhe(log)
ok, fehler, zuordnung, quell_dirs = [], [], [], set()
geplant_ziele = set()
virtuell = []   # Probe: bereits "umbenannte" Ordner (neu/, alt/), damit spätere Einträge ihre Quelle finden


def real(p):
    for neu_p, alt_p in virtuell:
        if p.startswith(neu_p): return alt_p + p[len(neu_p):]
    return p


for e in plan:
    a, n = wb.lokal_pfad(e["alt"]), wb.lokal_pfad(e["neu"])
    if not os.path.exists(real(a)): fehler.append((e["alt"], "Quelle fehlt")); continue
    if not RUN and os.path.isdir(real(a)): virtuell.append((n + "/", real(a) + "/"))
    if os.path.exists(n) or n in geplant_ziele: fehler.append((e["alt"], "Ziel existiert")); continue
    zd = os.path.dirname(n)
    if os.path.isdir(zd) and any(x.lower() == os.path.basename(n).lower() for x in os.listdir(zd)):
        fehler.append((e["alt"], "Ziel existiert (andere Schreibweise)")); continue
    geplant_ziele.add(n)
    paare = [(a, n)]
    tp = stamm(a) + ".trickplay"
    if e.get("art") == "video" and os.path.isdir(real(tp)): paare.append((tp, stamm(n) + ".trickplay"))
    for x, y in paare:
        if RUN:
            os.makedirs(os.path.dirname(y), exist_ok=True)
            os.rename(x, y)
        zuordnung.append((x, y)); quell_dirs.add(os.path.dirname(x))
    ok.append(e)

# leer gewordene Ordner entfernen (tiefste zuerst, dann Eltern bis unter die Bibliothek)
entfernt, rest = [], {}
if RUN:
    for d in sorted(quell_dirs, key=lambda x: -x.count("/")):
        while d.count("/") > 2 and os.path.isdir(d):
            inhalt = os.listdir(d)
            if inhalt:
                rest[d] = inhalt; break
            os.rmdir(d); entfernt.append(d); rest.pop(d, None)
            d = os.path.dirname(d)
    ziel_dirs = {os.path.dirname(y) for _, y in zuordnung} | {y for _, y in zuordnung}
    rest = {d: i for d, i in rest.items() if os.path.isdir(d) and os.listdir(d)
            and not any(z == d or z.startswith(d + "/") for z in ziel_dirs)}   # Ordner, in die etwas eingezogen ist, sind kein Rest

# Indizes nachziehen
if RUN and zuordnung:
    vi = sqlite3.connect(os.path.join(wb.DB_DIR, "video_index.sqlite"))
    n_vi = 0; dc_prefix = []
    def titel_von(p):
        t = p.split("\\"); return t[1] if len(t) > 2 else ""
    for x, y in zuordnung:
        px, py = wb.pc_pfad(x), wb.pc_pfad(y)
        if os.path.isdir(y):   # ganzer Ordner umbenannt: alle Pfade darunter per Präfix
            alt_p, neu_p, k = px + "\\", py + "\\", len(px) + 2
            for tab in ("files", "streams", "jf"):
                vi.execute(f"update {tab} set path = ? || substr(path, ?) where path like ? escape '^'",
                           (neu_p, k, alt_p.replace("^", "^^").replace("%", "^%").replace("_", "^_") + "%"))
            vi.execute("update files set dir = ? || substr(dir, ?) where dir like ? escape '^' or dir = ?",
                       (py, len(px) + 1, alt_p.replace("^", "^^").replace("%", "^%").replace("_", "^_") + "%", px))
            vi.execute("update files set titel = ? where path like ? escape '^'",
                       (titel_von(neu_p + "x"), neu_p.replace("^", "^^").replace("%", "^%").replace("_", "^_") + "%"))
            dc_prefix.append((x + "/", y + "/"))
            continue
        teile = py.split("\\")
        titel = teile[1] if len(teile) > 2 else ""
        if vi.execute("select 1 from files where path=?", (py,)).fetchone():
            vi.execute("delete from files where path=?", (px,)); vi.execute("delete from streams where path=?", (px,)); continue
        n_vi += vi.execute("update files set path=?, dir=?, name=?, titel=? where path=?",
                           (py, py.rsplit("\\", 1)[0], teile[-1], titel, px)).rowcount
        vi.execute("update streams set path=? where path=?", (py, px))
        vi.execute("update jf set path=? where path=?", (py, px))
    vi.commit(); vi.close()
    dc = sqlite3.connect(os.path.join(wb.DB_DIR, "datei_check.sqlite"))
    n_dc = 0
    for x, y in zuordnung:   # Reihenfolge wie ausgeführt: erst Ordner-Präfix, dann Einzeldateien
        if os.path.isdir(y):
            n_dc += dc.execute("update pruefung set path = ? || substr(path, ?) where substr(path, 1, ?) = ?",
                               (y + "/", len(x) + 2, len(x) + 1, x + "/")).rowcount
        else:
            n_dc += dc.execute("update pruefung set path=? where path=?", (y, x)).rowcount
    dc.commit(); dc.close()
    n_mt = 0
    if os.path.exists(os.path.join(wb.DB_DIR, "mkv_taggen.sqlite")):   # Tagging-Stand: sonst gälten umbenannte Dateien als neu
        mt = sqlite3.connect(os.path.join(wb.DB_DIR, "mkv_taggen.sqlite"))
        for x, y in zuordnung:
            if os.path.isdir(y):
                n_mt += mt.execute("update stand set path = ? || substr(path, ?) where substr(path, 1, ?) = ?",
                                   (y + "/", len(x) + 2, len(x) + 1, x + "/")).rowcount
            else:
                n_mt += mt.execute("update stand set path=? where path=?", (y, x)).rowcount
        mt.commit(); mt.close()
    log(f"Video-Index: {n_vi} Pfade umgeschrieben | Defekt-Check: {n_dc} | Tagging-Stand: {n_mt}")
    tsv = os.path.join(wb.BERICHTE, f"umbenennung_{STEMPEL}.tsv")
    with open(tsv, "w", encoding="utf-8") as f:
        f.write("alt\tneu\n")
        for x, y in zuordnung: f.write(f"{x[len('/media/'):]}\t{y[len('/media/'):]}\n")
        for d in entfernt: f.write(f"{d[len('/media/'):]}\t-\n")
    log(f"Zuordnung für WD-Spiegelung: {tsv}")

log(f"{'umbenannt' if RUN else 'würde umbenennen'}: {len(ok)} (+{len(zuordnung) - len(ok)} trickplay) | übersprungen: {len(fehler)}"
    + (f" | Ordner entfernt: {len(entfernt)} | nicht leer: {len(rest)}" if RUN else ""))
for p, g in fehler[:30]: log(f"  ÜBERSPRUNGEN {g}: {p}")
for d, i in list(rest.items())[:30]: log(f"  NICHT LEER {d}: {i[:6]}")
