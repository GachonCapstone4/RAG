from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from app.config import Settings


def _normalize_base_url(base_url: str) -> str:
  # 설정에 /v1이 빠져 있어도 내부에서 보정해
  # 호출부는 항상 같은 방식으로 client를 쓰게 한다.
  normalized = base_url.strip().rstrip("/")
  if not normalized:
    raise ValueError("LLM_API_BASE_URL 환경변수가 비어 있습니다.")
  if normalized.endswith("/v1"):
    return normalized
  return f"{normalized}/v1"


def _extract_error_message(raw_body: str) -> str:
  # 서버별 오류 응답 형식이 다를 수 있으므로
  # 사람이 읽기 쉬운 핵심 메시지만 뽑아내는 보조 함수다.
  try:
    payload = json.loads(raw_body)
  except json.JSONDecodeError:
    return raw_body.strip() or "응답 본문이 없습니다."

  if isinstance(payload, dict):
    error_payload = payload.get("error")
    if isinstance(error_payload, dict):
      message = error_payload.get("message")
      if isinstance(message, str) and message.strip():
        return message.strip()

    message = payload.get("message")
    if isinstance(message, str) and message.strip():
      return message.strip()

  return raw_body.strip() or "응답 본문이 없습니다."


@dataclass
class OpenAICompatibleApiError(Exception):
  path: str
  message: str
  status_code: int | None = None
  detail: str | None = None

  def __str__(self) -> str:
    parts = [self.message, f"path={self.path}"]
    if self.status_code is not None:
      parts.append(f"status={self.status_code}")
    if self.detail:
      parts.append(f"detail={self.detail}")
    return ", ".join(parts)


@dataclass
class OpenAICompatibleApiClient:
  base_url: str
  api_key: str
  timeout_seconds: float

  def create_chat_completion(
    self,
    *,
    messages: list[dict[str, Any]],
    model: str | None = None,
    stream: bool = False,
  ) -> dict[str, Any]:
    payload: dict[str, Any] = {
      "messages": messages,
      "stream": stream,
    }
    if model:
      payload["model"] = model
    return self._post_json("/chat/completions", payload)

  def create_embeddings(
    self,
    *,
    inputs: list[str],
    model: str | None = None,
  ) -> dict[str, Any]:
    payload: dict[str, Any] = {
      "input": inputs,
    }
    if model:
      payload["model"] = model
    return self._post_json("/embeddings", payload)

  def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    # requests 대신 표준 라이브러리 urllib를 써서 의존성을 최소화한다.
    target_url = f"{self.base_url}/{path.lstrip('/')}"
    raw_body = json.dumps(payload).encode("utf-8")
    http_request = request.Request(
      target_url,
      data=raw_body,
      headers={
        "Authorization": f"Bearer {self.api_key}",
        "Content-Type": "application/json",
      },
      method="POST",
    )

    try:
      with request.urlopen(http_request, timeout=self.timeout_seconds) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        response_body = response.read().decode(charset)
    except error.HTTPError as exc:
      error_body = exc.read().decode("utf-8", errors="replace")
      detail = _extract_error_message(error_body)
      raise OpenAICompatibleApiError(
        path=path,
        status_code=exc.code,
        detail=detail,
        message="LLM API 요청이 실패했습니다.",
      ) from exc
    except error.URLError as exc:
      raise OpenAICompatibleApiError(
        path=path,
        detail=str(exc.reason),
        message="LLM API 서버에 연결하지 못했습니다.",
      ) from exc

    try:
      parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
      raise OpenAICompatibleApiError(
        path=path,
        detail=response_body[:500],
        message="LLM API 응답이 JSON 형식이 아닙니다.",
      ) from exc

    if not isinstance(parsed, dict):
      raise OpenAICompatibleApiError(
        path=path,
        detail=str(type(parsed).__name__),
        message="LLM API 응답 형식이 예상과 다릅니다.",
      )

    return parsed


def create_openai_compatible_client(settings: Settings) -> OpenAICompatibleApiClient:
  return create_openai_compatible_client_with_overrides(settings)


def create_openai_compatible_client_with_overrides(
  settings: Settings,
  *,
  base_url: str | None = None,
  api_key: str | None = None,
) -> OpenAICompatibleApiClient:
  resolved_api_key = api_key if api_key is not None else settings.llm_api_key
  resolved_base_url = base_url if base_url is not None else settings.llm_api_base_url

  api_key = resolved_api_key
  if not api_key:
    # 생성/임베딩이 모두 이 client를 통해 가므로
    # API 키가 없으면 즉시 명확한 예외를 내도록 한다.
    raise ValueError("LLM_API_KEY 환경변수가 설정되지 않았습니다.")

  return OpenAICompatibleApiClient(
    base_url=_normalize_base_url(resolved_base_url),
    api_key=api_key,
    timeout_seconds=settings.llm_api_timeout_seconds,
  )
