import base64

import pymupdf
import pytest

from app.services.document_parser import (
  UnsupportedDocumentError,
  extract_text_from_payload,
)


def test_extract_text_from_pdf_with_pymupdf_text_layer():
  document = pymupdf.open()
  page = document.new_page()
  page.insert_text((72, 72), "Refund is available within seven days.")
  pdf_bytes = document.tobytes()
  document.close()

  extracted = extract_text_from_payload(
    file_name="guide.pdf",
    media_type="application/pdf",
    content_base64=base64.b64encode(pdf_bytes).decode("utf-8"),
  )

  assert "Refund" in extracted


def test_extract_text_from_pdf_removes_repeated_margin_noise_and_control_pages():
  document = pymupdf.open()

  cover = document.new_page()
  cover.insert_text((72, 120), "Document basic info\nApproval\nSignature")

  for page_number in range(1, 4):
    section = document.new_page()
    section.insert_text((72, 30), "Reusable Company Manual")
    section.insert_text((72, 48), "Confidential customer response guide")
    section.insert_text(
      (72, 120),
      f"SECTION {page_number}.0\nService category: Quote Request\nTemplate keyword: [QUOTE_REQUEST_{page_number}]",
    )
    section.insert_text((72, 810), f"Page {page_number} of 3")

  pdf_bytes = document.tobytes()
  document.close()

  extracted = extract_text_from_payload(
    file_name="corporate-guide.pdf",
    media_type="application/pdf",
    content_base64=base64.b64encode(pdf_bytes).decode("utf-8"),
  )

  assert "Service category: Quote Request" in extracted
  assert "[QUOTE_REQUEST_1]" in extracted
  assert "[QUOTE_REQUEST_3]" in extracted
  assert "Reusable Company Manual" not in extracted
  assert "Confidential customer response guide" not in extracted
  assert "Page 1 of 3" not in extracted
  assert "Approval" not in extracted


def test_extract_text_from_pdf_keeps_approval_signature_knowledge_page():
  document = pymupdf.open()
  page = document.new_page()
  page.insert_text(
    (72, 120),
    "Approval workflow\nSignature policy\nCustomers can approve quotes by email signature.",
  )
  pdf_bytes = document.tobytes()
  document.close()

  extracted = extract_text_from_payload(
    file_name="approval-guide.pdf",
    media_type="application/pdf",
    content_base64=base64.b64encode(pdf_bytes).decode("utf-8"),
  )

  assert "Approval workflow" in extracted
  assert "email signature" in extracted


def test_extract_text_from_pdf_keeps_repeated_body_lines():
  document = pymupdf.open()

  for page_number in range(1, 4):
    page = document.new_page()
    page.insert_text((72, 30), "Customer Support Handbook")
    page.insert_text((72, 180), "Refund eligibility must be checked before approval.")
    page.insert_text((72, 220), f"SECTION {page_number}.0\nPage-specific refund detail {page_number}")
    page.insert_text((72, 810), f"{page_number} / 3")

  pdf_bytes = document.tobytes()
  document.close()

  extracted = extract_text_from_payload(
    file_name="refund-guide.pdf",
    media_type="application/pdf",
    content_base64=base64.b64encode(pdf_bytes).decode("utf-8"),
  )

  assert "Customer Support Handbook" not in extracted
  assert extracted.count("Refund eligibility must be checked before approval.") == 3
  assert "Page-specific refund detail 1" in extracted
  assert "1 / 3" not in extracted


def test_extract_text_from_pdf_uses_ocr_fallback_for_image_only_pdf(monkeypatch):
  # PyMuPDF가 직접 만든 PNG를 사용해 이미지 손상 이슈 없이 테스트한다.
  pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
  image_bytes = pixmap.tobytes("png")

  document = pymupdf.open()
  page = document.new_page()
  page.insert_image(pymupdf.Rect(72, 72, 220, 220), stream=image_bytes)
  pdf_bytes = document.tobytes()
  document.close()

  monkeypatch.setattr(
    "app.services.document_parser._extract_text_from_pdf_with_tesseract",
    lambda raw_bytes: "OCR fallback result",
  )

  extracted = extract_text_from_payload(
    file_name="scanned.pdf",
    media_type="application/pdf",
    content_base64=base64.b64encode(pdf_bytes).decode("utf-8"),
  )

  assert extracted == "OCR fallback result"


def test_extract_text_from_pdf_raises_helpful_error_when_ocr_fails(monkeypatch):
  pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 10, 10), False)
  image_bytes = pixmap.tobytes("png")

  document = pymupdf.open()
  page = document.new_page()
  page.insert_image(pymupdf.Rect(72, 72, 220, 220), stream=image_bytes)
  pdf_bytes = document.tobytes()
  document.close()

  monkeypatch.setattr(
    "app.services.document_parser._extract_text_from_pdf_with_tesseract",
    lambda raw_bytes: (_ for _ in ()).throw(
      UnsupportedDocumentError("이미지형 PDF OCR에 실패했습니다. Tesseract가 설치되어 있고 언어 데이터를 확인해주세요.")
    ),
  )

  with pytest.raises(UnsupportedDocumentError) as error:
    extract_text_from_payload(
      file_name="scanned.pdf",
      media_type="application/pdf",
      content_base64=base64.b64encode(pdf_bytes).decode("utf-8"),
    )

  assert "Tesseract" in str(error.value)
