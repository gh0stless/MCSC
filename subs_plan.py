"""Untertitel aus Subs\\-Unterordnern neben die umbenannte Folge holen (Nachtrag zu serien_plan / umbenennen.py).
Liest die Zuordnungen berichte/umbenennung_*.tsv (alt -> neu), findet zu jedem <Folgenordner>/Subs die neue Folge und
schreibt einen Plan für umbenennen.py:  <neuer Stamm>[.eng|.ger][.forced].idx/.sub
Aufruf: python3 subs_plan.py <bibliothek, z. B. /media/serien> <plan.json>"""
import os, re, sys, glob, json
import werkbank as wb

BIB, AUS = sys.argv[1], sys.argv[2]
VIDEO = (".mkv", ".mp4", ".avi", ".m4v")
neu_von_dir = {}
for tsv in sorted(glob.glob(os.path.join(wb.BERICHTE, "umbenennung_*.tsv"))):
    for z in open(tsv, encoding="utf-8").read().splitlines()[1:]:
        a, n = z.split("\t")
        if n != "-" and a.lower().endswith(VIDEO):
            neu_von_dir["/media/" + os.path.dirname(a)] = "/media/" + os.path.splitext(n)[0]

plan, ohne = [], []
for subs in sorted(d for d in glob.glob(os.path.join(glob.escape(BIB), "**", "*"), recursive=True)
                   if os.path.isdir(d) and os.path.basename(d).lower() == "subs"):
    folge = os.path.dirname(subs)
    ziel_stamm = neu_von_dir.get(folge)
    if not ziel_stamm: ohne.append(subs); continue
    belegt = set()
    for f in sorted(os.listdir(subs)):
        stamm, ext = os.path.splitext(f)
        toks = re.split(r"[.\-_ ]", stamm.lower())
        forced = "forced" in toks[-2:]
        hinten = [t for t in toks[-3:] if t != "forced"]
        lang = ".eng" if any(t in ("eng", "english") for t in hinten) else ".ger" if any(t in ("ger", "deu") for t in hinten) else ""
        neu = ziel_stamm + lang + (".forced" if forced else "")
        k, i = neu, 2
        while (k + ext).lower() in belegt or os.path.exists(k + ext): k = f"{neu}.{i}"; i += 1
        belegt.add((k + ext).lower())
        plan.append({"alt": wb.pc_pfad(os.path.join(subs, f)), "neu": wb.pc_pfad(k + ext), "art": "begleit"})
json.dump(plan, open(AUS, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
print(f"{len(plan)} Untertitel-Dateien geplant, Subs-Ordner ohne zugeordnete Folge: {len(ohne)}")
for s in ohne[:20]: print("  ohne Folge:", s)
for e in plan[:6]: print("  ", e["alt"].rsplit("\\", 1)[1], "->", e["neu"].split("\\", 2)[2])
