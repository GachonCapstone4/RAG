# RAG API 명세

이 문서는 `현재 코드 기준`으로 Backend가 참고할 HTTP API 계약을 정리한다.
우선순위는 아래와 같다.

1. 외부 연동용 최종 API
2. 내부/레거시 API

## 1. 외부 연동용 최종 API

### 1-1. `POST /knowledge/ingest`

역할:

- FAQ와 업로드 문서(manual)를 한 번에 적재한다.
- 내부에서 텍스트 추출, chunking, embedding, Chroma upsert를 수행한다.
- 현재 manual 입력은 `PDF 전용`으로 고정한다.

요청:

```json
{
  "request_id": "req-knowledge-001",
  "user_id": 1,
  "faqs": [
    {
      "source_id": "faq-1",
      "question": "환불은 언제 가능한가요?",
      "answer": "결제 후 7일 이내 가능합니다.",
      "title": "환불 정책",
      "metadata": {
        "category": "billing"
      }
    }
  ],
  "manuals": [
    {
      "source_id": "manual-1",
      "file_name": "guide.pdf",
      "local_path": "/home/hojin/Projects/email-assistant/.local-uploads/1/guide.pdf",
      "title": "운영 매뉴얼",
      "metadata": {
        "category": "operations"
      }
    }
  ],
  "chunk_size": 1000,
  "chunk_overlap": 150
}
```

현재 manual 입력 규칙:

```text
현재 외부 계약
- manual은 PDF만 받음
- media_type / file_type 모두 전달하지 않음

로컬 테스트
- local_path 사용 가능

운영 상정
- presigned_url 필드 존재
- 하지만 현재 구현은 501
```

RAG 내부 처리 기준:

```text
manual ingest
-> application/pdf 로 고정 정규화
```

응답:

```json
{
  "request_id": "req-knowledge-001",
  "user_id": 1,
  "status": "SUCCESS",
  "indexed_document_count": 2,
  "indexed_chunk_count": 4,
  "indexed_source_ids": ["faq-1", "manual-1"]
}
```

주의:

- `indexed_chunk_count`는 운영/디버깅용이다.
- Backend가 꼭 쓰지 않아도 되지만, 적재 성공 여부 점검에는 유용하다.

### 1-2. `POST /templates/index`

역할:

- 생성된 템플릿을 template match용 컬렉션에 저장한다.
- `canonical_text`가 없으면 RAG가 내부에서 생성한다.
- `delete_template_ids`를 함께 보내면 해당 템플릿 벡터를 먼저 삭제한 뒤 신규 템플릿을 저장한다.

요청:

```json
{
  "request_id": "req-template-index-001",
  "user_id": 1,
  "delete_template_ids": [],
  "templates": [
    {
      "template_id": 101,
      "title": "견적 요청 응답",
      "intent": "견적 요청",
      "canonical_text": "제목: 견적 요청 응답\n카테고리: 견적 요청\n본문: ...",
      "mail_tone": "formal",
      "domain": "Sales",
      "metadata": {
        "template_purpose": "가격과 도입 절차를 안내한다",
        "recommended_situations": ["가격 문의", "도입 절차 문의"],
        "required_points": ["가격", "도입 절차"],
        "semantic_keywords": ["견적", "가격", "도입"]
      }
    }
  ]
}
```

응답:

```json
{
  "request_id": "req-template-index-001",
  "user_id": 1,
  "indexed_template_count": 1,
  "deleted_template_count": 0,
  "template_ids": [101]
}
```

### 1-3. `POST /templates/match`

역할:

- 이메일과 가장 가까운 template candidate를 찾는다.
- `canonical_text`를 직접 보낼 수도 있고, raw email 필드만 보내면 내부 canonicalization을 수행한다.

권장 입력:

```json
{
  "request_id": "req-match-001",
  "emailId": "email-1",
  "user_id": 1,
  "subject": "견적 문의",
  "body": "엔터프라이즈 플랜 가격과 도입 절차를 알려주세요.",
  "summary": "엔터프라이즈 플랜 견적 요청",
  "intent": "견적 요청",
  "domain": "Sales",
  "top_k": 5
}
```

응답:

```json
{
  "request_id": "req-match-001",
  "emailId": "email-1",
  "user_id": 1,
  "results": [
    {
      "template_id": 101,
      "title": "견적 요청 응답",
      "intent": "견적 요청",
      "score": 0.93,
      "mail_tone": "formal",
      "domain": "Sales",
      "metadata": {
        "template_purpose": "가격과 도입 절차를 안내한다"
      }
    }
  ]
}
```

현재 동작 규칙:

```text
1차 검색
- intent + mail_tone + domain 필터

fallback 1
- intent만 사용

fallback 2
- 필터 없이 검색
```

즉 결과 0건을 줄이기 위해 필터를 단계적으로 완화한다.

### 1-4. 템플릿 생성

역할:

- 온보딩 카테고리별 템플릿 variant를 생성한다.
- 현재 외부 연동 기준으로는 HTTP `/draft`가 아니라 RabbitMQ `rag.draft` 계약을 사용한다.
- 템플릿 생성에는 아래 값이 직접 사용된다.

```text
- category_name
- industry_type
- company_description
- email_tone
- rag_context
- template_count

추가 동작:

- `user_id`가 있으면 `category_name`, `industry_type`, `company_description`를 query로 사용해
  사용자별 knowledge index에서 `faq/manual` chunk를 검색한다.
- 검색 결과는 입력 `rag_context`와 합쳐 템플릿 생성 프롬프트의 참고 문맥으로 사용한다.
```

## 2. 내부 / 레거시 API

아래 API는 현재 코드에 남아 있지만, Backend의 최종 연동 진입점으로는 권장하지 않는다.

### 2-1. `POST /extract-text`

역할:

- 파일 바이트를 받아 텍스트만 추출한다.

입력:

```json
{
  "request_id": "req-extract-001",
  "file_name": "manual.pdf",
  "media_type": "application/pdf",
  "content_base64": "..."
}
```

응답:

```json
{
  "request_id": "req-extract-001",
  "file_name": "manual.pdf",
  "media_type": "application/pdf",
  "extracted_text": "...",
  "char_count": 1234
}
```

지금 이 API를 외부 계약에서 숨기려는 이유:

- `extracted_text`는 RAG 내부 다음 단계에 바로 쓰는 데이터다.
- Backend가 다시 받을 필요가 없다.

### 2-2. `POST /canonical/email`

역할:

- 이메일을 템플릿 매칭용 canonical text로 바꾼다.

이 API 역시 `templates/match` 내부 단계로 흡수하는 방향이다.

### 2-3. `POST /canonical/template`

역할:

- 템플릿 metadata를 template index용 canonical text로 바꾼다.

이 API 역시 `templates/index` 내부 단계로 흡수하는 방향이다.

### 2-4. `POST /knowledge/index`

역할:

- 실제 chunk -> embedding -> upsert를 직접 수행한다.

이 API는 현재 `knowledge/ingest` 내부에서 호출되는 구조로 보고,
외부 연동에서는 `knowledge/ingest`만 사용하면 된다.

### 2-5. `POST /knowledge/search`

역할:

- query_text를 임베딩해 knowledge 컬렉션에서 관련 chunk를 찾는다.

이 API는 현재 `draft` 내부 보강이나 운영 디버깅에는 유용하지만,
최초 Backend 연동의 필수 진입점은 아니다.

## 3. 상태 확인 API

### 3-1. `GET /health`

응답:

```json
{
  "status": "ok",
  "embedding_backend": "embedding_api",
  "vector_backend": "chroma"
}
```

용도:

- 배포/로컬 실행 시 설정 확인
- 현재 적용된 embedding/vector backend 확인

## 4. 최종 정리

Backend 기준 최소 외부 계약:

```text
필수
- POST /knowledge/ingest
- POST /templates/index
- POST /templates/match
- GET /health

보조
- GET /health
```

내부/레거시 API는 디버깅이나 점진적 마이그레이션 용도로만 본다.
