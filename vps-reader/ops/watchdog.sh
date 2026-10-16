#!/bin/bash
# watchdog.sh — watchdog EXTERNO del reader headless. Solo LEE mtimes y el
# /health del scanner del que depende (c). NO toca el proceso, ni su cron, ni su
# binario. Es el mismo patron que backups/vps/paper-monitors-backup/watchdog.sh.
#
# Que vigila y por que:
#   - cycle.log : si el cron deja de correr, el fichero se enfria. 1 ciclo/min,
#                 asi que >5 min de frio es un FALLO, no una pausa.
#   - scanner   : (c) es su UNICA fuente viva. Si el scanner cae, el reader
#                 graba filas UNEVALUABLE (que es lo correcto), pero hay que
#                 saber que la causa es esa y no el reader.
#   - disco     : este VPS esta al ~79 %.
#   - rotacion  : cycle.log crece ~140 KB/dia; sin rotar no se ve el final.
#
# Escribe ALERT en watchdog.log y toca watchdog.heartbeat. Nunca actua.
set -u

MON=/home/ubuntu/vps-reader
OUT=/home/ubuntu/vps-reader-backup
LOG="$OUT/watchdog.log"
HB="$OUT/watchdog.heartbeat"
NOW=$(date -u +%FT%TZ)

mkdir -p "$OUT"
say() { echo "$NOW $*" >> "$LOG"; }

# ── 1) el ciclo corre? ────────────────────────────────────────────────────────
if [ ! -f "$MON/cycle.log" ]; then
  say "ALERT vps-reader: no existe $MON/cycle.log"
else
  AGE_MIN=$(( ( $(date +%s) - $(stat -c %Y "$MON/cycle.log") ) / 60 ))
  if [ "$AGE_MIN" -gt 5 ]; then
    say "ALERT vps-reader: cycle.log sin escribir hace ${AGE_MIN} min (cron roto?)"
  fi
fi

# ── 2) la unica fuente viva de (c) ───────────────────────────────────────────
if ! curl -s -m 5 -o /dev/null "http://127.0.0.1:8090/health"; then
  say "ALERT scanner-engine :8090 NO responde — (c) se queda sin fuente"
fi

# ── 3) disco ─────────────────────────────────────────────────────────────────
PCT=$(df --output=pcent / | tail -1 | tr -dc '0-9')
if [ -n "$PCT" ] && [ "$PCT" -ge 90 ]; then
  say "ALERT disco / al ${PCT} %"
fi

# ── 4) rotacion de logs: un solo .1 por fichero (bounded, verificable) ───────
for f in "$MON/cycle.log" "$OUT/backup.log" "$LOG"; do
  [ -f "$f" ] || continue
  SZ=$(stat -c %s "$f")
  if [ "$SZ" -gt 5242880 ]; then
    mv -f "$f" "$f.1"
    say "rotado $(basename "$f") (${SZ} bytes)"
  fi
done

touch "$HB"
exit 0
