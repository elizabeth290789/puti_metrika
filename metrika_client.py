"""Small Yandex Metrika Logs API client for User Path Report."""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass
from typing import Iterable

import pandas as pd
import requests

try:
    import streamlit as st
except ImportError:  # pragma: no cover
    st = None


API_HOST = "https://api-metrika.yandex.net"
LOGREQUESTS_ENDPOINT = API_HOST + "/management/v1/counter/{counter_id}/logrequests"
LOGREQUEST_ENDPOINT = API_HOST + "/management/v1/counter/{counter_id}/logrequest/{request_id}"


class MetrikaAPIError(Exception):
    """User-safe Metrika API error without token details."""


LOGS_API_CONNECTION_ERROR = "Соединение с Logs API оборвалось. Уменьшите период, URL-фильтр или лимит visitID."


def get_metrika_token() -> str | None:
    """Read token from env first, then from Streamlit secrets."""
    token = os.getenv("YANDEX_METRIKA_TOKEN")
    if token:
        return token
    if st is not None:
        try:
            token = st.secrets.get("YANDEX_METRIKA_TOKEN")
            return str(token) if token else None
        except Exception:
            return None
    return None


@dataclass
class MetrikaLogsClient:
    token: str | None
    poll_interval: float = 2.0
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if self.token:
            self.session = requests.Session()
            self.session.headers.update({"Authorization": f"OAuth {self.token}"})
        else:
            self.session = None

    def _require_token(self) -> None:
        if not self.token or self.session is None:
            raise MetrikaAPIError("YANDEX_METRIKA_TOKEN не задан. Добавьте токен в Streamlit Secrets или переменную окружения.")

    @staticmethod
    def _create_request_url(counter_id: str | int) -> str:
        return LOGREQUESTS_ENDPOINT.format(counter_id=counter_id)

    @staticmethod
    def _request_url(counter_id: str | int, request_id: int) -> str:
        return LOGREQUEST_ENDPOINT.format(counter_id=counter_id, request_id=request_id)

    @staticmethod
    def _extract_error_message(response: requests.Response) -> str:
        try:
            payload = response.json()
            message = payload.get("message") or payload.get("errors", [{}])[0].get("message", "")
            return message or response.text[:500]
        except Exception:
            return response.text[:500]

    def _handle_json_response(self, response: requests.Response, context: str, endpoint: str) -> dict:
        if response.ok:
            try:
                payload = response.json()
            except Exception as exc:
                raise MetrikaAPIError(f"API вернул невалидный JSON на этапе {context}.") from exc
            if not payload:
                raise MetrikaAPIError(f"API вернул невалидный JSON на этапе {context}.")
            return payload

        message = self._extract_error_message(response)
        raise MetrikaAPIError(f"Ошибка Logs API ({context}): HTTP {response.status_code}. Endpoint: {endpoint}. {message}")

    def _handle_text_response(self, response: requests.Response, context: str, endpoint: str) -> str:
        if response.ok:
            return response.text

        message = self._extract_error_message(response)
        raise MetrikaAPIError(f"Ошибка Logs API ({context}): HTTP {response.status_code}. Endpoint: {endpoint}. {message}")

    def _create_request(self, counter_id: str | int, source: str, fields: list[str], date_from, date_to, filters: str) -> int:
        self._require_token()
        params = {"date1": str(date_from), "date2": str(date_to), "source": source, "fields": ",".join(fields), "filters": filters}
        url = self._create_request_url(counter_id)
        try:
            response = self.session.post(url, params=params, timeout=60)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
            raise MetrikaAPIError(LOGS_API_CONNECTION_ERROR) from exc
        data = self._handle_json_response(response, "создание запроса", url)
        return int(data["log_request"]["request_id"])

    def _wait_processed(self, counter_id: str | int, request_id: int) -> list[dict]:
        deadline = time.time() + self.timeout_seconds
        url = self._request_url(counter_id, request_id)
        while time.time() < deadline:
            try:
                response = self.session.get(url, timeout=60)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
                raise MetrikaAPIError(LOGS_API_CONNECTION_ERROR) from exc
            data = self._handle_json_response(response, "проверка статуса", url)
            request = data.get("log_request", {})
            status = request.get("status")
            if status == "processed":
                return request.get("parts", [])
            if status in {"created", "awaiting_retry", "processing"}:
                time.sleep(self.poll_interval)
                continue
            raise MetrikaAPIError(f"Запрос Logs API завершился со статусом {status}.")
        raise MetrikaAPIError("Превышено время ожидания обработки запроса Logs API.")

    def _download_parts(self, counter_id: str | int, request_id: int, parts: list[dict]) -> pd.DataFrame:
        frames = []
        for part in parts:
            part_number = part.get("part_number")
            url = f"{self._request_url(counter_id, request_id)}/part/{part_number}/download"
            try:
                response = self.session.get(url, timeout=120)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException) as exc:
                raise MetrikaAPIError(LOGS_API_CONNECTION_ERROR) from exc
            text = self._handle_text_response(response, "скачивание данных", url)
            if not text.strip():
                continue
            try:
                frames.append(pd.read_csv(io.StringIO(text), sep="\t"))
            except Exception as exc:
                raise MetrikaAPIError("Не удалось прочитать part-файл Logs API как TSV.") from exc
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def _clean_request(self, counter_id: str | int, request_id: int) -> None:
        try:
            self.session.delete(f"{self._request_url(counter_id, request_id)}/clean", timeout=60)
        except Exception:
            pass

    def _fetch(self, counter_id, source: str, fields: list[str], date_from, date_to, filters: str) -> pd.DataFrame:
        request_id = self._create_request(counter_id, source, fields, date_from, date_to, filters)
        try:
            parts = self._wait_processed(counter_id, request_id)
            return self._download_parts(counter_id, request_id, parts)
        finally:
            self._clean_request(counter_id, request_id)

    @staticmethod
    def _escape(value: str) -> str:
        return str(value).replace("'", "\\'")

    def fetch_visits(self, counter_id, date_from, date_to, url_filter: str) -> pd.DataFrame:
        if not str(url_filter).strip():
            raise MetrikaAPIError("URL-фильтр обязателен. Нельзя загружать весь счетчик без URL-фильтра.")
        fields = ["ym:s:visitID", "ym:s:clientID", "ym:s:dateTime", "ym:s:startURL", "ym:s:endURL", "ym:s:pageViews", "ym:s:visitDuration", "ym:s:bounce", "ym:s:goalsID", "ym:s:lastTrafficSource", "ym:s:UTMSource", "ym:s:UTMCampaign", "ym:s:deviceCategory"]
        filt = f"ym:s:startURL=='{self._escape(url_filter)}' OR ym:s:endURL=='{self._escape(url_filter)}'"
        return self._fetch(counter_id, "visits", fields, date_from, date_to, filt)

    def fetch_hits_for_url(self, counter_id, date_from, date_to, url_filter: str) -> pd.DataFrame:
        if not str(url_filter).strip():
            raise MetrikaAPIError("URL-фильтр обязателен. Нельзя загружать весь счетчик без URL-фильтра.")
        fields = ["ym:pv:visitID", "ym:pv:dateTime", "ym:pv:URL", "ym:pv:title", "ym:pv:referer", "ym:pv:goalsID"]
        return self._fetch(counter_id, "hits", fields, date_from, date_to, f"ym:pv:URL=='{self._escape(url_filter)}'")

    def fetch_hits_for_visit_ids(self, counter_id, date_from, date_to, visit_ids: Iterable, batch_size: int = 100, max_elapsed_seconds: int | None = None) -> pd.DataFrame:
        ids = [str(v) for v in visit_ids if str(v).strip()]
        if not ids:
            return pd.DataFrame()
        fields = ["ym:pv:visitID", "ym:pv:dateTime", "ym:pv:URL", "ym:pv:title", "ym:pv:referer", "ym:pv:goalsID"]
        frames = []
        started_at = time.monotonic()
        try:
            for idx in range(0, len(ids), batch_size):
                if max_elapsed_seconds is not None and time.monotonic() - started_at > max_elapsed_seconds:
                    raise MetrikaAPIError("Загрузка hits заняла слишком много времени. Уменьшите лимит visitID.")
                batch = ids[idx : idx + batch_size]
                filters = "ym:pv:visitID IN (" + ",".join(batch) + ")"
                frames.append(self._fetch(counter_id, "hits", fields, date_from, date_to, filters))
        except MetrikaAPIError as exc:
            if str(exc) in {LOGS_API_CONNECTION_ERROR, "Загрузка hits заняла слишком много времени. Уменьшите лимит visitID."}:
                raise
            if "Превышено время ожидания обработки запроса Logs API" in str(exc):
                raise MetrikaAPIError("Загрузка hits заняла слишком много времени. Уменьшите лимит visitID.") from exc
            raise MetrikaAPIError("Не удалось загрузить hits по visitID. Нельзя построить полный путь. Проверьте синтаксис фильтра Logs API или используйте более узкую дату/URL.") from exc
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
