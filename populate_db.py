"""Populate both raw_snapshots and computed_ticks with realistic pseudo data for today."""
import sqlite3
import random
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
DB_PATH = "/home/ubuntu/index_pcr/data/oi_data.db"

today = datetime.now(IST).strftime("%Y-%m-%d")
market_open = datetime.strptime(today + " 09:15:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
market_close = datetime.strptime(today + " 15:30:00", "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)

INSTRUMENTS = {
    "nifty": {"spot_base": 24850, "strike_step": 50, "strike_count": 5, "oi_scale": 1},
    "banknifty": {"spot_base": 53200, "strike_step": 100, "strike_count": 5, "oi_scale": 0.7},
    "sensex": {"spot_base": 82100, "strike_step": 100, "strike_count": 5, "oi_scale": 0.3},
}

def iso_ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")

# Clear today's data
conn.execute("DELETE FROM oi_snapshots WHERE substr(timestamp,1,10) = ?", (today,))
conn.execute("DELETE FROM computed_ticks WHERE substr(timestamp,1,10) = ?", (today,))
conn.execute("DELETE FROM daily_baselines WHERE date = ?", (today,))
conn.commit()

print("Populating data for " + today + "...")

raw_count = 0
for inst_name, cfg in INSTRUMENTS.items():
    spot = cfg["spot_base"]
    atm = round(spot / cfg["strike_step"]) * cfg["strike_step"]
    strikes = [atm + i * cfg["strike_step"] for i in range(-cfg["strike_count"], cfg["strike_count"] + 1)]

    t = market_open
    tick_num = 0
    while t <= market_close:
        ts = iso_ts(t)
        spot += random.uniform(-5, 5) + math.sin(tick_num * 0.01) * 2
        atm = round(spot / cfg["strike_step"]) * cfg["strike_step"]

        for strike in strikes:
            distance = abs(strike - atm) / cfg["strike_step"]
            base_oi = max(50000, int(500000 * cfg["oi_scale"] * math.exp(-0.3 * distance)))
            ce_oi = base_oi + random.randint(-20000, 50000) + (int((spot - strike) * 100) if strike < spot else 0)
            pe_oi = base_oi + random.randint(-20000, 50000) + (int((strike - spot) * 100) if strike > spot else 0)
            ce_oi = max(10000, ce_oi)
            pe_oi = max(10000, pe_oi)
            hour_factor = 1 + (t.hour - 9) * 0.2
            ce_volume = int(random.uniform(1000, 50000) * hour_factor * cfg["oi_scale"])
            pe_volume = int(random.uniform(1000, 50000) * hour_factor * cfg["oi_scale"])
            ce_iv = 12 + distance * 2 + random.uniform(-1, 1)
            pe_iv = 12 + distance * 2 + random.uniform(-1, 1)

            conn.execute(
                "INSERT INTO oi_snapshots (timestamp, instrument, expiry, strike, "
                "underlying_spot_price, atm_strike, pcr, "
                "ce_oi, pe_oi, ce_volume, pe_volume, ce_iv, pe_iv, ce_ltp, pe_ltp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, inst_name, today, strike, round(spot, 2), atm,
                 round(pe_oi / ce_oi, 4) if ce_oi > 0 else 0,
                 ce_oi, pe_oi, ce_volume, pe_volume,
                 round(ce_iv, 2), round(pe_iv, 2),
                 round(max(0.5, (spot - strike) * 0.5 + random.uniform(0, 20)), 2),
                 round(max(0.5, (strike - spot) * 0.5 + random.uniform(0, 20)), 2)))
            raw_count += 1

        t += timedelta(seconds=30)
        tick_num += 1

    # Save baseline
    first_ts = iso_ts(market_open)
    baseline_rows = conn.execute(
        "SELECT * FROM oi_snapshots WHERE instrument = ? AND timestamp = ?",
        (inst_name, first_ts)).fetchall()
    for row in baseline_rows:
        conn.execute(
            "INSERT OR REPLACE INTO daily_baselines "
            "(date, baseline_type, snapshot_timestamp, instrument, expiry, strike, "
            "underlying_spot_price, atm_strike, pcr, ce_oi, pe_oi, ce_volume, pe_volume, ce_iv, pe_iv) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (today, "post_settlement", first_ts, inst_name, row["expiry"], row["strike"],
             row["underlying_spot_price"], row["atm_strike"], row["pcr"],
             row["ce_oi"], row["pe_oi"], row["ce_volume"], row["pe_volume"],
             row["ce_iv"], row["pe_iv"]))

conn.commit()
print("  Raw snapshots: " + str(raw_count) + " rows")

# Computed ticks every 1 minute
computed_count = 0
for inst_name, cfg in INSTRUMENTS.items():
    t = market_open
    tick_num = 0
    prev_oi_diff = 0
    first_ce_oi = None
    first_pe_oi = None
    baseline_ce = None
    baseline_pe = None

    while t <= market_close:
        ts = iso_ts(t)
        rows = conn.execute(
            "SELECT SUM(ce_oi) as total_ce, SUM(pe_oi) as total_pe, "
            "AVG(underlying_spot_price) as spot, AVG(atm_strike) as atm, "
            "SUM(ce_volume) as ce_vol, SUM(pe_volume) as pe_vol, "
            "AVG(ce_iv) as ce_iv, AVG(pe_iv) as pe_iv "
            "FROM oi_snapshots WHERE instrument = ? AND timestamp = ?",
            (inst_name, ts)).fetchone()

        if rows and rows["total_ce"]:
            total_ce = rows["total_ce"]
            total_pe = rows["total_pe"]
            spot = rows["spot"]
            atm = rows["atm"]
            ce_vol = rows["ce_vol"] or 0
            pe_vol = rows["pe_vol"] or 0
            ce_iv_val = rows["ce_iv"]
            pe_iv_val = rows["pe_iv"]

            if first_ce_oi is None:
                first_ce_oi = total_ce
                first_pe_oi = total_pe
                baseline_ce = total_ce
                baseline_pe = total_pe

            pcr = total_pe / total_ce if total_ce > 0 else None
            ce_oi_change = total_ce - baseline_ce
            pe_oi_change = total_pe - baseline_pe
            ce_cumm = total_ce - first_ce_oi
            pe_cumm = total_pe - first_pe_oi
            oi_diff = pe_cumm - ce_cumm
            delta_pcr = pe_oi_change / ce_oi_change if ce_oi_change != 0 else None
            signed_pcr = pe_oi_change / abs(ce_oi_change) if ce_oi_change != 0 else None
            vol_pcr = pe_vol / ce_vol if ce_vol > 0 else None

            signal = None
            crossover = 0
            if tick_num > 0:
                if prev_oi_diff <= 0 and oi_diff > 0:
                    signal = "BUY"
                    crossover = 1
                elif prev_oi_diff >= 0 and oi_diff < 0:
                    signal = "SELL"
                    crossover = 1
            prev_oi_diff = oi_diff

            conn.execute(
                "INSERT OR REPLACE INTO computed_ticks "
                "(timestamp, instrument, spot_price, atm_strike, "
                "total_ce_oi, total_pe_oi, pcr, "
                "ce_oi_change, pe_oi_change, ce_oi_cumm_change, pe_oi_cumm_change, "
                "oi_difference, delta_pcr, signed_pcr, volume_pcr, "
                "ce_volume, pe_volume, ce_iv_avg, pe_iv_avg, signal, crossover) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, inst_name, spot, atm,
                 total_ce, total_pe, pcr,
                 ce_oi_change, pe_oi_change, ce_cumm, pe_cumm,
                 oi_diff, delta_pcr, signed_pcr, vol_pcr,
                 ce_vol, pe_vol, ce_iv_val, pe_iv_val,
                 signal, crossover))
            computed_count += 1

        t += timedelta(minutes=1)
        tick_num += 1

conn.commit()
print("  Computed ticks: " + str(computed_count) + " rows")

# Summary
total_raw = conn.execute("SELECT COUNT(*) FROM oi_snapshots WHERE substr(timestamp,1,10) = ?", (today,)).fetchone()[0]
total_computed = conn.execute("SELECT COUNT(*) FROM computed_ticks WHERE substr(timestamp,1,10) = ?", (today,)).fetchone()[0]
total_baselines = conn.execute("SELECT COUNT(*) FROM daily_baselines WHERE date = ?", (today,)).fetchone()[0]

print("\n=== SUMMARY ===")
print("Date: " + today)
print("Raw snapshots: " + str(total_raw))
print("Computed ticks: " + str(total_computed))
print("Baselines: " + str(total_baselines))

# Sample signals
sample = conn.execute(
    "SELECT timestamp, pcr, oi_difference, signal, crossover FROM computed_ticks "
    "WHERE instrument = 'nifty' AND signal IS NOT NULL LIMIT 5").fetchall()
print("\nSample NIFTY signals:")
for row in sample:
    print("  " + str(row[0]) + " | PCR=" + str(round(row[1], 3)) +
          " | OI_Diff=" + str(int(row[2])) + " | " + str(row[3]) +
          " | Crossover=" + str(row[4]))

conn.close()
print("\nDone!")
