#!/bin/bash
# backup.sh — copia diaria del ledger del reader headless, con huella sha256 y
# retencion 90 dias. Mismo patron que paper-monitors-backup/backup.sh.
#
# NO valida los datos: guarda lo que hay y deja la huella para poder detectar
# manipulacion o corrupcion mas tarde. Un backup que tambien "interpreta" deja
# de ser un backup.
set -u

SRC=/home/ubuntu/vps-reader
DST=/home/ubuntu/vps-reader-backup
DAY=$(date -u +%F)
mkdir -p "$DST"

# Copia EN CALIENTE y consistente. Este VPS NO tiene el CLI de sqlite3, y un
# `cp` de un .db vivo puede capturar una pagina a medias y dejar un fichero
# corrupto que no se descubre hasta que hay que restaurarlo — lo peor que puede
# hacer un backup, porque falla en silencio. Se usa la MISMA API de backup
# online que expone el CLI (`.backup`), desde el modulo sqlite3 de python3 (que
# esta garantizado), y se comprueba con quick_check.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
if ! python3 - "$SRC/reader/reader.db" "$TMP/reader.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
res = dst.execute("PRAGMA quick_check").fetchone()[0]
dst.close(); src.close()
if res != "ok":
    sys.exit(f"quick_check fallo: {res}")
PY
then
  echo "$(date -u +%FT%TZ) ERROR copia consistente de reader.db" >> "$DST/backup.log"
  exit 1
fi

# Lista explicita de lo que existe: tar con ficheros ausentes devuelve !=0 y
# ensucia el log. El binario del gate (verdict.py) se guarda porque es el que
# define como se calcula (c): sin el, el ledger no es interpretable solo.
FILES=()
[ -f "$TMP/reader.db" ] && FILES+=("-C" "$TMP" "reader.db")
for f in vps_reader.py reader/verdict.py reader/dom_vac_calibration.json cycle.log; do
  [ -f "$SRC/$f" ] && FILES+=("-C" "$SRC" "$f")
done
[ "${#FILES[@]}" -gt 0 ] && tar -czf "$DST/snap-$DAY.tar.gz" "${FILES[@]}"

if [ -f "$DST/snap-$DAY.tar.gz" ]; then
  ( cd "$DST" && sha256sum "snap-$DAY.tar.gz" > "snap-$DAY.sha256" )
  echo "$(date -u +%FT%TZ) backup ok: snap-$DAY.tar.gz $(stat -c %s "$DST/snap-$DAY.tar.gz") bytes" >> "$DST/backup.log"
else
  echo "$(date -u +%FT%TZ) ERROR backup: no se creo snap-$DAY.tar.gz" >> "$DST/backup.log"
  exit 1
fi

# retencion 90 dias
find "$DST" -maxdepth 1 -name 'snap-*.tar.gz' -mtime +90 -delete
find "$DST" -maxdepth 1 -name 'snap-*.sha256' -mtime +90 -delete
exit 0
