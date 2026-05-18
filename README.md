# EmailAssist RAG Server

EmailAssist의 별도 RAG 서버입니다.

이 서비스는 아래 책임에 집중합니다.

- 비즈니스 프로필, FAQ, 업로드 문서 텍스트 정규화
- chunking
- embedding 생성
- knowledge chunk 검색
- template metadata embedding 저장 및 email-template candidate search
- OpenAI 호환 LLM API 기반 생성 영역 관리

현재 상태:

- FastAPI 기반 최소 실행 스캐폴드
- 기본 embedding backend는 외부 OpenAI 호환 임베딩 API
- PDF는 LangChain 기반 semantic chunking을 우선 시도하고, 실패하면 기존 chunking으로 fallback
- PDF 텍스트 추출 시 상단/하단 반복 라인과 하단 단독 페이지 번호를 보수적으로 제거
- 기본 vector backend는 Chroma persistent collection
- 외부 ChromaDB 서버 연결을 위한 `VECTOR_BACKEND=chroma_http` 지원
- `knowledge/ingest`, `templates/index`, `templates/match` 기준 외부 API 정리
- 이미지형 PDF는 Tesseract OCR fallback을 시도
- 온보딩 템플릿 생성 책임을 `RAG` 내부 서비스로 관리
- RabbitMQ worker 기반 `knowledge.ingest / draft / templates.index / templates.match` consume/publish 골격 추가
- 현재 `rag/` 단독 테스트는 가능하지만, MQ end-to-end 송수신 검증은 별도 브로커 환경에서 확인이 필요

## Folder Structure

`rag/`는 크게 `앱 코드`, `테스트`, `내부 문서`, `실행 설정`으로 나뉩니다.

```text
rag/
├─ app/
│  ├─ main.py
│  ├─ worker.py
│  ├─ config.py
│  ├─ schemas.py
│  ├─ mq/
│  │  ├─ __init__.py
│  │  └─ schemas.py
│  └─ services/
│     ├─ canonicalization.py
│     ├─ claude_service.py
│     ├─ chunking.py
│     ├─ draft_service.py
│     ├─ document_chunking.py
│     ├─ document_parser.py
│     ├─ embedding.py
│     └─ index_store.py
├─ tests/
│  ├─ test_canonicalization.py
│  ├─ test_chunking.py
│  ├─ test_document_chunking.py
│  ├─ test_draft_service.py
│  └─ test_index_store.py
├─ docs/
│  ├─ README.md
│  ├─ 연동-개요.md
│  ├─ API-명세.md
│  ├─ RabbitMQ-명세.md
│  ├─ Chroma-저장구조-ERD.md
│  ├─ current-status.md
│  ├─ progress-log.md
│  └─ sample-manual.txt
├─ scripts/
│  └─ check_chroma.py
├─ pyproject.toml
├─ .env.example
├─ Dockerfile
└─ README.md
```

## File Roles

### App

- `app/main.py`
  - FastAPI 엔드포인트 진입점
  - 요청을 받아 canonicalization, indexing, matching 서비스에 연결
- `app/config.py`
  - 환경변수와 기본 설정값 관리
  - embedding backend, chunking backend, chunk size 같은 런타임 설정 보관
- `app/schemas.py`
  - API 요청/응답 스키마 정의
  - `AI/` 레포 naming과의 호환 alias 포함

### Services

- `app/services/canonicalization.py`
  - email canonical text
  - template canonical text 생성 규칙 관리
- `app/services/chunking.py`
  - 기본 native chunking
  - 문단 기반 + 길이 제한 + overlap 처리
- `app/services/claude_service.py`
  - OpenAI 호환 chat completion API 호출과 온보딩 템플릿 생성 프롬프트 관리
  - mock mode 포함
- `app/services/openai_compatible_client.py`
  - 공통 인증 헤더와 `/v1/chat/completions`, `/v1/embeddings` 호출 처리
- `app/services/draft_service.py`
  - 온보딩 템플릿 생성 오케스트레이션
  - variant별 템플릿 배열 조합
- `app/services/document_chunking.py`
  - 문서 종류별 chunking 진입점
  - PDF는 LangChain semantic chunking 우선 시도
  - 실패 시 native chunking으로 fallback
- `app/services/document_parser.py`
  - TXT/PDF 텍스트 추출
  - 현재는 `PyMuPDF` 기반 추출
  - 이미지형 PDF는 Tesseract OCR fallback 시도
- `app/services/embedding.py`
  - OpenAI 호환 embedding API backend 선택 및 생성
  - hash fallback embedding
- `app/services/index_store.py`
  - namespace별 vector index 관리
  - Chroma collection 기반 upsert / search
  - user namespace를 collection 이름으로 분리
  - metadata filter와 persistent directory / HTTP ChromaDB 연결 관리

### Scripts

- `scripts/check_chroma.py`
  - local persistent Chroma 또는 HTTP ChromaDB collection 목록과 record 수 확인

### Tests

- `tests/test_canonicalization.py`
  - canonical text 생성 규칙 테스트
- `tests/test_chunking.py`
  - native chunking 기본 동작 테스트
- `tests/test_document_chunking.py`
  - PDF LangChain 경로와 fallback 경로 테스트
- `tests/test_draft_service.py`
  - 온보딩 템플릿 생성 서비스 테스트
- `tests/test_index_store.py`
  - Chroma collection 기반 vector upsert / search 테스트

### Docs

- `docs/README.md`
  - `rag/docs` 내부 문서 인덱스
- `docs/연동-개요.md`
  - Backend, AI, Infra 관점의 한 장 요약
- `docs/API-명세.md`
  - 현재 코드 기준 HTTP API 계약
- `docs/RabbitMQ-명세.md`
  - MQ 전환 시 기준 메시지 설계
- `docs/Chroma-저장구조-ERD.md`
  - Chroma 내부 저장 구조와 확인 방법
- `docs/current-status.md`
  - 현재 구현된 기능과 미구현 범위 기록
- `docs/progress-log.md`
  - 작업 진행 내역을 날짜순으로 누적 기록
- `docs/sample-manual.txt`
  - 로컬 ingest 테스트용 샘플 문서

### Runtime / Packaging

- `pyproject.toml`
  - Python 의존성 및 테스트 설정
- `.env.example`
  - 로컬 개발용 환경변수 예시
- `Dockerfile`
  - 컨테이너 실행용 기본 이미지 설정

## Quick Start

```bash
cd rag
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

RabbitMQ worker 실행:

```bash
cd rag
python -m app.worker
```

기본 주소:

- `http://127.0.0.1:8090`
- `GET /health`

## Main Endpoints

- `GET /health`
- `POST /knowledge/ingest`
- `POST /templates/index`
- `POST /templates/match`

호환 경로:

- 기존 `/v1/...` 경로도 함께 유지한다.

## Notes

- 운영 환경에서는 `LLM_API_BASE_URL`, `LLM_API_KEY`를 설정해 chat 생성에 사용합니다.
- 현재 기본 임베딩 백엔드는 `EMBEDDING_BACKEND=embedding_api` 입니다.
- 현재 기본 임베딩 모델은 `EMBEDDING_MODEL=text-embedding-3-small` 입니다.
- OpenAI 임베딩만 별도로 쓰려면 `EMBEDDING_API_BASE_URL`, `EMBEDDING_API_KEY`를 설정하면 됩니다.
- 생성은 기존 OpenAI 호환 서버를 유지하고, 임베딩만 OpenAI 공식 API로 분리하는 구성이 가능합니다.
- `EMBEDDING_BACKEND=embedding_api`일 때 임베딩 차원은 API 응답에서 자동으로 학습합니다.
- 기본 vector backend는 `VECTOR_BACKEND=chroma`입니다.
- Chroma 데이터는 `CHROMA_PERSIST_DIRECTORY` 아래에 저장됩니다.
- 외부 ChromaDB 서버를 쓸 때는 `VECTOR_BACKEND=chroma_http`, `CHROMA_HOST`, `CHROMA_PORT`, `CHROMA_SSL`을 설정합니다.
- 운영 환경에서는 `CHUNKING_BACKEND=langchain`, `PDF_CHUNKING_STRATEGY=semantic` 조합을 권장합니다.
- 이미지형 PDF를 처리하려면 서버에 `tesseract-ocr`와 필요한 언어 데이터(`kor`)를 설치해야 합니다.
- OCR fallback은 `PDF_OCR_ENABLED`, `PDF_OCR_LANGUAGES`, `PDF_OCR_DPI`로 조절할 수 있습니다.
- 학교 LLM 서버 운영 가이드 기준 기본 예시는 `http://cellm.gachon.ac.kr:8000/v1` 입니다.
- 현재 기본 chat 모델 alias는 `LLM_CHAT_MODEL=text` 입니다.
- RAG는 별도 DB를 두지 않는 방향을 기준으로 합니다.
- 관계형 DB 대신 Chroma persistent storage로 벡터와 메타데이터를 관리합니다.
- 공통 식별자와 HTTP 스타일은 가능한 범위에서 `AI/` 레포의 naming 규칙을 우선한다.
- 루트 `docs/rag-*.md`는 서비스 전체 관점의 설계를 관리합니다.
- `rag/docs/`는 RAG 모듈 자체의 연동 문서와 상태 문서를 관리합니다.
- 진행 상황을 빠르게 보려면 아래 순서로 문서를 보면 됩니다.
  - `rag/README.md`
  - `rag/docs/연동-개요.md`
  - `rag/docs/API-명세.md`
  - `rag/docs/Chroma-저장구조-ERD.md`
  - `rag/docs/current-status.md`
  - `rag/docs/progress-log.md`
