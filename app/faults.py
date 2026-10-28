"""Fault functions: each takes a clean day of vendor-feed rows and breaks it in one specific way.

Every function returns (broken_df, description). The description is what we save as ground truth.
These are plain pandas functions with no database access, so they are easy to test on their own.
"""

import numpy as np
import pandas as pd


# ---------- simple faults: loud, easy to spot ----------

def renamed_column(df, rng, day):
    df = df.rename(columns={"amount_inr": "amount"})
    return df, "Vendor renamed column 'amount_inr' to 'amount'"


def null_spike(df, rng, day):
    df = df.copy()
    rows = rng.choice(len(df), size=int(len(df) * 0.30), replace=False)
    df.loc[df.index[rows], "amount_inr"] = np.nan
    return df, "30% of amount_inr values were blanked out (null spike)"


def duplicate_rows(df, rng, day):
    copies = df.sample(frac=0.15, random_state=int(rng.integers(0, 1000)))
    return pd.concat([df, copies], ignore_index=True), "15% of rows were sent twice with the same order_id"


def row_count_drop(df, rng, day):
    kept = df.sample(frac=0.40, random_state=int(rng.integers(0, 1000))).sort_index()
    return kept, "Only 40% of rows (picked at random through the day) arrived"


# ---------- silent faults: the pipeline still succeeds, but the data is wrong ----------

def currency_unit(df, rng, day):
    df = df.copy()
    df["amount_inr"] = df["amount_inr"] * 100
    return df, "Vendor switched amount from rupees to paise: amount_inr is 100x bigger"


def timezone_shift(df, rng, day):
    df = df.copy()
    shifted = pd.to_datetime(df["order_ts"]) - pd.Timedelta(hours=5, minutes=30)  # IST -> UTC
    df["order_ts"] = shifted.dt.strftime("%Y-%m-%d %H:%M:%S")
    return df, "Vendor switched order_ts from IST to UTC: early-morning orders land on the previous date"


def join_fanout(df, rng, day):
    # The vendor's upstream join changed from order level to order-line level, so some orders come
    # back 3 times. Each copy gets a NEW order_id, so a plain duplicate-key check will not see it.
    chosen = df.sample(frac=0.30, random_state=int(rng.integers(0, 1000)))
    next_id = df["order_id"].max() + 1
    copies = []
    for _ in range(2):
        copy = chosen.copy()
        copy["order_id"] = np.arange(next_id, next_id + len(copy))
        next_id += len(copy)
        copies.append(copy)
    out = pd.concat([df] + copies).sort_values("order_ts").reset_index(drop=True)
    return out, "Join key changed upstream: 30% of orders appear 3 times under new order_ids (fan-out)"


def late_partial(df, rng, day):
    cutoff = f"{day} 09:00:00"
    return df[df["order_ts"] < cutoff], "Feed was cut off early: only orders before 09:00 arrived (partial data)"


FAULTS = {
    "renamed_column": renamed_column,
    "null_spike": null_spike,
    "duplicate_rows": duplicate_rows,
    "row_count_drop": row_count_drop,
    "currency_unit": currency_unit,
    "timezone_shift": timezone_shift,
    "join_fanout": join_fanout,
    "late_partial": late_partial,
}


def inject(df, fault_names, rng, day):
    """Apply one or more faults in order (two names = a combo). Returns (broken_df, combined description)."""
    descriptions = []
    for name in fault_names:
        df, description = FAULTS[name](df, rng, day)
        descriptions.append(description)
    return df, "; ".join(descriptions)
