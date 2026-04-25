import os
from pathlib import Path

os.environ["EMBEDDING_BACKEND"] = "hash"

from app.main import ingest_knowledge, index_knowledge, match_templates
from app.schemas import (
  KnowledgeDocument,
  KnowledgeIndexRequest,
  KnowledgeIngestRequest,
  KnowledgeManualInput,
  TemplateIndexItem,
  TemplateIndexRequest,
  TemplateMatchRequest,
)


def test_knowledge_index_skips_profile_documents():
  response = index_knowledge(
    KnowledgeIndexRequest(
      user_id=1,
      documents=[
        KnowledgeDocument(
          source_type="profile",
          source_id="profile-1",
          content="회사소개 본문",
        ),
        KnowledgeDocument(
          source_type="faq",
          source_id="faq-1",
          content="질문: 환불은 언제 가능한가요?\n답변: 결제 후 7일 이내 가능합니다.",
        ),
      ],
    )
  )

  assert response.indexed_document_count == 1
  assert response.indexed_chunk_count >= 1
  assert all(chunk.source_type != "profile" for chunk in response.chunks)


def test_knowledge_ingest_reads_local_manual_file(tmp_path):
  import pymupdf

  manual_path = tmp_path / "guide.pdf"
  document = pymupdf.open()
  page = document.new_page()
  page.insert_text((72, 72), "환불은 결제 후 7일 이내 가능합니다.")
  manual_path.write_bytes(document.tobytes())
  document.close()

  response = ingest_knowledge(
    KnowledgeIngestRequest(
      request_id="req-ingest-1",
      user_id=1,
      manuals=[
        KnowledgeManualInput(
          source_id="manual-1",
          file_name="guide.pdf",
          local_path=str(manual_path),
        )
      ],
    )
  )

  assert response.status == "SUCCESS"
  assert response.indexed_document_count == 1
  assert response.indexed_chunk_count >= 1
  assert response.indexed_source_ids == ["manual-1"]


def test_knowledge_ingest_reads_presigned_url(monkeypatch):
  def _stub_extract_text_from_url(url: str, file_name: str, media_type: str) -> str:
    assert url == "https://example.com/manual.pdf?signature=stub"
    assert file_name == "manual.pdf"
    assert media_type == "application/pdf"
    return "계약 해지는 담당자 승인 후 처리됩니다."

  monkeypatch.setattr("app.main.extract_text_from_url", _stub_extract_text_from_url)

  response = ingest_knowledge(
    KnowledgeIngestRequest(
      request_id="req-ingest-url",
      user_id=1,
      manuals=[
        KnowledgeManualInput(
          source_id="manual-url-1",
          file_name="manual.pdf",
          presigned_url="https://example.com/manual.pdf?signature=stub",
        )
      ],
    )
  )

  assert response.status == "SUCCESS"
  assert response.indexed_document_count == 1
  assert response.indexed_chunk_count >= 1
  assert response.indexed_source_ids == ["manual-url-1"]


def test_templates_match_builds_canonical_text_from_email_fields():
  from app.main import index_templates

  index_templates(
    TemplateIndexRequest(
      request_id="req-template-index",
      user_id=1,
      templates=[
        TemplateIndexItem(
          template_id=101,
          title="견적 요청 답변",
          intent="견적 요청",
          canonical_text="subject: 견적 문의\nsummary: 엔터프라이즈 플랜 견적 요청\nintent: 견적 요청",
        )
      ],
    )
  )

  response = match_templates(
    TemplateMatchRequest(
      request_id="req-match-1",
      emailId="email-1",
      user_id=1,
      subject="견적 문의",
      body="엔터프라이즈 플랜의 가격과 도입 절차를 알려주세요.",
      summary="엔터프라이즈 플랜 견적 요청",
      intent="견적 요청",
      domain="Sales",
    )
  )

  assert response.results
  assert response.results[0].template_id == 101
