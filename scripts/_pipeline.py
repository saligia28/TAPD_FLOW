from __future__ import annotations

import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple, TypeVar

T = TypeVar("T")


@dataclass
class StepRecord:
    name: str
    success: bool
    duration: float
    message: str = ""


class NotificationDispatcher:
    """Background dispatcher with retry/backoff semantics."""

    def __init__(
        self,
        sender: Optional[Callable[[str], bool]],
        *,
        max_retries: int = 3,
        base_delay: float = 1.5,
    ) -> None:
        self._sender = sender
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        if sender is not None:
            self._worker = threading.Thread(target=self._worker_loop, name="notify-dispatcher", daemon=True)
            self._worker.start()

    def enqueue(self, content: str) -> None:
        if self._sender is None:
            return
        self._queue.put(content)

    def close(self, *, timeout: float = 8.0) -> None:
        if self._worker is None:
            return
        self._queue.join()
        self._stop.set()
        self._worker.join(timeout=timeout)

    def _worker_loop(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                content = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._deliver(content)
            finally:
                self._queue.task_done()

    def _deliver(self, content: str) -> None:
        assert self._sender is not None
        for attempt in range(1, self._max_retries + 1):
            try:
                ok = self._sender(content)
            except Exception as exc:  # pragma: no cover - defensive branch
                ok = False
                print(f"[notify] 发送异常（第 {attempt} 次）：{exc}")
            if ok:
                if attempt > 1:
                    print(f"[notify] 重试第 {attempt} 次后发送成功。")
                return
            if attempt < self._max_retries:
                delay = self._base_delay * attempt
                print(f"[notify] 发送失败，第 {attempt} 次；{delay:.1f}s 后重试…")
                time.sleep(delay)
        print("[notify] 超过最大重试次数，通知已放弃。")


def run_step(name: str, func: Callable[[], T]) -> Tuple[Optional[T], StepRecord]:
    start = time.time()
    print(f"[pipeline] ➤ 开始步骤：{name}")
    sys.stdout.flush()
    try:
        result = func()
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # pragma: no cover - surfaced to caller
        duration = time.time() - start
        message = str(exc)
        print(f"[pipeline] ✖ 步骤失败：{name}（{duration:.2f}s）原因：{message}")
        sys.stdout.flush()
        return None, StepRecord(name=name, success=False, duration=duration, message=message)
    duration = time.time() - start
    print(f"[pipeline] ✓ 步骤完成：{name}（{duration:.2f}s）")
    sys.stdout.flush()
    return result, StepRecord(name=name, success=True, duration=duration)


def format_failure_markdown(step: str, message: str) -> str:
    return "\n".join(
        [
            "**执行失败告警**",
            f"- 步骤：{step}",
            f"- 原因：{message[:200]}",
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}",
        ]
    )
