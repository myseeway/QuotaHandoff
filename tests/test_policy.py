import copy
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from policy import normalize, decide, resumable

NOW = 2000000000


def account(key, remaining, cards=None, windows=None):
    a = normalize({'rateLimits': {'limitId': 'codex', 'primary': {
        'usedPercent': 100-remaining, 'windowDurationMins': 10080, 'resetsAt': NOW+3600}},
        'rateLimitResetCredits': {'availableCount': len(cards or []), 'credits': cards or []}}, NOW)
    a['key'] = key
    if windows:
        a['windows'] = windows
        a['remaining'] = min(w['remaining'] for w in windows)
    return a


def card(key, expires):
    return dict(id=key, status='available', resetType='codexRateLimits', expiresAt=expires)


class PolicyTests(unittest.TestCase):
    def test_healthy_spare_precedes_spending_current_card(self):
        accounts = [account('a', 5, [card('today', NOW+100)]), account('b', 90)]
        self.assertEqual(decide(accounts, 'a', NOW)['action'], 'switch')

    def test_card_expiry_does_not_override_healthy_spare(self):
        for cards in ([card('expired', NOW-1)], [card('next_week', NOW+1000)], []):
            self.assertEqual(decide([account('a', 5, cards), account('b', 90)], 'a', NOW)['action'], 'switch')

    def test_at_five_switches(self):
        p = decide([account('a', 5), account('b', 6)], 'a', NOW)
        self.assertEqual((p['action'], p['target']), ('switch', 'b'))

    def test_stay_does_not_spend_nearly_expired_card(self):
        self.assertEqual(decide([account('a', 60, [card('c', NOW+1)]), account('b', 2)], 'a', NOW)['action'], 'stay')

    def test_earliest_card_across_accounts(self):
        p = decide([account('a', 0, [card('a1', NOW+40), card('a2', NOW+15)]),
                    account('b', 3, [card('b1', NOW+10)])], 'a', NOW)
        self.assertEqual((p['action'], p['target'], p['credit']), ('redeem', 'b', 'b1'))

    def test_earliest_card_on_current_account_needs_no_switch(self):
        p = decide([account('a', 5, [card('first', NOW+10)]), account('b', 3, [card('later', NOW+40)])], 'a', NOW)
        self.assertEqual((p['action'], p['target'], p['credit']), ('reset_current', 'a', 'first'))

    def test_equal_card_expiry_prefers_current_account(self):
        p = decide([account('a', 5, [card('one', NOW+10)]), account('b', 3, [card('two', NOW+10)])], 'b', NOW)
        self.assertEqual((p['action'], p['target']), ('reset_current', 'b'))

    def test_expired_and_wrong_type_skipped(self):
        invalid = dict(card('wrong', NOW+3), resetType='other')
        p = decide([account('a', 5, [card('old', NOW), invalid]), account('b', 5, [card('ok', None)])], 'a', NOW)
        self.assertEqual(p['credit'], 'ok')

    def test_no_expiry_sorts_last(self):
        p = decide([account('a', 0, [card('forever', None)]), account('b', 0, [card('soon', NOW+20)])], 'a', NOW)
        self.assertEqual(p['credit'], 'soon')

    def test_stale_spare_never_spends_card(self):
        b = account('b', 0); b['checkedAt'] -= 121
        self.assertEqual(decide([account('a', 0, [card('c', NOW+30)]), b], 'a', NOW)['action'], 'hold')

    def test_partial_details_hold(self):
        b = account('b', 0); b['cardsComplete'] = False
        self.assertEqual(decide([account('a', 0, [card('c', NOW+30)]), b], 'a', NOW)['action'], 'hold')

    def test_missing_second_account_hold(self):
        self.assertEqual(decide([account('a', 0, [card('c', NOW+30)])], 'a', NOW)['action'], 'hold')

    def test_all_blocking_windows_must_reset(self):
        a = account('a', 0, windows=[dict(remaining=0, resetsAt=NOW+10), dict(remaining=1, resetsAt=NOW+90)])
        b = account('b', 0, windows=[dict(remaining=0, resetsAt=NOW+40), dict(remaining=30, resetsAt=NOW+500)])
        self.assertEqual(decide([a, b], 'a', NOW)['wakeAt'], NOW+40)

    def test_primary_can_be_weekly(self):
        a = account('a', 68)
        self.assertEqual(a['windows'][0]['minutes'], 10080)
        self.assertEqual(a['remaining'], 68)

    def test_null_quota_not_zero(self):
        a = normalize({'rateLimits': None}, NOW); a['key'] = 'a'
        self.assertEqual(decide([a, account('b', 90)], 'a', NOW)['action'], 'unknown')

    def test_no_future_or_invalid_percentage(self):
        a = account('a', 2); a['checkedAt'] = NOW+1
        self.assertEqual(decide([a, account('b', 90)], 'a', NOW)['action'], 'unknown')
        with self.assertRaises(ValueError): account('a', float('nan'))

    def test_resume_only_original_interrupted_turn(self):
        saved = dict(turn='one', phase='saved')
        live = dict(turn='one', status='idle', turnStatus='interrupted', goal=None)
        self.assertTrue(resumable(saved, live))
        for patch in [dict(goal='paused'), dict(goal='blocked'), dict(turn='two'),
                      dict(status='active'), dict(turnStatus='completed'), dict(waiting=True)]:
            self.assertFalse(resumable(saved, dict(live, **patch)))
        for phase in ('sending', 'done', 'excluded'):
            self.assertFalse(resumable(dict(saved, phase=phase), live))


if __name__ == '__main__':
    unittest.main()
