"""Track consecutive failed scans to report a block once and its recovery once."""

from __future__ import annotations

import datetime


class ScanHealth:
    """Counts consecutive failed scan jobs.

    ``record_failure`` returns True once, when the streak reaches ``threshold``.
    ``record_success`` returns True once, when a reported streak ends.
    """

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.failures = 0
        self.status: str | None = None
        self.since: datetime.datetime | None = None
        self.reported = False

    def record_failure(self, status: str) -> bool:
        if self.failures == 0:
            self.since = datetime.datetime.now()
        self.failures += 1
        self.status = status
        if self.failures >= self.threshold and not self.reported:
            self.reported = True
            return True
        return False

    def record_success(self) -> bool:
        recovered = self.reported
        self.failures = 0
        self.status = None
        self.since = None
        self.reported = False
        return recovered
