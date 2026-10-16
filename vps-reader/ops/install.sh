#!/bin/bash
# install.sh — despliegue idempotente del reader headless en el VPS.
#
# Se ejecuta EN EL VPS, desde el directorio que se sube con scp. No inventa
# rutas: replica en ~/vps-reader/ el arbol del repo para que verdict.py resuelva
# `HERE.parent/atas-bridge/rules.json` sin tocar una linea de verdict.py.
#
# Lo que NO hace a proposito:
#   - No toca reader/reader.db ni dom_vac_calibration.json ni dom_gate_state.json.
#     Son ESTADO. Reinstalar el binario no puede resetear el ledger ni la
#     calibracion acumulada. (Ninguno de esos ficheros viene en el stage.)
#   - No toca el crontab de otros (grep -v 'vps-reader' solo borra lo propio).
set -euo pipefail

STAGE=$(cd "$(dirname "$0")" && pwd)
MON=/home/ubuntu/vps-reader
BAK=/home/ubuntu/vps-reader-backup

echo "== vps-reader install =="
mkdir -p "$MON/reader" "$MON/atas-bridge" "$BAK"

# 1) normalizar a LF: el stage viene de Windows y CRLF rompe bash y python
for f in "$STAGE"/vps_reader.py "$STAGE"/watchdog.sh "$STAGE"/backup.sh \
         "$STAGE"/reader/*.py "$STAGE"/atas-bridge/*.json; do
  [ -f "$f" ] && sed -i 's/\r$//' "$f"
done

# 2) colocar (el ledger y la calibracion NO estan aqui -> sobreviven)
install -m 0644 "$STAGE/vps_reader.py"                       "$MON/vps_reader.py"
install -m 0644 "$STAGE/reader/verdict.py"                   "$MON/reader/verdict.py"
install -m 0644 "$STAGE/reader/build_dom_vac_calibration.py" "$MON/reader/build_dom_vac_calibration.py"
install -m 0644 "$STAGE/atas-bridge/rules.json"              "$MON/atas-bridge/rules.json"
install -m 0755 "$STAGE/watchdog.sh"                         "$BAK/watchdog.sh"
install -m 0755 "$STAGE/backup.sh"                           "$BAK/backup.sh"

# 3) crontab, idempotente: se borran solo las lineas que contienen 'vps-reader'
crontab -l 2>/dev/null | grep -v 'vps-reader' > /tmp/ct.new || true
cat >> /tmp/ct.new <<EOF

# vps-reader — grabador headless de (c) 24/7 (cron: autocontenido, sin demonio)
* * * * * cd $MON && /usr/bin/python3 vps_reader.py >> $MON/cycle.log 2>&1
5 6 * * * /bin/bash $BAK/backup.sh >> $BAK/backup.log 2>&1
11 * * * * /bin/bash $BAK/watchdog.sh
EOF
crontab /tmp/ct.new
rm -f /tmp/ct.new

# 4) un ciclo de prueba en el acto
echo "-- ciclo de prueba --"
cd "$MON" && /usr/bin/python3 vps_reader.py

echo "-- crontab --"
crontab -l
echo "== fin =="
