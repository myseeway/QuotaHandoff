"""Per-account query pacing, independent of the 5% scheduling policy."""
import random

NORMAL = 30
NEAR_LIMIT = 10
BACKGROUND = 60
MIN_REFRESH = 30


def interval(mode, account, current, active, waiting):
    if mode != 'auto' or waiting or account.get('key') != current:
        return BACKGROUND
    remaining = account.get('remaining')
    return NEAR_LIMIT if active and remaining is not None and remaining <= 15 else NORMAL


def due_at(record, account, now, seconds, requested=False, review=False, waiting=False):
    last = record.get('attemptedAt')
    if last is None:
        return max(now, record.get('retryAt', 0))
    last = max(last, record.get('completedAt', last))
    due = last + seconds
    if requested or review:
        # A queued manual refresh must not postpone an already earlier automatic check.
        due = min(due, last + MIN_REFRESH)
    if waiting:
        blocking = [w for w in account.get('windows', []) if w.get('remaining', 100) <= 5]
        if blocking and all(w.get('resetsAt') for w in blocking):
            reset = max(w['resetsAt'] for w in blocking)
            if reset > last:
                due = min(due, max(last + MIN_REFRESH, reset + 2))
    return max(due, record.get('retryAt', 0))


def failed(record, now, jitter=None):
    count = min(record.get('failures', 0) + 1, 10)
    delay = min(1800, 120 * 2 ** (count - 1))
    jitter = random.uniform(0, .1) if jitter is None else max(0, min(.1, jitter))
    return dict(record, failures=count, retryAt=now + min(1800, delay * (1 + jitter)))
