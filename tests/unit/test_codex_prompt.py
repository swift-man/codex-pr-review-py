from codex_review.domain import FileDump, FileEntry, PullRequest, RepoRef
from codex_review.infrastructure.codex_prompt import build_prompt


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("octo", "demo"),
        number=7,
        title="제목",
        body="본문",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://github.com/octo/demo.git",
        changed_files=("src/a.py",),
        installation_id=1,
        is_draft=False,
    )


def test_prompt_contains_four_section_schema_and_korean_rule() -> None:
    dump = FileDump(
        entries=(FileEntry(path="src/a.py", content="x=1\ny=2", size_bytes=7, is_changed=True),),
        total_chars=7,
    )
    prompt = build_prompt(_pr(), dump)

    assert "한국어" in prompt
    assert "positives" in prompt
    assert "must_fix" in prompt
    assert "improvements" in prompt
    assert "comments" in prompt
    assert "--- FILE: src/a.py [CHANGED] ---" in prompt
    assert "    1| x=1" in prompt
    assert "    2| y=2" in prompt


def test_prompt_requires_line_numbers_and_severity_for_comments() -> None:
    prompt = build_prompt(_pr(), FileDump(entries=(), total_chars=0))
    assert "라인 번호" in prompt or "line" in prompt
    assert "severity" in prompt
    assert "반드시" in prompt


def test_prompt_declares_four_severity_levels() -> None:
    """회귀: LLM 에 네 단계 등급(critical/major/minor/suggestion) 을 명확히 지시한다."""
    prompt = build_prompt(_pr(), FileDump(entries=(), total_chars=0))
    # 각 값이 JSON 스키마 선언에 등장해야 한다.
    assert '"critical"' in prompt
    assert '"major"' in prompt
    assert '"minor"' in prompt
    assert '"suggestion"' in prompt
    # 각 등급의 판단 기준 키워드가 함께 설명돼야 모델이 의미 있게 고른다.
    assert "장애" in prompt or "데이터 손실" in prompt
    assert "버그 가능성" in prompt
    assert "가독성" in prompt
    assert "선택 제안" in prompt or "리팩터링" in prompt
    # 레거시 값을 쓰지 말라는 명시적 경고.
    assert "must_fix" in prompt  # must_fix 섹션 자체는 유지되므로 단어 존재는 OK
    # 다만 severity 에서는 네 값만 허용됨이 명시돼야 한다.
    assert "4단계 이외의 값" in prompt or "네 값 중 하나" in prompt


def test_prompt_lists_review_priority() -> None:
    """1~8 우선순위 리스트가 프롬프트에 포함돼 모델이 이 순서로 훑도록 한다."""
    prompt = build_prompt(_pr(), FileDump(entries=(), total_chars=0))
    assert "버그 가능성" in prompt
    assert "예외 처리" in prompt
    assert "동시성" in prompt
    assert "보안" in prompt
    assert "테스트" in prompt
    assert "설계" in prompt or "가독성" in prompt


def test_prompt_has_tone_rules() -> None:
    """모호한 칭찬 금지·가능성 표현·일반론 금지 규칙이 프롬프트에 박혀 있다."""
    prompt = build_prompt(_pr(), FileDump(entries=(), total_chars=0))
    assert "가능성" in prompt
    assert "깔끔합니다" in prompt          # 금지 예시로 명시
    assert "일반론" in prompt
    assert "왜 문제인지" in prompt         # 이유 + 수정 방향 동시 요구


def test_prompt_has_role_declaration() -> None:
    prompt = build_prompt(_pr(), FileDump(entries=(), total_chars=0))
    assert "시니어" in prompt
    assert "리뷰어" in prompt


def test_prompt_mentions_idiomatic_api_taste() -> None:
    prompt = build_prompt(_pr(), FileDump(entries=(), total_chars=0))
    assert "pathlib" in prompt
    assert "useMemo" in prompt or "useCallback" in prompt
    assert "Protocol" in prompt


def test_prompt_mentions_exclusions_when_budget_truncated() -> None:
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("big/foo.py",),
        exceeded_budget=True,
    )
    prompt = build_prompt(_pr(), dump)
    assert "제외된 파일" in prompt
    assert "big/foo.py" in prompt


# ---------------------------------------------------------------------------
# REVIEW HISTORY 섹션 — 이전 라운드 코멘트 / 다른 봇 의견 노출
# ---------------------------------------------------------------------------


from datetime import datetime

from codex_review.domain import ReviewComment, ReviewHistory


def test_prompt_omits_history_section_when_empty() -> None:
    """첫 리뷰 호환성: history 가 None 이거나 비어 있으면 섹션 자체 생략."""
    dump = FileDump(
        entries=(FileEntry(path="x.py", content="a", size_bytes=1, is_changed=True),),
        total_chars=1,
    )
    prompt_no_history = build_prompt(_pr(), dump)
    prompt_empty_history = build_prompt(_pr(), dump, history=ReviewHistory())
    # SYSTEM_RULES 본문에는 "REVIEW HISTORY" 가 메타리플라이 안내 일부로 등장 가능 →
    # 실제 섹션 헤더 (`=== REVIEW HISTORY ===`) 의 부재로 판단.
    assert "=== REVIEW HISTORY ===" not in prompt_no_history
    assert "=== REVIEW HISTORY ===" not in prompt_empty_history


def test_prompt_renders_history_section_chronologically() -> None:
    """history 가 있으면 섹션이 추가되고 시간순 (오래된 → 최신) 으로 직렬화."""
    dump = FileDump(
        entries=(FileEntry(path="x.py", content="a", size_bytes=1, is_changed=True),),
        total_chars=1,
    )
    history = ReviewHistory(comments=(
        ReviewComment(
            author_login="gemini-pr-review-bot[bot]",
            kind="inline",
            body="[Major] phantom quote 가능성",
            created_at=datetime(2026, 5, 1, 12, 0, 0),
            comment_id=12345,
            path="x.py",
            line=10,
        ),
        ReviewComment(
            author_login="codex-review-bot[bot]",
            kind="review-summary",
            body="이전 라운드 우리 봇 리뷰 본문",
            created_at=datetime(2026, 5, 2, 3, 0, 0),
        ),
    ))

    prompt = build_prompt(_pr(), dump, history=history)
    assert "=== REVIEW HISTORY ===" in prompt
    # 두 코멘트 모두 본문에 등장.
    assert "phantom quote" in prompt
    assert "이전 라운드 우리 봇 리뷰" in prompt
    # 시간순: 5월 1일 항목이 5월 2일 항목보다 앞에.
    pos_inline = prompt.find("phantom quote")
    pos_summary = prompt.find("이전 라운드 우리 봇 리뷰")
    assert 0 < pos_inline < pos_summary
    # inline 항목은 comment_id 노출 — 메타리플라이 타깃 회수용.
    assert "comment_id=12345" in prompt
    # deferred 키워드 가이드가 동봉됐는지.
    assert "deferred" in prompt or "별도 PR" in prompt


def test_prompt_history_truncates_oldest_when_exceeding_total_cap() -> None:
    """누적 크기 cap 초과 시 가장 오래된 항목부터 drop. 최근 라운드 정보 우선."""
    dump = FileDump(
        entries=(FileEntry(path="x.py", content="a", size_bytes=1, is_changed=True),),
        total_chars=1,
    )
    # 각 코멘트 약 1300 chars (cap 1500 미만이라 per-comment truncation 안 발동).
    big_body = "X" * 1300
    comments = tuple(
        ReviewComment(
            author_login=f"bot{i}",
            kind="issue",
            body=big_body + f" #{i}",
            created_at=datetime(2026, 5, i + 1, 0, 0, 0),
        )
        for i in range(15)  # 1300 * 15 ≈ 19500 chars > _HISTORY_TOTAL_CAP=12000
    )
    history = ReviewHistory(comments=comments)

    prompt = build_prompt(_pr(), dump, history=history)
    # 가장 오래된 #0 은 잘려 나가야 한다.
    assert "#0" not in prompt
    # 가장 최신 #14 는 보존돼야 한다.
    assert "#14" in prompt
