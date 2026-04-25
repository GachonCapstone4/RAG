from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
import logging
logging.basicConfig(level=logging.INFO)

from app.config import settings
from app.schemas import (
  CanonicalTextResponse,
  EmailCanonicalizationRequest,
  ExtractTextRequest,
  ExtractTextResponse,
  HealthResponse,
  KnowledgeFaqInput,
  KnowledgeIngestRequest,
  KnowledgeIngestResponse,
  IndexedChunkSummary,
  KnowledgeIndexRequest,
  KnowledgeIndexResponse,
  KnowledgeSearchRequest,
  KnowledgeSearchResponse,
  KnowledgeSearchResult,
  TemplateCanonicalizationRequest,
  TemplateIndexRequest,
  TemplateIndexResponse,
  TemplateMatchCandidate,
  TemplateMatchRequest,
  TemplateMatchResponse,
)
from app.services.canonicalization import build_email_canonical_text, build_template_canonical_text
from app.services.document_chunking import ChunkingDocument, split_document
from app.services.document_parser import (
  UnsupportedDocumentError,
  extract_text_from_file_path,
  extract_text_from_payload,
  extract_text_from_url,
)
from app.services.index_store import IndexRecord
from app.services.runtime import embedding_service, knowledge_namespace, template_namespace, vector_store


def _check_rabbitmq_on_startup() -> None:
  try:
    import pika
  except ImportError:
    logger.warning("RabbitMQ 연결 체크 생략: pika 패키지가 없습니다.")
    return

  inbound_queues = [
    settings.queue_knowledge_ingest_inbound,
    settings.queue_template_index_inbound,
    settings.queue_template_match_inbound,
    settings.queue_draft_inbound,
  ]

  try:
    connection = pika.BlockingConnection(
      pika.ConnectionParameters(
        host=settings.rabbitmq_host,
        port=settings.rabbitmq_port,
        virtual_host=settings.rabbitmq_virtual_host,
        credentials=pika.PlainCredentials(settings.rabbitmq_username, settings.rabbitmq_password),
        heartbeat=settings.rabbitmq_heartbeat_seconds,
        blocked_connection_timeout=settings.rabbitmq_blocked_connection_timeout_seconds,
      )
    )
  except Exception as error:
    logger.error(
      "RabbitMQ 연결 실패: host=%s port=%s vhost=%s error=%s",
      settings.rabbitmq_host,
      settings.rabbitmq_port,
      settings.rabbitmq_virtual_host,
      error,
    )
    return

  logger.info(
    "RabbitMQ 연결 성공: host=%s port=%s vhost=%s user=%s",
    settings.rabbitmq_host,
    settings.rabbitmq_port,
    settings.rabbitmq_virtual_host,
    settings.rabbitmq_username,
  )

  channel = connection.channel()
  for queue_name in inbound_queues:
    try:
      # passive=True: 큐가 이미 존재하는지만 확인하고 선언하지 않는다.
      channel.queue_declare(queue=queue_name, passive=True)
      logger.info("Queue 확인 완료: queue=%s", queue_name)
    except Exception as error:
      logger.error("Queue 확인 실패: queue=%s error=%s", queue_name, error)
      # 채널이 닫혔으므로 재생성해서 다음 큐 확인을 이어간다.
      channel = connection.channel()

  connection.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
  _check_rabbitmq_on_startup()
  yield


# 1. 앱 전역 공통 객체 초기화
app = FastAPI(title="EmailAssist RAG", version="0.1.0", lifespan=lifespan)
logger = logging.getLogger(__name__)
PDF_MEDIA_TYPE = "application/pdf"


def _is_indexable_knowledge_document(source_type: str) -> bool:
  # 회사소개는 retrieval 대상이 아니라 draft의 고정 문맥으로 다룬다.
  # 현재 knowledge index에는 FAQ와 업로드 문서(manual)만 올린다.
  return source_type in {"faq", "manual"}


def _index_knowledge_documents(
  request_id: str | None,
  user_id: int,
  documents,
  *,
  chunk_size: int | None = None,
  chunk_overlap: int | None = None,
) -> KnowledgeIndexResponse:
  # 외부용 ingest API와 내부용 index API를 연결하는 얇은 어댑터다.
  # 외부 계약은 단순하게 유지하고, 실제 chunk/index 구현은 재사용한다.
  request = KnowledgeIndexRequest(
    request_id=request_id,
    user_id=user_id,
    documents=documents,
    chunk_size=chunk_size,
    chunk_overlap=chunk_overlap,
  )
  return index_knowledge(request)


def _faq_to_content(faq: KnowledgeFaqInput) -> str:
  # FAQ는 질문과 답변을 함께 봐야 검색 의미가 안정적이어서
  # 하나의 본문 텍스트로 합쳐 임베딩한다.
  return f"질문: {faq.question}\n답변: {faq.answer}".strip()


# 2. 상태 확인 엔드포인트
@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
  # 현재 어떤 embedding/vector backend가 실제 적용됐는지 보여주면
  # 배포 환경 확인과 디버깅이 훨씬 쉬워진다.
  return HealthResponse(
    status="ok",
    embedding_backend=settings.embedding_backend,
    vector_backend=vector_store.backend_name(),
  )


# 3. 텍스트 추출 엔드포인트
@app.post("/extract-text", response_model=ExtractTextResponse)
@app.post("/v1/resources/extract-text", response_model=ExtractTextResponse)
def extract_text(request: ExtractTextRequest) -> ExtractTextResponse:
  # Backend가 원본 파일을 저장한 뒤 파싱 대상 바이트를 이 엔드포인트로 전달한다.
  # RAG는 텍스트 추출 책임만 가지며 원본 파일 보관 책임은 갖지 않는다.
  try:
    extracted_text = extract_text_from_payload(
      file_name=request.file_name,
      media_type=request.media_type,
      content_base64=request.content_base64,
    )
  except UnsupportedDocumentError as error:
    raise HTTPException(status_code=400, detail=str(error)) from error
  except Exception as error:  # pragma: no cover
    raise HTTPException(status_code=500, detail=f"Failed to extract text: {error}") from error

  return ExtractTextResponse(
    request_id=request.request_id,
    file_name=request.file_name,
    media_type=request.media_type,
    extracted_text=extracted_text,
    char_count=len(extracted_text),
  )


# 4. canonical text 생성 엔드포인트
@app.post("/canonical/email", response_model=CanonicalTextResponse)
@app.post("/v1/canonical/email", response_model=CanonicalTextResponse)
def canonicalize_email(request: EmailCanonicalizationRequest) -> CanonicalTextResponse:
  # Email canonical text는 embedding과 template matching이 함께 쓰는 공통 표현이다.
  # 이 조합 규칙을 한곳에 두어야 Backend와 AI의 해석이 어긋나지 않는다.
  return CanonicalTextResponse(
    request_id=request.request_id,
    emailId=request.emailId,
    canonical_text=build_email_canonical_text(
      subject=request.subject,
      body_clean=request.body,
      summary=request.summary,
      keyphrases=request.keyphrases,
      domain=request.domain,
      intent=request.intent,
      entities=request.entities,
    )
  )


@app.post("/canonical/template", response_model=CanonicalTextResponse)
@app.post("/v1/canonical/template", response_model=CanonicalTextResponse)
def canonicalize_template(request: TemplateCanonicalizationRequest) -> CanonicalTextResponse:
  # Template canonical text는 본문보다 metadata 중심으로 만든다.
  # 템플릿 검색은 최종 답장 문장보다 "언제 써야 하는 템플릿인지"를 더 잘 반영해야 한다.
  return CanonicalTextResponse(
    request_id=request.request_id,
    canonical_text=build_template_canonical_text(
      title=request.title,
      category_name=request.intent,
      email_tone=request.mail_tone,
      domain=request.domain,
      metadata=request.metadata,
    )
  )


# 5. knowledge index 엔드포인트
@app.post("/knowledge/ingest", response_model=KnowledgeIngestResponse)
@app.post("/v1/knowledge/ingest", response_model=KnowledgeIngestResponse)
def ingest_knowledge(request: KnowledgeIngestRequest) -> KnowledgeIngestResponse:
  # FAQ/manual을 한 번에 적재하는 최종 외부 API다.
  # Backend는 이 endpoint만 알면 되고, 내부에서 extract/index 세부 단계를 감춘다.
  documents = []

  for faq in request.faqs:
    documents.append(
      {
        "source_type": "faq",
        "source_id": faq.source_id,
        "title": faq.title or faq.question,
        "content": _faq_to_content(faq),
        "metadata": faq.metadata,
      }
    )

  for manual in request.manuals:
    if manual.local_path:
      # 로컬 개발 환경에서는 file path 직접 읽기가 가장 단순하다.
      try:
        content = extract_text_from_file_path(
          file_path=manual.local_path,
          file_name=manual.file_name,
          media_type=PDF_MEDIA_TYPE,
        )
      except UnsupportedDocumentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
      except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
      except Exception as error:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Failed to extract text from manual: {error}") from error
    elif manual.presigned_url:
      try:
        content = extract_text_from_url(
          url=manual.presigned_url,
          file_name=manual.file_name,
          media_type=PDF_MEDIA_TYPE,
        )
      except UnsupportedDocumentError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
      except Exception as error:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"Failed to extract text from presigned URL: {error}") from error
    else:
      raise HTTPException(status_code=422, detail="manual은 local_path 또는 presigned_url 중 하나가 필요합니다.")

    if not content.strip():
      # 텍스트 추출은 됐지만 실제 내용이 비어 있는 문서는 검색 효용이 낮으므로 제외한다.
      logger.warning(
        "Skipping manual with empty extracted text: user_id=%s source_id=%s file_name=%s",
        request.user_id,
        manual.source_id,
        manual.file_name,
      )
      continue

    documents.append(
      {
        "source_type": "manual",
        "source_id": manual.source_id,
        "title": manual.title or Path(manual.file_name).stem,
        "file_name": manual.file_name,
        # 현재 ingest 외부 계약에서는 manual을 PDF로 고정한다.
        "media_type": PDF_MEDIA_TYPE,
        "content": content,
        "metadata": manual.metadata,
      }
    )

  index_response = _index_knowledge_documents(
    request_id=request.request_id,
    user_id=request.user_id,
    documents=documents,
    chunk_size=request.chunk_size,
    chunk_overlap=request.chunk_overlap,
  )

  indexed_source_ids = list(dict.fromkeys(chunk.source_id for chunk in index_response.chunks))
  # source 단위 결과를 같이 돌려주면 Backend가 어떤 FAQ/manual이 적재됐는지 이해하기 쉽다.
  return KnowledgeIngestResponse(
    request_id=index_response.request_id,
    user_id=index_response.user_id,
    status="SUCCESS",
    indexed_document_count=index_response.indexed_document_count,
    indexed_chunk_count=index_response.indexed_chunk_count,
    indexed_source_ids=indexed_source_ids,
  )


@app.post("/knowledge/index", response_model=KnowledgeIndexResponse)
@app.post("/v1/knowledge/index", response_model=KnowledgeIndexResponse)
def index_knowledge(request: KnowledgeIndexRequest) -> KnowledgeIndexResponse:
  # 실제 chunk -> embedding -> vector upsert가 일어나는 핵심 endpoint다.
  chunk_size = request.chunk_size or settings.default_chunk_size
  chunk_overlap = request.chunk_overlap or settings.default_chunk_overlap

  records: list[IndexRecord] = []
  summaries: list[IndexedChunkSummary] = []
  indexed_document_count = 0

  for document in request.documents:
    if not _is_indexable_knowledge_document(document.source_type):
      # profile은 retrieval 대상이 아니라 고정 문맥으로 쓰기로 했기 때문에 건너뛴다.
      continue

    # 문서 전체를 한 덩어리로 두지 않고 여러 검색 단위로 나눠야
    # 이후 retrieval에서 실제로 필요한 부분만 반환할 수 있다.
    chunks = split_document(
      ChunkingDocument(
        source_type=document.source_type,
        source_id=document.source_id,
        title=document.title,
        file_name=document.file_name,
        media_type=document.media_type,
        content=document.content,
        metadata=document.metadata,
      ),
      embedding_service=embedding_service,
      chunking_backend=settings.chunking_backend,
      pdf_chunking_strategy=settings.pdf_chunking_strategy,
      max_chars=chunk_size,
      overlap=chunk_overlap,
    )
    if not chunks:
      continue

    indexed_document_count += 1
    vectors = embedding_service.embed_texts(chunks)
    for index, chunk in enumerate(chunks):
      # record_id에 source_id와 chunk index를 함께 넣어
      # 특정 문서를 부분 삭제/재색인할 때 다시 찾기 쉽게 만든다.
      record_id = f"{document.source_type}:{document.source_id}:{index}"
      payload = {
        "user_id": request.user_id,
        "source_type": document.source_type,
        "source_id": document.source_id,
        "title": document.title,
        "intent": document.intent,
        "chunk_text": chunk,
        "metadata": document.metadata,
        "chunk_index": index,
      }
      records.append(IndexRecord(record_id=record_id, vector=vectors[index], payload=payload))
      summaries.append(
        IndexedChunkSummary(
          record_id=record_id,
          source_type=document.source_type,
          source_id=document.source_id,
          chunk_index=index,
        )
      )

  # 사용자별 namespace를 분리해 같은 프로세스를 공유해도
  # profile/FAQ/manual 데이터가 서로 섞이지 않도록 한다.
  vector_store.upsert(knowledge_namespace(request.user_id), records)

  return KnowledgeIndexResponse(
    request_id=request.request_id,
    user_id=request.user_id,
    indexed_document_count=indexed_document_count,
    indexed_chunk_count=len(records),
    chunks=summaries,
  )


# 7. knowledge search 엔드포인트
@app.post("/knowledge/search", response_model=KnowledgeSearchResponse)
@app.post("/v1/knowledge/search", response_model=KnowledgeSearchResponse)
def search_knowledge(request: KnowledgeSearchRequest) -> KnowledgeSearchResponse:
  # 검색 시점에는 query_text를 즉석에서 임베딩해 저장된 chunk 벡터와 비교한다.
  query_vector = embedding_service.embed_texts([request.query_text])[0]
  # 별도 인덱스를 여러 개 두지 않고도 category/source 힌트로 retrieval 범위를
  # 좁힐 수 있도록 metadata filter를 함께 적용한다.
  filters = {
    "source_type": request.source_types if request.source_types else None,
    "intent": request.intent,
  }
  matches = vector_store.search(
    namespace=knowledge_namespace(request.user_id),
    query_vector=query_vector,
    top_k=request.top_k,
    filters=filters,
  )
  return KnowledgeSearchResponse(
    request_id=request.request_id,
    user_id=request.user_id,
    query_text=request.query_text,
    results=[
      KnowledgeSearchResult(
        record_id=record.record_id,
        score=score,
        source_type=record.payload["source_type"],
        source_id=record.payload["source_id"],
        chunk_text=record.payload["chunk_text"],
        title=record.payload.get("title"),
        intent=record.payload.get("intent"),
        metadata=record.payload.get("metadata", {}),
      )
      for record, score in matches
    ],
  )


# 8. template index 엔드포인트
@app.post("/templates/index", response_model=TemplateIndexResponse)
@app.post("/v1/templates/index", response_model=TemplateIndexResponse)
def index_templates(request: TemplateIndexRequest) -> TemplateIndexResponse:
  # 생성된 템플릿을 "나중에 다시 찾기 위한 검색용 표현"으로 저장하는 단계다.
  records: list[IndexRecord] = []
  template_ids: list[int] = []

  for template in request.templates:
    # Backend가 canonical text를 직접 넣을 수도 있지만
    # 주변 서비스가 아직 완성되지 않은 단계에서도 이 엔드포인트를 쓸 수 있게 fallback을 둔다.
    canonical_text = template.canonical_text or build_template_canonical_text(
      title=template.title,
      category_name=template.intent,
      email_tone=template.mail_tone,
      domain=template.domain,
      metadata=template.metadata,
    )
    vector = embedding_service.embed_texts([canonical_text])[0]
    record_id = f"template:{template.template_id}"
    records.append(
      IndexRecord(
        record_id=record_id,
        vector=vector,
        payload={
          # template_id는 이후 match 결과의 최종 참조 키가 되므로
          # payload 최상위에 반드시 보관한다.
          "template_id": template.template_id,
          "title": template.title,
          "intent": template.intent,
          "mail_tone": template.mail_tone,
          "domain": template.domain,
          "metadata": template.metadata.model_dump(),
          "canonical_text": canonical_text,
        },
      )
    )
    template_ids.append(template.template_id)

  # Template 검색은 knowledge chunk 검색과 목적이 다르므로
  # 전용 namespace에 따로 보관한다.
  vector_store.upsert(template_namespace(request.user_id), records)

  return TemplateIndexResponse(
    request_id=request.request_id,
    user_id=request.user_id,
    indexed_template_count=len(template_ids),
    template_ids=template_ids,
  )


# 9. template match 엔드포인트
@app.post("/templates/match", response_model=TemplateMatchResponse)
@app.post("/v1/templates/match", response_model=TemplateMatchResponse)
def match_templates(request: TemplateMatchRequest) -> TemplateMatchResponse:
  # canonical_text를 직접 받는 경우와,
  # raw email 필드에서 내부적으로 canonicalize 하는 경우를 모두 지원한다.
  canonical_text = request.canonical_text
  if not canonical_text:
    if not request.subject or not request.body:
      raise HTTPException(status_code=422, detail="canonical_text 또는 subject/body 조합이 필요합니다.")
    canonical_text = build_email_canonical_text(
      subject=request.subject,
      body_clean=request.body,
      summary=request.summary,
      keyphrases=request.keyphrases,
      domain=request.domain,
      intent=request.intent,
      entities=request.entities,
    )

  query_vector = embedding_service.embed_texts([canonical_text])[0]
  # 여기서는 candidate template만 반환한다.
  # tone, negative keyword, usage log를 반영한 최종 rerank는 Backend 책임이다.
  primary_filters = {
    # intent/mail_tone/domain은 있을 때 후보군을 줄이는 힌트다.
    # 아래에서 단계적으로 완화해 "결과 0건"을 최대한 줄인다.
    "intent": request.intent,
    "mail_tone": request.mail_tone,
    "domain": request.domain,
  }
  matches = vector_store.search(
    namespace=template_namespace(request.user_id),
    query_vector=query_vector,
    top_k=request.top_k,
    filters=primary_filters,
  )
  if not matches:
    # 1차 fallback: intent만 남기고 다시 검색한다.
    matches = vector_store.search(
      namespace=template_namespace(request.user_id),
      query_vector=query_vector,
      top_k=request.top_k,
      filters={"intent": request.intent},
    )
  if not matches:
    # 2차 fallback: 필터 없이 pure similarity search로 내려간다.
    matches = vector_store.search(
      namespace=template_namespace(request.user_id),
      query_vector=query_vector,
      top_k=request.top_k,
      filters=None,
    )
  return TemplateMatchResponse(
    request_id=request.request_id,
    emailId=request.emailId,
    user_id=request.user_id,
    results=[
      TemplateMatchCandidate(
        template_id=record.payload["template_id"],
        title=record.payload["title"],
        intent=record.payload["intent"],
        score=score,
        mail_tone=record.payload.get("mail_tone"),
        domain=record.payload.get("domain"),
        metadata=record.payload.get("metadata", {}),
      )
      for record, score in matches
    ],
  )
