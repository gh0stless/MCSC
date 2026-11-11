"""Umbenennungen (berichte/umbenennung_*.tsv, Pfade relativ zu /media) auf der WD-Backup-Platte nachspielen,
damit das nächste Backup nicht alles neu kopiert. Nur umbenennen, nie überschreiben, nie löschen (außer leere Ordner).
Läuft als Wegwerf-Container mit der Platte unter /wd (= .../backup/media):
  docker run --rm -u 0 --entrypoint python3 -v /mnt/disks/<PLATTE>/backup/media:/wd \
      -v /mnt/user/appdata/werkbank:/werkbank werkbank:1 /werkbank/skripte/wd_spiegeln.py [muster] [--run]
muster: Glob für die TSVs (Standard umbenennung_*.tsv)"""
import os, sys, glob, collections

RUN = "--run" in sys.argv
args = [a for a in sys.argv[1:] if not a.startswith("--")]
MUSTER = args[0] if args else "umbenennung_*.tsv"
WD = "/wd"
tsvs = sorted(glob.glob(os.path.join("/werkbank/berichte", MUSTER)))
print(("LAUF" if RUN else "PROBE"), "|", len(tsvs), "Listen:", ", ".join(os.path.basename(t) for t in tsvs))

st = collections.Counter(); meld = collections.defaultdict(list)
virtuell = []   # Probe: umbenannte Ordner simulieren (neu/ -> alt/)


def real(p):
    for n, a in virtuell:
        if p.startswith(n): return a + p[len(n):]
    return p


for tsv in tsvs:
    for z in open(tsv, encoding="utf-8").read().splitlines()[1:]:
        alt, neu = z.split("\t")
        a = os.path.join(WD, alt)
        if neu == "-":
            if RUN:
                if os.path.isdir(a):
                    if os.listdir(a): st["Ordner nicht leer"] += 1; meld["Ordner nicht leer"].append(f"{alt}: {os.listdir(a)[:5]}")
                    else: os.rmdir(a); st["Ordner entfernt"] += 1
            else: st["Ordner (würde entfernen falls leer)"] += 1
            continue
        n = os.path.join(WD, neu)
        q_da, z_da = os.path.exists(real(a)), os.path.exists(n)
        if q_da and not z_da:
            if RUN:
                os.makedirs(os.path.dirname(n), exist_ok=True); os.rename(a, n)
            elif os.path.isdir(real(a)): virtuell.insert(0, (n + "/", real(a) + "/"))
            st["umbenannt" if RUN else "würde umbenennen"] += 1
        elif not q_da and z_da: st["schon erledigt"] += 1
        elif q_da and z_da: st["Konflikt (beide da)"] += 1; meld["Konflikt"].append(f"{alt} -> {neu}")
        else: st["nicht im Backup"] += 1; meld["nicht im Backup"].append(alt)

print(dict(st))
for k, l in meld.items():
    print(f"--- {k}: {len(l)}")
    for x in l[:15]: print("   ", x)
