# import traceback
import json
import os
import time
from concurrent.futures import (
    Executor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
    Future,
)
from contextlib import contextmanager, AbstractContextManager
from itertools import chain, groupby
from multiprocessing import get_context
from pathlib import Path
from queue import Queue
from random import random
from threading import Barrier
from typing import (
    Any,
    TypeVar,
    Callable,
    Optional,
    Type,
    Protocol,
    cast,
    TypedDict,
    Union,
    Literal,
)

import pytest

from dreadlocks import (
    AcquiringLockWouldBlockError,
    AcquiringProcessLevelLockWouldBlockError,
    AcquiringThreadLevelLockWouldBlockError,
    RecursiveDeadlockError,
    path_lock,
    process_level_path_lock,
    thread_level_path_lock,
)

T = TypeVar("T")

mp = get_context(method="spawn")


class chdir(AbstractContextManager[None]):
    """Non thread-safe context manager to change the current working directory.
    (copied from contextlib's implementation in Python 3.11)
    """

    def __init__(self, path: str):
        self.path: str = path
        self._old_cwd: list[str] = []

    def __enter__(self):
        self._old_cwd.append(os.getcwd())
        os.chdir(self.path)

    def __exit__(self, *_):
        os.chdir(self._old_cwd.pop())


class Manager(Protocol):
    def Barrier(
        self,
        parties: int,
        action: Optional[Callable[[], None]] = None,
        timeout: Optional[int] = None,
    ) -> Barrier: ...

    def Queue(self, maxsize: int = 0) -> Queue[Any]: ...


class SimpleThreadManager:
    def Barrier(
        self,
        parties: int,
        action: Optional[Callable[[], None]] = None,
        timeout: Optional[int] = None,
    ) -> Barrier:
        return Barrier(parties, action, timeout)

    def Queue(self, maxsize: int = 0) -> Queue[Any]:
        return Queue[Any](maxsize)


class Parallelization(Protocol):
    def __call__(self, n: int) -> AbstractContextManager[tuple[Executor, Manager]]: ...


@contextmanager
def processes(n: int):
    with ProcessPoolExecutor(max_workers=n, mp_context=mp) as executor:
        with mp.Manager() as m:
            yield executor, cast(Manager, m)


@contextmanager
def threads(n: int):
    with ThreadPoolExecutor(max_workers=n) as executor:
        yield executor, SimpleThreadManager()


@contextmanager
def lock(directory: str):
    with chdir(directory):
        lock = Path("lock")
        lock.touch()
        path = str(lock)
        yield path


def lock_first(is_locked: Barrier, is_done: Barrier, path: str, shared: bool):
    # print("CALL", "lock_first", path, shared)
    with path_lock(path, shared=shared, blocking=False):
        # print("ENTER", "lock_first", path, shared)
        is_locked.wait()
        # print("WAIT", "lock_first", path, shared)
        is_done.wait()
        # print("DONE", "lock_first", path, shared)


def lock_rest(
    is_locked: Barrier,
    is_done: Optional[Barrier],
    path: str,
    shared: bool,
    blocking: bool,
):
    # print("CALL", "lock_rest", path, shared, blocking)
    is_locked.wait()
    # print("WITH", "lock_rest", path, shared, blocking)
    with path_lock(path, shared=shared, blocking=blocking):
        # print("ENTER", "lock_rest", path, shared, blocking)
        if is_done is not None and shared:
            # print("WAIT", "lock_rest", path, shared, blocking)
            is_done.wait()
            # print("DONE", "lock_rest", path, shared, blocking)


@pytest.mark.parametrize(
    "parallelization, exception",
    (
        (threads, AcquiringThreadLevelLockWouldBlockError),
        (processes, AcquiringProcessLevelLockWouldBlockError),
    ),
)
@pytest.mark.parametrize(
    "shared",
    (
        [False, True],
        [False, False],
        [False, True, False],
        [False, True, True],
        [False, False, True],
        [False, False, False],
        [True, False],
        [True, True, False],
        [True, False, False],
    ),
    ids=repr,
)
def test_non_blocking(
    tmp_path: str,
    parallelization: Parallelization,
    exception: Type[AcquiringLockWouldBlockError],
    shared: list[bool],
):
    if os.name == "nt" and "processes" in repr(parallelization):
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    assert len(shared) >= 2
    assert not shared[0] or not shared[-1]

    with lock(tmp_path) as path:
        n = len(shared)
        nb = 1 + (
            1
            if not shared[0]
            or (os.name == "nt" and "processes" in repr(parallelization))
            else sum(map(lambda s: 1 if s else 0, shared))
        )

        with parallelization(n) as [executor, m]:
            is_locked = m.Barrier(n)
            is_done = m.Barrier(nb)

            tasks: list[Future[None]] = []

            tasks.append(
                executor.submit(lock_first, is_locked, is_done, path, shared[0])
            )
            for s in shared[1:-1]:
                tasks.append(
                    executor.submit(
                        lock_rest,
                        is_locked,
                        is_done if nb >= 3 else None,
                        path,
                        s,
                        True,
                    )
                )
            tasks.append(
                executor.submit(lock_rest, is_locked, None, path, shared[-1], False)
            )

            with pytest.raises(exception):
                tasks[-1].result()

            is_done.wait()

            for task in tasks[:-1]:
                task.result()


def locked_threads(
    fn: Callable[..., T], parameters: list[tuple[Any, ...]], is_done: Barrier
):
    n = len(parameters)
    with threads(n) as (executor, _):
        tasks: list[Future[T]] = []
        for params in parameters:
            tasks.append(executor.submit(fn, *params))
        for task in as_completed(tasks):
            try:
                task.result()
            except:  # noqa E722
                is_done.wait()
                raise


@pytest.mark.parametrize("n_blocking", (0, 1, 2, 3, 4, 5))
def test_non_blocking_processes_and_threads(tmp_path: str, n_blocking: int):
    if os.name == "nt":
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    with lock(tmp_path) as path:
        with processes(2) as (executor, m):
            is_locked = m.Barrier(2 + n_blocking)
            is_done = m.Barrier(2)

            t1 = executor.submit(lock_first, is_locked, is_done, path, False)

            blocking = [(is_locked, None, path, True, True)] * n_blocking
            non_blocking = [(is_locked, None, path, True, False)]
            parameters = blocking + non_blocking

            t2 = executor.submit(
                locked_threads,
                lock_rest,
                parameters,
                is_done,
            )

            with pytest.raises(AcquiringProcessLevelLockWouldBlockError):
                t2.result()

            t1.result()


def lock_shared(are_locked: Barrier, q: Queue[int], path: str, i: int):
    with path_lock(path, shared=True):
        are_locked.wait()
        time.sleep(1)
        q.put(i)


def lock_exclusive(are_locked: Barrier, q: Queue[int], path: str, i: int):
    are_locked.wait()
    with path_lock(path, shared=False):
        q.put(i)


@pytest.mark.parametrize("parallelization", (threads, processes))
def test_many_shared_one_exclusive_blocking(
    tmp_path: str, parallelization: Parallelization
):
    if os.name == "nt" and "processes" in repr(parallelization):
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    with lock(tmp_path) as path:
        n = 10

        with parallelization(n) as [executor, m]:
            results_queue = m.Queue()

            # NOTE: We run the test twice to reuse workers to catch errors where
            # some workers are left in a locked state.
            for _ in range(2):
                are_locked = m.Barrier(n)

                for i in range(1, n):
                    executor.submit(lock_shared, are_locked, results_queue, path, i)
                last = executor.submit(
                    lock_exclusive, are_locked, results_queue, path, 0
                )

                last.result()

                results: list[int] = []
                for i in range(n):
                    results.append(results_queue.get())

                assert sorted(results) == sorted(range(n))
                assert results[-1] == 0


class KeyedLock(Protocol):
    def __call__(
        self,
        key: str,
        shared: bool = False,
        blocking: bool = True,
        reentrant: bool = False,
    ) -> AbstractContextManager[Any]: ...


@pytest.mark.parametrize(
    "acquire_lock", (path_lock, thread_level_path_lock, process_level_path_lock)
)
@pytest.mark.parametrize("shared", (True, False))
def test_non_reentrant_dead_lock(tmp_path: str, acquire_lock: KeyedLock, shared: bool):
    with lock(tmp_path) as path:
        with acquire_lock(path, shared=shared):
            with pytest.raises(RecursiveDeadlockError):
                with acquire_lock(path, shared=shared):
                    pass


@pytest.mark.parametrize("shared", (True, False))
def test_reentrant(tmp_path: str, shared: bool):
    with lock(tmp_path) as path:
        with path_lock(path, shared=shared, reentrant=False):
            with path_lock(path, shared=shared, reentrant=True):
                pass

        with path_lock(path, shared=shared, reentrant=True):
            with path_lock(path, shared=shared, reentrant=True):
                pass

        with path_lock(path, shared=shared, reentrant=False):
            with path_lock(path, shared=shared, reentrant=True):
                with path_lock(path, shared=shared, reentrant=True):
                    pass


@pytest.mark.parametrize(
    "shared",
    (
        [True, False],
        [False, True],
        [True, False, True],
        [False, True, False],
        [True, True, False],
        [False, False, True],
    ),
    ids=repr,
)
def test_reentrant_mixed(tmp_path: str, shared: list[bool]):
    with lock(tmp_path) as path:

        def rec(types: list[bool]):
            if types:
                shared, *rest = types
                with path_lock(path, shared=shared, reentrant=True):
                    rec(rest)

        rec(shared)


def exclusive_thread_write(path: str, i: int):
    try:
        with path_lock(path, shared=False) as fd:
            # print('reading {}'.format(i))
            try:
                with open(fd, closefd=False) as fp:
                    fp.seek(0)
                    done: list[int] = json.load(fp)
            except json.decoder.JSONDecodeError:
                done: list[int] = []

            done.append(i)
            # print('adding {}'.format(i))
            with open(fd, "w", closefd=False) as fp:
                fp.seek(0)
                fp.truncate(0)
                json.dump(done, fp)
                fp.seek(0)
            # print('done {}'.format(i))
    except Exception:
        # print(e)
        # print(traceback.format_exc())
        raise


def many_exclusive_threads_and_processes_rw_process(path: str, indices: list[int]):
    try:
        with ThreadPoolExecutor(max_workers=len(indices)) as executor:
            executor.map(exclusive_thread_write, [path] * len(indices), indices)
    except Exception:
        # print(e)
        # print(traceback.format_exc())
        raise


def test_many_exclusive_threads_and_processes_rw(tmp_path: str):
    if os.name == "nt":
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    with lock(tmp_path) as path:
        m = 100
        n = m**2
        items = list(range(n))
        partition: list[list[int]] = list(
            map(
                lambda g: list(map(lambda t: t[1], g[1])),
                groupby(enumerate(items), lambda t: t[0] // m),
            )
        )

        assert len(partition) == m
        assert sorted(chain(*partition)) == items

        with mp.Pool(processes=len(partition)) as pool:
            pool.starmap(
                many_exclusive_threads_and_processes_rw_process,
                map(lambda part: (path, part), partition),
            )

        with path_lock(path, shared=False) as fd:
            with open(fd, closefd=False) as fp:
                fp.seek(0)
                results = json.load(fp)
                fp.seek(0)

        assert sorted(results) == sorted(range(n))


@pytest.mark.parametrize("parallelization, n", ((threads, 200), (processes, 20)))
def test_many_exclusive(tmp_path: str, parallelization: Parallelization, n: int):
    if os.name == "nt" and "processes" in repr(parallelization):
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    with lock(tmp_path) as path:
        items = list(range(n))

        with parallelization(n) as (executor, _):
            executor.map(
                exclusive_thread_write,
                [path] * n,
                items,
            )

        with path_lock(path, shared=False) as fd:
            with open(fd, closefd=False) as fp:
                fp.seek(0)
                results = json.load(fp)
                fp.seek(0)

        assert sorted(results) == sorted(range(n))


def lock_shared_chained(
    path: str,
    first_is_locked: Barrier,
    recvp: Queue[int],
    send: Queue[int],
    recvn: Queue[int],
    results: Queue[int],
    n: int,
    i: int,
):
    if i > 0:
        j = recvp.get()  # Wait for previous thread to be locked
        assert j == i - 1
    with path_lock(path, shared=True):
        if i == 0:
            first_is_locked.wait()  # Allow exclusive lock attempt
        results.put(i)
        send.put(i)  # Notify next thread that we are locked
        send.put(i)  # Notify previous thread that we are locked
        if i < n - 2:
            j = recvn.get()  # Wait for next thread to be locked
            assert j == i + 1


@pytest.mark.parametrize("parallelization", (threads, processes))
def test_chained_shared_one_exclusive_blocking(
    tmp_path: str, parallelization: Parallelization
):
    if os.name == "nt" and "processes" in repr(parallelization):
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    with lock(tmp_path) as path:
        n = 10

        with parallelization(n) as (executor, m):
            results_queue = m.Queue()

            # NOTE: We run the test twice to reuse workers to catch errors where
            # some workers are left in a locked state.
            for _ in range(2):
                first_is_locked = m.Barrier(2)
                queues = [m.Queue() for _ in range(n + 1)]

                for i in range(n - 1):
                    executor.submit(
                        lock_shared_chained,
                        path,
                        first_is_locked,
                        queues[i],
                        queues[i + 1],
                        queues[i + 2],
                        results_queue,
                        n,
                        i,
                    )
                last = executor.submit(
                    lock_exclusive, first_is_locked, results_queue, path, n - 1
                )

                last.result()

                results: list[int] = []
                for i in range(n):
                    results.append(results_queue.get())

                assert results == sorted(range(n))


class Contents(TypedDict):
    id: int
    counter: int
    copy: int


class Message(TypedDict):
    type: Union[Literal["read"], Literal["write"]]
    id: int
    contents: Contents


def sync_read(
    path: str, filename: str, before: Barrier, after: Barrier, q: Queue[Message], i: int
):
    before.wait()
    time.sleep(0.01 * random())
    with path_lock(path, shared=True):
        after.wait()
        with open(filename) as fp:
            contents = json.load(fp)
    q.put({"type": "read", "id": i, "contents": contents})


def sync_write(path: str, filename: str, q: Queue[Message], i: int):
    with path_lock(path, shared=False):
        with open(filename) as fp:
            contents = json.load(fp)

        contents["id"] = i
        contents["counter"] += 1

        with open(filename, "w") as fp:
            json.dump(contents, fp)

        with open(filename) as fp:
            contents = json.load(fp)

        contents["copy"] += 1

        with open(filename, "w") as fp:
            json.dump(contents, fp)

    q.put({"type": "write", "id": i, "contents": contents})


@pytest.mark.parametrize("parallelization", (threads, processes))
def test_synchronized_reads_blocking(tmp_path: str, parallelization: Parallelization):
    if os.name == "nt" and "processes" in repr(parallelization):
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    with lock(tmp_path) as path:
        filename = "rw"

        with open(filename, "w") as fp:
            json.dump({"id": -1, "counter": 0, "copy": 0}, fp)

        n = 1000
        k = 7
        p = 10
        max_write = p - k

        with parallelization(p) as [executor, m]:
            q = m.Queue()
            i = 0
            g = 0
            w = 0
            messages: list[Message] = []
            group: dict[int, int] = {}
            while i < n:
                c = min(n - i, k)
                if w >= max_write or random() < 1 / (c + 1):
                    before = m.Barrier(c)
                    after = m.Barrier(c)
                    for _ in range(c):
                        executor.submit(sync_read, path, filename, before, after, q, i)
                        group[i] = g
                        i += 1
                    g += 1
                else:
                    executor.submit(sync_write, path, filename, q, i)
                    i += 1
                    w += 1

                while (
                    not q.empty() or len(messages) < i - k
                ):  # NOTE: We empty the queue as much as possible
                    messages.append(q.get())
                    if messages[-1]["type"] == "write":
                        w -= 1

            while len(messages) < n:
                messages.append(q.get())

        values: dict[int, int] = {}
        for message in messages:
            t = message["type"]
            if t == "read":
                i = message["id"]
                g = group[i]
                counter = message["contents"]["counter"]
                # NOTE: counter is the same for the whole group
                assert values.setdefault(g, counter) == counter
                # NOTE: copy is identical to counter
                assert message["contents"]["copy"] == counter
            else:
                assert t == "write"
                # NOTE: check nobody else wrote to file while we held the lock
                assert message["id"] == message["contents"]["id"]
                assert message["contents"]["copy"] == message["contents"]["counter"]


@pytest.mark.parametrize("n_shared", (1, 5))
@pytest.mark.parametrize("n_exclusive", (1, 5))
def test_blocking_processes_and_threads(tmp_path: str, n_shared: int, n_exclusive: int):
    """
    Process B spawns 1 thread that acquires a shared lock.
    Process A spawns m threads that acquire a shared lock and n threads that
    acquire an exclusive lock.
    Process B waits on communication from all threads of process A with a
    shared lock.
    The concurrent exclusive locks should not result in a dead lock.
    """
    if os.name == "nt":
        pytest.skip("TODO Processes-based tests randomly fail on Windows.")

    with lock(tmp_path) as path:
        with processes(2) as (executor, m):
            is_locked = m.Barrier(1 + n_shared + n_exclusive)
            is_done = m.Barrier(1 + n_shared)

            t1 = executor.submit(lock_first, is_locked, is_done, path, True)

            shared = [(is_locked, is_done, path, True, True)] * n_shared
            exclusive = [(is_locked, None, path, False, True)] * n_exclusive
            parameters = shared + exclusive

            t2 = executor.submit(
                locked_threads,
                lock_rest,
                parameters,
                is_done,
            )

            t1.result()

            t2.result()


"""
And vice versa, if one
thread of process A acquires an exclusive lock at the process-level first,
then attempts to exclusively acquire the thread-level lock, that
thread-level lock may already be acquired as shared by other threads of
process A, but they are waiting on a thread of process B that is in turn
waiting for the process-level lock to be downgraded to shared.
"""
