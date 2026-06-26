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
    normalize_metrika_columns,
    parse_goal_ids,
)

VISIT_ID_LIMIT_OPTIONS = [100, 500, 1000]
HITS_TIMEOUT_SECONDS = 180


def _format_percent_columns(df: pd.DataFrame) -> pd.DataFrame:
    result = df.copy()
    for column in ["share", "CR"]:
        if column in result.columns:
            result[column] = result[column].map(lambda value: f"{value:.2%}" if pd.notna(value) else "—")
    return result


def _csv_button(label: str, df: pd.DataFrame, file_name: str) -> None:
    st.download_button(label, df.to_csv(index=False).encode("utf-8-sig"), file_name=file_name, mime="text/csv", disabled=df.empty)


def _first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for column in candidates:
        if column in df.columns:
            return column
    return None


def _top_urls(visits: pd.DataFrame, candidates: list[str], limit: int) -> pd.DataFrame:
    column = _first_existing_column(visits, candidates)
    if column is None or visits.empty:
        return pd.DataFrame(columns=["URL", "visits"])
    top = (
        visits[column]
        .fillna("—")
        .astype(str)
        .value_counts(dropna=False)
        .head(limit)
        .reset_index()
    )
    top.columns = ["URL", "visits"]
    return top


def _url_filter_found(top_frames: list[pd.DataFrame], url_filter: str) -> bool:
    needle = str(url_filter).strip()
    if not needle:
        return False
    for frame in top_frames:
        if not frame.empty and frame["URL"].astype(str).str.contains(needle, regex=False, na=False).any():
            return True
    return False


def _show_url_filter_check(visits: pd.DataFrame, url_filter: str) -> None:
    st.subheader("Проверка URL-фильтра")
    st.write(f"Введенный URL-фильтр: `{url_filter}`")
    top_start = _top_urls(visits, ["startURL"], 10)
    top_end = _top_urls(visits, ["endURL"], 10)
    c1, c2 = st.columns(2)
    with c1:
        st.write("Top-10 startURL из выгрузки")
        st.dataframe(top_start, use_container_width=True, hide_index=True)
    with c2:
        st.write("Top-10 endURL из выгрузки")
        st.dataframe(top_end, use_container_width=True, hide_index=True)
    if not _url_filter_found([top_start, top_end], url_filter):
        st.warning("Похоже, URL-фильтр не применился или применился не так. Проверьте синтаксис фильтра.")


def _visit_id_column(visits: pd.DataFrame) -> str | None:
    if "visitID" in visits.columns:
        return "visitID"
    return None


def _client_url_filter(visits: pd.DataFrame, url_filter: str) -> pd.DataFrame:
    """Keep only visits where startURL or endURL contains the user URL filter."""
    needle = str(url_filter).strip()
    if visits.empty or not needle:
        return visits.copy()
    start_matches = visits.get("startURL", pd.Series(False, index=visits.index)).astype(str).str.contains(
        needle, regex=False, case=False, na=False
    )
    end_matches = visits.get("endURL", pd.Series(False, index=visits.index)).astype(str).str.contains(
        needle, regex=False, case=False, na=False
    )
    return visits[start_matches | end_matches].copy()


def _representative_visit_ids(visits: pd.DataFrame, limit: int, visit_id_col: str) -> list:
    """Pick a bounded, mixed set of visit IDs for the interactive report."""
    if visits.empty or limit <= 0:
        return []

    work = visits.dropna(subset=[visit_id_col]).drop_duplicates(subset=[visit_id_col]).copy()
    if len(work) <= limit:
        return work[visit_id_col].tolist()

    pageviews_col = "pageViews"
    duration_col = "visitDuration"
    bounce_col = "bounce"
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


def _show_goal_debug(visits: pd.DataFrame, selected_goal_ids: set[str]) -> None:
    goals = visits["goalsID"] if "goalsID" in visits.columns else pd.Series(dtype=object)
    non_empty_goals = int(goals.apply(lambda value: bool(parse_goal_ids(value))).sum()) if not goals.empty else 0
    matched_goals = int(visits["target_reached"].sum()) if "target_reached" in visits.columns else 0
    with st.expander("Отладка целей", expanded=False):
        st.write("Введенные ID целей")
        st.write(sorted(selected_goal_ids))
        st.write("Визитов с непустым goalsID")
        st.write(non_empty_goals)
        st.write("Визитов с выбранной целью")
        st.write(matched_goals)


def _show_visits_summary(visits: pd.DataFrame, selected_url: str, date_from, date_to, before_filter: int | None = None) -> None:
    st.subheader("2. Сводка по visits")
    total_visits = len(visits)
    before_filter = total_visits if before_filter is None else int(before_filter)
    target_visits = int(visits["target_reached"].sum()) if "target_reached" in visits.columns else 0
    cr = target_visits / total_visits if total_visits else 0
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("visits_before_client_filter", before_filter)
    c2.metric("visits_after_client_filter", total_visits)
    c3.metric("Visits с целью", target_visits)
    c4.metric("CR", f"{cr:.2%}")
    c5.metric("Выбранный URL", selected_url)
    c6.metric("Период", f"{date_from} — {date_to}")

    if before_filter > total_visits:
        st.warning("Серверный фильтр Logs API вернул лишние данные. Отчет построен после дополнительной клиентской фильтрации.")

    st.write("Top-5 startURL")
    st.dataframe(_top_urls(visits, ["startURL"], 5), use_container_width=True, hide_index=True)


def _reset_report_state() -> None:
    for key in ["hits", "paths", "sample_visit_ids", "sample_size", "visits", "matching_hits"]:
        st.session_state.pop(key, None)


def _limited_visit_ids_from_hits(hits: pd.DataFrame, limit: int) -> list:
    if hits.empty or "visitID" not in hits.columns:
        return []
    return hits["visitID"].dropna().drop_duplicates().head(limit).tolist()


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
    selected_url = st.text_input(
        "URL содержит",
        value="/promo/b24messenger/team/",
        help="Можно вводить часть адреса, например /promo/free-online-crm/. Сначала приложение безопасно ищет hits с этим URL и только потом грузит visits по найденным visitID.",
    )
    goal_ids_raw = st.text_input("ID цели", value="2898778")
    max_steps = st.number_input("Максимум шагов пути", min_value=1, max_value=10, value=3, step=1)
    visit_id_limit = st.selectbox("Максимум visitID для загрузки visits/hits", VISIT_ID_LIMIT_OPTIONS, index=2)
    load_report = st.button("Загрузить отчет", type="primary")
    clean_requests = st.button("Очистить подготовленные log requests")

st.subheader("1. Статус подключения")
token = get_metrika_token()
if token:
    st.success("Токен Яндекс Метрики найден.")
else:
    st.warning("YANDEX_METRIKA_TOKEN не задан. Добавьте токен в Streamlit Secrets.")

if clean_requests:
    if not token:
        st.error("Нельзя очистить log requests без YANDEX_METRIKA_TOKEN.")
    else:
        try:
            cleaned = MetrikaLogsClient(token=token).clean_log_requests(counter_id)
            st.success(f"Отправлена очистка подготовленных log requests: {cleaned}.")
        except MetrikaAPIError as exc:
            st.error(str(exc))
            st.info("Если видите ошибку requested log is too big, очистите подготовленные log requests в Метрике/API или подождите, пока они будут удалены.")


if load_report:
    _reset_report_state()
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
        client = MetrikaLogsClient(token=token, timeout_seconds=HITS_TIMEOUT_SECONDS)
        with st.spinner("Шаг 1/3: безопасно ищем hits по выбранному URL..."):
            matching_hits = client.fetch_hits_for_url(counter_id, date_from, date_to, selected_url)
        matching_hits = normalize_metrika_columns(matching_hits)
        if matching_hits.empty or "visitID" not in matching_hits.columns:
            st.info("По URL ничего не найдено. Попробуйте полный URL или проверьте дату.")
            st.stop()

        matching_visit_ids = matching_hits["visitID"].dropna().drop_duplicates().tolist()
        total_found = len(matching_visit_ids)
        st.success(f"Найдено visitID с выбранным URL: {total_found}")
        selected_visit_ids = _limited_visit_ids_from_hits(matching_hits, int(visit_id_limit))
        if total_found > len(selected_visit_ids):
            st.warning(f"Найдено {total_found} visitID. По лимиту сайдбара загружаем только первые {len(selected_visit_ids)}.")

        with st.spinner(f"Шаг 2/3: загружаем visits только по {len(selected_visit_ids)} visitID..."):
            visits = client.fetch_visits_for_visit_ids(counter_id, date_from, date_to, selected_visit_ids)
        visits = normalize_metrika_columns(visits)
        if visits.empty:
            st.info("По найденным visitID данные visits не вернулись.")
            st.stop()
        visits = mark_target_reached(visits, selected_goal_ids)

        with st.spinner(f"Шаг 3/3: загружаем full hits только по {len(selected_visit_ids)} visitID и строим пути..."):
            hits = client.fetch_hits_for_visit_ids(counter_id, date_from, date_to, selected_visit_ids, max_elapsed_seconds=HITS_TIMEOUT_SECONDS)
        hits = normalize_metrika_columns(hits)
        if hits.empty:
            st.error("Не удалось загрузить hits по выбранным visitID. Нельзя построить путь.")
            st.stop()

        visit_id_col = _visit_id_column(visits)
        sampled_visits = visits[visits[visit_id_col].astype(str).isin({str(v) for v in selected_visit_ids})] if visit_id_col else visits
        paths = build_paths(sampled_visits, hits, selected_url, int(max_steps))
        if paths.empty:
            st.info("Выбранный URL не найден внутри цепочек hits. Проверьте дату и URL-фильтр.")

        st.session_state.visits = visits
        st.session_state.matching_hits = matching_hits
        st.session_state.hits = hits
        st.session_state.paths = paths
        st.session_state.sample_visit_ids = selected_visit_ids
        st.session_state.sample_size = len(selected_visit_ids)
        st.session_state.report_params = {
            "counter_id": counter_id,
            "date_from": date_from,
            "date_to": date_to,
            "selected_url": selected_url,
            "goal_ids_raw": goal_ids_raw,
            "selected_goal_ids": sorted(selected_goal_ids),
            "max_steps": int(max_steps),
            "visit_id_limit": int(visit_id_limit),
            "total_found": total_found,
            "server_filter_mode": str(matching_hits.get("server_filter_mode", pd.Series(["—"])).iloc[0]) if not matching_hits.empty else "—",
        }
    except MetrikaAPIError as exc:
        st.error(str(exc))
        if "слишком" in str(exc).lower() or "too big" in str(exc).lower():
            st.info("Серверный URL-фильтр не сработал, Logs API пытается выгрузить слишком много данных. Попробуйте полный URL или более узкий период.")
    except Exception as exc:
        st.exception(exc)

visits = st.session_state.get("visits", pd.DataFrame())
params = st.session_state.get("report_params", {})
paths = st.session_state.get("paths", pd.DataFrame())
hits = st.session_state.get("hits", pd.DataFrame())
matching_hits = st.session_state.get("matching_hits", pd.DataFrame())

if not visits.empty:
    active_selected_url = params.get("selected_url", selected_url)
    active_date_from = params.get("date_from", date_from)
    active_date_to = params.get("date_to", date_to)
    active_goal_ids = set(params.get("selected_goal_ids", parse_goal_ids(params.get("goal_ids_raw", goal_ids_raw))))
    st.subheader("2. Найденные visitID и ограниченная выгрузка")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Найдено visitID с URL", params.get("total_found", 0))
    c2.metric("Загружено visits", len(visits))
    c3.metric("Лимит visitID", params.get("visit_id_limit", visit_id_limit))
    c4.metric("Режим URL-фильтра", params.get("server_filter_mode", "—"))
    st.caption("Отчет начинается с маленькой выгрузки hits по URL. Visits и full hits загружаются только по найденным visitID через IN (...), без fallback на выгрузку всего счетчика.")
    _show_goal_debug(visits, active_goal_ids)

    if not matching_hits.empty:
        with st.expander("Hits, по которым найдены visitID", expanded=False):
            st.dataframe(matching_hits.head(100), use_container_width=True)

    if not paths.empty:
        next_steps = aggregate_next_steps(paths)
        top_paths = aggregate_top_paths(paths)
        watchlist = build_watchlist(paths)

        st.subheader("3. Сводка по путям")
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
        st.write("Первые 50 строк full hits")
        st.dataframe(hits.head(50), use_container_width=True)
        st.write("Первые 50 строк paths")
        st.dataframe(paths.head(50), use_container_width=True)
        st.write("Выбранные goal_ids", sorted(parse_goal_ids(params.get("goal_ids_raw", goal_ids_raw))))
elif not load_report:
    st.info("Задайте параметры в сайдбаре и нажмите «Загрузить отчет». Сначала загрузятся маленькие hits по URL, затем visits и full hits только по найденным visitID.")
