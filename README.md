# SpecKit-Start

프로젝트마다 GitHub Spec Kit CLI와 에이전트 연동을 확인하는 작업을 줄여 주는 공용 스킬입니다. Codex 또는 Claude Code에서 한 번 호출하면 현재 프로젝트의 준비 상태를 확인하고, 승인받은 설치와 설정을 공식 `specify` CLI로 진행합니다. 스펙 작성부터 구현까지의 SDD 작업은 GitHub Spec Kit의 스킬에 맡깁니다.

## 지원 환경

- Windows의 로컬 Codex 및 Claude Code를 기준으로 개발했습니다. macOS와 Linux에서도 같은 `SKILL.md`를 사용할 수 있으며, 생성되는 Spec Kit 스크립트는 운영체제에 맞게 선택합니다.
- 사용할 Codex 또는 Claude Code를 먼저 설치하세요. Specify CLI가 없을 때는 [uv](https://docs.astral.sh/uv/getting-started/installation/)를 사용합니다.
- 새 프로젝트 설정에는 `init --integration`과 `integration status --json`을 지원하는 CLI가 필요합니다. 기존 프로젝트에 에이전트를 추가하려면 `integration install`과 해당 조합의 멀티 연동도 지원해야 합니다.

## 전역 설치

이 저장소를 내려받고 저장소 루트에서 아래 PowerShell 명령을 실행하세요. `$SkillHomes`에서 설치할 도구의 경로를 선택할 수 있습니다. 실행 전에 선택한 대상과 그 상위 경로에 심볼릭 링크나 정션이 없는지 확인하세요. 대상에 같은 이름의 파일이 있으면 해당 파일의 복사가 중단됩니다.

```powershell
$SkillSource = (Resolve-Path -LiteralPath 'skills/speckit-start/SKILL.md').Path
$SkillHomes = @(
    (Join-Path $HOME '.agents/skills')
    (Join-Path $HOME '.claude/skills')
)

foreach ($SkillHome in $SkillHomes) {
    $SkillFolder = Join-Path $SkillHome 'speckit-start'
    [System.IO.Directory]::CreateDirectory($SkillFolder) | Out-Null
    [System.IO.File]::Copy($SkillSource, (Join-Path $SkillFolder 'SKILL.md'), $false)
}
```

직접 복사할 때도 같은 경로를 사용합니다.

| 도구 | 사용자 스킬 경로 | 프로젝트 대화창에서 실행 |
| --- | --- | --- |
| Codex | `~/.agents/skills/speckit-start/SKILL.md` | `$speckit-start` |
| Claude Code | `~/.claude/skills/speckit-start/SKILL.md` | `/speckit-start` |

사용할 프로젝트를 각 도구에서 열고 해당 명령을 입력합니다.

## 스킬 실행 과정

1. PATH와 패키지 관리자 기록을 대조해 실행 파일의 출처를 확인한 뒤 `specify version`을 실행합니다. 설치가 필요하면 공식 PyPI 배포의 정확한 버전과 설치 위치를 제시하고 승인받습니다.
2. 작업 범위 안에서 프로젝트 루트를 확인합니다. 외부 어댑터 설치 흔적을 먼저 점검하고, 안전하게 호출할 수 있을 때 `specify integration status --json`으로 상태를 확인합니다.
3. 새 프로젝트에서는 **Codex + Claude Code**, **Codex만**, **Claude Code만** 중 원하는 연동을 선택합니다. 두 가지를 선택하면 Codex로 한 번 초기화하고 Claude Code를 추가합니다.
4. 대상 경로와 명령, 예상 변경을 승인받습니다. 승인 후 임시 디렉터리에서 생성 경로를 확인하고 실제 프로젝트의 충돌과 링크를 검사합니다. 변경 범위가 늘어나면 다시 승인받습니다. Spec Kit 이벤트 등록을 끄고 기본 SDD 스킬을 구성합니다.
5. 상태와 실제 스킬 파일을 다시 확인합니다. 준비가 끝나면 Codex에서는 `$speckit-specify`, `$speckit-clarify`, `$speckit-plan`, `$speckit-tasks`를, Claude Code에서는 같은 이름의 `/speckit-…` 명령을 사용할 수 있습니다.

이미 초기화된 프로젝트에서는 별도 선택이 없으면 현재 실행 중인 에이전트를 기준으로 확인합니다. 예를 들어 Codex가 연동된 프로젝트에서 Claude Code로 실행하면, 승인을 받은 뒤 공식 `specify integration install claude` 명령으로 Claude Code 연동을 추가합니다. 프로젝트의 기본 에이전트는 Codex로 유지되며, 두 에이전트는 같은 프로젝트의 Spec Kit 설정과 SDD 문서를 사용합니다. 준비된 프로젝트에서는 상태 확인만으로 끝납니다.

## 문제 해결

| 증상 | 확인할 사항 |
| --- | --- |
| 설치했지만 `specify`를 찾지 못함 | `uv tool dir --bin`이 알려 주는 폴더에서 실행 파일을 확인하세요. uv로 설치한 경우 `uv tool update-shell`로 PATH를 설정한 뒤 터미널과 에이전트를 다시 열 수 있습니다. |
| `uv`가 없음 | [uv 공식 설치 안내](https://docs.astral.sh/uv/getting-started/installation/)에 따라 설치한 뒤 새 터미널에서 다시 실행하세요. |
| 기존 CLI 또는 프로젝트 형식이 오래됨 | `specify version`과 [공식 업그레이드 가이드](https://github.github.com/spec-kit/upgrade.html)를 확인하세요. 프로젝트에 로컬 수정이 있다면 먼저 백업하거나 커밋해 두세요. |
| 파일 충돌이나 쓰기 권한 오류 | 보고된 경로의 기존 파일과 권한을 확인하세요. 초기화가 도중에 실패했다면 다시 실행하기 전에 `.specify` 상태와 Git 변경 사항을 확인하세요. |
| 스킬이 대화창에 표시되지 않음 | 설치 경로와 프로젝트 루트를 확인하고 새 세션에서 다시 여세요. 지원되는 Claude Code에서는 `/reload-skills`도 사용할 수 있습니다. |

## 검증

Windows에서 Specify CLI와 두 에이전트가 설치된 상태로 아래 통합 테스트를 실행할 수 있습니다. `specify`가 PATH에 없으면 `-SpecifyCli`에 확인된 실행 파일의 절대 경로를 전달하세요. 각 테스트는 임시 디렉터리에서 실행됩니다.

```powershell
./tests/smoke.ps1
```

대화형 승인, 안전 중단, 재실행, 실패 처리와 클라이언트 내 스킬 인식은 [보안 테스트 시나리오](tests/security.md)에 따라 각 에이전트에서 확인합니다.

## 공식 문서

- [GitHub Spec Kit](https://github.com/github/spec-kit)
- [Spec Kit 설치 가이드](https://github.github.com/spec-kit/installation.html)
- [Spec Kit 연동 CLI](https://github.github.com/spec-kit/integrations/cli.html)
- [Codex 스킬](https://developers.openai.com/codex/skills)
- [Claude Code 스킬](https://code.claude.com/docs/en/skills)
