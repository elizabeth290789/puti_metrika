"""Streamlit UI for User Path Report."""

from __future__ import annotations

import time
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from metrika_client import MetrikaAPIError, MetrikaLogsClient, get_metrika_token
from path_builder import (
    aggregate_next_steps,
    aggregate_top_paths,
    build_paths,
    build_watchlist,
    mark_target_reached,
    parse_goal_ids,
)

HITS_VISIT_ID_LIMIT_OPTIONS = [100, 500, 1000, 3000, 5000]
HITS_TIMEOUT_SECONDS = 180


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


def _representative_visit_ids(visits: pd.DataFrame, limit: int, visit_id_col: str) -> list:
    """Pick a bounded, mixed set of visit IDs for the interactive report."""
    if visits.empty or limit <= 0:
        return []

    work = visits.dropna(subset=[visit_id_col]).drop_duplicates(subset=[visit_id_col]).copy()
    if len(work) <= limit:
        return work[visit_id_col].tolist()

    pageviews_col = "pageViews" if "pageViews" in work.columns else "ym:s:pageViews"
    duration_col = "visitDuration" if "visitDuration" in work.columns else "ym:s:visitDuration"
    bounce_col = "bounce" if "bounce" in work.columns else "ym:s:bounce"
    work["pageViews_numeric"] = pd.to_numeric(work[pageviews_col], errors="coerce").fillna(0) if pageviews_col in work.columns else 0
    work["visitDuration_numeric"] = pd.to_numeric(work[duration_col], errors="coerce").fillna(0) if duration_col in work.columns else 0
    work["bounce_numeric"] = pd.to_numeric(work[bounce_col], errors="coerce").fillna(0) if bounce_col in work.columns else 0

    buckets = [
        work[work.get("target_reached", False) == True],  # noqa: E712 - pandas boolean mask
        work[work.get("target_reached", False) == False],  # noqa: E712 - pandas boolean mask
        work.sort_values("visitDuration_numeric", ascending=False),
        work[(work["bounce_numeric"] == 1) | (work["pageViews_numeric"] <= 1)].sort_values("visitDuration_numeric"),
        work[work["pageViews_numeric"] >= 2].sort_values("pageViews_numeric", ascending=False),
    ]
    per_bucket = max(1, limit // len(buckets))
    selected: list = []
    seen = set()

    def add_ids(values) -> None:
        for value in values:
            key = str(value)
            if key not in seen:
                selected.append(value)
                seen.add(key)
                if len(selected) >= limit:
                    return

    for bucket in buckets:
        add_ids(bucket[visit_id_col].head(per_bucket).tolist())
        if len(selected) >= limit:
            return selected[:limit]

    add_ids(work[visit_id_col].tolist())
    return selected[:limit]


def _show_visits_summary(visits: pd.DataFrame, selected_url: str, date_from, date_to) -> None:
    st.subheader("2. Сводка по visits")
    total_visits = len(visits)
    target_visits = int(visits["target_reached"].sum()) if "target_reached" in visits.columns else 0
    cr = target_visits / total_visits if total_visits else 0
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Найдено visits", total_visits)
    c2.metric("Visits с целью", target_visits)
    c3.metric("CR", f"{cr:.2%}")
    c4.metric("Выбранный URL", selected_url)
    c5.metric("Период", f"{date_from} — {date_to}")


def _reset_hits_state() -> None:
    for key in ["hits", "paths", "sample_visit_ids", "sample_size"]:
        st.session_state.pop(key, None)


st.set_page_config(page_title="User Path Report", layout="wide")
st.title("User Path Report из Яндекс Метрики")
st.caption("Интерактивный отчет по путям пользователей на основе Logs API. Для больших счетчиков hits загружаются по ограниченной выборке visitID.")

if "visits" not in st.session_state:
    st.session_state.visits = pd.DataFrame()

yesterday = date.today() - timedelta(days=1)
with st.sidebar:
    st.header("Параметры отчета")
    counter_id = st.text_input("counter_id", value="18477952")
    date_from = st.date_input("date_from", value=yesterday, max_value=yesterday)
    date_to = st.date_input("date_to", value=yesterday, max_value=yesterday)
    selected_url = st.text_input("selected_url", value="/promo/b24messenger/team/")
    goal_ids_raw = st.text_input("ID цели", value="2898778")
    max_steps = st.number_input("Максимум шагов пути", min_value=1, max_value=10, value=3, step=1)
    hits_visit_id_limit = st.selectbox("Максимум visitID для загрузки hits", HITS_VISIT_ID_LIMIT_OPTIONS, index=2)
    report_mode = st.radio("Режим отчета", ["Быстрый интерактивный отчет", "Полный отчет"], index=0)
    load_report = st.button("Загрузить отчет", type="primary")

st.subheader("1. Статус подключения")
token = get_metrika_token()
if token:
    st.success("Токен Яндекс Метрики найден.")
else:
    st.warning("YANDEX_METRIKA_TOKEN не задан. Добавьте токен в Streamlit Secrets.")

if load_report:
    _reset_hits_state()
    if not token:
        st.error("Запрос не выполнен: задайте YANDEX_METRIKA_TOKEN в Streamlit Secrets или переменной окружения.")
        st.stop()
    if not selected_url.strip():
        st.error("URL-фильтр обязателен. Нельзя загружать весь счетчик без URL-фильтра.")
        st.stop()
    if date_from > date_to:
        st.error("date_from не может быть позже date_to.")
        st.stop()

    selected_goal_ids = parse_goal_ids(goal_ids_raw)
    try:
        client = MetrikaLogsClient(token=token)
        with st.spinner("Загружаем visits по URL-фильтру..."):
            visits = client.fetch_visits(counter_id, date_from, date_to, selected_url)
        if visits.empty:
            st.info("По выбранным параметрам данных нет. Проверьте дату, URL-фильтр и ID цели.")
            st.session_state.visits = pd.DataFrame()
            st.stop()

        visit_id_col = _visit_id_column(visits)
        if visit_id_col is None:
            st.error("В данных visits нет колонки visitID. Невозможно загрузить hits для путей.")
            st.session_state.visits = pd.DataFrame()
            st.stop()

        visits = mark_target_reached(visits, selected_goal_ids)
        st.session_state.visits = visits
        st.session_state.report_params = {
            "counter_id": counter_id,
            "date_from": date_from,
            "date_to": date_to,
            "selected_url": selected_url,
            "goal_ids_raw": goal_ids_raw,
            "max_steps": int(max_steps),
            "hits_visit_id_limit": int(hits_visit_id_limit),
            "report_mode": report_mode,
        }
    except MetrikaAPIError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.exception(exc)

visits = st.session_state.get("visits", pd.DataFrame())
params = st.session_state.get("report_params", {})
if not visits.empty:
    active_selected_url = params.get("selected_url", selected_url)
    active_date_from = params.get("date_from", date_from)
    active_date_to = params.get("date_to", date_to)
    active_limit = int(params.get("hits_visit_id_limit", hits_visit_id_limit))
    active_mode = params.get("report_mode", report_mode)
    active_max_steps = int(params.get("max_steps", max_steps))
    active_counter_id = params.get("counter_id", counter_id)

    _show_visits_summary(visits, active_selected_url, active_date_from, active_date_to)
    visit_id_col = _visit_id_column(visits)
    visit_ids = visits[visit_id_col].dropna().unique().tolist() if visit_id_col else []
    total_found = len(visit_ids)

    if active_mode == "Полный отчет":
        st.info("Полный несемплированный отчет для большого счетчика нужно считать отдельным батч-процессом, не в синхронном Streamlit-запросе.")
    else:
        selected_visit_ids = _representative_visit_ids(visits, active_limit, visit_id_col) if visit_id_col else []
        sample_size = len(selected_visit_ids)
        if total_found > active_limit:
            st.warning(
                f"Найдено {total_found} визитов. Для интерактивного отчета по путям будут загружены hits только для первых/выбранных {sample_size} visitID. "
                "Для полного несемплированного отчета нужен отдельный offline-режим/батч-выгрузка."
            )
            st.info("Быстрый интерактивный отчет будет построен по ограниченной репрезентативной выборке visitID, а не по всем найденным визитам.")
        else:
            st.info(f"Найдено {total_found} visitID — это не больше лимита {active_limit}, hits можно загрузить для всех найденных visitID.")

        load_hits = st.button("Загрузить hits и построить пути", type="primary")
        if load_hits:
            if not token:
                st.error("Запрос не выполнен: задайте YANDEX_METRIKA_TOKEN в Streamlit Secrets или переменной окружения.")
                st.stop()
            if not selected_visit_ids:
                st.error("Нет visitID для загрузки hits.")
                st.stop()
            try:
                client = MetrikaLogsClient(token=token, timeout_seconds=HITS_TIMEOUT_SECONDS)
                started_at = time.monotonic()
                with st.spinner(f"Загружаем hits для {sample_size} visitID из {total_found} найденных..."):
                    hits = client.fetch_hits_for_visit_ids(active_counter_id, active_date_from, active_date_to, selected_visit_ids, max_elapsed_seconds=HITS_TIMEOUT_SECONDS)
                elapsed = time.monotonic() - started_at
                if elapsed > HITS_TIMEOUT_SECONDS:
                    st.warning("Загрузка hits заняла больше 3 минут. Для больших объемов используйте отдельный offline-режим/батч-выгрузку, а не синхронный Streamlit-запрос.")
                if hits.empty:
                    st.error("Не удалось загрузить hits по выбранным visitID. Нельзя построить путь.")
                    st.stop()

                sampled_visits = visits[visits[visit_id_col].astype(str).isin({str(v) for v in selected_visit_ids})]
                paths = build_paths(sampled_visits, hits, active_selected_url, active_max_steps)
                if paths.empty:
                    st.info("Выбранный URL не найден внутри цепочек hits. Проверьте дату, URL-фильтр и ID цели.")
                    st.stop()
                st.session_state.hits = hits
                st.session_state.paths = paths
                st.session_state.sample_visit_ids = selected_visit_ids
                st.session_state.sample_size = sample_size
            except MetrikaAPIError as exc:
                if "Превышено время ожидания" in str(exc) or "дольше 3 минут" in str(exc):
                    st.error("Загрузка hits идет дольше 3 минут. Остановили синхронный запрос: для большого счетчика нужен offline-режим/батч-выгрузка.")
                else:
                    st.error(str(exc))
            except Exception as exc:
                st.exception(exc)

    paths = st.session_state.get("paths", pd.DataFrame())
    hits = st.session_state.get("hits", pd.DataFrame())
    if not paths.empty:
        next_steps = aggregate_next_steps(paths)
        top_paths = aggregate_top_paths(paths)
        watchlist = build_watchlist(paths)

        st.subheader("3. Сводка по быстрым путям")
        total_paths = len(paths)
        target_paths = int(paths["target_reached"].sum()) if "target_reached" in paths.columns else 0
        cr_paths = target_paths / total_paths if total_paths else 0
        avg_duration = pd.to_numeric(paths.get("visitDuration"), errors="coerce").mean()
        avg_pageviews = pd.to_numeric(paths.get("pageViews"), errors="coerce").mean()
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Визиты в выборке", total_paths)
        c2.metric("Визиты с целью", target_paths)
        c3.metric("CR выборки", f"{cr_paths:.2%}")
        c4.metric("Средняя длительность", f"{avg_duration:.1f} сек" if pd.notna(avg_duration) else "—")
        c5.metric("Средняя глубина", f"{avg_pageviews:.1f}" if pd.notna(avg_pageviews) else "—")
        c6.metric("Уникальные пути", paths["path"].nunique())
        st.caption("Отчет по путям построен по ограниченной выборке visitID и не является полным несемплированным отчетом по всем найденным visits.")

        st.subheader("4. Следующий шаг после выбранной страницы")
        st.dataframe(_format_percent_columns(next_steps), use_container_width=True, hide_index=True)

        st.subheader("5. Топ путей")
        st.dataframe(_format_percent_columns(top_paths), use_container_width=True, hide_index=True)

        st.subheader("6. VisitID для проверки")
        visible_watchlist = watchlist.drop(columns=[c for c in ["visitDuration", "pageViews"] if c in watchlist.columns])
        st.dataframe(visible_watchlist, use_container_width=True, hide_index=True)

        st.subheader("7. Экспорт")
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
elif not load_report:
    st.info("Задайте параметры в сайдбаре и нажмите «Загрузить отчет». Сначала загрузятся только visits; hits загружаются отдельной кнопкой после сводки.")
