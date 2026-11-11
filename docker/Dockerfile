# Werkbank/MCSC: ein Image für den Dienst-Container (werkbank) und die Web-Oberfläche (MCSC)
# Werkbank: Dienst-Container für Medien-Läufe (Defekt-Check, Indizes, Tagging)
# Bauen:   docker build -t werkbank:1 /mnt/user/appdata/werkbank/skripte/docker
FROM debian:trixie-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg mkvtoolnix mediainfo sqlite3 python3 python3-mutagen python3-numpy python3-pil python3-flask \
      mp3val flac lame sox rsgain libchromaprint-tools 7zip \
      python3-acoustid python3-requests python3-musicbrainzngs \
      rsync util-linux procps tini ca-certificates curl tzdata git less nano \
 && rm -rf /var/lib/apt/lists/*

# supercronic: Cron für Container (läuft ohne root, Ausgabe ins Docker-Log)
RUN curl -fsSL -o /usr/local/bin/supercronic \
      https://github.com/aptible/supercronic/releases/download/v0.2.33/supercronic-linux-amd64 \
 && chmod +x /usr/local/bin/supercronic

ENV TZ=Europe/Berlin \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/werkbank/skripte

WORKDIR /werkbank
# Unraid-Standard nobody:users – alles, was die Werkbank schreibt, passt zu den Freigaberechten
USER 99:100
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/supercronic", "-passthrough-logs", "/werkbank/skripte/crontab"]
