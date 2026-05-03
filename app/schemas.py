from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


# source_type은 현재 RAG가 이해하는 지식 원본의 구분이다.
# profile은 고정 문맥 성격의 데이터, faq는 Q/A 데이터,
# manual은 업로드된 문서형 지식으로 본다.
SourceType = Literal["profile", "faq", "manual"]


class ApiModel(BaseModel):
  # 1. 공통 API 모델 설정
  # alias 입력을 허용해 Backend/AI naming 차이를 흡수한다.
  # 예: email_tone / mail_tone, category_name / intent
  model_config = ConfigDict(populate_by_name=True, extra="ignore")


# 2. 서버 상태 확인 스키마
class HealthResponse(ApiModel):
  status: str
  embedding_backend: str
  vector_backend: str


# 3. 원본 파일 텍스트 추출 스키마
class ExtractTextRequest(ApiModel):
  request_id: str | None = None
  file_name: str
  media_type: str
  content_base64: str


class ExtractTextResponse(ApiModel):
  request_id: str | None = None
  file_name: str
  media_type: str
  extracted_text: str
  char_count: int


# 4. knowledge index 입력 스키마
# profile, faq, manual처럼 출처가 다른 텍스트를 공통 형식으로 받는다.
class KnowledgeFaqInput(ApiModel):
  # FAQ는 질문과 답변이 분리돼 저장되므로
  # ingest 단계에서 하나의 검색용 본문으로 다시 합쳐진다.
  source_id: str
  question: str
  answer: str
  title: str | None = None
  metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeManualInput(ApiModel):
  # 현재 manual 업로드는 PDF만 받는다고 가정한다.
  # 따라서 외부 계약에서는 type 필드를 받지 않고,
  # RAG 내부에서 application/pdf로 고정 처리한다.
  # local_path는 로컬 테스트용, presigned_url은 운영용 업로드 경로를 상정한다.
  source_id: str
  file_name: str
  local_path: str | None = None
  presigned_url: str | None = None
  title: str | None = None
  metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIngestRequest(ApiModel):
  # Backend가 가장 단순하게 호출할 외부 진입점이다.
  # FAQ와 manual을 한 번에 받아 내부 index 단계로 내려보낸다.
  request_id: str | None = None
  user_id: int
  faqs: list[KnowledgeFaqInput] = Field(default_factory=list)
  manuals: list[KnowledgeManualInput] = Field(default_factory=list)
  metadata: dict[str, Any] = Field(default_factory=dict)
  chunk_size: int | None = None
  chunk_overlap: int | None = None


class KnowledgeIngestResponse(ApiModel):
  request_id: str | None = None
  user_id: int
  status: str
  indexed_document_count: int
  indexed_chunk_count: int
  indexed_source_ids: list[str]


class KnowledgeDocument(ApiModel):
  # 실제 chunk/embedding/index 단계에서 쓰는 내부 공통 문서 형식이다.
  source_type: SourceType
  source_id: str
  title: str | None = None
  file_name: str | None = None
  media_type: str | None = None
  content: str = Field(min_length=1)
  intent: str | None = Field(
    default=None,
    validation_alias=AliasChoices("intent", "category_name"),
  )
  metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIndexRequest(ApiModel):
  request_id: str | None = None
  user_id: int
  documents: list[KnowledgeDocument]
  chunk_size: int | None = None
  chunk_overlap: int | None = None


class IndexedChunkSummary(ApiModel):
  record_id: str
  source_type: SourceType
  source_id: str
  chunk_index: int


class KnowledgeIndexResponse(ApiModel):
  request_id: str | None = None
  user_id: int
  indexed_document_count: int
  indexed_chunk_count: int
  chunks: list[IndexedChunkSummary]


# 5. knowledge search 요청/응답 스키마
class KnowledgeSearchRequest(ApiModel):
  # query_text는 검색 순간에만 임베딩되는 입력 텍스트다.
  # 저장된 chunk 벡터와 비교하기 위한 "질문" 역할을 한다.
  request_id: str | None = None
  user_id: int
  query_text: str = Field(min_length=1)
  top_k: int = Field(default=5, ge=1, le=20)
  source_types: list[SourceType] | None = None
  intent: str | None = Field(
    default=None,
    validation_alias=AliasChoices("intent", "category_name"),
  )


class KnowledgeSearchResult(ApiModel):
  record_id: str
  score: float
  source_type: SourceType
  source_id: str
  chunk_text: str
  title: str | None = None
  intent: str | None = None
  metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeSearchResponse(ApiModel):
  request_id: str | None = None
  user_id: int
  query_text: str
  results: list[KnowledgeSearchResult]


# 6. 이메일 canonical text 스키마
class EmailCanonicalizationRequest(ApiModel):
  request_id: str | None = None
  emailId: str | None = None
  threadId: str | None = None
  subject: str
  body: str = Field(validation_alias=AliasChoices("body", "body_clean"))
  summary: str | None = None
  keyphrases: list[str] = Field(default_factory=list)
  domain: str | None = None
  intent: str | None = None
  entities: dict[str, Any] = Field(default_factory=dict)


class CanonicalTextResponse(ApiModel):
  request_id: str | None = None
  emailId: str | None = None
  canonical_text: str


# 7. 템플릿 metadata / canonical text / index 스키마
class TemplateMetadataInput(ApiModel):
  # 템플릿 메타데이터는 "언제 어떤 템플릿을 써야 하는가"를 설명하는 값이다.
  # 최종 본문보다 retrieval 품질에 더 직접적으로 영향을 준다.
  template_purpose: str | None = None
  recommended_situations: list[str] = Field(default_factory=list)
  required_points: list[str] = Field(default_factory=list)
  must_avoid: list[str] = Field(default_factory=list)
  variables: list[str] = Field(default_factory=list)
  semantic_keywords: list[str] = Field(default_factory=list)
  negative_keywords: list[str] = Field(default_factory=list)
  search_summary: str | None = None


class TemplateCanonicalizationRequest(ApiModel):
  request_id: str | None = None
  title: str
  intent: str = Field(validation_alias=AliasChoices("intent", "category_name"))
  mail_tone: str | None = Field(
    default=None,
    validation_alias=AliasChoices("mail_tone", "email_tone"),
  )
  domain: str | None = None
  metadata: TemplateMetadataInput = Field(default_factory=TemplateMetadataInput)


class TemplateIndexItem(ApiModel):
  # canonical_text는 optional이다.
  # Backend가 안 보내면 RAG가 내부에서 title/intent/metadata로 다시 만든다.
  template_id: int
  title: str
  intent: str = Field(validation_alias=AliasChoices("intent", "category_name"))
  canonical_text: str | None = None
  mail_tone: str | None = Field(
    default=None,
    validation_alias=AliasChoices("mail_tone", "email_tone"),
  )
  domain: str | None = None
  metadata: TemplateMetadataInput = Field(default_factory=TemplateMetadataInput)


class TemplateIndexRequest(ApiModel):
  request_id: str | None = None
  user_id: int
  templates: list[TemplateIndexItem]
  delete_template_ids: list[int] = Field(default_factory=list)


class TemplateIndexResponse(ApiModel):
  request_id: str | None = None
  user_id: int
  indexed_template_count: int
  deleted_template_count: int = 0
  template_ids: list[int]


# 8. 템플릿 매칭 스키마
class TemplateMatchRequest(ApiModel):
  # match는 두 방식 모두 허용한다.
  # 1) canonical_text를 직접 전달
  # 2) subject/body/summary를 전달하면 RAG가 내부에서 canonicalize 수행
  request_id: str | None = None
  emailId: str | None = None
  user_id: int
  canonical_text: str | None = None
  subject: str | None = None
  body: str | None = Field(default=None, validation_alias=AliasChoices("body", "body_clean"))
  summary: str | None = None
  top_k: int = Field(default=5, ge=1, le=20)
  intent: str | None = Field(
    default=None,
    validation_alias=AliasChoices("intent", "category_name"),
  )
  mail_tone: str | None = Field(
    default=None,
    validation_alias=AliasChoices("mail_tone", "email_tone"),
  )
  domain: str | None = None
  entities: dict[str, Any] = Field(default_factory=dict)
  keyphrases: list[str] = Field(default_factory=list)


class TemplateMatchCandidate(ApiModel):
  template_id: int
  title: str
  intent: str
  score: float
  mail_tone: str | None = None
  domain: str | None = None
  metadata: dict[str, Any] = Field(default_factory=dict)


class TemplateMatchResponse(ApiModel):
  request_id: str | None = None
  emailId: str | None = None
  user_id: int
  results: list[TemplateMatchCandidate]
