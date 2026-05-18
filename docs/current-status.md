# Current Status

기준일: 2026-04-15

이 문서는 `현재 코드 기준으로 확인된 사실`만 적는다.
목표 구조나 팀 간 역할 분리는 [연동-개요.md](./연동-개요.md)를 함께 본다.

## 1. 현재 구현된 외부 API

현재 백엔드와 연동 대상으로 보는 외부 API는 아래 4개다.

- `POST /knowledge/ingest`
- `POST /templates/index`
- `POST /templates/match`
- `GET /health`

보조/레거시 성격으로 아래 API도 코드에는 남아 있다.

- `GET /health`
- `POST /extract-text`
- `POST /canonical/email`
- `POST /canonical/template`
- `POST /knowledge/index`
- `POST /knowledge/search`

호환용으로 `/v1/...` 경로도 함께 유지한다.

## 2. 현재 구현된 내부 기능

- `PyMuPDF` 기반 PDF/TXT 텍스트 추출
- 이미지형 PDF 감지 및 Tesseract OCR fallback
- PDF 상단/하단 반복 라인과 하단 단독 페이지 번호에 대한 보수적 노이즈 제거
- 기본 chunking
- PDF 문서에 대한 semantic chunking 시도 + fallback
- `embedding_api` 기반 외부 임베딩 API 호출
- `hash` fallback 임베딩
- Chroma persistent collection 기반 지식/템플릿 인덱싱
- knowledge 검색
- template candidate match
- 온보딩 템플릿 variant 생성
- RabbitMQ worker 기반 `knowledge.ingest`, `draft.generate`, `templates.index`, `templates.match` consume/publish 골격

## 3. 현재 적용 중인 문맥 기준

고정 문맥:

- `company_description`
- `domain`
- `industry_type`
- `mail_tone`

검색 대상:

- `faq`
- `manual`

현재 업로드 문서는 세부 유형을 더 나누지 않고 모두 `manual`로 취급한다.
`profile` source_type은 스키마에는 남아 있지만 운영 기준 검색 대상은 아니다.

## 4. 현재 저장 방식

- 별도 RAG 전용 비즈니스 DB 없음
- Chroma persistent directory 사용
- 기본 저장 경로: `.rag-data/chroma`
- knowledge / template를 사용자별 namespace로 분리

예시:

```text
knowledge namespace
- knowledge:1
- knowledge:2

template namespace
- template:1
- template:2
```

실제 Chroma collection 이름은 내부적으로 아래처럼 정리된다.

```text
knowledge:1 -> knowledge_1
template:1  -> template_1
```

## 5. 현재 미구현 상태

- RabbitMQ 브로커와의 end-to-end 송수신 검증
- presigned URL 기반 manual 다운로드 ingest
- 삭제 / 재색인 API
- 운영용 인증/권한 검증

## 6. 현재 한계

- 이미지형 PDF는 Tesseract 설치와 언어 데이터가 준비되어 있어야 처리된다.
- `knowledge/ingest`의 `presigned_url` 경로는 현재 501이다.
- `templates/match`는 candidate 검색만 수행하고 최종 rerank는 하지 않는다.
- 현재 `Backend_Server` 코드 기준으로는 RAG HTTP 호출이 아직 붙어 있지 않다.
- 현재 `knowledge/ingest`의 manual 입력은 PDF 전용으로 고정했고, 외부 계약에서 `media_type/file_type`을 받지 않는다.
- 현재 `draft`는 일반 답장 초안 생성이 아니라 온보딩 템플릿 생성 계약을 의미한다.
- 온보딩 템플릿 생성은 사용자별 knowledge index에서 `faq/manual` chunk를 검색해 카테고리별 참고 문맥을 보강하지만, 카테고리 키워드 전용 저장 구조는 아직 없다.

## 7. 테스트 상태

2026-04-15 기준 로컬 테스트:

```text
cd rag && ./.venv/bin/python -m pytest tests
-> 20 passed
```

즉 `rag/` 모듈 단독 기준으로는 테스트 가능한 상태다.

## 8. 연동 관점 요약

- 지금 RAG는 `HTTP API 서버`와 `MQ worker`를 함께 둘 수 있는 상태다.
- MQ worker는 구현됐지만 실제 RabbitMQ 브로커와의 통합 검증은 아직 남아 있다.
- Backend가 실제로 붙을 때는 [API-명세.md](./API-명세.md)의 4개 외부 API만 먼저 호출하면 된다.
