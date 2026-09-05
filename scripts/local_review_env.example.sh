#!/usr/bin/env bash
# Copy to `scripts/local_review_env.sh` and fill in. That file is gitignored.

# --- GitHub App ---
export GITHUB_APP_ID="123456"
export GITHUB_APP_PRIVATE_KEY_PATH="/absolute/path/to/codex-review.private-key.pem"
export GITHUB_WEBHOOK_SECRET="change-me-long-random"

# Optional admin.gorani.me restart control: openssl rand -hex 32
# Match admin ADMIN_CODEX_CONTROL_SHARED_SECRET: local .env or production LaunchAgent.
# Production does not read .env. Use a separate secret, not the webhook/proxy secret.
# export CODEX_CONTROL_SHARED_SECRET="GENERATE_INDEPENDENT_256_BIT_SECRET"

# --- Codex CLI ---
# Available models (queryable via ~/.codex/models_cache.json):
#   gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-reserve, gpt-5.5, gpt-5.4,
#   gpt-5.4-mini, gpt-5.3-codex-spark, codex-auto-review
export CODEX_MODEL="gpt-5.6-sol"
export CODEX_MODEL_FALLBACKS="gpt-reserve,gpt-5.3-codex-spark"
export CODEX_REASONING_EFFORT="xhigh"  # low | medium | high | xhigh | max | ultra (모델별 지원)
# 1순위 Sol 에만 Codex CLI 확장 컨텍스트를 적용한다. fallback 모델은 각자 CLI
# 카탈로그 기본 윈도우를 유지하며, 큰 입력을 못 받으면 diff-only 로 재시도한다.
export CODEX_MODEL_CONTEXT_WINDOW="872000"
export CODEX_MAX_INPUT_TOKENS="828400"
export CODEX_TIMEOUT_SEC="600"
# npm 또는 Homebrew로 설치한 CLI를 쓴다면:
#   export CODEX_BIN="/opt/homebrew/bin/codex"
# Codex Desktop app bundle이 설치돼 있다면 앱 번들의 최신 경로를 확인해 지정한다.

# --- Repo cache / files ---
export REPO_CACHE_DIR="$HOME/.codex-review/repos"
export GIT_TIMEOUT_SEC="120"
export FILE_MAX_BYTES="204800"
# JSON/YAML/XML 같은 모호한 확장자에 대한 더 엄격한 상한 (설정/매니페스트 이름은 예외로 항상 포함).
export DATA_FILE_MAX_BYTES="20000"

# --- Server ---
export HOST="127.0.0.1"
export PORT="8000"
# 동시에 처리할 리뷰 개수. 1 이면 완전 직렬 (기본). 2~ 로 올리면 PR 이 동시에 들어왔을 때
# 병렬 처리. Codex 쿼터 여유와 맞춰 조절한다.
export REVIEW_CONCURRENCY="1"
# export DRY_RUN="1"    # uncomment to log reviews without posting
