# codex-review

Codex OAuth(ChatGPT 구독) 기반 GitHub PR **전체 코드베이스** 리뷰 봇.
GitHub App 웹훅으로 PR 이벤트를 받아, 레포를 체크아웃하고 전체 파일을 컨텍스트로 넣어
`codex exec` CLI로 리뷰를 생성한 뒤 PR에 리뷰를 게시합니다.

## 특징

- GitHub App 설치 토큰 기반 인증 (PAT 불필요)
- diff가 아닌 **전체 코드베이스**를 컨텍스트로 사용
- Codex CLI를 `subprocess`로 호출 → 로그인된 ChatGPT 계정의 OAuth 토큰 사용 (기본 리뷰 순서 `gpt-5.6-sol` → `gpt-reserve` → `gpt-5.3-codex-spark`, 모두 `xhigh`)
- 한국어 리뷰 고정 출력 (JSON 스키마 강제)
- **리뷰 4섹션**: `좋은 점` / `🔴 반드시 수정할 사항` / `💡 권장 개선 사항` / `기술 단위 코멘트(라인 고정)`
- 라인 코멘트는 **4단계 등급**(`Critical` / `Major` / `Minor` / `Suggestion`) 으로 분류되고, PR 화면에서 각 코멘트 본문 최상단에 `[Critical] …` 형태의 대괄호 접두로 표기
- 라인 고정 코멘트만 인라인으로 게시, 라인 번호 없는 지적은 `개선할 점`으로 이동
- 기초 타입(`str`/`list`/`String`/`Array` 등) 수준의 팁 배제, **Python/TypeScript/React 공식 상위 API**에 초점
- 리뷰 큐는 **제한된 동시성**으로 처리 — `REVIEW_CONCURRENCY` env(기본 `1`=직렬)로 동시 리뷰 개수 조절. 같은 저장소에 대한 checkout 은 저장소별 lock 으로 직렬화되어 작업 트리 경쟁을 방지
- 컨텍스트 예산 초과 시 **자동 diff-only 모드 fallback** — 전체 코드베이스가 `CODEX_MAX_INPUT_TOKENS` 를 넘어 변경 파일이 빠지면, PR unified patch 만 가지고 리뷰를 계속한다. diff 조차 담기지 않으면 그때서야 안내 코멘트만 게시
- SOLID — 계층 분리, `Protocol`로 의존성 역전

## 아키텍처

### Admin 모델 저장 후 안전한 재기동

`admin.gorani.me` 연동 시 봇의 비공개 `scripts/local_review_env.sh`에
`CODEX_CONTROL_SHARED_SECRET`을 설정합니다. `openssl rand -hex 32`로 생성한 별도
난수이며 admin의 `ADMIN_CODEX_CONTROL_SHARED_SECRET`과 같아야 합니다. admin 로컬
개발에서는 `.env`, 운영에서는 비공개 LaunchAgent `EnvironmentVariables`에 설정합니다.
운영 LaunchAgent는 `.env`를 읽지 않습니다.
GitHub webhook, 프록시, Claude control secret과 재사용하지 마세요. 처음 적용할 때는
리뷰가 없는 시점에 기존 `scripts/run_webhook_server.sh`로 한 번 재시동해야 합니다.

- `/internal/control/status`, `/drain`, `/resume`, `/commit-restart`는 실제 loopback peer와
  `X-Gorani-Bot-Control-Secret`이 모두 맞아야 접근할 수 있습니다. Secret 미설정은
  접근 거부이며 `--no-proxy-headers`로 전달 헤더의 loopback 위장을 차단합니다.
- Drain은 작업 ID별 배타 lease로 신규 리뷰를 차단하고 이미 받은 큐와 실행 작업을
  최대 60초 기다립니다. 종료 인계 확정 전에는 타임아웃·고아 lease 만료로 intake를 재개합니다.
  이 구간의 새 delivery는 `503`이므로 GitHub에서 실패 delivery를 확인·재전송하세요.
- 관리 전용 `scripts/restart_webhook_server.sh --no-tail`은 인증된 drain 후에만
  호출합니다. 임의 PID/포트/명령 인자를 받지 않으며, `8022`의 단일 소유 프로세스와
  빈 큐를 확인하고 `operationId`·`instanceId`를 `/commit-restart`에 전달해 종료 인계를
  원자적으로 확정합니다. 확정한 서버가 응답 전송과 독립적으로 자기 자신에게 TERM을
  보내고, 스크립트는 포트가 비워진 뒤에만 새 서버를 시작합니다. 기존 PID나 다른
  listener에 종료 신호를 보내지 않습니다.
- 확정 상태(`restartCommitted=true`)에서는 `/resume`과 lease 만료가 접수를 다시 열지
  않습니다. 확정 응답 유실 시에도 서버는 종료를 진행하고, 관리 스크립트는 최대 15초 동안
  포트가 비워지는지 확인합니다. 종료되지 않으면 강제 종료나 새 서버 기동 없이 실패합니다.
  관리 프로세스 자체가 취소되면 서버 종료는 진행되지만 새 서버는 뜨지 않을 수 있습니다.
  이때 private 로그를 확인하고 `bash scripts/run_webhook_server.sh`로 수동 복구하세요.
- 고정 `.venv/bin/python -m uvicorn`을 `0.0.0.0:8022`로 시작하고 새 instance/PID를
  확인합니다. 기존 `HOST`, `PORT`, `VENV_DIR` 설정은 관리 재기동 경로를 바꾸지 않습니다.
  기존 `.runtime`과 별개인 owner-only `.admin-runtime/`에 잠금과 로그를 저장합니다.
  로그는 `O_APPEND`로 열어 기존 프로세스의 종료 로그와 새 기동 로그를 함께 보존합니다.
- Admin은 새 instance의 primary 모델·추론 강도·fallback 목록까지 검증합니다.
  실패하면 저장값은 보존하고 재시도를 표시합니다. 시작 실패 시 봇이 중지된 상태일 수
  있으므로 private 로그를 확인하고 수동으로 복구한 후 재시도하세요.

Control API나 `8022` 포트를 admin 공개 경로로 프록시하지 마세요. 여러 Uvicorn worker,
외부 supervisor 자동 재시작과의 혼용은 지원하지 않습니다. 기존 리뷰 댓글의 모델
표시는 해당 리뷰 당시 기록이므로 설정 변경이나 재기동으로 수정되지 않습니다.

### 리뷰 처리 흐름

단일 파일이 `FILE_MAX_BYTES` 또는 `DATA_FILE_MAX_BYTES`를 넘으면 전체 파일 대신
PR의 unified patch로 리뷰를 재시도합니다. 대형 테스트 파일도 작은 변경분은 검토할 수
있으며, `.reviewbot.yml`의 경로 제외·바이너리·생성물 제외 규칙은 재시도에서도 유지합니다.
patch가 없거나 변경분도 예산을 넘으면 해당 파일을 읽지 못했다는 사실을 표시합니다.

full/diff 프롬프트에는 PR의 전체 변경 파일 수와 각 파일의 전달·정책 제외·크기/예산 제외·
patch 누락 상태가 포함됩니다. 정책 제외 파일은 이름만 기록하며 본문은 전달하지 않습니다.
모델에는 읽지 못한 테스트를 “없음”으로 단정하지 말고 검증 불가로 구분하도록 지시합니다.
이는 판단 근거를 명확히 하는 장치이며 모델 오탐을 완전히 차단한다는 보장은 아닙니다.

```
GitHub PR event
  → FastAPI /github/webhook (HMAC 검증, 게시자 확인, 정상 시 202 즉시 응답)
  → asyncio.Queue (Semaphore(REVIEW_CONCURRENCY) 로 제한된 동시성)
      1. Installation Token 발급 (JWT → GitHub App API)
      2. PR 메타 / 변경 파일 조회
      3. git clone --filter=blob:none + checkout head SHA (캐시)
      4. 파일 수집 + 필터 + 우선순위 + 토큰 예산
      5. `codex exec --model ... -` 호출 (stdin: 프롬프트, 모델 실패 시 fallback 순서대로 재시도)
      6. JSON 파싱 → POST /pulls/{n}/reviews
```

```
src/codex_review/
├── interfaces/       # Protocol: GitHubClient, ReviewEngine, RepoFetcher, FileCollector
├── domain/           # PullRequest, ReviewResult, Finding, FileDump (frozen dataclass)
├── application/
│   ├── review_pr_use_case.py   # 오케스트레이션
│   └── webhook_handler.py      # HMAC 검증 + asyncio.Queue + Semaphore(N) 워커
├── infrastructure/
│   ├── github_app_client.py    # JWT → installation token → REST
│   ├── git_repo_fetcher.py     # clone/fetch/checkout
│   ├── file_dump_collector.py  # 필터 + 우선순위 + 토큰 예산
│   ├── codex_prompt.py         # 한국어 시스템 규칙 + 파일 직렬화
│   ├── codex_parser.py         # JSON 추출 + fallback
│   └── codex_cli_engine.py     # subprocess(codex exec) 호출
├── config.py         # pydantic-settings
└── main.py           # FastAPI 조립 (DI)
```

## 전제 조건

- macOS, Python 3.11+
- `git` 설치
- `codex` CLI가 PATH에 있고 ChatGPT 계정으로 로그인되어 있어야 함
  - 확인: `codex whoami` / `ls ~/.codex/auth.json`
- GitHub App 생성 및 대상 레포에 설치
  - 권한: Pull requests (R/W), Contents (R), Metadata (R)
  - 이벤트 구독: `Pull request`

## 설치

```bash
bash scripts/install_local_review.sh
cp scripts/local_review_env.example.sh scripts/local_review_env.sh
$EDITOR scripts/local_review_env.sh   # App ID / key path / webhook secret 입력
```

## 실행

```bash
bash scripts/run_webhook_server.sh
# → http://127.0.0.1:8000/github/webhook 수신 대기
```

테스트 웹훅 발사:
```bash
REPO_FULL_NAME=owner/repo PR_NUMBER=1 INSTALLATION_ID=1234567 \
    bash scripts/send_test_webhook.sh
```

## 릴리스 정보

- 현재 버전: [`VERSION.txt`](VERSION.txt)
- 변경 이력: [`CHANGELOG.md`](CHANGELOG.md)

## 환경 변수

> 참고: 기본 모델인 `gpt-5.6-sol`은 Codex CLI(ChatGPT auth) 카탈로그의 확장
> 입력 윈도우 872,000을 명시하고, 그 95%인 828,400을 유효 프롬프트 예산으로 사용한다.
> Spark를 1순위 모델로 운영할 때는 더 작은 입력 예산 `121600`을
> 자동으로 선택한다. 명시한 `CODEX_MAX_INPUT_TOKENS`는 그대로 검증한다.
> `CODEX_MODEL_CONTEXT_WINDOW`은
> 1순위 모델에만 전달되며 fallback 모델에는 적용되지 않는다.
> 모델 토큰 예산과 별개로 Codex CLI `turn/start` 입력은 1,048,576자 제한이 있으므로,
> 수집기는 리뷰 이력 공간을 남긴 1,000,000자에서 제한하고 큰 저장소는 diff-only로 전환한다.
> 추론 강도는 1순위와 모든 fallback 모델이 함께 지원해야 한다. 기본 모델인
> `gpt-5.6-sol`에서 `max`나 `ultra`를 사용하려면 호환되는 fallback만 지정하거나
> `CODEX_MODEL_FALLBACKS`를 비워야 한다. 알려진 비호환 조합은 서버 기동 시 거부된다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `GITHUB_APP_ID` | — | GitHub App ID (필수) |
| `GITHUB_APP_PRIVATE_KEY_PATH` | — | PEM 경로 (또는 `GITHUB_APP_PRIVATE_KEY` inline) |
| `GITHUB_WEBHOOK_SECRET` | — | HMAC 서명 검증용 비밀 (필수) |
| `CODEX_BIN` | `codex` | Codex CLI 실행 파일 |
| `CODEX_MODEL` | `gpt-5.6-sol` | 1순위 리뷰 모델 |
| `CODEX_MODEL_FALLBACKS` | `gpt-reserve,gpt-5.3-codex-spark` | 쉼표로 구분한 fallback 모델 목록. 기본 순서는 Sol `xhigh` → Reserve `xhigh` → Spark `xhigh`. 비우면 fallback 없이 `CODEX_MODEL`만 사용 |
| `CODEX_REASONING_EFFORT` | `xhigh` | `low`/`medium`/`high`/`xhigh`/`max`/`ultra`. 대소문자와 주변 공백을 정규화하고 알려진 모델 시퀀스의 호환성을 검증 |
| `CODEX_MODEL_CONTEXT_WINDOW` | `(모델별 자동)` | 1순위 모델에 전달할 Codex CLI `model_context_window`. 기본 Sol은 확장 `872000`; 다른 내장 모델은 CLI 기본값. 명시값은 모델별 카탈로그 최대값 이하로 제한 |
| `CODEX_MAX_INPUT_TOKENS` | `(모델 윈도우의 95%)` | 모델 입력 프롬프트 토큰 예산. Sol은 `828400`, 일반 272K 모델은 `258400`, Spark는 `121600`. 실제 수집 입력은 Codex CLI 제한에 맞춰 최대 1,000,000자로 제한 |
| `CODEX_TIMEOUT_SEC` | `600` | 호출 타임아웃 |
| `REPO_CACHE_DIR` | `~/.codex-review/repos` | clone 캐시 위치 |
| `GIT_TIMEOUT_SEC` | `120` | git clone/fetch/checkout/ls-files 호출 타임아웃 |
| `FILE_MAX_BYTES` | `204800` | 단일 파일 크기 상한 |
| `DATA_FILE_MAX_BYTES` | `20000` | JSON/YAML/XML 등 모호한 확장자의 별도 상한. `package.json`·`tsconfig.json`·`pyproject.toml` 같은 화이트리스트 매니페스트는 두 파일 크기 제한 모두 면제. 단 전체 컨텍스트 예산(`CODEX_MAX_INPUT_TOKENS`) 초과 시에는 우선순위에 따라 제외될 수 있음. |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | 바인딩 주소 |
| `REVIEW_CONCURRENCY` | `1` | 동시 실행 리뷰 개수. `1`=직렬, `2~`=병렬. Codex 쿼터와 맞춰 조절 |
| `REVIEW_QUEUE_MAXSIZE` | `(concurrency × 10)` | 웹훅 큐 상한. 가득 차면 503 반환. 비우면 자동 계산 |
| `CODEX_ENABLE_DIFF_FALLBACK` | `true` | 예산 초과 시 diff-only 모드 자동 전환. `false` 로 내리면 "리뷰 스킵 + 안내 코멘트" 경로만 사용 |
| `GITHUB_APP_SLUG` | — | GitHub App slug (예: `codex-review-bot`). 설정 시 follow-up 기능 활성화 — `synchronize`/`reopened` 이벤트에서 봇이 단 옛 코멘트의 자동 해소 여부를 판정해 답글 + thread resolve 처리. 미설정 시 리뷰 게시 전에 GitHub App identity API로 봇 login을 확인하며, 확인 실패 시 `503`을 반환해 GitHub webhook 재시도를 유도 (`DRY_RUN=1`에서는 확인 생략) |
| `DRY_RUN` | `0` | `1`이면 로그만 남기고 게시 안 함 |

## 동작 규칙

- 수신 이벤트: `opened`, `synchronize`, `reopened`, `ready_for_review`
- Draft PR은 skip
- 리뷰 게시자 identity를 확인할 수 없으면 큐에 넣지 않고 `503`을 반환해 GitHub가 webhook을 재시도하도록 함
- 리뷰 POST 전송 오류가 발생하면 동일 HEAD이고 게시 marker가 없을 때 1회 재시도하며, HEAD가 바뀌었거나 이미 게시된 경우 중복·stale 리뷰를 남기지 않음
- `DRY_RUN=1`이면 publisher identity 사전 확인을 포함한 GitHub mutation을 모두 건너뜀
- 파일 필터: `.git`, `node_modules`, `dist`, `build`, `vendor`, `__pycache__` 등 디렉터리와
  `*.lock`, 바이너리, 미디어, 폰트, `package-lock.json` 등은 자동 제외
- 우선순위: 변경 파일 → `src/app/lib/pkg/...` → 기타
- 예산 초과 시:
  1. **1차 fallback** — 변경 파일이 빠졌다면 diff-only 모드로 자동 전환하여 PR 의 unified patch 만 가지고 리뷰를 수행. 리뷰 본문 최상단에 `> ⚠️ 리뷰 범위: diff-only (자동 전환)` 배지가 표시됨.
  2. **2차 fallback** — diff 조차 예산을 넘거나 GitHub 가 patch 를 전혀 돌려주지 않으면 리뷰를 **수행하지 않고** PR에 안내 코멘트만 게시.

## 리뷰 출력 (4섹션 + 4단계 라인 등급)

모델은 아래 JSON 스키마를 엄격히 따라야 합니다.

```json
{
  "summary": "...",
  "event": "COMMENT | REQUEST_CHANGES | APPROVE",
  "positives":    ["좋은 점 ..."],
  "must_fix":     ["반드시 수정할 사항 (파일/모듈 단위) ..."],
  "improvements": ["권장 개선 사항 (파일/모듈 단위) ..."],
  "comments": [
    {
      "path": "src/x.py",
      "line": 42,
      "severity": "critical | major | minor | suggestion",
      "body": "기술 단위 코멘트 (라인 고정)"
    }
  ]
}
```

- `positives` / `must_fix` / `improvements` → PR 리뷰 본문 해당 섹션으로 렌더
- `comments` → GitHub 인라인 리뷰 코멘트로 라인에 붙음 (**line / severity 필수**)
- 인라인 코멘트 본문은 등급별 대괄호 접두와 함께 게시됨 — 예: `[Critical] None 체크 누락…`

### 라인 코멘트 등급 기준

| 등급 | 의미 | 대표 예시 |
|---|---|---|
| `critical` | 반드시 막아야 하는 문제 | 장애 가능성 · 데이터 손실 · 보안 취약점 · 크래시 |
| `major` | 머지 전에 고치는 게 좋은 문제 | 버그 가능성 · 예외 처리 누락 · 상태 불일치 · 동시성 · 큰 테스트 누락 |
| `minor` | 당장 큰 문제는 아니지만 개선 가치 있음 | 가독성 · 중복 · 네이밍 · 구조 개선 |
| `suggestion` | 선택 제안 | 대안 · 취향 차이 · 리팩터링 아이디어 |

`critical` / `major` 가 하나라도 있으면 리뷰 이벤트는 자동으로 `REQUEST_CHANGES` 로 승격됩니다(모델이 `COMMENT` 로 내려도 서버가 덮어씀).

### 기술 단위 코멘트의 취향

기초 수준(`str`/`list`/`String`/`Array`/`JSON.parse` 등)의 팁은 제외하도록 프롬프트에서 강제합니다.
대신 아래와 같은 **공식 상위 API** 사용을 지적/권장하도록 유도합니다.

- **Python**: `collections.Counter/defaultdict/deque`, `itertools`, `functools.cache/singledispatch`,
  `dataclasses(frozen=True, slots=True)`, `typing.Protocol/TypedDict/assert_never`,
  `pathlib.Path`, `contextlib.ExitStack/suppress`, `asyncio.TaskGroup`, `enum.StrEnum`, pydantic `BaseModel`
- **TypeScript**: `Map/Set/WeakMap/WeakRef`, 유틸리티 타입(`Readonly/Pick/Omit/ReturnType/Awaited`),
  `satisfies`, discriminated union exhaustiveness, `structuredClone`, `AbortController`,
  `Promise.allSettled/any`, `Intl.*`, Zod `z.infer`
- **React**: `useMemo/useCallback`의 올바른 의존성, `useReducer`, `useId`,
  `useSyncExternalStore`, `startTransition`, `useDeferredValue`, `Suspense/ErrorBoundary`,
  `use()` hook, `useFormStatus/useOptimistic`, React Query `queryKey/staleTime`

모두 `src/codex_review/infrastructure/codex_prompt.py`에서 조정 가능합니다.

## 테스트

```bash
.venv/bin/pytest tests/unit -q
```

## 배포 (선택)

- `deploy/nginx-codex-review.conf`: 리버스 프록시 예시 (TLS, `/github/webhook`, `/healthz`)
- macOS LaunchAgent / `tmux`+`nohup` 등으로 서버 상주
- GitHub App 웹훅 URL을 nginx 엔드포인트로 지정, 시크릿을 `GITHUB_WEBHOOK_SECRET`과 일치시킬 것

## 참고

설계상 `/Users/m4_25/develop/codereview`의 운영 패턴(HMAC 검증 → 202 즉시 응답 →
백그라운드 처리, 구조화 로그, App JWT 흐름)을 재사용했습니다.
본 프로젝트는 MLX 로컬 모델 대신 **Codex CLI + OAuth**를 사용하고,
**diff가 아닌 전체 코드**를 컨텍스트로 넣는다는 점에서 차이가 있습니다.
