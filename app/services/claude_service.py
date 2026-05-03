from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.config import settings
from app.services.openai_compatible_client import create_openai_compatible_client


# 1. 외부 OpenAI 호환 LLM client 준비
def _get_client():
  # client 생성 책임을 분리해두면,
  # provider를 바꾸더라도 템플릿 생성 흐름은 유지할 수 있다.
  return create_openai_compatible_client(settings)

def _extract_message_content(response: dict) -> str:
  choices = response.get("choices")
  if not isinstance(choices, list) or not choices:
    raise ValueError("LLM API 응답에 choices가 없습니다.")

  first_choice = choices[0]
  if not isinstance(first_choice, dict):
    raise ValueError("LLM API 응답의 choice 형식이 예상과 다릅니다.")

  message = first_choice.get("message")
  if not isinstance(message, dict):
    raise ValueError("LLM API 응답에 message가 없습니다.")

  content = message.get("content")
  if isinstance(content, str) and content.strip():
    return content.strip()

  if isinstance(content, list):
    # 일부 OpenAI 호환 서버는 content를 블록 배열로 돌려줄 수 있어 이를 다시 합친다.
    text_blocks = [
      block.get("text", "").strip()
      for block in content
      if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    joined = "\n".join(block for block in text_blocks if block)
    if joined:
      return joined

  raise ValueError("LLM API 응답에서 본문을 찾지 못했습니다.")


def _parse_json_object(raw_text: str) -> dict:
  text = raw_text.strip()
  if text.startswith("```"):
    # 코드 펜스로 감싼 JSON도 지원해 worker가 provider 차이에 덜 민감하게 한다.
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
      lines = lines[1:]
    if lines and lines[-1].startswith("```"):
      lines = lines[:-1]
    text = "\n".join(lines).strip()

  start = text.find("{")
  end = text.rfind("}")
  if start == -1 or end == -1 or end < start:
    raise ValueError("LLM API 응답에서 JSON 객체를 찾지 못했습니다.")

  try:
    parsed = json.loads(text[start : end + 1])
  except json.JSONDecodeError as error:
    raise ValueError("LLM API 응답 JSON을 해석하지 못했습니다.") from error

  if not isinstance(parsed, dict):
    raise ValueError("LLM API 응답 JSON 형식이 예상과 다릅니다.")
  return parsed


class _OnboardingTemplateItem(BaseModel):
  model_config = ConfigDict(extra="forbid")

  variant_label: str
  title: str
  subject_template: str
  body_template: str

  @field_validator("variant_label", "title", "subject_template", "body_template")
  @classmethod
  def _validate_non_empty(cls, value: str) -> str:
    normalized = value.strip()
    if not normalized:
      raise ValueError("빈 문자열은 허용되지 않습니다.")
    return normalized


class _OnboardingTemplatesPayload(BaseModel):
  model_config = ConfigDict(extra="forbid")

  templates: list[_OnboardingTemplateItem]

  @field_validator("templates")
  @classmethod
  def _validate_templates_non_empty(cls, value: list[_OnboardingTemplateItem]) -> list[_OnboardingTemplateItem]:
    if not value:
      raise ValueError("templates 배열이 비어 있습니다.")
    return value


def generate_onboarding_templates(
  *,
  category_name: str,
  variants: list[dict[str, str]],
  category_keywords: list[str] | None = None,
  industry_type: str | None = None,
  company_description: str | None = None,
  email_tone: str | None = None,
  rag_context: str | None = None,
) -> list[dict[str, str]]:
  if settings.mock_mode:
    return [
      {
        "variant_label": variant["variant_label"],
        "title": f"{category_name} {variant['variant_label']} 템플릿",
        "subject_template": f"[{category_name}] {variant['variant_label']} 안내",
        "body_template": (
          f"안녕하세요.\n\n{category_name} 관련 문의 주셔서 감사합니다.\n"
          f"{variant['template_purpose']} 관점에서 필요한 안내를 드리겠습니다.\n\n감사합니다."
        ),
      }
      for variant in variants
    ]

  client = _get_client()
  variant_lines = "\n".join(
    f'{index + 1}. variant_label="{variant["variant_label"]}", '
    f'template_purpose="{variant["template_purpose"]}"'
    for index, variant in enumerate(variants)
  )
  prompt = f"""당신은 한국 B2B SaaS 기업의 이메일 템플릿 설계자입니다.
아래 정보를 바탕으로 지정된 템플릿 유형별 답변 템플릿을 정확히 {len(variants)}개 설계해주세요.

[카테고리]
{category_name}

[카테고리 키워드]
{", ".join(keyword.strip() for keyword in (category_keywords or []) if keyword and keyword.strip())}

[생성할 템플릿 유형]
{variant_lines}

[회사/업종 정보]
- 업종: {industry_type or ""}
- 회사 설명: {company_description or ""}
- 답장 어조: {email_tone or ""}

[참고 문맥]
{rag_context or ""}

[정보 배분 원칙]
- 세 템플릿은 같은 문장, 같은 안내 흐름, 같은 근거를 반복하지 마세요.
- 입력 정보와 참고 문맥은 각 템플릿의 목적에 맞게 나누어 사용하세요.
- 기본 응답형은 접수 확인, 핵심 안내, 다음 단계 중심으로 작성하세요.
- 상세 안내형은 참고 문맥, FAQ, 업무 자료에서 확인 가능한 구체 정보 중심으로 작성하세요.
- 추가 정보 요청형은 답변에 필요한 부족 정보와 확인 질문 중심으로 작성하세요.
- 회사 설명, 업종, 카테고리 키워드, 참고 문맥을 모든 템플릿에 동일하게 반복하지 마세요.
- 같은 표현을 재사용하기보다 제목, 첫 문장, 본문 구조, 마무리 문장을 서로 다르게 구성하세요.

[자동완성 변수 제한]
아래 변수만 subject_template/body_template에 사용할 수 있습니다.
- {{{{고객명}}}}: 수신 메일의 발신자명
- {{{{문의주제}}}}: AI가 분류한 문의 주제
- {{{{문의요약}}}}: AI가 요약한 문의 내용
- {{{{수신일자}}}}: 메일 수신 일시
- {{{{담당자명}}}}: 서비스 담당자 이름

회사/업종 정보와 답장 어조는 템플릿 문체와 내용 구성에만 반영하고,
메일 본문에 그대로 노출되는 placeholder로 만들지 마세요.

중요한 출력 규칙:
- 반드시 JSON 객체만 출력하세요.
- 설명, 마크다운, 코드펜스, 주석을 절대 추가하지 마세요.
- "templates" 배열 길이는 반드시 {len(variants)} 이어야 합니다.
- 각 항목의 "variant_label" 값은 위에 제시된 값과 정확히 일치해야 합니다.
- 모든 필드는 문자열이어야 하며 비워두지 마세요.
- 자동완성 변수는 필요한 경우에만 사용하고, 위 목록 밖의 placeholder를 절대 만들지 마세요.

반드시 아래 JSON 객체 형식만 출력하세요.
{{
  "templates": [
    {{
      "variant_label": "기본 응답형",
      "title": "템플릿 제목",
      "subject_template": "메일 제목 템플릿",
      "body_template": "메일 본문 템플릿"
    }}
  ]
}}
"""
  response = client.create_chat_completion(
    model=settings.llm_chat_model,
    messages=[{"role": "user", "content": prompt}],
    stream=False,
  )
  parsed = _parse_json_object(_extract_message_content(response))

  try:
    payload = _OnboardingTemplatesPayload.model_validate(parsed)
  except ValidationError as error:
    raise ValueError("LLM API 응답에 온보딩 템플릿 필수 필드가 없거나 형식이 잘못되었습니다.") from error

  if len(payload.templates) != len(variants):
    raise ValueError("LLM API 응답 템플릿 개수가 요청 개수와 다릅니다.")

  requested_variant_labels = [variant["variant_label"] for variant in variants]
  returned_variant_labels = [template.variant_label for template in payload.templates]
  if returned_variant_labels != requested_variant_labels:
    raise ValueError("LLM API 응답의 variant_label 순서 또는 값이 요청과 다릅니다.")

  return [
    {
      "variant_label": template.variant_label,
      "title": template.title,
      "subject_template": template.subject_template,
      "body_template": template.body_template,
    }
    for template in payload.templates
  ]
