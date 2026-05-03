from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import TemplateMetadataInput


class MqModel(BaseModel):
  # RabbitMQ payload도 HTTP 스키마처럼 추가 필드에 관대하게 두어
  # 백엔드가 부가 정보를 함께 보내더라도 worker가 바로 깨지지 않게 한다.
  model_config = ConfigDict(populate_by_name=True, extra="ignore")


class KnowledgeIngestFaqPayload(MqModel):
  source_id: str
  question: str
  answer: str


class KnowledgeIngestManualPayload(MqModel):
  source_id: str
  title: str | None = None
  file_name: str
  local_path: str | None = None
  presigned_url: str | None = None


class KnowledgeIngestPayload(MqModel):
  faqs: list[KnowledgeIngestFaqPayload] = Field(default_factory=list)
  manuals: list[KnowledgeIngestManualPayload] = Field(default_factory=list)


class KnowledgeIngestMessage(MqModel):
  job_id: str
  request_id: str
  user_id: int
  payload: KnowledgeIngestPayload = Field(default_factory=KnowledgeIngestPayload)


class DraftGenerateMessage(MqModel):
  job_id: str
  request_id: str
  user_id: int
  mode: str = "generate"
  category_id: int | None = None
  category_name: str
  category_keywords: list[str] = Field(default_factory=list)
  industry_type: str | None = None
  company_description: str | None = None
  email_tone: str | None = None
  rag_context: str | None = None
  template_count: int = 1


class TemplateIndexPayloadItem(MqModel):
  template_id: int
  title: str
  category_name: str
  canonical_text: str | None = None
  email_tone: str | None = None
  metadata: TemplateMetadataInput = Field(default_factory=TemplateMetadataInput)


class TemplateIndexPayload(MqModel):
  templates: list[TemplateIndexPayloadItem] = Field(default_factory=list)
  delete_template_ids: list[int] = Field(default_factory=list)


class TemplateIndexMessage(MqModel):
  request_id: str
  user_id: int
  payload: TemplateIndexPayload = Field(default_factory=TemplateIndexPayload)


class TemplateMatchPayload(MqModel):
  emailId: str
  subject: str
  body: str
  summary: str | None = None
  intent: str | None = None
  domain: str | None = None
  top_k: int = 5


class TemplateMatchMessage(MqModel):
  job_id: str
  request_id: str
  user_id: int
  payload: TemplateMatchPayload


class ErrorPayload(MqModel):
  code: str
  message: str


class ProgressEvent(MqModel):
  job_id: str
  request_id: str
  user_id: int
  job_type: str
  target_type: str | None = None
  target_id: str | None = None
  status: str
  progress_step: str
  progress_message: str
  payload: dict[str, Any] | None = None
  error: ErrorPayload | None = None
