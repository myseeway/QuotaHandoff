"""Pure scheduling policy. No credentials, I/O, or side effects."""
import math

THRESHOLD = 5
MAX_AGE = 120


def normalize(result, now):
    buckets = result.get('rateLimitsByLimitId')
    if not buckets:
        one = result.get('rateLimits')
        buckets = {one.get('limitId') or 'codex': one} if one else {}
    windows = []
    denied = result.get('ordinaryUsageAllowed') is False
    for key, bucket in buckets.items():
        denied |= bool(bucket.get('spendControlReached') or bucket.get('rateLimitReachedType'))
        for slot in ('primary', 'secondary'):
            w = bucket.get(slot)
            if not w:
                continue
            used, minutes = w.get('usedPercent'), w.get('windowDurationMins')
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
                raise ValueError('额度百分比无效')
            if not isinstance(minutes, (int, float)) or minutes <= 0:
                raise ValueError('额度窗口未知')
            windows.append(dict(bucket=key, slot=slot, remaining=100-used,
                                minutes=minutes, resetsAt=w.get('resetsAt')))
    resets = result.get('rateLimitResetCredits') or {}
    cards = resets.get('credits')
    count = resets.get('availableCount')
    complete = isinstance(cards, list) and isinstance(count, int) and len(cards) >= count
    return dict(checkedAt=now, windows=windows, remaining=min((w['remaining'] for w in windows), default=None),
                denied=denied, cards=cards or [], cardCount=count, cardsComplete=complete)


def known(a, now):
    return not a.get('error') and a.get('remaining') is not None and 0 <= now-a.get('checkedAt', 0) <= MAX_AGE


def usable(a, now):
    return known(a, now) and not a.get('denied') and a['remaining'] > THRESHOLD and all(
        w.get('resetsAt') is None or w['resetsAt'] > now for w in a['windows'])


def decide(accounts, current, now):
    by_id = {a['key']: a for a in accounts}
    a = by_id.get(current)
    if not a or not known(a, now):
        return dict(action='unknown', reason='等待当前账号的有效额度')
    if usable(a, now):
        return dict(action='stay', target=current, reason='当前额度充足，继续使用')
    if a['remaining'] > THRESHOLD:
        return dict(action='unknown', reason='当前账号受限或数据待刷新')
    others = [x for x in accounts if x['key'] != current and usable(x, now)]
    if others:
        target = max(others, key=lambda x: x['remaining'])
        return dict(action='switch', target=target['key'], reason='额度到达 5%，切换到可用账号')
    if len(accounts) < 2 or not all(known(x, now) for x in accounts):
        return dict(action='hold', reason='先暂停任务；另一账号的额度尚未确认')
    if not all(x.get('cardsComplete') for x in accounts):
        return dict(action='hold', reason='重置卡明细不完整，等待确认最早到期的卡')
    cards = [(c.get('expiresAt') if c.get('expiresAt') is not None else float('inf'),
              0 if x['key'] == current else 1, x['key'], c['id'])
             for x in accounts for c in x['cards']
             if c.get('status') == 'available' and c.get('resetType') == 'codexRateLimits'
             and c.get('id') and (c.get('expiresAt') is None or c['expiresAt'] > now)]
    if cards:
        _, _, target, credit = min(cards)
        return dict(action='reset_current' if target == current else 'redeem', target=target, credit=credit,
                    reason='轮换账号额度均低，使用到期最早的重置卡' + ('；无需切号' if target == current else ''))
    # Each account needs ALL exhausted windows to recover. Then choose the earliest account.
    ready = []
    for x in accounts:
        blocking = [w for w in x['windows'] if w['remaining'] <= THRESHOLD]
        if blocking and all(w.get('resetsAt') and w['resetsAt'] > now for w in blocking):
            ready.append(max(w['resetsAt'] for w in blocking))
    return dict(action='wait', wakeAt=min(ready) if ready else None, reason='没有可用重置卡，等待额度刷新')


def resumable(saved, live):
    """An uncertain send or a subsequent user action must never cause a second send."""
    if saved.get('phase') in ('sending', 'done', 'excluded'):
        return False
    if live.get('status') != 'idle' or live.get('waiting'):
        return False
    if live.get('goal') in ('paused', 'blocked', 'complete', 'budgetLimited'):
        return False
    return live.get('turn') == saved.get('turn') and live.get('turnStatus') == 'interrupted'
