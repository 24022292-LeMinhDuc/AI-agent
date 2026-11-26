"""Cấu hình Gemini dùng chung cho mapping, hỏi đáp và báo cáo."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

MODEL_NAME = "gemini-3.8-flash"
FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
RETRYABLE_STATUS_CODES = (500, 503, 504)


def _load_environment() -> None:
    """Nạp cấu hình Gemini từ .env, ưu tiên biến môi trường đã đặt."""
    path = Path(__file__).with_name(".env")
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, _, value = line.partition("=")
        name = name.strip()
        if name in {"GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODELS"}:
            os.environ.setdefault(name, value.strip().strip("\"'"))


_load_environment()


def create_client() -> genai.Client:
    """Khởi tạo client có giới hạn thời gian và thử lại lỗi tạm thời."""
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(
            timeout=20_000,
            retry_options=types.HttpRetryOptions(
                attempts=2,
                initial_delay=0.5,
                max_delay=1.5,
                jitter=0.1,
                http_status_codes=list(RETRYABLE_STATUS_CODES),
            ),
        ),
    )


class GeminiUnavailableError(RuntimeError):
    """Gemini hoặc model được cấu hình đang không khả dụng."""


class GeminiSession:
    """Giữ model đã tạo phản hồi trong các vòng function calling kế tiếp."""

    def __init__(self, client: genai.Client) -> None:
        self.client = client
        primary = os.getenv("GEMINI_MODEL", "").strip() or MODEL_NAME
        fallbacks = os.getenv("GEMINI_FALLBACK_MODELS", ",".join(FALLBACK_MODELS))
        fallback_names = [name.strip() for name in fallbacks.split(",") if name.strip()]
        self.model_names = tuple(dict.fromkeys([primary, *fallback_names]))
        self.active_model: str | None = None

    def generate(self, *, contents: Any, config: types.GenerateContentConfig) -> Any:
        models = (self.active_model,) if self.active_model else self.model_names
        for model in models:
            try:
                response = self.client.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                status = getattr(exc, "status", None)
                if code not in (404, *RETRYABLE_STATUS_CODES) and status not in (
                    "NOT_FOUND", "UNAVAILABLE", "INTERNAL", "DEADLINE_EXCEEDED"
                ):
                    raise
                continue
            self.active_model = model
            return response
        raise GeminiUnavailableError(
            "Gemini đang quá tải hoặc model được cấu hình không khả dụng. "
            "Hãy thử lại sau."
        )


def generate_content(
    client: genai.Client, *, contents: Any, config: types.GenerateContentConfig
) -> Any:
    """Tạo một phản hồi văn bản hoặc dữ liệu có cấu trúc."""
    return GeminiSession(client).generate(contents=contents, config=config)


def error_message(error: Exception) -> str:
    """Trả thông báo có thể hiển thị mà không đưa chi tiết SDK ra giao diện."""
    if isinstance(error, GeminiUnavailableError):
        return str(error)
    code = getattr(error, "code", None)
    if code in (401, 403):
        return "Gemini từ chối quyền truy cập. Hãy kiểm tra cấu hình API key."
    if code == 429:
        return "Gemini đã chạm giới hạn yêu cầu. Hãy thử lại sau."
    return "Không thể nhận phản hồi từ Gemini. Hãy kiểm tra kết nối và cấu hình."
