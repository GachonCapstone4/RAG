from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException
from pydantic import ValidationError

from app.config import settings
from app.main import ingest_knowledge, index_templates, match_templates
from app.mq.schemas import (
  DraftGenerateMessage,
  ErrorPayload,
  KnowledgeIngestMessage,
  ProgressEvent,
  TemplateIndexMessage,
  TemplateMatchMessage,
)
from app.schemas import (
  KnowledgeFaqInput,
  KnowledgeIngestRequest,
  KnowledgeManualInput,
  TemplateIndexItem,
  TemplateIndexRequest,
  TemplateMatchRequest,
)
from app.services.draft_service import run_onboarding_template_draft
from app.services.openai_compatible_client import OpenAICompatibleApiError


logger = logging.getLogger(__name__)


@dataclass
class RetryableWorkerError(Exception):
  message: str

  def __str__(self) -> str:
    return self.message


def _require_pika():
  try:
    import pika
  except ImportError as error:  # pragma: no cover
    raise RuntimeError(
      "RabbitMQ worker를 실행하려면 pika 패키지가 필요합니다. "
      "먼저 `pip install -e .` 또는 `pip install pika`로 의존성을 설치해주세요."
    ) from error
  return pika


class RagMqWorker:
  def __init__(self):
    self._pika = _require_pika()
    credentials = self._pika.PlainCredentials(
      settings.rabbitmq_username,
      settings.rabbitmq_password,
    )
    parameters = self._pika.ConnectionParameters(
      host=settings.rabbitmq_host,
      port=settings.rabbitmq_port,
      virtual_host=settings.rabbitmq_virtual_host,
      credentials=credentials,
      heartbeat=settings.rabbitmq_heartbeat_seconds,
      blocked_connection_timeout=settings.rabbitmq_blocked_connection_timeout_seconds,
    )
    self.connection = self._pika.BlockingConnection(parameters)
    self.channel = self.connection.channel()
    self.channel.basic_qos(prefetch_count=1)
    logger.info(
      "RabbitMQ 연결 성공: host=%s port=%s vhost=%s user=%s",
      settings.rabbitmq_host,
      settings.rabbitmq_port,
      settings.rabbitmq_virtual_host,
      settings.rabbitmq_username,
    )

  def run_forever(self) -> None:
    logger.info(
      "RAG MQ worker 시작: host=%s port=%s vhost=%s",
      settings.rabbitmq_host,
      settings.rabbitmq_port,
      settings.rabbitmq_virtual_host,
    )
    consumers = [
      (settings.queue_knowledge_ingest_inbound, self._handle_knowledge_ingest),
      (settings.queue_template_index_inbound, self._handle_template_index),
      (settings.queue_template_match_inbound, self._handle_template_match),
      (settings.queue_draft_inbound, self._handle_draft_generate),
    ]
    for queue_name, handler in consumers:
      self._register_consumer(queue_name, handler)
    logger.info("총 %d개 큐 consume 대기 중", len(consumers))
    self.channel.start_consuming()

  def close(self) -> None:
    try:
      if self.channel.is_open:
        self.channel.close()
    finally:
      if self.connection.is_open:
        self.connection.close()

  def _register_consumer(self, queue_name: str, handler: Callable[[dict[str, Any], int], None]) -> None:
    try:
      self.channel.basic_consume(
        queue=queue_name,
        on_message_callback=self._wrap_handler(handler),
        auto_ack=False,
      )
      logger.info("Consumer 등록 완료: queue=%s handler=%s", queue_name, handler.__name__)
    except Exception as error:
      logger.error("Consumer 등록 실패: queue=%s error=%s", queue_name, error)
      raise

  def _wrap_handler(self, handler: Callable[[dict[str, Any], int], None]):
    def _consumer(channel, method, properties, body):
      retry_count = self._extract_retry_count(properties.headers if properties else None)
      logger.info(
        "MQ 메시지 수신: routing_key=%s retry_count=%s",
        method.routing_key,
        retry_count,
      )
      try:
        payload = json.loads(body.decode("utf-8"))
      except Exception as error:
        logger.exception("MQ 메시지 JSON 디코딩 실패: queue=%s error=%s", method.routing_key, error)
        channel.basic_ack(method.delivery_tag)
        return

      try:
        handler(payload, retry_count)
        logger.info("MQ 메시지 처리 완료: routing_key=%s", method.routing_key)
        channel.basic_ack(method.delivery_tag)
      except RetryableWorkerError as error:
        logger.warning(
          "MQ 메시지 재시도 예정: routing_key=%s retry_count=%s error=%s",
          method.routing_key,
          retry_count,
          error,
        )
        channel.basic_nack(method.delivery_tag, False, False)
      except Exception:
        logger.exception("MQ 메시지 처리 실패: routing_key=%s", method.routing_key)
        channel.basic_ack(method.delivery_tag)

    return _consumer

  def _extract_retry_count(self, headers: dict[str, Any] | None) -> int:
    if not headers:
      return 0
    x_death = headers.get("x-death")
    if isinstance(x_death, list) and x_death:
      first = x_death[0]
      if isinstance(first, dict):
        count = first.get("count")
        if isinstance(count, int):
          return count
    return 0

  def _publish_json(self, routing_key: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    properties = self._pika.BasicProperties(
      content_type="application/json",
      delivery_mode=2,
    )
    self.channel.basic_publish(
      exchange=settings.exchange_rag_to_app,
      routing_key=routing_key,
      body=body,
      properties=properties,
    )

  def _publish_dead_letter(
    self,
    *,
    inbound_routing_key: str,
    payload: dict[str, Any],
    error: ErrorPayload,
    retry_count: int,
  ) -> None:
    body = json.dumps(
      {
        "inbound_routing_key": inbound_routing_key,
        "retry_count": retry_count,
        "error": error.model_dump(),
        "payload": payload,
      },
      ensure_ascii=False,
    ).encode("utf-8")
    properties = self._pika.BasicProperties(
      content_type="application/json",
      delivery_mode=2,
    )
    # q.dlx.failed는 default exchange를 통해 queue 이름으로 직접 적재한다.
    self.channel.basic_publish(
      exchange="",
      routing_key=settings.queue_dlx_failed,
      body=body,
      properties=properties,
    )

  def _publish_progress(
    self,
    *,
    job_id: str,
    request_id: str,
    user_id: int,
    job_type: str,
    target_type: str | None,
    target_id: str | None,
    status: str,
    progress_step: str,
    progress_message: str,
    payload: dict[str, Any] | None = None,
    error: ErrorPayload | None = None,
  ) -> None:
    event = ProgressEvent(
      job_id=job_id,
      request_id=request_id,
      user_id=user_id,
      job_type=job_type,
      target_type=target_type,
      target_id=target_id,
      status=status,
      progress_step=progress_step,
      progress_message=progress_message,
      payload=payload,
      error=error,
    )
    self._publish_json(
      settings.routing_key_rag_progress,
      event.model_dump(exclude_none=True),
    )

  def _build_error(self, error: Exception) -> ErrorPayload:
    if isinstance(error, OpenAICompatibleApiError):
      if error.status_code is not None:
        code = f"EXTERNAL_API_HTTP_{error.status_code}"
      else:
        code = "EXTERNAL_API_CONNECTION_ERROR"
      return ErrorPayload(code=code, message=str(error))
    if isinstance(error, HTTPException):
      return ErrorPayload(code=f"HTTP_{error.status_code}", message=str(error.detail))
    if isinstance(error, ValidationError):
      return ErrorPayload(code="VALIDATION_ERROR", message=str(error))
    return ErrorPayload(code=error.__class__.__name__.upper(), message=str(error))

  def _is_retryable_error(self, error: Exception) -> bool:
    if isinstance(error, OpenAICompatibleApiError):
      if error.status_code is None:
        return True
      return error.status_code == 429 or error.status_code >= 500
    if isinstance(error, HTTPException):
      detail = str(error.detail)
      if error.status_code == 404 and "Document file not found" in detail:
        return True
      return error.status_code >= 500
    if isinstance(error, ValidationError):
      return False
    if isinstance(error, FileNotFoundError):
      return True
    if isinstance(error, ValueError):
      return False
    return True

  def _log_job_error(
    self,
    *,
    job_type: str,
    job_id: str,
    request_id: str,
    user_id: int,
    target_type: str | None,
    target_id: str | None,
    retry_count: int,
    error: Exception,
  ) -> None:
    if isinstance(error, OpenAICompatibleApiError):
      logger.warning(
        "RAG job 외부 API 실패: job_type=%s job_id=%s request_id=%s user_id=%s "
        "target_type=%s target_id=%s retry_count=%s api_path=%s status=%s detail=%s",
        job_type,
        job_id,
        request_id,
        user_id,
        target_type,
        target_id,
        retry_count,
        error.path,
        error.status_code,
        error.detail,
      )
      return
    logger.exception(
      "RAG job 처리 실패: job_type=%s job_id=%s request_id=%s user_id=%s "
      "target_type=%s target_id=%s retry_count=%s",
      job_type,
      job_id,
      request_id,
      user_id,
      target_type,
      target_id,
      retry_count,
    )

  def _publish_failed_result(
    self,
    *,
    routing_key: str,
    job_id: str | None = None,
    request_id: str,
    user_id: int,
    error: ErrorPayload,
  ) -> None:
    response = {
      "request_id": request_id,
      "user_id": user_id,
      "status": "FAILED",
      "error": error.model_dump(),
    }
    if job_id:
      response["job_id"] = job_id
    self._publish_json(routing_key, response)

  def _should_retry(self, error: Exception, retry_count: int) -> bool:
    return self._is_retryable_error(error) and retry_count < settings.rabbitmq_max_retry_count

  def _handle_knowledge_ingest(self, raw_payload: dict[str, Any], retry_count: int) -> None:
    message = KnowledgeIngestMessage.model_validate(raw_payload)
    target_id = f"user:{message.user_id}"
    self._publish_progress(
      job_id=message.job_id,
      request_id=message.request_id,
      user_id=message.user_id,
      job_type="knowledge.ingest",
      target_type="knowledge",
      target_id=target_id,
      status="PROCESSING",
      progress_step="INGESTING",
      progress_message="업로드한 자료를 확인하고 있습니다.",
    )
    try:
      response = ingest_knowledge(
        KnowledgeIngestRequest(
          request_id=message.request_id,
          user_id=message.user_id,
          faqs=[
            KnowledgeFaqInput(
              source_id=item.source_id,
              question=item.question,
              answer=item.answer,
            )
            for item in message.payload.faqs
          ],
          manuals=[
            KnowledgeManualInput(
              source_id=item.source_id,
              title=item.title,
              file_name=item.file_name,
              local_path=item.local_path,
              presigned_url=item.presigned_url,
            )
            for item in message.payload.manuals
          ],
        )
      )
    except Exception as error:
      self._log_job_error(
        job_type="knowledge.ingest",
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        target_type="knowledge",
        target_id=target_id,
        retry_count=retry_count,
        error=error,
      )
      if self._should_retry(error, retry_count):
        raise RetryableWorkerError(str(error)) from error
      error_payload = self._build_error(error)
      if self._is_retryable_error(error):
        self._publish_dead_letter(
          inbound_routing_key=settings.routing_key_knowledge_ingest_inbound,
          payload=raw_payload,
          error=error_payload,
          retry_count=retry_count,
        )
      self._publish_progress(
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        job_type="knowledge.ingest",
        target_type="knowledge",
        target_id=target_id,
        status="FAILED",
        progress_step="FAILED",
        progress_message="자료 처리에 실패했습니다.",
        error=error_payload,
      )
      self._publish_failed_result(
        routing_key=settings.routing_key_knowledge_ingest_result,
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        error=error_payload,
      )
      return

    self._publish_json(
      settings.routing_key_knowledge_ingest_result,
      {
        "job_id": message.job_id,
        "request_id": response.request_id,
        "user_id": response.user_id,
        "status": response.status,
        "payload": {
          "indexed_document_count": response.indexed_document_count,
          "indexed_chunk_count": response.indexed_chunk_count,
          "indexed_source_ids": response.indexed_source_ids,
        },
      },
    )

  def _handle_template_index(self, raw_payload: dict[str, Any], retry_count: int) -> None:
    message = TemplateIndexMessage.model_validate(raw_payload)
    self._publish_progress(
      job_id=f"template-index-{message.user_id}-{message.request_id}",
      request_id=message.request_id,
      user_id=message.user_id,
      job_type="templates.index",
      target_type="template",
      target_id=message.request_id,
      status="PROCESSING",
      progress_step="INDEXING",
      progress_message="생성된 템플릿을 정리하고 있습니다.",
    )
    try:
      response = index_templates(
        TemplateIndexRequest(
          request_id=message.request_id,
          user_id=message.user_id,
          templates=[
            TemplateIndexItem(
              template_id=item.template_id,
              title=item.title,
              category_name=item.category_name,
              email_tone=item.email_tone,
              metadata=item.metadata,
            )
            for item in message.payload.templates
          ],
        )
      )
    except Exception as error:
      self._log_job_error(
        job_type="templates.index",
        job_id=f"template-index-{message.user_id}-{message.request_id}",
        request_id=message.request_id,
        user_id=message.user_id,
        target_type="template",
        target_id=message.request_id,
        retry_count=retry_count,
        error=error,
      )
      if self._should_retry(error, retry_count):
        raise RetryableWorkerError(str(error)) from error
      error_payload = self._build_error(error)
      if self._is_retryable_error(error):
        self._publish_dead_letter(
          inbound_routing_key=settings.routing_key_template_index_inbound,
          payload=raw_payload,
          error=error_payload,
          retry_count=retry_count,
        )
      self._publish_progress(
        job_id=f"template-index-{message.user_id}-{message.request_id}",
        request_id=message.request_id,
        user_id=message.user_id,
        job_type="templates.index",
        target_type="template",
        target_id=message.request_id,
        status="FAILED",
        progress_step="FAILED",
        progress_message="템플릿 정리에 실패했습니다.",
        error=error_payload,
      )
      self._publish_failed_result(
        routing_key=settings.routing_key_template_index_result,
        request_id=message.request_id,
        user_id=message.user_id,
        error=error_payload,
      )
      return

    self._publish_json(
      settings.routing_key_template_index_result,
      {
        "request_id": response.request_id,
        "user_id": response.user_id,
        "status": "SUCCESS",
        "payload": {
          "indexed_template_count": response.indexed_template_count,
          "template_ids": response.template_ids,
        },
      },
    )

  def _handle_template_match(self, raw_payload: dict[str, Any], retry_count: int) -> None:
    message = TemplateMatchMessage.model_validate(raw_payload)
    target_id = message.payload.emailId
    self._publish_progress(
      job_id=message.job_id,
      request_id=message.request_id,
      user_id=message.user_id,
      job_type="templates.match",
      target_type="email",
      target_id=target_id,
      status="PROCESSING",
      progress_step="MATCHING",
      progress_message="추천 템플릿을 찾고 있습니다.",
    )
    try:
      response = match_templates(
        TemplateMatchRequest(
          request_id=message.request_id,
          emailId=message.payload.emailId,
          user_id=message.user_id,
          subject=message.payload.subject,
          body=message.payload.body,
          summary=message.payload.summary,
          intent=message.payload.intent,
          domain=message.payload.domain,
          top_k=message.payload.top_k,
        )
      )
    except Exception as error:
      self._log_job_error(
        job_type="templates.match",
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        target_type="email",
        target_id=target_id,
        retry_count=retry_count,
        error=error,
      )
      if self._should_retry(error, retry_count):
        raise RetryableWorkerError(str(error)) from error
      error_payload = self._build_error(error)
      if self._is_retryable_error(error):
        self._publish_dead_letter(
          inbound_routing_key=settings.routing_key_template_match_inbound,
          payload=raw_payload,
          error=error_payload,
          retry_count=retry_count,
        )
      self._publish_progress(
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        job_type="templates.match",
        target_type="email",
        target_id=target_id,
        status="FAILED",
        progress_step="FAILED",
        progress_message="추천 템플릿 조회에 실패했습니다.",
        error=error_payload,
      )
      self._publish_failed_result(
        routing_key=settings.routing_key_template_match_result,
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        error=error_payload,
      )
      return

    self._publish_json(
      settings.routing_key_template_match_result,
      {
        "job_id": message.job_id,
        "request_id": response.request_id,
        "user_id": response.user_id,
        "status": "SUCCESS",
        "payload": {
          "emailId": response.emailId,
          "results": [
            {
              "template_id": item.template_id,
              "title": item.title,
              "intent": item.intent,
              "score": item.score,
            }
            for item in response.results
          ],
        },
      },
    )

  def _handle_draft_generate(self, raw_payload: dict[str, Any], retry_count: int) -> None:
    message = DraftGenerateMessage.model_validate(raw_payload)
    target_id = str(message.category_id) if message.category_id is not None else None
    self._publish_progress(
      job_id=message.job_id,
      request_id=message.request_id,
      user_id=message.user_id,
      job_type="draft.generate",
      target_type="category",
      target_id=target_id,
      status="PROCESSING",
      progress_step="GENERATING_TEMPLATE",
      progress_message="카테고리별 맞춤 템플릿을 만들고 있습니다.",
    )
    try:
      templates = run_onboarding_template_draft(
        user_id=message.user_id,
        category_id=message.category_id,
        category_name=message.category_name,
        category_keywords=message.category_keywords,
        industry_type=message.industry_type,
        company_description=message.company_description,
        email_tone=message.email_tone,
        rag_context=message.rag_context,
        template_count=message.template_count,
      )
    except Exception as error:
      self._log_job_error(
        job_type="draft.generate",
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        target_type="category",
        target_id=target_id,
        retry_count=retry_count,
        error=error,
      )
      if self._should_retry(error, retry_count):
        raise RetryableWorkerError(str(error)) from error
      error_payload = self._build_error(error)
      if self._is_retryable_error(error):
        self._publish_dead_letter(
          inbound_routing_key=settings.routing_key_draft_inbound,
          payload=raw_payload,
          error=error_payload,
          retry_count=retry_count,
        )
      self._publish_progress(
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        job_type="draft.generate",
        target_type="category",
        target_id=target_id,
        status="FAILED",
        progress_step="FAILED",
        progress_message="템플릿 생성에 실패했습니다.",
        error=error_payload,
      )
      self._publish_failed_result(
        routing_key=settings.routing_key_draft_result,
        job_id=message.job_id,
        request_id=message.request_id,
        user_id=message.user_id,
        error=error_payload,
      )
      return

    self._publish_json(
      settings.routing_key_draft_result,
      {
        "job_id": message.job_id,
        "request_id": message.request_id,
        "user_id": message.user_id,
        "status": "SUCCESS",
        "payload": {
          "category_id": message.category_id,
          "category_name": message.category_name,
          "templates": [
            {
              "variant_label": template.variant_label,
              "template_purpose": template.template_purpose,
              "title": template.title,
              "subject_template": template.subject_template,
              "body_template": template.body_template,
            }
            for template in templates
          ],
        },
      },
    )


def run_worker() -> None:
  logging.basicConfig(level=logging.INFO)
  worker = RagMqWorker()
  try:
    worker.run_forever()
  finally:
    worker.close()


if __name__ == "__main__":  # pragma: no cover
  run_worker()
