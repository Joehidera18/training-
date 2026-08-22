"""
Crypto Research Lab - Data Utilities

Responsible for cleaning and validating OHLCV candle data before it is
used by the research, feature-engineering, learning, and backtesting layers.

Important:
- No future information is added here.
- Rows remain chronological.
- Invalid/duplicate candles are removed.
- Data-quality statistics are returned so bad datasets can be rejected.
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize common OHLCV column names."""
    out = df.copy()

    out.columns = [str(c).strip().lower() for c in out.columns]

    aliases = {
        "timestamp": "timestamp",
        "time": "timestamp",
        "date": "timestamp",
        "datetime": "timestamp",
        "open_time": "timestamp",
        "opentime": "timestamp",
        "vol": "volume",
        "base_volume": "volume",
    }

    out = out.rename(
        columns={c: aliases[c] for c in out.columns if c in aliases}
    )

    return out


def _detect_timestamp_column(df: pd.DataFrame) -> str | None:
    """Find the timestamp column if one exists."""
    candidates = (
        "timestamp",
        "open_time",
        "time",
        "datetime",
        "date",
    )

    for column in candidates:
        if column in df.columns:
            return column

    return None


def _timestamp_to_ms(series: pd.Series) -> pd.Series:
    """
    Convert timestamps to Unix milliseconds.

    Supports numeric timestamps in seconds/ms/us/ns and datetime strings.
    """
    numeric = pd.to_numeric(series, errors="coerce")

    if numeric.notna().mean() >= 0.90:
        median = numeric.dropna().abs().median()

        if pd.isna(median):
            return numeric

        # Rough epoch magnitude detection.
        if median < 1e11:
            numeric = numeric * 1000.0
        elif median >= 1e17:
            numeric = numeric / 1_000_000.0
        elif median >= 1e14:
            numeric = numeric / 1000.0

        return numeric.round()

    parsed = pd.to_datetime(series, errors="coerce", utc=True)

    result = pd.Series(np.nan, index=series.index, dtype="float64")
    valid = parsed.notna()

    if valid.any():
        result.loc[valid] = (
            parsed.loc[valid].astype("int64") // 1_000_000
        ).astype(float)

    return result


def _infer_interval_ms(timestamps: pd.Series) -> int | None:
    """Infer the most common candle spacing."""
    values = (
        pd.to_numeric(timestamps, errors="coerce")
        .dropna()
        .sort_values()
        .drop_duplicates()
    )

    if len(values) < 3:
        return None

    diffs = values.diff().dropna()
    diffs = diffs[diffs > 0]

    if diffs.empty:
        return None

    return int(round(float(diffs.median())))


def validate_ohlcv(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Produce data-quality diagnostics for an OHLCV dataframe.

    This does not modify the dataframe.
    """
    if df is None or len(df) == 0:
        return {
            "valid": False,
            "rows": 0,
            "duplicates": 0,
            "bad_ohlc": 0,
            "gaps": 0,
            "coverage_pct": 0.0,
            "start_ts": None,
            "end_ts": None,
            "interval_ms": None,
        }

    work = _normalize_columns(df)

    missing = [c for c in REQUIRED_COLUMNS if c not in work.columns]

    if missing:
        return {
            "valid": False,
            "rows": int(len(work)),
            "duplicates": 0,
            "bad_ohlc": 0,
            "gaps": 0,
            "coverage_pct": 0.0,
            "start_ts": None,
            "end_ts": None,
            "interval_ms": None,
            "missing_columns": missing,
        }

    timestamp_col = _detect_timestamp_column(work)

    duplicates = 0
    gaps = 0
    coverage_pct = 100.0
    start_ts = None
    end_ts = None
    interval_ms = None

    if timestamp_col is not None:
        ts = _timestamp_to_ms(work[timestamp_col])
        valid_ts = ts.dropna().sort_values()

        duplicates = int(valid_ts.duplicated().sum())

        if not valid_ts.empty:
            start_ts = int(valid_ts.iloc[0])
            end_ts = int(valid_ts.iloc[-1])

            unique_ts = valid_ts.drop_duplicates()
            interval_ms = _infer_interval_ms(unique_ts)

            if interval_ms and len(unique_ts) > 1:
                span = end_ts - start_ts
                expected_rows = int(round(span / interval_ms)) + 1

                if expected_rows > 0:
                    missing_bars = max(expected_rows - len(unique_ts), 0)
                    gaps = int(missing_bars)
                    coverage_pct = round(
                        min(100.0, len(unique_ts) / expected_rows * 100.0),
                        3,
                    )

    numeric = work[list(REQUIRED_COLUMNS)].apply(
        pd.to_numeric,
        errors="coerce",
    )

    missing_numeric = numeric.isna().any(axis=1)

    impossible = (
        (numeric["high"] < numeric["low"])
        | (numeric["high"] < numeric["open"])
        | (numeric["high"] < numeric["close"])
        | (numeric["low"] > numeric["open"])
        | (numeric["low"] > numeric["close"])
        | (numeric["open"] <= 0)
        | (numeric["high"] <= 0)
        | (numeric["low"] <= 0)
        | (numeric["close"] <= 0)
        | (numeric["volume"] < 0)
    )

    bad_ohlc = int((missing_numeric | impossible).sum())

    valid = (
        len(work) > 0
        and not missing
        and bad_ohlc == 0
        and coverage_pct >= 95.0
    )

    return {
        "valid": bool(valid),
        "rows": int(len(work)),
        "duplicates": duplicates,
        "bad_ohlc": bad_ohlc,
        "gaps": gaps,
        "coverage_pct": coverage_pct,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "interval_ms": interval_ms,
    }


def prepare_ohlcv(
    df: pd.DataFrame,
    min_rows: int = 300,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Clean OHLCV candles and return:

        cleaned_dataframe, quality_report

    The function intentionally does NOT forward-fill OHLC prices because
    fabricating candles can contaminate backtests.
    """
    if df is None:
        raise ValueError("OHLCV dataframe is None.")

    work = _normalize_columns(df)

    missing = [c for c in REQUIRED_COLUMNS if c not in work.columns]

    if missing:
        raise ValueError(
            "Missing required OHLCV columns: " + ", ".join(missing)
        )

    timestamp_col = _detect_timestamp_column(work)

    if timestamp_col is not None:
        work["timestamp"] = _timestamp_to_ms(work[timestamp_col])

        if timestamp_col != "timestamp":
            work = work.drop(columns=[timestamp_col])

        work = work.dropna(subset=["timestamp"])
        work["timestamp"] = work["timestamp"].astype("int64")

        work = (
            work.sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"], keep="last")
            .reset_index(drop=True)
        )

    for column in REQUIRED_COLUMNS:
        work[column] = pd.to_numeric(work[column], errors="coerce")

    work = work.replace([np.inf, -np.inf], np.nan)
    work = work.dropna(subset=list(REQUIRED_COLUMNS))

    valid_prices = (
        (work["open"] > 0)
        & (work["high"] > 0)
        & (work["low"] > 0)
        & (work["close"] > 0)
        & (work["volume"] >= 0)
    )

    valid_structure = (
        (work["high"] >= work["low"])
        & (work["high"] >= work["open"])
        & (work["high"] >= work["close"])
        & (work["low"] <= work["open"])
        & (work["low"] <= work["close"])
    )

    work = work.loc[valid_prices & valid_structure].copy()
    work.reset_index(drop=True, inplace=True)

    if len(work) < min_rows:
        raise ValueError(
            f"Not enough valid candles: {len(work)}. "
            f"Need at least {min_rows}."
        )

    quality = validate_ohlcv(work)

    return work, quality


def chronological_split(
    df: pd.DataFrame,
    train_pct: float = 0.70,
    embargo_bars: int = 0,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Chronological train/test split.

    An optional embargo separates training data from test data to reduce
    leakage around the boundary.
    """
    if not 0.50 <= train_pct < 1.0:
        raise ValueError("train_pct must be between 0.50 and 1.0.")

    if embargo_bars < 0:
        raise ValueError("embargo_bars cannot be negative.")

    n = len(df)

    if n < 2:
        raise ValueError("Not enough rows to split.")

    split = int(n * train_pct)

    train_end = max(split - embargo_bars, 1)
    test_start = min(split + embargo_bars, n)

    train = df.iloc[:train_end].copy()
    test = df.iloc[test_start:].copy()

    if train.empty or test.empty:
        raise ValueError(
            "Train/test split produced an empty dataset. "
            "Reduce embargo_bars or provide more history."
        )

    return train, test
