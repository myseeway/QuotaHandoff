import concurrent.futures
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
import polling
from service import Engine, Store, UserControlChanged
from test_policy import account, NOW


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.temp.name))
        self.engine = Engine(self.store, Mock())
        self.engine.pool.shutdown()
        self.engine.pool = Mock()
        self.engine.pool.submit.side_effect = lambda *args: concurrent.futures.Future()
        self.engine.profiles = [dict(key='a', alias='甲'), dict(key='b', alias='乙')]
        self.engine.accounts = [account('a', 80), account('b', 3)]
        self.engine.current = 'a'
        self.engine.mode = 'auto'
        self.engine.identity_verified = True
        self.engine.tasks = [dict(status='active')]
        self.engine.poll_records = {key: dict(attemptedAt=NOW) for key in ('a', 'b')}

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_low_spare_does_not_accelerate_all_accounts(self):
        self.engine.poll_quotas(NOW+29)
        self.engine.pool.submit.assert_not_called()
        self.engine.poll_quotas(NOW+30)
        self.assertEqual(set(self.engine.futures), {'a'})
        self.engine.poll_quotas(NOW+60)
        self.assertEqual(set(self.engine.futures), {'a', 'b'})

    def test_active_current_near_threshold_uses_ten_seconds(self):
        self.engine.accounts[0]['remaining'] = 15
        self.engine.poll_quotas(NOW+9)
        self.engine.pool.submit.assert_not_called()
        self.engine.poll_quotas(NOW+10)
        self.assertEqual(set(self.engine.futures), {'a'})

    def test_idle_current_near_threshold_does_not_use_fast_polling(self):
        self.engine.accounts[0]['remaining'] = 15
        self.engine.tasks = []
        self.engine.poll_quotas(NOW+10)
        self.engine.pool.submit.assert_not_called()
        self.engine.poll_quotas(NOW+30)
        self.assertEqual(set(self.engine.futures), {'a'})

    def test_observe_polls_all_accounts_every_minute(self):
        self.engine.mode = 'observe'
        self.engine.poll_quotas(NOW+59)
        self.engine.pool.submit.assert_not_called()
        self.engine.poll_quotas(NOW+60)
        self.assertEqual(set(self.engine.futures), {'a', 'b'})

    def test_threshold_rechecks_stale_spare_before_scheduling(self):
        self.engine.accounts[0] = dict(account('a', 5), checkedAt=NOW+180)
        self.engine.poll_records['a']['attemptedAt'] = NOW+180
        self.engine.poll_quotas(NOW+180)
        self.assertEqual(set(self.engine.futures), {'b'})

    def test_paused_does_not_poll_but_explicit_refresh_runs_once(self):
        self.engine.mode = 'paused'
        self.engine.poll_quotas(NOW+600)
        self.engine.pool.submit.assert_not_called()
        self.engine.refresh()
        self.engine.poll_quotas(NOW+600)
        self.assertEqual(set(self.engine.futures), {'a', 'b'})
        for key, future in self.engine.futures.items():
            future.set_result(dict(account(key, 50), checkedAt=NOW+600))
        self.engine.poll_quotas(NOW+600)
        self.engine.poll_quotas(NOW+1200)
        self.assertEqual(self.engine.pool.submit.call_count, 2)

    def test_refresh_clicks_are_coalesced_and_rate_limited(self):
        for _ in range(10): self.engine.refresh()
        self.engine.poll_quotas(NOW+10)
        self.engine.pool.submit.assert_not_called()
        self.engine.poll_quotas(NOW+30)
        for _ in range(10): self.engine.refresh()
        self.engine.poll_quotas(NOW+31)
        self.assertEqual(self.engine.pool.submit.call_count, 2)
        self.assertEqual(self.engine.refresh_requested, set())

    def test_manual_refresh_does_not_delay_ten_second_automatic_check(self):
        self.engine.accounts[0]['remaining'] = 15
        self.engine.refresh()
        self.engine.poll_quotas(NOW+10)
        self.assertEqual(set(self.engine.futures), {'a'})
        self.assertEqual(self.engine.refresh_requested, {'b'})

    def test_failure_backs_off_and_manual_refresh_cannot_bypass_it(self):
        self.engine.mode = 'paused'
        future = concurrent.futures.Future()
        future.set_exception(RuntimeError('query refused'))
        self.engine.futures = {'a': future}
        with patch('polling.random.uniform', return_value=0):
            self.engine.poll_quotas(NOW)
        self.assertEqual(self.engine.poll_records['a']['retryAt'], NOW+120)
        self.assertEqual(self.engine.accounts[0]['remaining'], 80)
        self.assertTrue(self.engine.accounts[0]['error'])
        self.engine.refresh_requested = {'a'}
        self.engine.poll_quotas(NOW+119)
        self.engine.pool.submit.assert_not_called()
        self.engine.poll_quotas(NOW+120)
        self.assertEqual(set(self.engine.futures), {'a'})

    def test_backoff_grows_and_is_capped(self):
        record = dict(attemptedAt=NOW)
        delays = []
        for _ in range(8):
            record = polling.failed(record, NOW, jitter=0)
            delays.append(record['retryAt']-NOW)
        self.assertEqual(delays, [120, 240, 480, 960, 1800, 1800, 1800, 1800])

    def test_restart_preserves_retry_deadline(self):
        self.engine.quota_failed('a', RuntimeError('offline'), NOW)
        restarted = Engine(self.store, Mock())
        try:
            self.assertEqual(restarted.poll_records['a'], self.engine.poll_records['a'])
        finally:
            restarted.pool.shutdown()

    def test_success_clears_failure_backoff_and_waits_after_completion(self):
        self.engine.mode = 'paused'
        self.engine.poll_records['a'].update(failures=3, retryAt=NOW+500)
        future = concurrent.futures.Future()
        future.set_result(dict(account('a', 70), checkedAt=NOW+60))
        self.engine.futures = {'a': future}
        self.engine.poll_quotas(NOW+60)
        self.assertEqual(self.engine.poll_records['a']['failures'], 0)
        self.assertEqual(self.engine.poll_records['a']['retryAt'], 0)
        self.engine.mode = 'auto'
        self.assertEqual(self.engine.next_query_at(self.engine.accounts[0], NOW+60), NOW+90)

    def test_waiting_for_reset_polls_at_reset_boundary(self):
        a = account('a', 0, windows=[dict(remaining=0, resetsAt=NOW+80)])
        self.assertEqual(polling.due_at(dict(attemptedAt=NOW), a, NOW, 300, waiting=True), NOW+82)

    def test_waiting_reset_honors_all_blocking_windows(self):
        a = account('a', 0, windows=[dict(remaining=0, resetsAt=NOW+80), dict(remaining=2, resetsAt=NOW+200)])
        self.assertEqual(polling.due_at(dict(attemptedAt=NOW), a, NOW, 300, waiting=True), NOW+202)

    def test_recovered_spare_rechecks_stale_current_before_resuming(self):
        self.engine.pending = dict(phase='paused', tasks=[])
        self.engine.plan = dict(action='unknown')
        self.engine.accounts = [account('a', 0), dict(account('b', 80), checkedAt=NOW+180)]
        self.engine.poll_records['b']['attemptedAt'] = NOW+180
        self.engine.poll_quotas(NOW+180)
        self.assertEqual(set(self.engine.futures), {'a'})

    def test_pause_discards_queued_refresh_and_preserves_inflight_completion(self):
        self.engine.refresh()
        self.engine.command(dict(command='mode', value='paused'))
        self.assertFalse(self.engine.refresh_requested)
        self.engine.poll_quotas(NOW+600)
        self.engine.pool.submit.assert_not_called()

    def test_mode_button_cycles_and_persists_all_three_states(self):
        self.engine.mode = 'observe'
        for expected in ('auto', 'paused', 'observe', 'auto'):
            self.engine.command(dict(command='cycle_mode'))
            self.assertEqual(self.engine.mode, expected)
            self.assertEqual(self.store.get('desired_mode'), expected)

    def test_mode_cycle_interrupts_pending_automation_before_next_action(self):
        self.engine.signal(dict(command='cycle_mode'))
        with self.assertRaises(UserControlChanged): self.engine.check_control()
        self.engine.command(dict(command='cycle_mode'))
        self.assertEqual(self.engine.mode, 'paused')

    def test_date_override_is_archived_and_not_exposed_to_product_ui(self):
        previous = dict(label='legacy day', startsAt=NOW, endsAt=NOW+100)
        self.store.put('priority_reset', previous)
        restarted = Engine(self.store, Mock())
        try:
            self.assertIsNone(self.store.get('priority_reset'))
            self.assertEqual(self.store.get('legacy_priority_reset'), previous)
            self.assertNotIn('priorityLabel', restarted.view())
        finally:
            restarted.pool.shutdown()
