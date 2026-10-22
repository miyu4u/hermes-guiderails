# Behavior Contract

## External Contracts

### Tools

- **모델 도구** `guiderails_patterns`**:** JSON 객체의 `action`에 동작을 지정합니다. 입력 필드는 문자열입니다.
  - `status`, `list`: 현재 상태와 규칙을 조회합니다.
  - `stats`, `report`: 집계와 감지 이력을 각각 조회합니다. `stats`는 선택적 `session_id`, `report`는 선택적 `session_id`와 `operation_id` 필터를 받습니다.
  - `test`: `rule_id`와 선택적 `sample`을 받아 규칙을 검사합니다.
  - `propose`: `request`를 받아 사용자 검토용 비활성 초안을 저장합니다.
- **사용자 명령** `/guiderails`**:** `status`, `list`, `test <rule_id> [샘플 ...]`, `stats`, `report`, `propose <요청>`을 제공합니다. 규칙을 활성화하는 `approve <draft_id> <digest>`와 규칙 파일을 다시 읽는 `reload`는 사용자 명령에서만 제공합니다.
- **관리 응답:** 성공은 `ok: true`와 결과, 실패는 `ok: false`와 `error_code`를 담은 JSON입니다. `status`, `report`, `approve`, `reload` 자체는 성공하고 감사 내보내기가 실패하면 `audit_pending: true`와 `audit_error_code`가 포함됩니다.

### Hooks

- `pre_tool_call` 실행 전 훅: Hermes가 도구명, 인자 객체, 호출 메타데이터를 전달합니다.
  - `session_id`, `turn_id`, `tool_call_id`, `api_request_id` 등의 식별자는 감지와 교정의 귀속에 사용합니다.
  - 반환값 `None`은 통과를, `{"action": "block", "message": "…"}`은 도구 핸들러 실행 전 차단을 뜻합니다.
- `llm_request` 요청 미들웨어: Hermes가 모델 요청과 세션·요청 식별자, 요청 형식(`api_mode`), 턴 식별자를 전달합니다.
  - 변경할 내용이 없으면 `None`, 변경이 있으면 `request`와 `source: guiderails`를 담은 객체를 반환합니다.

### File Schemas

#### Rule Files `patterns/*.yaml`

규칙 파일은 `<profile-home>/guiderails/patterns/*.yaml`에 둡니다. [v1 규칙 스키마](../src/guiderails/schemas/pattern-set.v1.schema.json)와 [예시](../examples/README.md)가 필드 형식과 샘플을 제공합니다.

- `schema_version: 1`과 `rules`를 선언합니다. 규칙은 고유 `id`, 양의 `revision`, `enabled`, `description`, `priority`, `scopes`, `matcher`, `edit_policy`, `announce.instruction`, 양성·음성 `tests`를 가집니다. 동일 ID의 내용을 변경하려면 revision을 올립니다. 로더는 규칙 샘플의 기대 결과도 검증합니다.
- `match`는 리터럴, `regex`는 정규식입니다. 정규화는 `none` 또는 `nfc`를 지원합니다. `edit_policy`는 `introduced`와 `whole_result`를 받습니다. `keep` 기본값은 `true`입니다.
- 선택자 `argument`와 `arguments`는 서로 배타적이고 `scopes: [tool.arguments]`에서만 씁니다. `arguments`의 `/pages/*/content`에서 `*`는 배열 인덱스 하나를 뜻합니다. 단일 `argument`에서 `*`는 리터럴입니다.

#### Configuration File `config.yaml`

[설정 스키마](../src/guiderails/config.py)는 `<profile-home>/guiderails/config.yaml`이 있으면 `schema_version: 1`을 요구합니다. 누락된 항목과 파일에는 기본값을 적용합니다. 설정 변경은 새 플러그인 런타임에서 반영합니다.

| 설정 | 기본값과 적용 효과 |
| --- | --- |
| `mode`, `effect_policy` | `strict`, `pattern` |
| `max_rejections` | `3`. 0 이상의 정수이며 `0`이면 첫 감지부터 경고 후 통과 |
| `limits` | 후보 1 MiB, 규칙 1,000개, 패턴 4,096자, 매치 10,000개, 정규식 20 ms, 검사 1,000 ms, 컴파일 2초, 변경 비교 1,000 ms |
| `logging` | outbox 10,000개(최솟값 2), JSONL 10 MiB 또는 UTC 날짜 기준 회전, 보관 30일·회전본 최대 200 MiB. 발췌 기본 꺼짐; 사용 시 기본 160자·1일 |
| `guidance_templates` | 항목별 선택 설정. 기본값은 [내장 영어 문구](../src/guiderails/guidance.py)입니다. |
| `context_mode`, `display`, `retry`, `semantic` | 앞의 세 항목은 v1 스키마에서 검증하지만 이 런타임에서 교정 보존, 화면 처리, 재시도 제한을 제어하지 않습니다. `semantic.enabled`는 `false`만 받습니다. |

`guidance_templates`는 메시지 이름과 비어 있지 않은 문자열의 매핑입니다. [예시 설정](../examples/config.yaml)처럼 `|-`로 여러 줄을 작성할 수 있습니다.

- `correction`: `{rule_id}`, `{description}`, `{instruction}`, `{continuation}`을 모두 포함합니다.
- `warning`: `{rule_id}`, `{description}`, `{instruction}`을 모두 포함합니다.
- `continuation`, `ambiguous_file_format`, `ambiguous_or_fuzzy_edit`, `inspection_rejection`, `invalid_tool_payload`, `human_approval_required`: 치환 없이 표시하는 문구입니다.

`correction`과 `warning`에서 중괄호 자체를 표시하려면 `{{`와 `}}`를 사용합니다. 자리표시자에는 이름만 사용합니다. 규칙 설명과 교정 지침은 규칙 YAML의 문구를 그대로 사용합니다. `<guiderails>` 태그와 도구 오류 코드는 고정입니다.

생략한 항목은 기본값을 사용합니다. 값이나 자리표시자가 잘못된 항목은 `ERROR` 로그를 남기고 해당 항목만 기본값을 사용합니다. 알 수 없는 키는 `ERROR` 로그를 남기고 무시합니다. `guidance_templates` 전체가 매핑이 아니면 `ERROR` 로그를 남기고 전체 기본값을 사용합니다.

## Internal Contracts

### Inspection Scope and Boundaries

- 활성 검사 범위는 도구 인자의 `tool.arguments`와 지원되는 로컬 파일 후보의 `artifact.text`입니다. `assistant.text`와 `tool.result`는 스키마에서 받는 값이며 활성 검사 지점이 없습니다.
- 일반 도구와 MCP 도구는 Hermes가 전달한 인자의 문자열 값을 검사합니다. 인자 선택자를 지정하면 해당 도구명과 경로를 적용하고 감사 위치에는 실제 배열 인덱스를 남깁니다. 객체 키 자체나 원격 서비스가 저장한 결과는 검사하지 않습니다.
- 로컬 `write_file`, `edit`, `edit_file`, `patch`의 지원 형식에서는 쓰기·정확 교체·지원되는 v4a 패치의 변경 후 후보를 계산합니다. 실행 전 게이트는 변경 후 파일 후보 전체를 검사하므로 `introduced`를 지정해도 이전부터 남아 있는 표현을 감지할 수 있습니다. 대체 문자열만 검사하려면 해당 인자에 선택자를 지정합니다.
- 네이티브 런타임에서 확인 가능한 절대 경로와 `~/` 경로를 검사합니다. 상대 경로 기준이나 로컬 백엔드를 확인할 수 없으면 Hermes가 호출을 계속 처리합니다. 검사 결과 교체 위치가 모호하면 호출을 차단합니다.
- 직접 `read_file`과 `search_files`는 인자 형식과 기본 인자 한도를 확인하고 내용 규칙을 건너뜁니다. `effect_policy: pattern`은 터미널·실행 코드·미등록 도구의 효과를 추적하지 않고 인자에만 규칙을 적용합니다. `verified`는 터미널·코드 실행과 검증되지 않은 외부 효과를 거절합니다. 이 설정은 임의 MCP 도구의 검증이나 파일 시스템 접근 통제를 제공하지 않습니다.
- 파일 후보는 검사 시점에 관찰한 내용에 근거합니다. 통과 판정 이후 실제 실행 전까지 파일 상태가 달라질 수 있습니다. 원격 문서의 최종 저장 내용, 동적 산출물, 답변 스트림, 도구 결과는 내용 검사 범위 밖에 있습니다.



### Strict and Observe Decisions

| 검사 결과 | `strict` | `observe` |
| --- | --- | --- |
| 새 위반 없음 | 통과 | 통과 |
| 새 위반 감지 | 해당 세션·규칙의 한도 내에서 차단하고 교정 예약; 소진 시 경고를 예약하고 통과 | 감지를 기록하고 통과 |
| 위반을 확인하기 전 검사 자원 한도 도달 | 검사 생략 알림을 시도하고 통과 | 동일 |
| 잘못된 호출 구조, 관리 권한 위반, 효과 정책 거절, 모호한 편집 | 차단 | 차단 |
| 플러그인 장애로 검사나 판정 저장을 완료하지 못함 | 통과 | 통과 |
| 판정 저장 후 JSONL 내보내기 실패 | 저장된 판정 적용 | 통과 |

동시에 검사한 규칙 중 먼저 확인된 위반으로 판정합니다. `priority`는 검사 순서에 관여하지만 병렬 완료 순서까지 보장하지 않습니다. 런타임 감사 이력을 전체 규칙 위반 목록으로 해석하면 안 됩니다. 이미 확인한 위반이 자원 한도와 함께 반환되면 해당 위반을 판정에 사용합니다. 한도 검사는 세션 ID가 있는 경우에 누적됩니다. 세션 ID가 없으면 `max_rejections > 0`일 때 누적 한도를 적용할 수 없어 감지마다 차단될 수 있습니다.

### Follow-up Model Request Guidance

차단 또는 한도 소진 경고의 안내는 세션과 요청을 식별할 수 있는 적격 후속 모델 요청에 `<guiderails>` 블록으로 첨부합니다. 원래 요청과 보조 질문은 새 안내를 소비하지 않습니다. `keep: true`는 차단된 호출과 실패 결과를 모델 요청에 남깁니다. `keep: false`는 세션·호출 ID가 있을 때 거절 표식을 사용하고, 지원되는 요청 형식에서 그 호출과 결과 쌍만 제거합니다. 같은 배치의 정상 결과와 Hermes 대화 기록은 그대로 유지합니다. 교정된 호출을 실제로 제출할지는 모델이 결정합니다.



### User Session Notifications

세션 ID가 있으면 다음 조건에서 화면용 알림을 비동기로 제출합니다.

- `Guiderails · BLOCKED · <도구> · <이벤트 접두어>`: strict 패턴 차단의 판정을 새로 확정하고 모델 교정 안내를 예약한 경우입니다.
- `Guiderails · WARNED · <도구> · <이벤트 접두어>`: 거절 한도 소진 경고를 최초로 저장한 경우입니다. 같은 판정을 재처리할 때는 알림을 생략합니다.
- `Guiderails · BYPASSED · Inspection budget exceeded`: 위반을 확인하기 전에 검사 자원 한도에 도달해 호출을 허용한 경우입니다. 같은 런타임·세션에서 대기하거나 전달 중인 검사 생략 알림은 한 건으로 합칩니다.

이벤트 접두어는 감사 이벤트 ID의 첫 8자입니다. 영문자·숫자·`_`·`-`로 된 1~64자 도구명만 표시하고 그 외에는 `tool`로 표기합니다. 알림에는 인자 값, 위반 발췌, 내부 오류 본문이 들어가지 않습니다.

지원되는 live Ink는 알림을 대화에 시스템 줄로 표시합니다. Ink 채널이 선택 전에 미지원으로 판명되면 공개 `ctx.post_message` 경로의 표시·지연 정책을 따릅니다. 메시징 플랫폼에서는 공개 경로가 원래 채널·스레드로 전송을 시도합니다. 채널을 선택한 이후에는 결과가 실패하거나 불확실해도 재전송하지 않습니다. 도구 판정, 감사와 모델 교정은 알림 전달 결과와 독립적입니다.

Guiderails는 사용자 알림을 위해 새 턴을 열거나 현재 턴의 모델 요청 및 Hermes 저장 대화에 알림을 추가하지 않습니다. Discord의 후속 채널 히스토리나 Telegram의 인용 답장에는 플랫폼 동작에 따라 알림이 들어갈 수 있습니다. 채널별 의존성과 격리 조건은 [설계](DESIGN.md#사용자-세션-알림-announcer)를 참조합니다.



### Policy Activation

호출은 시작 시점의 정책 스냅샷을 검사와 감사에 사용합니다. `approve`는 검토한 초안의 ID와 digest를 확인한 뒤 적용하고, `reload`는 디스크의 규칙을 읽어 검증한 뒤 새 스냅샷을 게시합니다. 규칙 갱신에 실패하면 기존 활성 스냅샷을 유지합니다.



### Audit and Recovery

- JSONL은 검사에 사용한 정책 digest를 포함한 감사 메타데이터를 기록합니다. 발췌를 켜면 가린 텍스트를 만료 조건이 있는 SQLite 레코드로만 저장합니다.
- 감사 아웃박스와 관리 변경 저널은 재시작 후에도 유지됩니다. 미내보내기 이벤트는 다음 내보내기 시 저장 순서대로 처리합니다. 교정 대기열과 화면 알림은 프로세스 메모리에 있습니다. 교정 요청을 구성하는 중 오류가 나면 Hermes가 원래 모델 요청을 처리합니다.
