"""Controller-owned append-only visible interaction records; never credentials."""
from datetime import datetime, timezone
import json
import os


class Journal:
    def __init__(self, path, clock, start):
        self.path, self.clock, self.start = path, clock, start
        with path.open('x', encoding='utf-8'):
            pass
        self.sequence = 0

    def write(self, event, **fields):
        self.sequence += 1
        record = dict(sequence=self.sequence, event=event,
                      utc=datetime.now(timezone.utc).isoformat(),
                      elapsed_seconds=self.clock() - self.start, **fields)
        with self.path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
