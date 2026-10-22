# Guiderails 설정 예시

Guiderails가 활성화된 테스트용 Hermes 프로필에 설정 파일과 필요한 규칙 파일을 복사합니다. 각 규칙 파일은 독립된 v1 YAML 문서입니다.

| 예시 파일 | 프로필 내 위치 |
| --- | --- |
| [`config.yaml`](config.yaml) | `<profile-home>/guiderails/config.yaml` |
| [`patterns/ko.avoid-vague-timing.yaml`](patterns/ko.avoid-vague-timing.yaml) | `<profile-home>/guiderails/patterns/ko.avoid-vague-timing.yaml` |
| [`patterns/ko.avoid-generic-record.yaml`](patterns/ko.avoid-generic-record.yaml) | `<profile-home>/guiderails/patterns/ko.avoid-generic-record.yaml` |
| [`patterns/ko.avoid-meaning-disclaimer.yaml`](patterns/ko.avoid-meaning-disclaimer.yaml) | `<profile-home>/guiderails/patterns/ko.avoid-meaning-disclaimer.yaml` |

기존 설정이 있다면 덮어쓰지 말고 필요한 항목만 병합합니다. `config.yaml` 변경은 새 플러그인 런타임에서 적용됩니다. 규칙 파일만 추가하거나 바꿨다면 `/guiderails reload`로 반영하고 `/guiderails list`에서 확인합니다. 기존 규칙의 내용을 바꿀 때는 `id`를 유지하고 `revision`을 증가시킵니다.

이전 예시의 `writing.yaml`을 사용 중이라면 분리 파일과 동시에 두지 않습니다. 사용자 정의 규칙이 섞여 있으면 분리하는 규칙 ID만 기존 파일에서 제거합니다. 아래 결과는 세 예시 규칙을 모두 적용한 경우입니다.

## 동작 확인

테스트 프로필에서 쓰기 가능한 임시 절대경로를 대상으로 `write_file`을 호출합니다.

| `content` | 이 예시 규칙의 결과 (`strict`) |
| --- | --- |
| `가능한 한 빨리 처리한다.` | `ko.avoid-vague-timing`이 문구를 감지합니다. 파일 쓰기를 차단하고 기한을 명시하도록 안내합니다. |
| `임시 처리로 남겨 둔다.` | `ko.avoid-generic-record`가 문구를 감지합니다. 파일 쓰기를 차단하고 작업을 명시하도록 안내합니다. |
| `검토는 끝났다. 공식 승인을 뜻하지 않는다.` | 이 파일 쓰기에서는 `ko.avoid-meaning-disclaimer`를 적용하지 않습니다. |
| `검토는 끝났다. 공식 승인은 금요일에 진행한다.` | 예시 규칙에 걸리지 않습니다. |
| `금요일 17시까지 처리한다.` | 예시 규칙에 걸리지 않습니다. |

첫 규칙은 파일 후보 등 `artifact.text`에만 적용합니다. 두 번째 규칙은 `tool.arguments`에도 적용하고 `임시처리`처럼 공백 없는 입력도 감지합니다. `mode: observe`로 바꾸고 새 런타임을 열면 규칙 위반을 기록하되 도구 호출은 진행합니다. 별도의 인자 정책, 효과 정책 또는 저장 오류는 여전히 호출을 차단할 수 있습니다.

### Notion 페이지 부분 편집

Hermes의 MCP 서버 키가 `notion`이면 `ko.avoid-meaning-disclaimer`는 `mcp__notion__notion_create_pages`의 `/pages/*/content`와 `mcp__notion__notion_update_page`의 `/content`, `/new_str`, `/content_updates/*/new_str`를 검사합니다. Hermes는 서버가 제공하는 원본 이름 `notion-update-page`에 서버 네임스페이스를 붙입니다. 서버 키를 바꿨다면 규칙에도 Hermes에 등록된 전체 도구명을 사용합니다. 파일에 같은 패턴을 적용하려면 별도 ID의 `artifact.text` 규칙을 사용합니다.

`mcp__notion__notion_update_page`에 아래처럼 전달하면 검색 대상인 `old_str`에 위반 문구가 있어도 정상 대체문으로 교정할 수 있습니다. 반대로 위반 문구를 `new_str`에 넣으면 `strict`의 거절 한도가 남아 있을 때 호출 전에 차단합니다.

```json
{
  "page_id": "<test-page-id>",
  "command": "update_content",
  "content_updates": [
    {
      "old_str": "검토는 끝났지만, 공식 승인을 뜻하는 건 아닙니다.",
      "new_str": "검토를 마쳤습니다. 공식 승인은 별도 절차에서 진행합니다."
    }
  ]
}
```

규칙 필드는 [v1 JSON 스키마](../src/guiderails/schemas/pattern-set.v1.schema.json)에서 확인할 수 있습니다. 설치 및 검증은 [한국어 README](../README.ko.md)를 참고합니다.
