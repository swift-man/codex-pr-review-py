import asyncio

from codex_review.application.intake_control import IntakeControl


async def test_pause_is_exclusive_and_only_lease_owner_can_resume() -> None:
    intake = IntakeControl()
    try:
        assert intake.pause("a" * 64, 60)
        assert not intake.pause("b" * 64, 60)
        assert not intake.resume("b" * 64)
        assert intake.operation_id == "a" * 64
        assert intake.resume("a" * 64)
        assert intake.pause("b" * 64, 60)
    finally:
        await intake.close()
    assert intake.operation_id is None


async def test_expired_lease_restores_intake_and_allows_new_owner() -> None:
    intake = IntakeControl()
    assert intake.pause("a" * 64, 0)
    async with asyncio.timeout(1):
        while intake.operation_id is not None:
            await asyncio.sleep(0)
    assert intake.pause("b" * 64, 60)
    await intake.close()


async def test_committed_restart_cannot_expire_resume_or_reopen_on_close() -> None:
    intake = IntakeControl()
    assert intake.pause("a" * 64, 0)
    assert intake.commit_restart("a" * 64)
    assert not intake.resume("a" * 64)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert intake.operation_id == "a" * 64
    assert not intake.pause("b" * 64, 60)
    await intake.close()
    assert intake.restart_committed
    assert intake.operation_id == "a" * 64


async def test_cancelled_old_expiry_cannot_release_replacement_lease() -> None:
    intake = IntakeControl()
    assert intake.pause("a" * 64, 0)
    assert intake.resume("a" * 64)
    assert intake.pause("b" * 64, 60)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert intake.operation_id == "b" * 64
    await intake.close()
