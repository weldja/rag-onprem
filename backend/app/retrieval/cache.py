"""In-memory cache of model answers, keyed on exactly what the model would read.

The key is a hash of the model settings and the full message list: system
prompt, history and the user turn holding the retrieved excerpts and the
question. So:
- Access control is preserved by construction. A hit needs the same excerpts,
  which the asking user's own (ACL-filtered) retrieval just returned.
- Edited, re-indexed or deleted documents change or drop excerpts, so stale
  answers are never served; nothing has to be invalidated.
- Retrieval still runs on every question (well under a second); only the model
  call, which takes seconds, is skipped.

Lives in the api process (single uvicorn process). Holds customer text in
memory only; never written to disk or logged.
"""

import hashlib
import json
import threading
import time
from collections import OrderedDict

from app.llm.base import ChatMessage


class AnswerCache:
    def __init__(self, max_entries: int, ttl_s: float, salt: str = "") -> None:
        self.max_entries = max_entries
        self.ttl_s = ttl_s
        self._salt = salt  # model name and options: a model change misses
        self._items: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def key(self, messages: list[ChatMessage]) -> str:
        payload = json.dumps([self._salt, messages], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(self, key: str) -> str | None:
        if self.max_entries <= 0:
            return None
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            stored_at, text = item
            if time.monotonic() - stored_at > self.ttl_s:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return text

    def put(self, key: str, text: str) -> None:
        if self.max_entries <= 0:
            return
        with self._lock:
            self._items[key] = (time.monotonic(), text)
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def __len__(self) -> int:
        return len(self._items)
