from dataclasses import replace

import pytest

from voice_mode.broker import (
    HostCapability,
    HostDisposition,
    HostErrorKind,
    HostProbe,
    HostThreadSummary,
)
from voice_mode.broker.hosts import (
    HostAdapter,
    HostAdapterError,
    ThreadSelectionSource,
    select_thread,
)


class SelectionAdapter(HostAdapter):
    def __init__(self, threads=(), *, available=True):
        self.threads = tuple(threads)
        self.probe_result = HostProbe(
            "selection-fake", available, frozenset(HostCapability), reason="offline" if not available else None
        )
        self.calls = []

    def probe(self):
        self.calls.append(("probe",))
        return self.probe_result

    def list_threads(self, repo_root=None):
        self.calls.append(("list", repo_root))
        return self.threads

    def read_thread(self, thread_id):
        raise NotImplementedError

    def attach_thread(self, thread_id):
        self.calls.append(("attach", thread_id))
        return next(thread for thread in self.threads if thread.thread_id == thread_id)

    def create_thread(self, repo_root, label):
        self.calls.append(("create", repo_root, label))
        return HostThreadSummary("created", repo_root, label, active=True, broker_owned=True)

    def start_turn(self, **kwargs):
        raise NotImplementedError

    def steer_turn(self, **kwargs):
        raise NotImplementedError

    def interrupt_turn(self, **kwargs):
        raise NotImplementedError

    def subscribe(self, sink):
        raise NotImplementedError

    def query_disposition(self, **kwargs):
        return HostDisposition.ABSENT

    def close(self):
        return None


def summary(thread_id, repo_root, *, active=True):
    return HostThreadSummary(thread_id, str(repo_root), active=active)


def test_explicit_thread_wins_without_listing(tmp_path):
    adapter = SelectionAdapter((summary("explicit", tmp_path),))
    selected = select_thread(adapter, tmp_path, explicit_thread_id="explicit")
    assert selected.source is ThreadSelectionSource.EXPLICIT
    assert selected.thread.thread_id == "explicit"
    assert adapter.calls == [("probe",), ("attach", "explicit")]


def test_registered_host_thread_is_second_priority(tmp_path):
    adapter = SelectionAdapter((summary("registered", tmp_path),))
    selected = select_thread(adapter, tmp_path, registered_thread_id="registered")
    assert selected.source is ThreadSelectionSource.REGISTERED
    assert selected.thread.thread_id == "registered"


def test_single_active_repository_thread_is_autodetected(tmp_path):
    adapter = SelectionAdapter(
        (
            summary("inactive", tmp_path, active=False),
            summary("active", tmp_path),
            summary("elsewhere", tmp_path / "elsewhere"),
        )
    )
    selected = select_thread(adapter, tmp_path)
    assert selected.source is ThreadSelectionSource.AUTODETECTED
    assert selected.thread.thread_id == "active"


@pytest.mark.parametrize("active_count", [0, 2])
def test_zero_or_many_active_threads_create_labeled_broker_thread(tmp_path, active_count):
    threads = tuple(
        summary(f"thread-{index}", tmp_path, active=index < active_count)
        for index in range(max(1, active_count))
    )
    adapter = SelectionAdapter(threads)
    selected = select_thread(adapter, tmp_path)
    assert selected.source is ThreadSelectionSource.CREATED
    assert selected.thread.thread_id == "created"
    assert selected.thread.broker_owned is True
    assert selected.alternatives == tuple(sorted(thread.thread_id for thread in threads))
    assert selected.reason


def test_new_thread_never_lists_or_guesses(tmp_path):
    adapter = SelectionAdapter((summary("existing", tmp_path),))
    selected = select_thread(adapter, tmp_path, new_thread=True, label="Voice only")
    assert selected.source is ThreadSelectionSource.CREATED
    assert selected.thread.title == "Voice only"
    assert all(call[0] != "list" for call in adapter.calls)


def test_conflicting_or_unavailable_selection_fails_before_thread_io(tmp_path):
    adapter = SelectionAdapter((summary("thread", tmp_path),))
    with pytest.raises(HostAdapterError) as caught:
        select_thread(
            adapter,
            tmp_path,
            explicit_thread_id="thread",
            new_thread=True,
        )
    assert caught.value.kind is HostErrorKind.AMBIGUOUS
    assert adapter.calls == []

    unavailable = SelectionAdapter(available=False)
    with pytest.raises(HostAdapterError) as caught:
        select_thread(unavailable, tmp_path)
    assert caught.value.kind is HostErrorKind.UNAVAILABLE


def test_repository_comparison_is_canonical(tmp_path):
    canonical = summary("same", tmp_path)
    adapter = SelectionAdapter((replace(canonical, repo_root=str(tmp_path / ".")),))
    assert select_thread(adapter, tmp_path / ".").thread.thread_id == "same"
