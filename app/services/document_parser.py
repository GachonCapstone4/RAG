from __future__ import annotations

import base64
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib import request as urllib_request

import pymupdf

from app.config import settings

PDF_HEADER_BAND_RATIO = 0.12
PDF_FOOTER_BAND_RATIO = 0.10
PDF_REPEATED_LINE_MIN_PAGES = 3
PDF_REPEATED_LINE_MIN_RATIO = 0.6
PDF_REPEATED_LINE_MAX_LENGTH = 120
PDF_NON_KNOWLEDGE_PAGE_MARKERS = (
  ("문서 기본 정보", "결재"),
  ("문서 통제 정보", "개정 이력"),
  ("Document basic info", "Approval", "Signature"),
  ("Document Control", "Revision History"),
  ("Document Control", "Approval"),
)
PDF_PAGE_NUMBER_PATTERN = re.compile(
  r"^(?:page\s*)?\d{1,4}\s*(?:/|of)\s*\d{1,4}$|^-?\s*\d{1,4}\s*-?$",
  re.IGNORECASE,
)
PDF_PROTECTED_LINE_PREFIXES = (
  "SECTION ",
  "Q.",
  "A.",
  "질문:",
  "답변:",
)


@dataclass(frozen=True)
class PdfTextLine:
  text: str
  top: float
  bottom: float


@dataclass(frozen=True)
class PdfPageText:
  lines: list[PdfTextLine]
  page_height: float


class UnsupportedDocumentError(ValueError):
  # 지원하지 않는 파일 형식일 때 400 계열 응답으로 연결하기 쉬운 예외다.
  pass


def extract_text_from_file_path(file_path: str, file_name: str | None = None, media_type: str | None = None) -> str:
  # local_path 기반 ingest에서 사용하는 진입점이다.
  # 운영 환경에선 presigned URL 다운로드 단계가 추가될 수 있다.
  path = Path(file_path)
  if not path.exists():
    raise FileNotFoundError(f"Document file not found: {file_path}")

  resolved_file_name = file_name or path.name
  resolved_media_type = media_type or _guess_media_type(path.suffix.lower())
  return _extract_text_from_bytes(
    raw_bytes=path.read_bytes(),
    file_name=resolved_file_name,
    media_type=resolved_media_type,
  )


def extract_text_from_payload(file_name: str, media_type: str, content_base64: str) -> str:
  # 외부 API에서 base64 파일 바이트를 직접 받을 때 사용하는 경로다.
  return _extract_text_from_bytes(
    raw_bytes=base64.b64decode(content_base64),
    file_name=file_name,
    media_type=media_type,
  )


def extract_text_from_url(url: str, file_name: str, media_type: str) -> str:
  # 운영 환경에서는 Backend가 S3 presigned URL을 전달하고,
  # RAG는 AWS 키 없이 임시 URL에서 파일 bytes만 내려받는다.
  raw_bytes = _download_with_limit(url)
  return _extract_text_from_bytes(
    raw_bytes=raw_bytes,
    file_name=file_name,
    media_type=media_type,
  )


def _download_with_limit(url: str) -> bytes:
  request = urllib_request.Request(url, headers={"User-Agent": "emailassist-rag/0.1"})
  with urllib_request.urlopen(request, timeout=30) as response:
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > settings.max_upload_bytes:
      raise ValueError(f"Document download exceeds max_upload_bytes: {content_length}")

    chunks: list[bytes] = []
    total_size = 0
    while True:
      chunk = response.read(64 * 1024)
      if not chunk:
        break
      total_size += len(chunk)
      if total_size > settings.max_upload_bytes:
        raise ValueError(f"Document download exceeds max_upload_bytes: {total_size}")
      chunks.append(chunk)

  return b"".join(chunks)


def _extract_text_from_bytes(*, raw_bytes: bytes, file_name: str, media_type: str) -> str:
  suffix = Path(file_name).suffix.lower()

  if media_type == "text/plain" or suffix == ".txt":
    return raw_bytes.decode("utf-8", errors="ignore").strip()

  if media_type == "application/pdf" or suffix == ".pdf":
    return _extract_text_from_pdf(raw_bytes)

  raise UnsupportedDocumentError(
    f"Unsupported file type for RAG extraction: media_type={media_type}, suffix={suffix}"
  )


def _guess_media_type(suffix: str) -> str:
  if suffix == ".pdf":
    return "application/pdf"
  if suffix == ".txt":
    return "text/plain"
  return "application/octet-stream"


def _extract_text_from_pdf(raw_bytes: bytes) -> str:
  # PyMuPDF는 속도와 추출 품질이 비교적 안정적이고,
  # 페이지별 이미지 개수도 함께 확인할 수 있어 이미지형 PDF 감지에 유리하다.
  document = pymupdf.open(stream=raw_bytes, filetype="pdf")

  try:
    pdf_pages: list[PdfPageText] = []
    image_page_count = 0

    for page in document:
      page_text = _extract_pdf_page_text(page)
      image_count = len(page.get_images(full=True))

      if image_count > 0:
        image_page_count += 1

      if page_text.lines:
        pdf_pages.append(page_text)

    page_texts = _clean_pdf_page_texts(pdf_pages)
    total_text_length = sum(len(page_text) for page_text in page_texts)

    extracted_text = "\n\n".join(page_texts).strip()
    looks_like_image_pdf = _looks_like_image_pdf(
      page_count=document.page_count,
      image_page_count=image_page_count,
      total_text_length=total_text_length,
    )

    if extracted_text and not looks_like_image_pdf:
      return extracted_text

    # 이미지 비중이 높고 텍스트가 거의 없으면 스캔본으로 보고
    # 설정이 켜져 있을 때만 Tesseract OCR fallback을 시도한다.
    if image_page_count > 0 or looks_like_image_pdf:
      if settings.pdf_ocr_enabled:
        return _extract_text_from_pdf_with_tesseract(raw_bytes)
      raise UnsupportedDocumentError(
        "이미지형 PDF로 보입니다. 현재 OCR fallback이 비활성화되어 있으므로 "
        "텍스트 기반 PDF를 업로드하거나 OCR 설정을 활성화해주세요."
      )

    raise UnsupportedDocumentError("PDF에서 추출 가능한 텍스트를 찾지 못했습니다.")
  finally:
    document.close()


def _extract_pdf_page_text(page: pymupdf.Page) -> PdfPageText:
  text_dict = page.get_text("dict")
  lines: list[PdfTextLine] = []

  for block in text_dict.get("blocks", []):
    if not isinstance(block, dict) or block.get("type") != 0:
      continue
    for line in block.get("lines", []):
      spans = line.get("spans", []) if isinstance(line, dict) else []
      text = _normalize_pdf_line("".join(str(span.get("text", "")) for span in spans if isinstance(span, dict)))
      if not text:
        continue
      bbox = line.get("bbox", [0, 0, 0, 0])
      lines.append(PdfTextLine(text=text, top=float(bbox[1]), bottom=float(bbox[3])))

  if not lines:
    fallback_lines = [
      _normalize_pdf_line(line)
      for line in page.get_text("text").splitlines()
      if _normalize_pdf_line(line)
    ]
    lines = [PdfTextLine(text=line, top=0.0, bottom=0.0) for line in fallback_lines]

  return PdfPageText(lines=lines, page_height=float(page.rect.height))


def _clean_pdf_page_texts(pages: list[PdfPageText]) -> list[str]:
  repeated_margin_lines = _find_repeated_margin_lines(pages)
  cleaned_pages: list[str] = []

  for page in pages:
    lines = [
      line.text
      for line in page.lines
      if not _is_removable_pdf_noise_line(line, page.page_height, repeated_margin_lines)
    ]
    page_text = "\n".join(lines).strip()
    if page_text and not _is_non_knowledge_pdf_page(page_text):
      cleaned_pages.append(page_text)

  return cleaned_pages


def _find_repeated_margin_lines(pages: list[PdfPageText]) -> set[str]:
  if len(pages) < PDF_REPEATED_LINE_MIN_PAGES:
    return set()

  line_pages: dict[str, set[int]] = defaultdict(set)
  for page_index, page in enumerate(pages):
    for line in page.lines:
      if not _is_margin_line(line, page.page_height):
        continue
      key = _repeated_line_key(line.text)
      if key:
        line_pages[key].add(page_index)

  required_count = max(
    PDF_REPEATED_LINE_MIN_PAGES,
    int(len(pages) * PDF_REPEATED_LINE_MIN_RATIO + 0.999),
  )
  return {key for key, indexes in line_pages.items() if len(indexes) >= required_count}


def _is_removable_pdf_noise_line(line: PdfTextLine, page_height: float, repeated_margin_lines: set[str]) -> bool:
  if _is_footer_line(line, page_height) and _is_page_number_line(line.text):
    return True

  key = _repeated_line_key(line.text)
  return bool(key and key in repeated_margin_lines and _is_margin_line(line, page_height))


def _is_margin_line(line: PdfTextLine, page_height: float) -> bool:
  return _is_header_line(line, page_height) or _is_footer_line(line, page_height)


def _is_header_line(line: PdfTextLine, page_height: float) -> bool:
  return line.top <= page_height * PDF_HEADER_BAND_RATIO


def _is_footer_line(line: PdfTextLine, page_height: float) -> bool:
  return line.bottom >= page_height * (1 - PDF_FOOTER_BAND_RATIO)


def _is_page_number_line(text: str) -> bool:
  return bool(PDF_PAGE_NUMBER_PATTERN.match(text.strip()))


def _repeated_line_key(text: str) -> str | None:
  normalized = _normalize_pdf_line(text)
  if not normalized or len(normalized) > PDF_REPEATED_LINE_MAX_LENGTH:
    return None
  if any(normalized.startswith(prefix) for prefix in PDF_PROTECTED_LINE_PREFIXES):
    return None
  if _is_page_number_line(normalized):
    return None
  return normalized.casefold()


def _normalize_pdf_line(text: str) -> str:
  return re.sub(r"[ \t]+", " ", text.replace("\u00a0", " ")).strip()


def _is_non_knowledge_pdf_page(text: str) -> bool:
  if not text:
    return True
  if _has_knowledge_line_marker(text):
    return False
  normalized = text.casefold()
  return any(
    all(marker.casefold() in normalized for marker in markers)
    for markers in PDF_NON_KNOWLEDGE_PAGE_MARKERS
  )


def _has_knowledge_line_marker(text: str) -> bool:
  return any(marker in text for marker in PDF_PROTECTED_LINE_PREFIXES)


def _looks_like_image_pdf(*, page_count: int, image_page_count: int, total_text_length: int) -> bool:
  # 아래 기준은 완벽한 판별이 아니라 실무적인 휴리스틱이다.
  # 페이지 대부분이 이미지이고 텍스트 길이가 매우 짧으면 스캔본일 가능성이 높다고 본다.
  if page_count <= 0:
    return False

  image_heavy = image_page_count >= max(1, page_count // 2)
  text_too_short = total_text_length < 40
  return image_heavy and text_too_short


def _extract_text_from_pdf_with_tesseract(raw_bytes: bytes) -> str:
  # PyMuPDF의 OCR 진입점을 사용해 페이지 단위 OCR을 수행한다.
  # Tesseract가 설치되어 있지 않거나 언어팩이 없으면 예외를 명확히 안내한다.
  document = pymupdf.open(stream=raw_bytes, filetype="pdf")

  try:
    page_texts: list[str] = []

    for page in document:
      text_page = page.get_textpage_ocr(
        language=settings.pdf_ocr_languages,
        dpi=settings.pdf_ocr_dpi,
      )
      page_text = text_page.extractText().strip()
      if page_text:
        page_texts.append(page_text)

    extracted_text = "\n\n".join(page_texts).strip()
    if extracted_text:
      return extracted_text

    raise UnsupportedDocumentError(
      "OCR을 수행했지만 PDF에서 추출 가능한 텍스트를 찾지 못했습니다."
    )
  except UnsupportedDocumentError:
    raise
  except Exception as error:
    raise UnsupportedDocumentError(
      "이미지형 PDF OCR에 실패했습니다. Tesseract가 설치되어 있고 "
      f"언어 데이터({settings.pdf_ocr_languages})를 사용할 수 있는지 확인해주세요. "
      f"원본 오류: {error}"
    ) from error
  finally:
    document.close()
