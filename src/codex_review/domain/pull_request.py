from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class PullRequest:
    repo: RepoRef
    number: int
    title: str
    body: str
    head_sha: str
    head_ref: str
    base_sha: str
    base_ref: str
    clone_url: str
    changed_files: tuple[str, ...]
    installation_id: int
    is_draft: bool
    # path → 해당 파일에서 인라인 코멘트를 달 수 있는 RIGHT-side 라인 번호 집합.
    # unified diff 의 context( ) 와 add(+) 라인이 포함되며, 모델이 이 범위 밖에 코멘트를
    # 제안하면 GitHub 가 422 로 거부하므로 `post_review` 직전에 필터링 기준으로 쓴다.
    # PR 을 아직 조회하지 못한 상태(예: 테스트)에서는 빈 dict 를 기본값으로 둔다.
    diff_right_lines: dict[str, frozenset[int]] = field(default_factory=dict)
