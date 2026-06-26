"""Streamlit UI for User Path Report."""

from __future__ import annotations

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


def _format_percent_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for column in ["share", "CR"]:
        if column in result.columns:
            result[column] = result[column].map(lambda value: f"{value:.2%}" if pd.notna(value) else "—")
    return result


def _csv_button(label: str, df: pd.DataFrame, file_name: str) -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8-sig"), file_name=file_name, mime="text/csv", disabled=df.empty)


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
    load_report = st.button("Загрузить отчет", type="primary")

st.subheader("1. Статус подключения")
token = get_metrika_token()
if token:
    st.success("Токен Яндекс Метрики найден.")
else:
    st.warning("YANDEX_METRIKA_TOKEN не задан. Добавьте токен в Streamlit Secrets.")

if load_report:
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
    visits = pd.DataFrame()
    hits = pd.DataFrame()
    paths = pd.DataFrame()
    try:
        client = MetrikaLogsClient(token=token)
        with st.spinner("Загружаем visits по URL-фильтру..."):
            visits = client.fetch_visits(counter_id, date_from, date_to, selected_url)
        if visits.empty:
            st.info("По выбранным параметрам данных нет. Проверьте дату, URL-фильтр и ID цели.")
            st.stop()

        visit_id_col = "ym:s:visitID" if "ym:s:visitID" in visits.columns else "visitID"
        if visit_id_col not in visits.columns:
            st.error("В данных visits нет колонки visitID. Невозможно загрузить hits для путей.")
            st.stop()

        visits = mark_target_reached(visits, selected_goal_ids)
        visit_ids = visits[visit_id_col].dropna().unique().tolist()
        with st.spinner("Загружаем hits для найденных visitID..."):
            hits = client.fetch_hits_for_visit_ids(counter_id, date_from, date_to, visit_ids)
        if hits.empty:
            st.error("Не удалось загрузить hits по найденным visitID. Нельзя построить полный путь.")
            st.stop()

        paths = build_paths(visits, hits, selected_url, int(max_steps))
        if paths.empty:
            st.info("Выбранный URL не найден внутри цепочек hits. Проверьте дату, URL-фильтр и ID цели.")
            st.stop()

        next_steps = aggregate_next_steps(paths)
        top_paths = aggregate_top_paths(paths)
        watchlist = build_watchlist(paths)

        st.subheader("2. Сводка")
        total_visits = len(paths)
        target_visits = int(paths["target_reached"].sum()) if "target_reached" in paths.columns else 0
        cr = target_visits / total_visits if total_visits else 0
        avg_duration = pd.to_numeric(paths.get("visitDuration"), errors="coerce").mean()
        avg_pageviews = pd.to_numeric(paths.get("pageViews"), errors="coerce").mean()
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("Визиты с URL", total_visits)
        c2.metric("Визиты с целью", target_visits)
        c3.metric("CR", f"{cr:.2%}")
        c4.metric("Средняя длительность", f"{avg_duration:.1f} сек" if pd.notna(avg_duration) else "—")
        c5.metric("Средняя глубина", f"{avg_pageviews:.1f}" if pd.notna(avg_pageviews) else "—")
        c6.metric("Уникальные пути", paths["path"].nunique())

        st.subheader("3. Следующий шаг после выбранной страницы")
        st.dataframe(_format_percent_columns(next_steps), use_container_width=True, hide_index=True)

        st.subheader("4. Топ путей")
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
            st.write("Выбранные goal_ids", sorted(selected_goal_ids))
    except MetrikaAPIError as exc:
        st.error(str(exc))
    except Exception as exc:
        st.exception(exc)
else:
    st.info("Задайте параметры в сайдбаре и нажмите «Загрузить отчет». Запросы к Метрике до нажатия кнопки не выполняются.")
