"""
compare_utils.py
-----------------
Generic dataframe comparison helpers reused across Stage 2 (source->staging),
Stage 3 (staging->datamart) and beyond. Keeping the comparison logic here
means each stage script just calls these and focuses on *what* to compare,
not *how*.
"""

import pandas as pd


def find_duplicates(df: pd.DataFrame, key_col: str) -> pd.DataFrame:
    """Rows in df whose key_col value appears more than once."""
    counts = df[key_col].value_counts()
    dup_keys = counts[counts > 1].index
    return df[df[key_col].isin(dup_keys)].sort_values(key_col)


def find_missing_keys(source_df: pd.DataFrame, target_df: pd.DataFrame, key_col: str):
    """Keys present in source but absent from target (e.g. dropped during load)."""
    src_keys = set(source_df[key_col].dropna())
    tgt_keys = set(target_df[key_col].dropna())
    return sorted(src_keys - tgt_keys)


def find_extra_keys(source_df: pd.DataFrame, target_df: pd.DataFrame, key_col: str):
    """Keys present in target but not in source (shouldn't happen; phantom records)."""
    src_keys = set(source_df[key_col].dropna())
    tgt_keys = set(target_df[key_col].dropna())
    return sorted(tgt_keys - src_keys)


def _normalize(val):
    """Make values comparable across pandas/mysql-connector type quirks."""
    if pd.isna(val):
        return None
    if isinstance(val, float) and val.is_integer():
        return round(val, 2)
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    if hasattr(val, "isoformat"):  # date/datetime
        return str(val)[:10] if hasattr(val, "year") and not hasattr(val, "hour") else str(val)
    return str(val).strip()


def compare_common_rows(source_df: pd.DataFrame, target_df: pd.DataFrame,
                         key_col: str, compare_cols: list) -> pd.DataFrame:
    """
    For keys present in BOTH source and target, compare each column in
    compare_cols. Returns one row per (key, column) mismatch found.
    """
    merged = source_df.merge(target_df, on=key_col, suffixes=("_src", "_tgt"), how="inner")
    mismatches = []
    for _, row in merged.iterrows():
        for col in compare_cols:
            src_val = _normalize(row.get(f"{col}_src", row.get(col)))
            tgt_val = _normalize(row.get(f"{col}_tgt", row.get(col)))
            if src_val != tgt_val:
                mismatches.append({
                    key_col: row[key_col],
                    "column": col,
                    "source_value": src_val,
                    "target_value": tgt_val,
                })
    result = pd.DataFrame(mismatches)
    if not result.empty:
        result = result.drop_duplicates().reset_index(drop=True)
    return result


def null_check(df: pd.DataFrame, mandatory_cols: list) -> pd.DataFrame:
    """Rows that have a null in any mandatory column."""
    mask = df[mandatory_cols].isna().any(axis=1)
    return df[mask]


def compare_aggregates(expected_df: pd.DataFrame, actual_df: pd.DataFrame,
                        key_cols: list, value_cols: list, tolerance: float = 0.01):
    """
    Compare two aggregate/summary tables grouped by key_cols.
    Returns (missing_groups_df, extra_groups_df, value_mismatches_df).

    missing_groups: combinations present in expected (recomputed) but absent from actual
                     (e.g. stored summary table wasn't refreshed for a new date/dimension)
    extra_groups:   combinations present in actual but not in expected (phantom aggregate rows)
    value_mismatches: for groups present in both, any value_col differing beyond tolerance
    """
    exp = expected_df.copy()
    act = actual_df.copy()
    for c in key_cols:
        exp[c] = exp[c].astype(str)
        act[c] = act[c].astype(str)

    exp_keys = exp[key_cols].drop_duplicates()
    act_keys = act[key_cols].drop_duplicates()

    merged_keys = exp_keys.merge(act_keys, on=key_cols, how="outer", indicator=True)
    missing_groups = merged_keys[merged_keys["_merge"] == "left_only"][key_cols]
    extra_groups = merged_keys[merged_keys["_merge"] == "right_only"][key_cols]

    merged = exp.merge(act, on=key_cols, suffixes=("_expected", "_actual"), how="inner")
    mismatches = []
    for _, row in merged.iterrows():
        for col in value_cols:
            e_val = row.get(f"{col}_expected", row.get(col))
            a_val = row.get(f"{col}_actual", row.get(col))
            e_val = 0.0 if pd.isna(e_val) else float(e_val)
            a_val = 0.0 if pd.isna(a_val) else float(a_val)
            if abs(e_val - a_val) > tolerance:
                mismatches.append({
                    **{k: row[k] for k in key_cols},
                    "column": col, "expected_value": e_val, "actual_value": a_val,
                    "difference": round(a_val - e_val, 2)
                })

    return missing_groups.reset_index(drop=True), extra_groups.reset_index(drop=True), pd.DataFrame(mismatches)
