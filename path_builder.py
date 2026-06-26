"""Utilities for building user path reports from Yandex Metrika Logs API data."""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urlparse

import pandas as pd

EMPTY_URL = "—"
EXIT_URL = "Выход"

METRIKA_COLUMN_MAPPING = {
    "ym:s:visitID": "visitID",
    "ym:s:clientID": "clientID",
    "ym:s:dateTime": "dateTime",
    "ym:s:startURL": "startURL",
    "ym:s:endURL": "endURL",
    "ym:s:pageViews": "pageViews",
    "ym:s:visitDuration": "visitDuration",
    "ym:s:bounce": "bounce",
    "ym:s:goalsID": "goalsID",
    "ym:s:lastTrafficSource": "lastTrafficSource",
    "ym:s:UTMSource": "UTMSource",
    "ym:s:UTMCampaign": "UTMCampaign",
    "ym:s:deviceCategory": "deviceCategory",
    "ym:pv:visitID": "visitID",
    "ym:pv:dateTime": "dateTime",
    "ym:pv:URL": "URL",
    "ym:pv:title": "title",
    "ym:pv:referer": "referer",
    "ym:pv:goalsID": "goalsID",
}


def normalize_url(url) -> str:
    """Normalize URL to path without query parameters."""
    if url is None or (isinstance(url, float) and pd.isna(url)):
        return EMPTY_URL
    text = str(url).strip()
    if not text:
        return EMPTY_URL
    parsed = urlparse(text if "://" in text else text)
    path = parsed.path or (text.split("?", 1)[0] if not parsed.path else parsed.path)
    if not path or path == "/":
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return path


def parse_goal_ids(value) -> set[str]:
    """Extract numeric goal IDs from common Logs API goalsID formats."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return set()
    ids = set()
    for match in re.findall(r"\d+(?:\.0)?", str(value)):
        ids.add(match[:-2] if match.endswith(".0") else match)
    return ids


def normalize_metrika_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename known Yandex Metrika Logs API columns to short names."""
    if df.empty:
        return df.copy()
    return df.rename(columns={column: METRIKA_COLUMN_MAPPING[column] for column in df.columns if column in METRIKA_COLUMN_MAPPING})


def _coerce_goal_set(selected_goal_ids: Iterable) -> set[str]:
    result: set[str] = set()
    for value in selected_goal_ids or []:
        result.update(parse_goal_ids(value))
    return result


def mark_target_reached(visits_df: pd.DataFrame, selected_goal_ids: Iterable) -> pd.DataFrame:
    """Add target_reached flag based on selected goal IDs."""
    result = visits_df.copy()
    target_ids = _coerce_goal_set(selected_goal_ids)
    if "goalsID" not in result.columns:
        result["target_reached"] = False
        return result
    result["target_reached"] = result["goalsID"].apply(lambda value: bool(parse_goal_ids(value) & target_ids))
    return result


def _rename_metrika_columns(df: pd.DataFrame) -> pd.DataFrame:
    return normalize_metrika_columns(df)


def _dedupe_consecutive(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if not deduped or deduped[-1] != value:
            deduped.append(value)
    return deduped


def build_paths(visits_df: pd.DataFrame, hits_df: pd.DataFrame, selected_url: str, max_steps: int) -> pd.DataFrame:
    """Build paths that start strictly from selected_url inside each visit."""
    if visits_df.empty or hits_df.empty:
        return pd.DataFrame()

    visits = _rename_metrika_columns(visits_df.copy())
    hits = _rename_metrika_columns(hits_df.copy())
    required_hits = {"visitID", "dateTime", "URL"}
    if not required_hits.issubset(hits.columns) or "visitID" not in visits.columns:
        return pd.DataFrame()

    selected = normalize_url(selected_url)
    max_steps = max(1, int(max_steps or 1))
    hits["dateTime_sort"] = pd.to_datetime(hits["dateTime"], errors="coerce")
    hits["normalized_url"] = hits["URL"].apply(normalize_url)
    visits = visits.drop_duplicates(subset=["visitID"]).copy()

    rows = []
    for visit_id, group in hits.sort_values(["visitID", "dateTime_sort"]).groupby("visitID", sort=False):
        chain = _dedupe_consecutive(group["normalized_url"].tolist())
        if selected not in chain:
            continue
        start_idx = chain.index(selected)
        steps = chain[start_idx : start_idx + max_steps]
        next_url = steps[1] if len(steps) > 1 else EXIT_URL
        rows.append({"visitID": visit_id, "path_steps": steps, "path": " → ".join(steps), "next_url": next_url})

    if not rows:
        return pd.DataFrame()

    paths = pd.DataFrame(rows)
    merged = paths.merge(visits, on="visitID", how="left")
    ordered = [
        "visitID", "dateTime", "deviceCategory", "UTMSource", "UTMCampaign", "startURL", "endURL",
        "visitDuration", "pageViews", "goalsID", "target_reached", "path", "path_steps", "next_url",
    ]
    for col in ordered:
        if col not in merged.columns:
            merged[col] = pd.NA if col != "target_reached" else False
    return merged[ordered]


def aggregate_next_steps(paths_df: pd.DataFrame) -> pd.DataFrame:
    if paths_df.empty or "next_url" not in paths_df.columns:
        return pd.DataFrame(columns=["next_url", "visits", "share", "target_visits", "CR"])
    total = len(paths_df)
    grouped = paths_df.groupby("next_url", dropna=False).agg(visits=("visitID", "nunique"), target_visits=("target_reached", "sum")).reset_index()
    grouped["share"] = grouped["visits"] / total
    grouped["CR"] = grouped["target_visits"] / grouped["visits"]
    return grouped.sort_values(["visits", "target_visits"], ascending=False)


def aggregate_top_paths(paths_df: pd.DataFrame) -> pd.DataFrame:
    cols = ["path", "visits", "share", "target_visits", "CR", "avg_duration", "avg_pageviews"]
    if paths_df.empty or "path" not in paths_df.columns:
        return pd.DataFrame(columns=cols)
    df = paths_df.copy()
    df["visitDuration"] = pd.to_numeric(df.get("visitDuration"), errors="coerce")
    df["pageViews"] = pd.to_numeric(df.get("pageViews"), errors="coerce")
    total = len(df)
    grouped = df.groupby("path", dropna=False).agg(
        visits=("visitID", "nunique"), target_visits=("target_reached", "sum"),
        avg_duration=("visitDuration", "mean"), avg_pageviews=("pageViews", "mean"),
    ).reset_index()
    grouped["share"] = grouped["visits"] / total
    grouped["CR"] = grouped["target_visits"] / grouped["visits"]
    return grouped[cols].sort_values(["visits", "target_visits"], ascending=False)


def build_watchlist(paths_df: pd.DataFrame) -> pd.DataFrame:
    cols = ["reason_group", "visitID", "dateTime", "deviceCategory", "UTMSource", "UTMCampaign", "path", "target_reached", "visitDuration", "pageViews", "reason_to_watch"]
    if paths_df.empty:
        return pd.DataFrame(columns=cols)
    buckets = [
        ("exit", paths_df[paths_df.get("next_url").eq(EXIT_URL)], "Путь заканчивается выходом после выбранной страницы."),
        ("prices", paths_df[paths_df.get("path", pd.Series(dtype=str)).str.contains("prices", case=False, na=False)], "В пути есть страница цен."),
        ("register/create/auth", paths_df[paths_df.get("path", pd.Series(dtype=str)).str.contains("register|create|auth", case=False, na=False)], "В пути есть регистрация, создание или авторизация."),
        ("target", paths_df[paths_df.get("target_reached", False).eq(True)], "Визит достиг выбранной цели."),
    ]
    frames = []
    for group_name, data, reason in buckets:
        if not data.empty:
            sample = data.head(5).copy()
            sample["reason_group"] = group_name
            sample["reason_to_watch"] = reason
            frames.append(sample)
    if not frames:
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True).drop_duplicates("visitID").head(20)[cols]
