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

# SQLite en caliente: `sqlite3 .backup` es consistente aunque haya escritura
# concurrente. Un `cp` de un .db vivo puede capturar una escritura a medias.
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$SRC/reader/reader.db" ".backup '$TMP/reader.db'"
else
  cp "$SRC/reader/reader.db" "$TMP/reader.db"
  echo "$(date -u +%FT%TZ) AVISO sqlite3 no esta: backup por cp (no consistente en caliente)" >> "$DST/backup.log"
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
