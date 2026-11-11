# MCSC – Media Collections Service Container

A self-hosted service container for large home media collections (music, audiobooks, movies, TV shows),
built for **Unraid** and **Jellyfin**. It runs the slow, boring maintenance work on the server itself – at night,
incrementally, and out of the way of backups – and shows what happened in a small web dashboard.

> **Status:** personal project in active use, published as-is. Code comments, log output and the web UI are
> currently in **German**. An installable Unraid Community Applications release is planned (see *Roadmap*).

## What it does

| Job | Description |
|---|---|
| **Music index** | Incremental SQLite index of MP3 collections (tags, duration, bitrate/VBR, embedded and folder cover art, lyrics, catalog and MusicBrainz IDs) via mutagen |
| **Audiobook index** | Same for audiobook collections (all common audio formats) |
| **Video index** | Movies, TV shows and other videos: ffprobe streams, NFO data, plus a read-only snapshot of the Jellyfin database, joined per file |
| **Defect check** | Reads every video once without decoding, decodes short samples, detects truncated files and silent audio tracks; only new/changed files are checked again |
| **MKV tagging** | Writes Jellyfin metadata into MKV headers (title, Matroska tags, cleaned track/chapter names, cover) with `mkvpropedit` – idempotent, only touches files whose metadata actually changed |
| **Rename executor** | Applies rename plans (e.g. scene names → Jellyfin naming scheme), never overwrites, moves trickplay folders along, updates all indexes, writes an `old → new` list |
| **Backup mirror helper** | Replays those rename lists on an rsync backup disk so the next backup does not copy everything again |
| **Web dashboard** | Overview of the last night (per job: OK / running / paused / error), live logs, reports as filterable tables, defect counts, backup state |

Everything that writes waits while the Unraid **mover** or the **backup** is running (lock/pid detection) and runs
with idle CPU/IO priority.

## Architecture

```
Browser ──► MCSC (web, port 8095)                 werkbank (jobs)
            Flask, read-only mounts               supercronic schedule (nightly)
                                                   /media rw, Jellyfin data ro
            both from the same image, sharing /werkbank (scripts, db/, logs/, berichte/)
```

* `werkbank.py` – shared helpers (paths, pause detection, logging, DB snapshots)
* `index_lauf.py` – nightly run: music → audiobooks → video index, then consistent DB snapshots in `db/stand/`
* `musik_index.py`, `hoerbuch_index.py`, `video_index.py` – the indexes
* `datei_check.py` – defect check (`--bis HH:MM` = do not start new files after that time)
* `mkv_taggen.py` – MKV tagging (`--plan`, `--schreiben`, `--nacht`)
* `umbenennen.py`, `subs_plan.py`, `wd_spiegeln.py` – renaming and backup mirroring
* `mcsc/` – the web dashboard
* `docker/` – Dockerfile and Unraid templates
* `crontab` – schedule (supercronic format)

## Installation (manual, for now)

1. Copy the repository to `/mnt/user/appdata/werkbank/skripte/` on your Unraid server.
2. Build the image:
   `docker build -t werkbank:1 /mnt/user/appdata/werkbank/skripte/docker`
3. Add both containers using the templates in `docker/` (`my-werkbank.xml`, `my-MCSC.xml`) – copy them to
   `/boot/config/plugins/dockerMan/templates-user/` and add them via *Docker → Add Container*.
4. Open `http://<server>:8095`.

### Mounts

| Container path | Host path | Mode | Purpose |
|---|---|---|---|
| `/media` | `/mnt/user/media` | rw (jobs) | your media share (`audio/`, `filme/`, `serien/`, `video/`) |
| `/werkbank` | `/mnt/user/appdata/werkbank` | rw (jobs) / ro (web) | scripts, databases, logs, reports |
| `/jellyfin` | Jellyfin's `data` folder | ro | Jellyfin database (read-only snapshot) |
| `/host/lock`, `/host/run` | `/var/lock`, `/run` | ro | pause while backup / mover run |
| `/host/log` | `/var/log` | ro (web) | backup log for the dashboard |

### Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MCSC_SMB_PREFIX` | `//server/media/` | how `/media` paths are stored in the indexes (as your PC sees the share), so desktop tools can open them directly |
| `MCSC_JELLYFIN_URL` | `http://jellyfin:8096` | Jellyfin server (cover images for MKV tagging) |
| `MCSC_JF_PFADE` | `/data/movies/=filme,/data/tvshows/=serien,/data/video/=video` | Jellyfin library paths → media folders |
| `MCSC_MUSIK_PRAEFIX` | `Sammlung-MP3` | folders under `/media/audio` that belong to the music index |
| `MCSC_HOERBUCH_PRAEFIX` | `Sammlung-Hörbücher` | folders under `/media/audio` that belong to the audiobook index |
| `MCSC_PORT` | `8095` | web port |
| `TZ` | `Europe/Berlin` | time zone for the schedule |

## Tools in the image

Debian trixie with ffmpeg, MKVToolNix, MediaInfo, mp3val, FLAC, LAME, SoX, rsgain, Chromaprint (`fpcalc`), 7-Zip,
SQLite and Python 3 (mutagen, numpy, Pillow, Flask, pyacoustid, requests, musicbrainzngs), scheduled by
[supercronic](https://github.com/aptible/supercronic).

## Roadmap

1. ~~Dashboard: overview, logs, reports~~
2. **Everything configurable:** one settings file (and a settings page in the dashboard) for libraries and
   their folders/types, which jobs run and when, time windows, pause rules, backup integration (optional),
   Jellyfin connection – no assumptions about folder names or a specific setup left in the code.
   A **setup wizard** walks you through your own conventions once (library detection, folder/file naming,
   tag grammar, collection rules, target formats, languages, providers, schedule) with a live preview on
   real files from your collection, and writes a readable settings file
3. **Mass transcoding** with profiles and verification before any original is removed: FLAC/WAV → MP3
   (tags and cover kept), CUE splitting, HandBrake/ffmpeg video profiles, surround extraction –
   with Intel Quick Sync (`/dev/dri`) where available
4. **Research toolchains** as modules: one provider layer (MusicBrainz, Discogs, AcoustID/Chromaprint,
   Cover Art Archive, Audible, Wikipedia, TMDB/Jellyfin) with caching and rate limiting; results become
   *proposals* that are only applied after review. Chains for music (fingerprint → release match →
   length verification → tags/cover/ReplayGain), audiobooks (author/title/first year/genre/cover) and video
   (import with NFO, extras, rename plans, audio language detection). Curation rules become settings.
   Further building blocks: synced lyrics (LRCLIB), BPM analysis, genre system, label/catalog number and
   spelling normalization, placeholder covers, compilations, release split/merge, MP3 repair (mp3val, Xing
   headers), version comparison, duplicate detection, final QA reports, a staging → library workflow with
   collision checks, and an optional AI provider for judgments and descriptions (always as proposals)
5. **Plugin jobs:** other tools register their own jobs (command, schedule, time window, log) in the settings;
   MCSC runs them with the same pause rules and shows them on the dashboard – e.g. a retro music
   (SID/MOD/MIDI) catalog scanner, with `sidplayfp`, `openmpt123` and `fluidsynth` available for rendering
6. Search across all indexes, defect list, statistics
7. Review lists in the browser (decide *keep / replace / delete* instead of passing CSV files around)
8. Job queue: start jobs from the dashboard, executed by the job container with the same pause rules
9. **Unraid Community Applications release:** prebuilt image on `ghcr.io` (GitHub Actions), English UI,
   CA template + support thread

## License

[MIT](LICENSE)
