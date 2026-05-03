# RAG RabbitMQ 명세

이 문서는 `운영 기준 명세`와 `현재 구현/로컬 테스트 기준`을 함께 정리한 문서다.
현재 `rag/` 코드는 HTTP API 서버와 MQ worker 골격을 함께 갖고 있으며,
실제 RabbitMQ 브로커와의 end-to-end smoke test도 로컬에서 확인한 상태다.

## 1. 현재 판단

현재 작업을 MQ 관점에서 나누면 아래와 같다.

```text
MQ 전환하기 좋은 작업
- knowledge/ingest
- templates/index
- templates/match
- draft

HTTP 유지가 더 자연스러운 작업
- health
```

이유:

- `knowledge/ingest`, `templates/index`, `draft`는 시간이 걸리거나 재시도 가치가 높다.
- `templates/match`도 사용자가 직접 누를 때보다 `메일 수신 파이프라인에서 미리 수행`하는 쪽이 더 자연스럽다.
- 즉 자동 파이프라인은 MQ, 직접 조회/제어는 HTTP로 나누는 것이 현재 서비스 흐름과 더 잘 맞는다.

## 2. 권장 토폴로지

### 2-1. 요청 / 응답 exchange

```text
+--------------------+--------+--------------------------------------+
| 이름               | 타입   | 역할                                 |
+--------------------+--------+--------------------------------------+
| x.app2rag.direct   | direct | Backend -> RAG 요청                  |
| x.rag2app.direct   | direct | RAG -> Backend 응답                  |
+--------------------+--------+--------------------------------------+
```

### 2-2. queue

```text
+----------------------------+-------------------------------------------+
| Queue 이름                 | 역할                                      |
+----------------------------+-------------------------------------------+
| q.2rag.knowledge.ingest    | 문서 인덱싱 요청 수신                     |
| q.2rag.templates.index     | 템플릿 인덱싱 요청 수신                   |
| q.2rag.templates.match     | 메일 수신 후 템플릿 매칭 요청 수신        |
| q.2rag.draft               | 온보딩 템플릿 생성 요청 수신              |
| q.2app.rag.progress        | RAG 진행 상태 이벤트                      |
| q.2app.knowledge.ingest    | 문서 인덱싱 완료/실패 응답                |
| q.2app.templates.index     | 템플릿 인덱싱 완료/실패 응답              |
| q.2app.templates.match     | 템플릿 매칭 결과 응답                     |
| q.2app.rag.draft           | 온보딩 템플릿 생성 결과 응답              |
+----------------------------+-------------------------------------------+
```

### 2-3. routing key

```text
+--------------------------+--------------------------------------+
| Routing Key              | 설명                                 |
+--------------------------+--------------------------------------+
| 2rag.knowledge.ingest    | Backend -> RAG 문서 인덱싱 요청      |
| 2rag.templates.index     | Backend -> RAG 템플릿 인덱싱 요청    |
| 2rag.templates.match     | Backend -> RAG 템플릿 매칭 요청      |
| 2rag.draft               | Backend -> RAG draft 요청            |
| 2app.rag.progress        | RAG -> Backend 진행 상태 이벤트      |
| 2app.knowledge.ingest    | RAG -> Backend 문서 인덱싱 응답      |
| 2app.templates.index     | RAG -> Backend 템플릿 인덱싱 응답    |
| 2app.templates.match     | RAG -> Backend 템플릿 매칭 응답      |
| 2app.rag.draft           | RAG -> Backend draft 응답            |
+--------------------------+--------------------------------------+
```

### 2-4. 운영형 RabbitMQ 리소스 원칙

운영 환경에서는 기존 Backend / AI 파이프라인과 같은 기준을 따른다.

```text
요청 queue
- durable
- DLX: x.retry.direct
- x-dead-letter-routing-key: *.retry

retry queue
- durable
- x-message-ttl: 30000
- x-dead-letter-exchange: 원래 요청/응답 exchange
- x-dead-letter-routing-key: 원래 routing key

최종 실패 queue
- *.dlq 또는 q.dlx.failed
- 3회 재시도 이후 적재
```

즉 운영형 기준에서는 `durable`만으로 끝나지 않고,
각 요청/응답 큐마다 `retry queue`, `TTL`, `DLQ`가 함께 있어야 한다.

### 2-5. 로컬 smoke test 토폴로지

로컬에서 worker 로직과 메시지 계약만 빠르게 확인할 때는 아래처럼 최소 토폴로지를 써도 된다.

```text
- x.app2rag.direct
- x.rag2app.direct
- q.2rag.*
- q.2app.*
```

이 경우 queue 속성은 `durable` 위주로만 두고,
`DLX / TTL / retry / DLQ`는 생략할 수 있다.

이 최소 토폴로지는 어디까지나 `로직 연결성 확인용`이며,
운영 리소스 기준을 대체하지 않는다.

## 3. 작업별 MQ 전환 기준

### 3-1. `knowledge/ingest`

가장 MQ로 바꾸기 좋은 작업이다.

이유:

- 파일 다운로드 / 텍스트 추출 / chunking / embedding / 저장까지 시간이 걸릴 수 있음
- 재시도 가치가 높음
- 사용자가 즉시 결과 본문을 받아야 하는 작업은 아님

요청 메시지 예시:

```json
{
  "request_id": "req-knowledge-001",
  "user_id": 1,
  "payload": {
    "faqs": [
      {
        "source_id": "faq-1",
        "question": "환불은 언제 가능한가요?",
        "answer": "결제 후 7일 이내 가능합니다."
      }
    ],
    "manuals": [
      {
        "source_id": "manual-1",
        "file_name": "policy.pdf",
        "presigned_url": "https://..."
      }
    ]
  }
}
```

응답 메시지 예시:

```json
{
  "request_id": "req-knowledge-001",
  "user_id": 1,
  "status": "SUCCESS",
  "payload": {
    "indexed_document_count": 2,
    "indexed_chunk_count": 4,
    "indexed_source_ids": ["faq-1", "manual-1"]
  },
  "error": null
}
```

### 3-2. `templates/index`

MQ 전환이 가능한 작업이다.

이유:

- 템플릿 저장 후 비동기 인덱싱이 가능함
- 템플릿 인덱스 실패 시 재시도하기 쉬움

요청 메시지 예시:

```json
{
  "request_id": "req-template-index-001",
  "user_id": 1,
  "payload": {
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
          "semantic_keywords": ["견적", "가격"]
        }
      }
    ]
  }
}
```

응답 메시지 예시:

```json
{
  "request_id": "req-template-index-001",
  "user_id": 1,
  "status": "SUCCESS",
  "payload": {
    "indexed_template_count": 1,
    "deleted_template_count": 0,
    "template_ids": [101]
  },
  "error": null
}
```

### 3-3. `draft`

기존 `ai.draft`는 서비스 책임이 정리되기 전의 초기안으로 본다.
현재 기준에서 `draft`는 일반 답장 초안이 아니라 온보딩 템플릿 생성 계약이며, 별도 `rag.draft` 큐를 사용한다.

즉:

```text
Backend -> q.2rag.draft
RAG consumer -> 처리
RAG -> q.2app.rag.draft
```

요청 메시지 예시:

```json
{
  "job_id": "job-draft-001",
  "request_id": "req-draft-001",
  "user_id": 1,
  "payload": {
    "mode": "generate",
    "category_id": 3,
    "category_name": "견적 요청",
    "industry_type": "B2B SaaS",
    "company_description": "기업용 메일 자동화 솔루션을 제공한다.",
    "email_tone": "formal",
    "rag_context": "FAQ와 매뉴얼 검색 결과",
    "template_count": 1
  }
}
```

응답 메시지 예시:

```json
{
  "job_id": "job-draft-001",
  "request_id": "req-draft-001",
  "user_id": 1,
  "status": "SUCCESS",
  "payload": {
    "category_id": 3,
    "category_name": "견적 요청",
    "title": "견적 요청 응답 템플릿",
    "subject_template": "견적 문의에 대한 안내",
    "body_template": "안녕하세요. 문의 주신 내용에 대해 안내드립니다..."
  },
  "error": null
}
```

현재 명세의 `q.2rag.draft`는 온보딩 템플릿 생성 계약을 기준으로 정의한다.

### 3-4. `templates/match`

현재 서비스 파이프라인 기준으로는 MQ 전환 대상에 포함하는 편이 더 자연스럽다.

이유:

- 메일이 수신될 때 자동 분류/요약/매칭을 함께 돌리는 흐름이 더 자연스럽다.
- 프론트가 메일 상세를 열 때마다 즉시 계산하기보다, 미리 후보를 저장해두는 편이 안정적이다.
- 추천 결과를 DB에 저장해두면 프론트는 조회만 하면 된다.

권장 흐름:

```text
메일 수신
-> Backend가 메일 저장
-> classify / summarize 완료
-> q.2rag.templates.match 발행
-> RAG가 후보 검색
-> q.2app.templates.match 응답
-> Backend가 추천 결과 DB 저장
-> Frontend는 저장된 추천 결과 조회
```

요청 메시지 예시:

```json
{
  "request_id": "req-match-001",
  "job_id": "job-match-001",
  "user_id": 1,
  "payload": {
    "emailId": "email-1",
    "subject": "견적 문의",
    "body": "엔터프라이즈 플랜 가격과 도입 절차를 알려주세요.",
    "summary": "엔터프라이즈 플랜 견적 요청",
    "intent": "견적 요청",
    "domain": "Sales",
    "top_k": 5
  }
}
```

응답 메시지 예시:

```json
{
  "request_id": "req-match-001",
  "job_id": "job-match-001",
  "user_id": 1,
  "status": "SUCCESS",
  "payload": {
    "emailId": "email-1",
    "results": [
      {
        "template_id": 101,
        "title": "견적 요청 응답",
        "intent": "견적 요청",
        "score": 0.93
      }
    ]
  },
  "error": null
}
```

다만 예외적으로 사용자가 직접 `재추천` 버튼을 누르는 액션은 HTTP로 둬도 된다.

## 4. 진행 상태 전달 방식

RabbitMQ는 작업 전달용이고, 사용자에게 보여줄 상태는 별도 저장이 필요하다.
권장 구조는 아래와 같다.

```text
Frontend
  -> Backend에 작업 시작 요청

Backend
  -> job_id 생성
  -> DB에 jobs row 생성
  -> MQ에 작업 발행

RAG
  -> 단계가 바뀔 때마다 progress 이벤트 발행

Backend
  -> progress 이벤트 consume
  -> jobs 상태 업데이트
  -> SSE 또는 polling 응답으로 사용자에게 전달
```

즉:

```text
MQ
- 작업 요청 / 진행 이벤트 / 완료 이벤트 전달

DB
- 현재 상태 저장

SSE / polling
- 사용자 화면 전달
```

### 4-1. Job 상태 저장 ERD

Backend는 MQ 메시지를 그대로 사용자 화면에 보여주지 않고,
작업 상태를 별도 테이블에 저장한 뒤 조회/API/SSE의 기준으로 사용한다.

권장 최소 구조:

```text
+----------------------+
| jobs                 |
+----------------------+
| job_id (PK)          |
| request_id           |
| user_id              |
| job_type             |
| target_type          |
| target_id            |
| status               |
| progress_step        |
| progress_message     |
| error_code           |
| error_message        |
| payload_json         |
| created_at           |
| updated_at           |
| completed_at         |
+----------------------+
```

필드 의미:

```text
job_id
- 장시간 작업을 식별하는 키

request_id
- 같은 요청 흐름을 추적하는 키

job_type
- knowledge_ingest / templates_index / templates_match / draft

target_type
- faq / manual / email / template / onboarding_batch 등

target_id
- source_id, emailId, template_id 같은 실제 대상 식별자

status
- 현재 최상위 상태

progress_step
- 세부 단계

progress_message
- 사용자/운영자에게 보여줄 문장

payload_json
- 진행 중간 결과나 완료 결과 일부를 저장할 확장 필드
```

실무적으로는 아래 인덱스를 같이 두는 것이 좋다.

```text
index(user_id, created_at desc)
index(status, updated_at desc)
index(job_type, target_id)
unique(request_id)
```

### 4-2. 공통 식별자

- `request_id`
  - 요청 추적용
- `job_id`
  - 장시간 작업 상태 추적용
- `user_id`
  - 사용자 소유권 식별

권장:

```text
request_id = 요청 단위
job_id = 진행 상태 단위
```

### 4-3. progress event 표준 스키마

요청/응답 envelope와 별개로, 진행 상태 이벤트는 아래 정도로 고정하는 걸 권장한다.

```json
{
  "request_id": "req-knowledge-001",
  "job_id": "job-knowledge-001",
  "user_id": 1,
  "job_type": "knowledge_ingest",
  "target_type": "manual",
  "target_id": "manual-1",
  "status": "PROCESSING",
  "progress_step": "EXTRACTING",
  "progress_message": "PDF 텍스트 추출 중",
  "processed_at": "2026-04-15T10:00:03+09:00",
  "payload": {
    "file_name": "policy.pdf"
  },
  "error": null
}
```

권장 필드 의미:

```text
status
- QUEUED / PROCESSING / COMPLETED / FAILED

progress_step
- 작업별 세부 단계

progress_message
- 사용자/운영자에게 바로 보여줄 수 있는 설명

payload
- 단계별 부가 정보
  예: file_name, indexed_chunk_count, matched_template_count

error
- 실패 시 code / message
```

상태값은 아래 두 층으로 분리하는 걸 권장한다.

```text
상위 상태(status)
- QUEUED
- PROCESSING
- COMPLETED
- FAILED

세부 단계(progress_step)
- EXTRACTING
- CHUNKING
- EMBEDDING
- MATCHING
- GENERATING
...
```

### 4-4. progress 이벤트 예시

```json
{
  "request_id": "req-knowledge-001",
  "job_id": "job-knowledge-001",
  "user_id": 1,
  "status": "EXTRACTING",
  "processed_at": "2026-04-15T10:00:03+09:00",
  "payload": {
    "source_id": "manual-1",
    "progress_message": "PDF 텍스트 추출 중"
  },
  "error": null
}
```

완료 이벤트 예시:

```json
{
  "request_id": "req-knowledge-001",
  "job_id": "job-knowledge-001",
  "user_id": 1,
  "job_type": "knowledge_ingest",
  "target_type": "manual",
  "target_id": "manual-1",
  "status": "COMPLETED",
  "progress_step": "INDEXING",
  "progress_message": "문서 인덱싱 완료",
  "processed_at": "2026-04-15T10:00:08+09:00",
  "payload": {
    "indexed_document_count": 1,
    "indexed_chunk_count": 8,
    "indexed_source_ids": ["manual-1"]
  },
  "error": null
}
```

실패 이벤트 예시:

```json
{
  "request_id": "req-knowledge-001",
  "job_id": "job-knowledge-001",
  "user_id": 1,
  "job_type": "knowledge_ingest",
  "target_type": "manual",
  "target_id": "manual-1",
  "status": "FAILED",
  "progress_step": "EXTRACTING",
  "progress_message": "문서 처리 실패",
  "processed_at": "2026-04-15T10:00:05+09:00",
  "payload": {
    "file_name": "policy.pdf"
  },
  "error": {
    "code": "OCR_REQUIRED",
    "message": "이미지형 PDF는 현재 OCR이 필요합니다."
  }
}
```

### 4-5. 단계 예시

`knowledge/ingest`

```text
QUEUED
DOWNLOADING
EXTRACTING
CHUNKING
EMBEDDING
INDEXING
COMPLETED
FAILED
```

`templates/index`

```text
QUEUED
CANONICALIZING
EMBEDDING
INDEXING
COMPLETED
FAILED
```

`templates/match`

```text
QUEUED
CANONICALIZING
EMBEDDING
MATCHING
COMPLETED
FAILED
```

`draft`

```text
QUEUED
RETRIEVING_CONTEXT
GENERATING
EMBEDDING_REPLY
COMPLETED
FAILED
```

### 4-6. Backend 상태 반영 규칙

Backend consumer는 progress 이벤트를 받을 때 아래처럼 처리하면 된다.

```text
1. request_id / job_id로 기존 row 조회
2. 없으면 신규 jobs row 생성
3. status / progress_step / progress_message 갱신
4. payload_json 최신값으로 갱신
5. FAILED면 error_code / error_message 저장
6. COMPLETED / FAILED면 completed_at 기록
7. 사용자 SSE 채널로 상태 이벤트 발행
```

즉 Backend는 `작업 계산`이 아니라 `상태 집계`를 담당한다.

## 5. 공통 메시지 envelope

요청:

```json
{
  "request_id": "req-20260415-001",
  "job_id": "job-20260415-001",
  "user_id": 1,
  "requested_at": "2026-04-15T10:00:00+09:00",
  "payload": {}
}
```

응답:

```json
{
  "request_id": "req-20260415-001",
  "job_id": "job-20260415-001",
  "user_id": 1,
  "status": "SUCCESS",
  "processed_at": "2026-04-15T10:00:03+09:00",
  "payload": {},
  "error": null
}
```

실패 응답:

```json
{
  "request_id": "req-20260415-001",
  "job_id": "job-20260415-001",
  "user_id": 1,
  "status": "FAILURE",
  "processed_at": "2026-04-15T10:00:03+09:00",
  "payload": {},
  "error": {
    "code": "PDF_INGEST_FAILED",
    "message": "이미지형 PDF는 현재 OCR이 필요합니다."
  }
}
```

## 6. DLQ / 재시도 기준

운영형 최소 권장:

```text
요청 queue마다 DLQ 1개
- q.2rag.knowledge.ingest.dlq
- q.2rag.templates.index.dlq
- q.2rag.templates.match.dlq
- q.2rag.draft.dlq

응답 queue마다 DLQ 1개
- q.2app.knowledge.ingest.dlq
- q.2app.templates.index.dlq
- q.2app.templates.match.dlq
- q.2app.rag.draft.dlq

progress queue
- q.2app.rag.progress.dlq
```

재시도 기준 예시:

- 네트워크 / presigned URL 실패: 재시도 가능
- 이미지형 PDF / OCR 필요: 즉시 실패
- validation 실패: 즉시 실패

권장 retry / TTL 예시:

```text
retry 횟수: 최대 3회
retry TTL: 30000ms
```

## 7. 최종 권장안

현재 시점 기준으로 가장 현실적인 혼합안:

```text
HTTP
- health
- 사용자가 직접 누르는 재추천 / 재생성 시작 요청

RabbitMQ
- knowledge.ingest
- templates.index
- templates.match
- draft
```

즉 MQ는 `자동 파이프라인`, HTTP는 `직접 조회 / 제어` 중심으로 나누는 것이 현재 서비스 흐름에 더 잘 맞는다.
