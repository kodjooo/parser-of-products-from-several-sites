from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Sequence

from collections.abc import Callable

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from tenacity import retry, stop_after_attempt, wait_exponential

from app.logger import get_logger

logger = get_logger(__name__)


class GoogleSheetsClient:
    """Обёртка над Google Sheets API с батч-записью и созданием вкладок."""

    def __init__(
        self,
        spreadsheet_id: str,
        client_secret_path: Path,
        token_path: Path,
        scopes: Sequence[str],
        batch_size: int,
        subject: str | None = None,
    ):
        self.spreadsheet_id = spreadsheet_id
        self.client_secret_path = client_secret_path
        self.token_path = token_path
        self.scopes = list(scopes)
        self.batch_size = batch_size
        self.subject = subject
        self._client_config_type = self._detect_client_type()
        self.service = build("sheets", "v4", credentials=self._authorize())

    def _retry_call(self, func: Callable[[], dict]) -> dict:
        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=2, min=2, max=10),
        )
        def _inner() -> dict:
            return func()

        return _inner()

    def _detect_client_type(self) -> str:
        try:
            data = json.loads(self.client_secret_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Не найден файл OAuth/Service Account: {self.client_secret_path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Некорректный JSON в {self.client_secret_path}") from exc
        if data.get("type") == "service_account":
            return "service_account"
        if "installed" in data or data.get("type") == "installed" or "web" in data:
            return "installed"
        raise RuntimeError("Client secrets must describe installed app or service account")

    def _authorize(self) -> Credentials:
        if self._client_config_type == "service_account":
            creds = service_account.Credentials.from_service_account_file(
                str(self.client_secret_path), scopes=self.scopes
            )
            if self.subject:
                creds = creds.with_subject(self.subject)
            return creds
        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path), scopes=self.scopes
            )
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.client_secret_path), self.scopes
                )
                creds = flow.run_console()
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            with self.token_path.open("w", encoding="utf-8") as token_file:
                token_file.write(creds.to_json())
        return creds

    def ensure_tabs(self, tab_names: Iterable[str]) -> None:
        existing = self._get_existing_tabs()
        missing = [name for name in tab_names if name not in existing]
        if not missing:
            return
        requests = [
            {"addSheet": {"properties": {"title": name}}}
            for name in missing
        ]
        self._batch_update(requests)

    def ensure_aux_tabs(self, *tab_names: str) -> None:
        self.ensure_tabs(tab_names)

    def _get_existing_tabs(self) -> set[str]:
        meta = self._retry_call(
            lambda: self.service.spreadsheets()
            .get(spreadsheetId=self.spreadsheet_id)
            .execute()
        )
        sheets = meta.get("sheets", [])
        return {sheet["properties"]["title"] for sheet in sheets}

    def _batch_update(self, requests: list[dict]) -> None:
        if not requests:
            return
        body = {"requests": requests}
        self._retry_call(
            lambda: self.service.spreadsheets()
            .batchUpdate(spreadsheetId=self.spreadsheet_id, body=body)
            .execute()
        )

    def get_existing_product_urls(self, tab_name: str) -> set[str]:
        range_name = f"{tab_name}!C:C"
        response = self._retry_call(
            lambda: self.service.spreadsheets()
            .values()
            .get(spreadsheetId=self.spreadsheet_id, range=range_name)
            .execute()
        )
        values = response.get("values", [])[1:]  # пропускаем заголовок
        return {row[0] for row in values if row}

    def append_rows(self, tab_name: str, rows: list[list[str]]) -> None:
        if not rows:
            return
        for chunk_start in range(0, len(rows), self.batch_size):
            chunk = rows[chunk_start : chunk_start + self.batch_size]
            body = {"values": chunk}
            self._retry_call(
                lambda chunk=chunk: self.service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self.spreadsheet_id,
                    range=tab_name,
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body=body,
                )
                .execute()
            )

    def append_runs(self, rows: list[list[str]], tab_name: str) -> None:
        if not rows:
            return
        self.append_rows(tab_name, rows)

    def ensure_header(self, tab_name: str, header: list[str]) -> None:
        range_name = f"{tab_name}!1:1"
        try:
            response = self._retry_call(
                lambda: self.service.spreadsheets()
                .values()
                .get(spreadsheetId=self.spreadsheet_id, range=range_name)
                .execute()
            )
            values = response.get("values", [])
        except HttpError as exc:
            if exc.resp.status in {400, 404}:
                values = []
            else:
                raise
        if values:
            existing = values[0]
            if len(existing) >= len(header) and all(
                (existing[i] if i < len(existing) else "").strip() == header[i]
                for i in range(len(header))
            ):
                return
        body = {
            "range": range_name,
            "majorDimension": "ROWS",
            "values": [header],
        }
        self._retry_call(
            lambda: self.service.spreadsheets()
            .values()
            .update(
                spreadsheetId=self.spreadsheet_id,
                range=range_name,
                valueInputOption="RAW",
                body=body,
            )
            .execute()
        )

    def replace_state_rows(self, tab_name: str, rows: list[list[str]]) -> None:
        clear_body = {}
        self._retry_call(
            lambda: self.service.spreadsheets()
            .values()
            .clear(
                spreadsheetId=self.spreadsheet_id,
                range=f"{tab_name}!A:F",
                body=clear_body,
            )
            .execute()
        )
        header = [
            ["site_name", "category_url", "last_page", "last_offset", "last_product_count", "last_run_ts"]
        ]
        self.append_rows(tab_name, header + rows)


def _column_name(index: int) -> str:
    """Конвертация номера колонки (1-based) в вид A, B, ..., AA."""
    if index <= 0:
        return "A"
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name
