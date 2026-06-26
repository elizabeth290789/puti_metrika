"""Streamlit UI for User Path Report."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import requests
import streamlit as st

from metrika_client import LOGS_API_CONNECTION_ERROR, MetrikaAPIError, MetrikaLogsClient, get_metrika_token
from path_builder import (
    aggregate_next_steps,
    aggregate_top_paths,
    build_paths,
    build_watchlist,
    mark_target_reached,
    parse_goal_ids,
)

HITS_TIMEOUT_SECONDS = 180
PATH_REPORT_LIMITS = [100, 500, 1000, 3000]


def _format_percent_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for column in ["share", "CR"]:
        if column in result.columns:
            result[column] = result[column].map(lambda value: f"{value:.2%}" if pd.notna(value) else "—")
    return result


def _csv_button(label: str, df: pd.DataFrame, file_name: str) -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8-sig"), file_name=file_name, mime="text/csv", disabled=df.empty)


def _visit_id_column(visits: pd.DataFrame) -> str | None:
    if "ym:s:visitID" in visits.columns:
        return "ym:s:visitID"
    if "visitID" in visits.columns:
        return "visitID"
    return None


def _numeric_series(df: pd.DataFrame, short_name: str) -> pd.Series:
    column = short_name if short_name in df.columns else f"ym:s:{short_name}"
    if column not in df.columns:
        return pd.Series([pd.NA] * len(df), index=df.index)
    return pd.to_numeric(df[column], errors="coerce")


def _sample_visit_ids_for_quick_report(visits: pd.DataFrame, limit: int) -> list:
    visit_id_col = _visit_id_column(visits)
    if visit_id_col is None or visits.empty:
        return []

    data = visits.copy()
    data["_pageViews"] = _numeric_series(data, "pageViews")
    data["_visitDuration"] = _numeric_series(data, "visitDuration")
    target_reached = data["target_reached"] if "target_reached" in data.columns else pd.Series(False, index=data.index)
    buckets = [
        data[target_reached.eq(True)],
        data[target_reached.eq(False)],
        data[data["_pageViews"] >= 2],
        data.sort_values("_visitDuration", ascending=False),
        data.sort_values("_visitDuration", ascending=True),
    ]

    selected = []
    seen = set()
    per_bucket = max(1, limit // len(buckets))
    for bucket in buckets:
        for visit_id in bucket[visit_id_col].dropna().head(per_bucket).tolist():
            if visit_id not in seen:
                selected.append(visit_id)
                seen.add(visit_id)
            if len(selected) >= limit:
                return selected

    for visit_id in data[visit_id_col].dropna().tolist():
        if visit_id not in seen:
            selected.append(visit_id)
            seen.add(visit_id)
        if len(selected) >= limit:
            break
    return selected


def _show_visits_summary(visits: pd.DataFrame, date_from, date_to, selected_url: str) -> None:
    st.subheader("2. Сводка по visits")
    total_visits = len(visits)
    target_visits = int(visits["target_reached"].sum()) if "target_reached" in visits.columns else 0
    cr = target_visits / total_visits if total_visits else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Visits найдено", total_visits)
    c2.metric("Visits с целью", target_visits)
    c3.metric("CR", f"{cr:.2%}")
    c4.metric("Период", f"{date_from} — {date_to}")
    c5.metric("URL-фильтр", selected_url or "—")


def _show_full_report_stub() -> None:
    st.subheader("Полный несемплированный отчет")
    st.info("Полный отчет без ограничения visitID нужно считать отдельным батч-процессом, не в интерактивном Streamlit-запросе.")


st.set_page_config(page_title="User Path Report", layout="wide")
st.title("User Path Report из Яндекс Метрики")
st.caption("Несемплированный отчет по путям пользователей на основе Logs API.")

yesterday = date.today() - timedelta(days=1)
with st.sidebar:
    st.header("Параметры отчета")
    counter_id = st.text_input("counter_id", value="18477952")
    date_from = st.date_input("date_from", value=yesterday, max_value=yesterday)
    date_to = st.date_input("date_to", value=yesterday, max_value=yesterday)
    selected_url = st.text_input("selected_url", value="/promo/b24messenger/team/")
    goal_ids_raw = st.text_input("ID цели", value="2898778")
    max_steps = st.number_input("Максимум шагов пути", min_value=1, max_value=10, value=3, step=1)
    path_report_limit = st.selectbox("Максимум visitID для path report", PATH_REPORT_LIMITS, index=1)
    load_report = st.button("Загрузить отчет", type="primary")

st.subheader("1. Статус подключения")
token = get_metrika_token()
if token:
    st.success("Токен Яндекс Метрики найден.")
else:
    st.warning("YANDEX_METRIKA_TOKEN не задан. Добавьте токен в Streamlit Secrets.")

if load_report:
    st.session_state.pop("path_report_error", None)
    st.session_state.pop("path_report", None)
    if not token:
        st.error("Запрос не выполнен: задайте YANDEX_METRIKA_TOKEN в Streamlit Secrets или переменной окружения.")
        st.stop()
    if not selected_url.strip():
        st.error("URL-фильтр обязателен. Нельзя загружать весь счетчик без URL-фильтра.")
        st.stop()
    if date_from > date_to:
        st.error("date_from не может быть позже date_to.")
        st.stop()

    try:
        client = MetrikaLogsClient(token=token)
        with st.spinner("Загружаем только visits по URL-фильтру..."):
            visits = client.fetch_visits(counter_id, date_from, date_to, selected_url)
        if visits.empty:
            st.info("По выбранным параметрам данных нет. Проверьте дату, URL-фильтр и ID цели.")
            st.stop()
        visits = mark_target_reached(visits, parse_goal_ids(goal_ids_raw))
        st.session_state["visits"] = visits
        st.session_state["report_params"] = {
            "counter_id": counter_id,
            "date_from": date_from,
            "date_to": date_to,
            "selected_url": selected_url,
            "goal_ids_raw": goal_ids_raw,
            "max_steps": int(max_steps),
        }
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
        st.error(LOGS_API_CONNECTION_ERROR)
    except MetrikaAPIError as exc:
        st.error(str(exc))

visits = st.session_state.get("visits")
params = st.session_state.get("report_params", {})
if visits is not None and not visits.empty:
    _show_visits_summary(visits, params.get("date_from", date_from), params.get("date_to", date_to), params.get("selected_url", selected_url))
    total_visits = len(visits)
    if total_visits > int(path_report_limit):
        st.warning(
            f"Найдено {total_visits} визитов. Быстрый интерактивный отчет построит пути только по {int(path_report_limit)} visitID. "
            "Это не полный несемплированный отчет. Полный отчет нужно считать в отдельном offline/batch-режиме."
        )
    st.caption("Hits не загружаются автоматически. Нажмите отдельную кнопку ниже, чтобы построить быстрый отчет по ограниченному числу visitID.")

    if st.button("Построить быстрый отчет по путям"):
        st.session_state.pop("path_report_error", None)
        st.session_state.pop("path_report", None)
        try:
            visit_id_col = _visit_id_column(visits)
            if visit_id_col is None:
                st.error("В данных visits нет колонки visitID. Невозможно загрузить hits для путей.")
                st.stop()
            visit_ids = _sample_visit_ids_for_quick_report(visits, int(path_report_limit))
            st.info(f"Для быстрого отчета выбрано {len(visit_ids)} visitID смешанной выборкой: с целью, без цели, с pageViews >= 2, длинные и короткие визиты.")
            client = MetrikaLogsClient(token=token, timeout_seconds=HITS_TIMEOUT_SECONDS)
            with st.spinner("Загружаем hits только для выбранного лимита visitID..."):
                hits = client.fetch_hits_for_visit_ids(
                    params.get("counter_id", counter_id),
                    params.get("date_from", date_from),
                    params.get("date_to", date_to),
                    visit_ids,
                    max_elapsed_seconds=HITS_TIMEOUT_SECONDS,
                )
            if hits.empty:
                st.session_state["path_report_error"] = "Не удалось загрузить hits по выбранным visitID. Сводка по visits остается доступной."
            else:
                paths = build_paths(visits, hits, params.get("selected_url", selected_url), int(params.get("max_steps", max_steps)))
                if paths.empty:
                    st.session_state["path_report_error"] = "Выбранный URL не найден внутри цепочек hits. Сводка по visits остается доступной."
                else:
                    st.session_state["path_report"] = {"hits": hits, "paths": paths, "visit_ids": visit_ids}
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
            st.session_state["path_report_error"] = LOGS_API_CONNECTION_ERROR
        except MetrikaAPIError as exc:
            st.session_state["path_report_error"] = str(exc)

    if st.session_state.get("path_report_error"):
        st.error(st.session_state["path_report_error"])

    report = st.session_state.get("path_report")
    if report and not report["paths"].empty:
        paths = report["paths"]
        hits = report["hits"]
        next_steps = aggregate_next_steps(paths)
        top_paths = aggregate_top_paths(paths)
        watchlist = build_watchlist(paths)

        st.subheader("3. Быстрый отчет: следующий шаг после выбранной страницы")
        st.dataframe(_format_percent_columns(next_steps), use_container_width=True, hide_index=True)

        st.subheader("4. Быстрый отчет: топ путей")
        st.dataframe(_format_percent_columns(top_paths), use_container_width=True, hide_index=True)

        st.subheader("5. VisitID для проверки")
        visible_watchlist = watchlist.drop(columns=[c for c in ["visitDuration", "pageViews"] if c in watchlist.columns])
        st.dataframe(visible_watchlist, use_container_width=True, hide_index=True)

        st.subheader("6. Экспорт")
        e1, e2, e3 = st.columns(3)
        with e1:
            _csv_button("CSV: топ путей", top_paths, "top_paths.csv")
        with e2:
            _csv_button("CSV: следующие шаги", next_steps, "next_steps.csv")
        with e3:
            _csv_button("CSV: visitID для проверки", watchlist, "watchlist.csv")

        with st.expander("Отладка", expanded=False):
            st.write("Первые 50 строк visits")
            st.dataframe(visits.head(50), use_container_width=True)
            st.write("Первые 50 строк hits")
            st.dataframe(hits.head(50), use_container_width=True)
            st.write("Первые 50 строк paths")
            st.dataframe(paths.head(50), use_container_width=True)
            st.write("Выбранные goal_ids", sorted(parse_goal_ids(params.get("goal_ids_raw", goal_ids_raw))))
else:
    st.info("Задайте параметры в сайдбаре и нажмите «Загрузить отчет». Запросы к Метрике до нажатия кнопки не выполняются.")

_show_full_report_stub()
