# AGENTS.md

이 문서는 본 저장소에서 작업하는 AI 에이전트 및 개발자를 위한 가이드입니다.

## 프로젝트 개요

- 언어: Python 3.11+
- 설계 원칙: SOLID
- 패키지 관리: `uv` 또는 `pip` + `venv`
- 테스트: `pytest`
- 타입 체크: `mypy` (strict)
- 린트/포맷: `ruff`, `black`

## 핵심 개발 원칙 (SOLID)

모든 코드는 SOLID 원칙을 준수해야 합니다.

### 1. SRP (Single Responsibility Principle)
- 하나의 클래스/모듈은 하나의 변경 이유만 가진다.
- 함수는 한 가지 일만 수행한다.
- 파일당 하나의 주요 클래스를 권장한다.

### 2. OCP (Open/Closed Principle)
- 확장에는 열려 있고, 수정에는 닫혀 있어야 한다.
- 새로운 동작은 기존 코드 수정이 아닌 추가(상속/조합)로 구현한다.
- `abc.ABC`를 이용한 추상 클래스 또는 `typing.Protocol`을 적극 활용한다.

### 3. LSP (Liskov Substitution Principle)
- 하위 타입은 상위 타입을 대체할 수 있어야 한다.
- 하위 클래스는 상위 클래스의 계약(contract)을 깨지 않는다.
- 예외를 더 강하게 던지거나 반환 타입을 좁히지 않는다.

### 4. ISP (Interface Segregation Principle)
- 사용하지 않는 메서드에 의존하도록 강요하지 않는다.
- 크고 일반적인 인터페이스 대신, 작고 구체적인 인터페이스를 여러 개 만든다.
- `Protocol`을 사용해 역할별로 인터페이스를 분리한다.

### 5. DIP (Dependency Inversion Principle)
- 상위 모듈은 하위 모듈에 의존하지 않는다. 둘 다 추상화에 의존한다.
- 구체 클래스가 아닌 `Protocol`/`ABC`에 의존한다.
- 의존성은 생성자 주입(Constructor Injection)을 기본으로 한다.

## 프로젝트 구조

```
.
├── src/
│   └── <package_name>/
│       ├── __init__.py
│       ├── domain/          # 엔티티, 값 객체, 도메인 서비스
│       ├── application/     # 유스케이스, 애플리케이션 서비스
│       ├── infrastructure/  # 외부 시스템 어댑터 (DB, HTTP 등)
│       └── interfaces/      # Protocol / ABC 정의
├── tests/
│   ├── unit/
│   └── integration/
├── pyproject.toml
├── README.md
└── AGENTS.md
```

계층 간 의존 방향: `interfaces` ← `domain` ← `application` ← `infrastructure`
(상위 계층이 하위 계층의 추상화에만 의존)

## 코딩 규칙

- 모든 public 함수/메서드에 타입 힌트를 붙인다.
- 가변 전역 상태 금지. 의존성은 주입한다.
- `print` 대신 `logging` 모듈을 사용한다.
- 예외는 구체적으로 처리한다. `except Exception:` 지양.
- 매직 넘버/문자열은 상수 또는 Enum으로 추출한다.
- 함수 길이는 50줄, 클래스는 200줄을 넘지 않도록 한다.

## 예시 패턴

```python
from typing import Protocol
from dataclasses import dataclass

# interfaces/
class UserRepository(Protocol):
    def find_by_id(self, user_id: str) -> "User | None": ...
    def save(self, user: "User") -> None: ...

# domain/
@dataclass(frozen=True)
class User:
    id: str
    email: str

# application/ — DIP: 추상화에 의존
class RegisterUserUseCase:
    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    def execute(self, user_id: str, email: str) -> User:
        user = User(id=user_id, email=email)
        self._repo.save(user)
        return user

# infrastructure/ — 구체 구현
class InMemoryUserRepository:
    def __init__(self) -> None:
        self._store: dict[str, User] = {}

    def find_by_id(self, user_id: str) -> User | None:
        return self._store.get(user_id)

    def save(self, user: User) -> None:
        self._store[user.id] = user
```

## 테스트 규칙

- 모든 새 기능에는 단위 테스트를 작성한다.
- 테스트는 AAA (Arrange-Act-Assert) 구조를 따른다.
- 외부 의존성은 Fake/Stub으로 대체한다 (DIP의 이점 활용).
- 커버리지 목표: 핵심 도메인 90%+, 전체 80%+.

## 커밋 및 PR

- 커밋 메시지: `<type>: <subject>` (예: `feat: add user registration use case`)
- PR 생성 전: `ruff check`, `mypy`, `pytest` 모두 통과해야 한다.
- 한 PR은 하나의 논리적 변경만 포함한다.
- `main`/기본 브랜치에는 직접 push 하지 않는다. 모든 변경은 작업 브랜치에서 커밋 후 PR로 제출한다.
- 사용자가 "즉시 머지", "바로 반영"을 요청해도 먼저 PR을 생성하고, PR URL과 검증 결과를 공유한 뒤 머지 절차를 따른다.
- 실수로 `main`/기본 브랜치에 직접 push 한 경우 즉시 사용자에게 알리고, 직접 push 된 커밋을 되돌리는 PR과 원래 변경 PR을 분리해 복구한다.

## 코드리뷰 분석 및 대응

- 코드리뷰 분석은 Git PR의 변경사항, 리뷰 댓글, 대댓글 흐름을 기준으로 수행한다.
- 코드리뷰를 처리할 때는 관련 PR 코드리뷰 댓글에 대댓글을 달거나, 필요한 경우 새 댓글을 남긴다.
- 댓글에는 어떤 리뷰를 어떻게 처리했는지와 남은 이슈가 있는지를 명확히 적는다.
- 코드가 변경되면 변경사항 반영을 위한 재시작 명령어를 사용자에게 안내한다.

## 에이전트가 지켜야 할 것

1. 기존 코드 스타일과 구조를 먼저 파악한 뒤 수정한다.
2. SOLID 원칙을 위반하는 변경은 거부하거나 리팩터링을 제안한다.
3. 불필요한 추상화는 피한다 (YAGNI). 단, 계층 경계는 유지한다.
4. 변경 범위를 최소화한다. 요청되지 않은 리팩터링은 하지 않는다.
5. 새 파일 생성보다 기존 파일 수정을 우선한다.

<!-- BEGIN GSTACK-CODEX MANAGED BLOCK -->
## gstack — AI Engineering Workflow

This block is managed by `gstack-codex`. Do not edit inside this block.

Skills live in `.agents/skills`. Invoke them by name, e.g. `/office-hours`.
Refresh with `npx gstack-codex init --project`.
This repo currently has the `full` pack installed.

## Available skills

| Skill | What it does |
|-------|-------------|
| `/office-hours` | YC Office Hours — two modes. Startup mode: six forcing questions that expose demand reality, status quo, desperate specificity, narrowest wedge, observation, and future-fit. |
| `/plan-ceo-review` | CEO/founder-mode plan review. Rethink the problem, find the 10-star product, challenge premises, expand scope when it creates a better product. |
| `/plan-eng-review` | Eng manager-mode plan review. Lock in the execution plan — architecture, data flow, diagrams, edge cases, test coverage, performance. |
| `/plan-design-review` | Designer's eye plan review — interactive, like CEO and Eng review. |
| `/design-consultation` | Design consultation: understands your product, researches the landscape, proposes a complete design system (aesthetic, typography, color, layout, spacing, motion), and generates font+color preview pages. |
| `/review` | Pre-landing PR review. Analyzes diff against the base branch for SQL safety, LLM trust boundary violations, conditional side effects, and other structural issues. |
| `/investigate` | Systematic debugging with root cause investigation. Four phases: investigate, analyze, hypothesize, implement. |
| `/design-review` | Designer's eye QA: finds visual inconsistency, spacing issues, hierarchy problems, AI slop patterns, and slow interactions — then fixes them. |
| `/qa` | Systematically QA test a web application and fix bugs found. |
| `/qa-only` | Report-only QA testing. Systematically tests a web application and produces a structured report with health score, screenshots, and repro steps — but never fixes anything. |
| `/ship` | Ship workflow: detect + merge base branch, run tests, review diff, bump VERSION, update CHANGELOG, commit, push, create PR. |
| `/document-release` | Post-ship documentation update. Reads all project docs, cross-references the diff, builds a Diataxis coverage map (reference/how-to/tutorial/explanation), updates README/ARCHITECTURE/CONTRIBUTING/CLAUDE.md to match what shipped, detects architecture diagram drift, polishes CHANGELOG voice with a sell-test rubric, cleans up TODOS, and optionally bumps VERSION. |
| `/retro` | Weekly engineering retrospective. Analyzes commit history, work patterns, and code quality metrics with persistent history and trend tracking. |
| `/browse` | Fast headless browser for QA testing and site dogfooding. Navigate any URL, interact with elements, verify page state, diff before/after actions, take annotated screenshots, check responsive layouts, test forms and uploads, handle dialogs, and assert element states. |
| `/setup-browser-cookies` | Import cookies from your real Chromium browser into the headless browse session. |
| `/careful` | Safety guardrails for destructive commands. Warns before rm -rf, DROP TABLE, force-push, git reset --hard, kubectl delete, and similar destructive operations. |
| `/freeze` | Restrict file edits to a specific directory for the session. |
| `/guard` | Full safety mode: destructive command warnings + directory-scoped edits. |
| `/unfreeze` | Clear the freeze boundary set by /freeze, allowing edits to all directories again. |
| `/gstack-upgrade` | Upgrade gstack to the latest version. Detects global vs vendored install, runs the upgrade, and shows what's new. |
| `/autoplan` | Auto-review pipeline — reads the full CEO, design, eng, and DX review skills from disk and runs them sequentially with auto-decisions using 6 decision principles. |
| `/benchmark` | Performance regression detection using the browse daemon. Establishes baselines for page load times, Core Web Vitals, and resource sizes. |
| `/benchmark-models` | Cross-model benchmark for gstack skills. Runs the same prompt through Claude, GPT (via Codex CLI), and Gemini side-by-side — compares latency, tokens, cost, and optionally quality via LLM judge. |
| `/canary` | Post-deploy canary monitoring. Watches the live app for console errors, performance regressions, and page failures using the browse daemon. |
| `/claude` | Claude Code CLI wrapper for non-Claude hosts - three modes. Review: independent diff review via claude -p. |
| `/context-restore` | Restore working context saved earlier by /context-save. Loads the most recent saved state (across all branches by default) so you can pick up where you left off — even across Conductor workspace handoffs. |
| `/context-save` | Save working context. Captures git state, decisions made, and remaining work so any future session can pick up without losing a beat. |
| `/cso` | Chief Security Officer mode. Infrastructure-first security audit: secrets archaeology, dependency supply chain, CI/CD pipeline security, LLM/AI security, skill supply chain scanning, plus OWASP Top 10, STRIDE threat modeling, and active verification. |
| `/design-html` | Design finalization: generates production-quality Pretext-native HTML/CSS. |
| `/design-shotgun` | Design shotgun: generate multiple AI design variants, open a comparison board, collect structured feedback, and iterate. |
| `/devex-review` | Live developer experience audit. Uses the browse tool to actually TEST the developer experience: navigates docs, tries the getting started flow, times TTHW, screenshots error messages, evaluates CLI help text. |
| `/diagram` | Turn an English description (or mermaid source) into a diagram triplet: the source, an editable .excalidraw file you can open on excalidraw.com, and rendered SVG + PNG (clean mermaid style; the .excalidraw carries the hand-drawn aesthetic). |
| `/document-generate` | Generate missing documentation from scratch for a feature, module, or entire project. |
| `/health` | Code quality dashboard. Wraps existing project tools (type checker, linter, test runner, dead code detector, shell linter), computes a weighted composite 0-10 score, and tracks trends over time. |
| `/ios-clean` | Remove the DebugBridge SPM package and all #if DEBUG wiring from an iOS app. |
| `/ios-design-review` | Visual design audit for iOS apps on real hardware. Connects to a real iPhone via the same StateServer as /ios-qa, screenshots every screen, evaluates against Apple HIG, DESIGN.md, and design best practices. |
| `/ios-fix` | Autonomous iOS bug fixer. Takes a bug found by /ios-qa, reads the source, writes the fix, rebuilds, redeploys, and verifies the fix on the real device. |
| `/ios-qa` | Live-device iOS QA for SwiftUI apps. Connects to a real iPhone via USB CoreDevice IPv6 tunnel, reads Swift source to understand every screen, then runs a vision-driven agent loop: screenshot → analyze → decide → act → verify → repeat. |
| `/ios-sync` | Regenerate the iOS debug bridge against the latest upstream gstack templates. |
| `/land-and-deploy` | Land and deploy workflow. Merges the PR, waits for CI and deploy, verifies production health via canary checks. |
| `/landing-report` | Read-only queue dashboard for workspace-aware ship. Shows which VERSION slots are currently claimed by open PRs, which sibling Conductor workspaces have WIP work likely to ship soon, and what slot /ship would pick next. |
| `/learn` | Manage project learnings. Review, search, prune, and export what gstack has learned across sessions. |
| `/make-pdf` | Turn any markdown file into a publication-quality PDF. Proper 1in margins, intelligent page breaks, page numbers, cover pages, running headers, curly quotes and em dashes, clickable TOC, diagonal DRAFT watermark. |
| `/open-gstack-browser` | Launch GStack Browser — AI-controlled Chromium with the sidebar extension baked in. |
| `/pair-agent` | Pair a remote AI agent with your browser. One command generates a setup key and prints instructions the other agent can follow to connect. |
| `/plan-devex-review` | Interactive developer experience plan review. Explores developer personas, benchmarks against competitors, designs magical moments, and traces friction points before scoring. |
| `/plan-tune` | Self-tuning question sensitivity + developer psychographic for gstack (v1: observational). |
| `/scrape` | Pull data from a web page. First call on a new intent prototypes the flow via $B primitives and returns JSON. |
| `/setup-deploy` | Configure deployment settings for /land-and-deploy. Detects your deploy platform (Fly.io, Render, Vercel, Netlify, Heroku, GitHub Actions, custom), production URL, health check endpoints, and deploy status commands. |
| `/setup-gbrain` | Set up gbrain for this coding agent: install the CLI, initialize a local PGLite or Supabase brain, register MCP, capture per-remote trust policy. |
| `/skillify` | Codify the most recent successful /scrape flow into a permanent browser-skill on disk. |
| `/spec` | Turn vague intent into a precise, executable spec in five phases. |
| `/sync-gbrain` | Keep gbrain current with this repo's code and refresh agent search guidance in CLAUDE.md. |

Repo installs include the full generated skill pack. Heavy browser/runtime binaries stay machine-local in v1.
Installed release: `0.2.8`
<!-- END GSTACK-CODEX MANAGED BLOCK -->
