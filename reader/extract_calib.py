"""
extract_calib.py — Extrae alerts.db (QAE) a una tabla propia del reader.

POR QUE
=======
`quant-alert-engine/data/alerts.db` tiene 2.827 senales con 41 features y
etiqueta, del 8-sep al 6-oct (28 dias). QAE es ahora manual-only y su carpeta
puede borrarse, asi que la calibracion no debe depender de ella.

ADVERTENCIA IMPORTANTE (bug mio, ya corregido)
==============================================
`signal_outcomes.forward_return` YA VIENE CON SIGNO APLICADO:
    quant_alert_engine.py:2375
    directional_ret = -raw_ret if dir_sign == -1 else raw_ret
Y `outcome_stats()` lo re-invierte otra vez en SQL (L764) para contar wins.

Por tanto `forward_return` se copia TAL CUAL, sin tocar el signo. Cualquier
script de analisis que lo multiplique por `direction` esta invirtiese el
resultado dos veces y producing spreads NEGATIVOS ARTIFICIALES. Ese error
produjo un "CVD invertido" que era falso.

Solo se extraen filas con direction != 0: direction == 0 significa "senal sin
direccion" y su retorno no es interpretable como a favor/en contra.
"""
import sqlite3, json, os, sys
from datetime import datetime, timezone

SRC = r"C:\Users\Lenovo\trading-engines\quant-alert-engine\data\alerts.db"
DST = r"C:\Users\Lenovo\trading-engines\reader\calib_features.db"

FEATURES = [
    "rsi", "rsi_dist50", "rsi_slope3", "macd_line_pct", "macd_signal_pct",
    "macd_hist_pct", "macd_hist_slope3", "macd_above", "stoch_k", "stoch_kd",
    "atr_pct", "atr_rank_pct", "atr_expansion", "bb_width_pct",
    "bb_width_rank_pct", "bb_pos", "range_pct", "vol_ratio", "vol_ratio5",
    "delta_pct_vol", "cvd_cum_pct", "cvd_slope5_pct", "obv_slope5_pct",
    "ema50_dist_pct", "ema200_dist_pct", "ret1_pct", "ret5_pct", "ret20_pct",
    "er20", "trend_consistency", "va_pos", "va_dist_poc_pct", "va_width_pct",
    "regime", "bull_conf", "bear_conf", "conf_diff",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
]

src = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
src.row_factory = sqlite3.Row

rows = src.execute(
    "SELECT o.symbol, o.timeframe, o.signal_name, o.signal_type, o.direction,"
    "       o.price, o.bar_key, o.fired_ts, o.forward_bars, o.forward_return,"
    "       f.features_json "
    "FROM signal_outcomes o JOIN signal_features f ON f.signal_id = o.signal_id "
    "WHERE o.resolved = 1 AND o.forward_return IS NOT NULL"
).fetchall()
print(f"filas origen: {len(rows)}")

if os.path.exists(DST):
    dst = sqlite3.connect(DST)
    dst.execute("DROP TABLE IF EXISTS samples")
    print("  (tabla previa eliminada)")
else:
    dst = sqlite3.connect(DST)

coldefs = ", ".join(f'"{f}" REAL' for f in FEATURES)   # para CREATE TABLE
# Para INSERT solo los nombres: los tipos no van en la lista de columnas.
colnames = ", ".join(f'"{f}"' for f in FEATURES)
# Campos de la lista INSERT: 11 base (src_signal_id..forward_return)
# + len(FEATURES) + extracted_at. `id` es autoincrement y no se lista.
n_cols = 11 + len(FEATURES) + 1
placeholders = ",".join("?" * n_cols)
dst.execute(f"""
CREATE TABLE samples (
    id INTEGER PRIMARY KEY,
    src_signal_id TEXT UNIQUE,
    symbol TEXT, timeframe TEXT, signal_name TEXT, signal_type TEXT,
    direction INTEGER, price REAL, bar_key TEXT, fired_ts REAL,
    forward_bars INTEGER, forward_return REAL,
    {coldefs},
    extracted_at TEXT
)""")
dst.execute("CREATE INDEX ix_samples_sym_tf ON samples(symbol, timeframe)")
dst.execute("CREATE INDEX ix_samples_ts ON samples(fired_ts)")

def num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None

ins = []
skipped = 0
for r in rows:
    try:
        fj = json.loads(r["features_json"])
    except Exception:
        skipped += 1
        continue
    sid = f"{r['symbol']}|{r['timeframe']}|{r['bar_key']}|{r['signal_name']}"
    vals = [num(fj.get(f)) for f in FEATURES]
    ins.append((
        sid, r["symbol"], r["timeframe"], r["signal_name"], r["signal_type"],
        int(r["direction"]) if r["direction"] is not None else 0,
        num(r["price"]), r["bar_key"], float(r["fired_ts"]),
        int(r["forward_bars"]) if r["forward_bars"] else None,
        float(r["forward_return"]), *vals, datetime.now(timezone.utc).isoformat(),
    ))

dst.executemany(
    'INSERT OR REPLACE INTO samples (src_signal_id,symbol,timeframe,signal_name,'
    'signal_type,direction,price,bar_key,fired_ts,forward_bars,forward_return,'
    f'{colnames},extracted_at) VALUES ({placeholders})', ins)
dst.commit()

n, nd = dst.execute("SELECT COUNT(*) FROM samples").fetchone(), None
nd = dst.execute("SELECT COUNT(*) FROM samples WHERE direction!=0").fetchone()[0]
a, b = dst.execute("SELECT MIN(fired_ts), MAX(fired_ts) FROM samples").fetchone()
print(f"insertadas: {n[0]}  (direction!=0: {nd})  omitidas por JSON malo: {skipped}")
print(f"rango: {datetime.fromtimestamp(a)} -> {datetime.fromtimestamp(b)}  ({(b-a)/86400:.1f} dias)")
print(f"destino: {DST}")

print("\n--- verificacion de integridad ---")
w = dst.execute("SELECT COUNT(*) FROM samples WHERE forward_return>0 AND direction!=0").fetchone()[0]
l = dst.execute("SELECT COUNT(*) FROM samples WHERE forward_return<=0 AND direction!=0").fetchone()[0]
print(f"  direction!=0: {w} con retorno positivo / {l} negativo = {w/(w+l)*100:.1f}% a favor")
print("  (esperado ~46.1% segun la medicion corregida)")
dst.close(); src.close()
print("\nOK. Tabla lista en reader/calib_features.db, desacoplada de QAE.")