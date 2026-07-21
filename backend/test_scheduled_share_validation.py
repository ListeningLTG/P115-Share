import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.services import scheduler
from app.services.p115 import P115Service


def snapshot(cid, items, size, files, folders):
    return {
        "cid": cid,
        "items": items,
        "stats": {
            "size": size,
            "file_count": files,
            "folder_count": folders,
        },
    }


class FakeClock:
    def __init__(self, step=1):
        self.value = 0
        self.step = step

    def monotonic(self):
        self.value += self.step
        return self.value


async def no_sleep(_seconds):
    return None


async def test_stable_source_snapshot_resets_after_error():
    expected = snapshot(1, [{"id": "a", "name": "A", "is_dir": True}], 10, 1, 1)
    responses = [expected, RuntimeError("temporary"), expected, expected]

    async def fake_read(_svc, _cid):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    with patch.object(scheduler, "_read_dir_snapshot", fake_read), patch.object(
        scheduler.asyncio, "sleep", no_sleep
    ):
        result = await scheduler._capture_stable_dir_snapshot(
            object(), 1, attempts=4, interval=0
        )
    assert result == expected


async def test_copy_waits_for_complete_stats_and_duplicate_names():
    source_items = [
        {"id": "1", "name": "same", "is_dir": False},
        {"id": "2", "name": "same", "is_dir": True},
    ]
    baseline = snapshot(1, source_items, 100, 1, 1)
    partial = snapshot(
        2,
        [{"id": "11", "name": "same", "is_dir": False}],
        50,
        1,
        0,
    )
    complete = snapshot(
        2,
        [
            {"id": "11", "name": "same", "is_dir": False},
            {"id": "12", "name": "same", "is_dir": True},
        ],
        100,
        1,
        1,
    )
    responses = [partial, complete, complete, complete]

    async def fake_read(_svc, _cid):
        return responses.pop(0)

    with patch.object(scheduler, "_read_dir_snapshot", fake_read), patch.object(
        scheduler.asyncio, "sleep", no_sleep
    ):
        result = await scheduler._wait_transfer_complete(
            SimpleNamespace(client=SimpleNamespace()),
            mode="copy",
            source_cid=1,
            target_cid=2,
            baseline=baseline,
            stable_required=3,
        )
    assert result == complete


async def test_move_allows_new_source_items_after_snapshot():
    items = [{"id": "1", "name": "A", "is_dir": True}]
    baseline = snapshot(1, items, 100, 2, 1)
    complete = snapshot(2, items, 100, 2, 1)

    class Service:
        client = SimpleNamespace()

        async def _get_dir_items(self, _cid, *, strict=False):
            assert strict
            return [{"id": "new", "name": "later", "is_dir": False}]

    async def fake_read(_svc, _cid):
        return complete

    with patch.object(scheduler, "_read_dir_snapshot", fake_read), patch.object(
        scheduler.asyncio, "sleep", no_sleep
    ):
        result = await scheduler._wait_transfer_complete(
            Service(),
            mode="move",
            source_cid=1,
            target_cid=2,
            baseline=baseline,
            stable_required=3,
        )
    assert result == complete


async def test_move_progress_failure_stops_immediately():
    class Service:
        client = SimpleNamespace(fs_move_progress=object())

        async def _api_call_with_timeout(self, *_args, **_kwargs):
            return {"state": True, "data": {"status": "failed"}}

        def _is_margin_response(self, _resp):
            return False

    try:
        await scheduler._read_move_progress(Service(), "move-task")
    except scheduler.MoveProgressFailed:
        pass
    else:
        raise AssertionError("移动任务明确失败时必须立即终止")


async def test_incomplete_cached_stats_time_out():
    baseline = snapshot(
        1, [{"id": "1", "name": "A", "is_dir": True}], 100, 2, 1
    )
    partial = snapshot(
        2, [{"id": "2", "name": "A", "is_dir": True}], 50, 1, 1
    )
    clock = FakeClock(step=2)

    async def fake_read(_svc, _cid):
        return partial

    with patch.object(scheduler, "_read_dir_snapshot", fake_read), patch.object(
        scheduler.asyncio, "sleep", no_sleep
    ), patch.object(scheduler.time, "monotonic", clock.monotonic):
        try:
            await scheduler._wait_transfer_complete(
                SimpleNamespace(client=SimpleNamespace()),
                mode="copy",
                source_cid=1,
                target_cid=2,
                baseline=baseline,
                no_progress_timeout=8,
                absolute_timeout=100,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("不完整但稳定的缓存统计不应通过")


async def test_absolute_timeout_stops_continuously_changing_target():
    baseline = snapshot(
        1, [{"id": "1", "name": "A", "is_dir": False}], 100, 1, 0
    )
    clock = FakeClock(step=10)
    sequence = 0

    async def fake_read(_svc, _cid):
        nonlocal sequence
        sequence += 1
        return snapshot(
            2,
            [{"id": str(sequence), "name": "A", "is_dir": False}],
            sequence,
            1,
            0,
        )

    with patch.object(scheduler, "_read_dir_snapshot", fake_read), patch.object(
        scheduler.asyncio, "sleep", no_sleep
    ), patch.object(scheduler.time, "monotonic", clock.monotonic):
        try:
            await scheduler._wait_transfer_complete(
                SimpleNamespace(client=SimpleNamespace()),
                mode="copy",
                source_cid=1,
                target_cid=2,
                baseline=baseline,
                no_progress_timeout=1000,
                absolute_timeout=25,
            )
        except TimeoutError:
            pass
        else:
            raise AssertionError("持续变化的目标也必须受绝对超时限制")


async def test_margin_response_cannot_be_treated_as_empty_snapshot():
    class Service:
        client = SimpleNamespace(fs_category_get_app=object())

        async def _get_dir_items(self, _cid, *, strict=False):
            assert strict
            return []

        async def _api_call_with_timeout(self, *_args, **_kwargs):
            return {"margin": 5}

        def _is_margin_response(self, response):
            return "margin" in response

        def _get_ios_ua_kwargs(self):
            return {}

    try:
        await scheduler._read_dir_snapshot(Service(), 1)
    except RuntimeError:
        pass
    else:
        raise AssertionError("限速响应不能被当成空目录快照")


async def test_strict_directory_listing_does_not_turn_error_into_empty():
    class Service:
        client = SimpleNamespace(fs_files_app2=object())

        async def _api_call_with_timeout(self, *_args, **_kwargs):
            raise RuntimeError("page failed")

        def _is_margin_response(self, _resp):
            return False

        def _get_ios_ua_kwargs(self):
            return {}

    service = Service()
    non_strict = await P115Service._get_dir_items(service, 1)
    assert non_strict == []
    try:
        await P115Service._get_dir_items(service, 1, strict=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("严格目录读取必须传播分页/API错误")


async def test_directory_listing_continues_after_short_page():
    pages = [
        [
            {"fid": "1", "n": "one"},
            {"cid": "2", "n": "two"},
        ],
        [{"fid": "3", "n": "three"}],
        [],
    ]

    class Service:
        client = SimpleNamespace(fs_files_app2=object())

        def __init__(self):
            self.offsets = []

        async def _api_call_with_timeout(self, _func, payload, **_kwargs):
            self.offsets.append(payload["offset"])
            return {"state": True, "data": pages.pop(0)}

        def _is_margin_response(self, _resp):
            return False

        def _get_ios_ua_kwargs(self):
            return {}

    service = Service()
    items = await P115Service._get_dir_items(service, 1, strict=True)
    assert [item["id"] for item in items] == ["1", "2", "3"]
    assert service.offsets == [0, 2, 3]


async def test_cleanup_switch_disabled_keeps_temp_dir():
    class Service:
        client = SimpleNamespace(fs_delete_app=object())

        async def _api_call_with_timeout(self, *_args, **_kwargs):
            raise AssertionError("关闭清理开关时不应调用删除接口")

        async def cleanup_recycle_bin(self):
            raise AssertionError("关闭清理开关时不应清空回收站")

        def _get_ios_ua_kwargs(self):
            return {}

    result = await scheduler._cleanup_scheduled_temp_dir(
        Service(), cid=2, folder_name="temp", enabled=False
    )
    assert result is False


async def test_cleanup_switch_enabled_deletes_and_clears_recycle_bin():
    class Service:
        client = SimpleNamespace(fs_delete_app=object())

        def __init__(self):
            self.deleted = None
            self.recycle_cleaned = False

        async def _api_call_with_timeout(self, _func, fids, **_kwargs):
            self.deleted = fids
            return {"state": True}

        async def cleanup_recycle_bin(self):
            self.recycle_cleaned = True
            return True

        def _get_ios_ua_kwargs(self):
            return {}

    service = Service()
    with patch.object(scheduler.asyncio, "sleep", no_sleep):
        result = await scheduler._cleanup_scheduled_temp_dir(
            service, cid=2, folder_name="temp", enabled=True
        )
    assert result is True
    assert service.deleted == [2]
    assert service.recycle_cleaned is True


async def main():
    await test_stable_source_snapshot_resets_after_error()
    await test_copy_waits_for_complete_stats_and_duplicate_names()
    await test_move_allows_new_source_items_after_snapshot()
    await test_move_progress_failure_stops_immediately()
    await test_incomplete_cached_stats_time_out()
    await test_absolute_timeout_stops_continuously_changing_target()
    await test_margin_response_cannot_be_treated_as_empty_snapshot()
    await test_strict_directory_listing_does_not_turn_error_into_empty()
    await test_directory_listing_continues_after_short_page()
    await test_cleanup_switch_disabled_keeps_temp_dir()
    await test_cleanup_switch_enabled_deletes_and_clears_recycle_bin()
    print("All scheduled share validation tests passed")


if __name__ == "__main__":
    asyncio.run(main())
