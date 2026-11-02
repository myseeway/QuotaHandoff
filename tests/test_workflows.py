"""End-to-end controller scenarios with all desktop/network mutations replaced."""
import contextlib
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from service import Engine, Store
from policy import decide
from test_policy import account, card


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.temp.name))
        self.engine = Engine(self.store, Mock())
        self.engine.profiles = [dict(key='a', alias='甲'), dict(key='b', alias='乙')]
        self.engine.current = 'a'; self.engine.mode = 'auto'
        self.engine.identity_verified = True
        self.engine.command(dict(command='auto_reset', value=True))
        self.world = dict(running=True, key='a', interrupted=False)
        self.task = dict(id='00000000-0000-4000-8000-000000000001', title='测试任务', status='active',
                         waiting=False, turn='original', turnStatus='inProgress', goal=None, owner='owner')
        self.desktop = Mock()
        def interrupt(saved, live):
            self.world['interrupted'] = True
            return True
        self.desktop.interrupt_chat.side_effect = interrupt
        def stop_for_manual(live):
            self.world['interrupted'] = True
            if self.task.get('goal') == 'active':
                self.task['goal'] = 'paused'
        self.desktop.stop_chat_for_manual_switch.side_effect = stop_for_manual
        self.desktop.snapshot.side_effect = lambda *a, **kw: [dict(self.task,
            status='idle' if self.world['interrupted'] else 'active',
            turnStatus='interrupted' if self.world['interrupted'] else 'inProgress')]
        self.context = Mock()
        self.context.__enter__ = Mock(return_value=self.desktop)
        self.context.__exit__ = Mock(return_value=False)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch('service.Desktop', return_value=self.context))
        self.stack.enter_context(patch('service.desktop_running', side_effect=lambda: self.world['running']))
        self.stack.enter_context(patch('service.read_auth', side_effect=lambda: ({}, self.world['key'], 'hash')))
        self.stack.enter_context(patch('service.identity', return_value='b'))
        self.close = self.stack.enter_context(patch('service.close_desktop', side_effect=lambda: self.world.update(running=False)))
        self.open = self.stack.enter_context(patch('service.open_desktop', side_effect=lambda *a: self.world.update(running=True)))
        self.replace = self.stack.enter_context(patch('service.replace_auth', side_effect=lambda *a: self.world.update(key='b')))
        self.engine.query = Mock(return_value=self.quota('b', 90))
        self.rpc = Mock(); self.rpc.call.return_value = dict(outcome='reset')
        rpc_context = Mock(); rpc_context.__enter__ = Mock(return_value=self.rpc); rpc_context.__exit__ = Mock(return_value=False)
        self.rpc_factory = self.stack.enter_context(patch('service.RPC', return_value=rpc_context))

    def tearDown(self):
        self.stack.close(); self.engine.pool.shutdown(); self.store.db.close(); self.temp.cleanup()

    def quota(self, key, remaining, cards=None):
        a = account(key, remaining, cards)
        a['checkedAt'] = time.time(); a['windows'][0]['resetsAt'] = time.time()+3600
        return a

    def plan(self, first, second):
        self.engine.accounts = [first, second]
        self.engine.plan = decide(self.engine.accounts, self.engine.current, time.time())

    def test_both_low_redeems_earliest_then_resumes_once(self):
        self.plan(self.quota('a', 5, [card('later', time.time()+120)]), self.quota('b', 3, [card('first', time.time()+60)]))
        self.engine.automate()
        self.close.assert_called_once(); self.replace.assert_called_once()
        self.assertEqual(self.rpc.call.call_args.args[1]['creditId'], 'first')
        self.desktop.continue_chat.assert_called_once()
        self.assertIsNone(self.engine.pending)

    def test_interrupt_is_durable_and_confirmed_before_quit(self):
        self.plan(self.quota('a', 5), self.quota('b', 90))
        def interrupt(saved, live):
            journal = self.store.get('pending')
            self.assertEqual(journal['phase'], 'interrupting')
            self.assertIn('pauseRequestedAt', journal['tasks'][0])
            self.close.assert_not_called()
            self.world['interrupted'] = True
            return True
        self.desktop.interrupt_chat.side_effect = interrupt
        def close():
            self.assertTrue(self.store.get('pending')['tasks'][0]['pauseConfirmed'])
            self.assertTrue(self.world['interrupted'])
            self.world['running'] = False
        self.close.side_effect = close
        self.engine.automate()
        self.desktop.interrupt_chat.assert_called_once()
        self.assertEqual(self.store.get('last_completed')['continuedCount'], 1)

    def test_all_active_chats_are_stopped_before_app_quit(self):
        second = dict(self.task, id='00000000-0000-4000-8000-000000000002', turn='second')
        stopped = set()
        self.desktop.snapshot.side_effect = lambda: [dict(t, status='idle' if t['id'] in stopped else 'active',
            turnStatus='interrupted' if t['id'] in stopped else 'inProgress') for t in (self.task, second)]
        def interrupt(saved, live):
            stopped.add(saved['id']); return True
        def close():
            self.assertEqual(len(stopped), 2)
            self.world['running'] = False
        self.desktop.interrupt_chat.side_effect = interrupt
        self.close.side_effect = close
        self.plan(self.quota('a', 5), self.quota('b', 90))
        self.engine.automate()
        self.assertEqual(self.desktop.continue_chat.call_count, 2)
        self.assertEqual(self.store.get('last_completed')['continuedCount'], 2)

    def test_active_goal_is_detected_before_interrupting_any_chat(self):
        self.task['goal'] = 'active'
        self.plan(self.quota('a', 5), self.quota('b', 90))
        with self.assertRaisesRegex(RuntimeError, '持续运行目标'):
            self.engine.automate()
        self.desktop.interrupt_chat.assert_not_called(); self.close.assert_not_called()

    def test_new_turn_after_interrupt_blocks_quit_and_switch(self):
        self.plan(self.quota('a', 5), self.quota('b', 90))
        def interrupt(saved, live):
            self.task['turn'] = 'new-user-turn'
            return True
        self.desktop.interrupt_chat.side_effect = interrupt
        with self.assertRaisesRegex(RuntimeError, '状态改变'):
            self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called()

    def test_interrupt_rejection_leaves_desktop_open(self):
        self.plan(self.quota('a', 5), self.quota('b', 90))
        self.desktop.interrupt_chat.return_value = False
        self.desktop.interrupt_chat.side_effect = None
        with self.assertRaisesRegex(RuntimeError, '暂停结果未确认'):
            self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called()

    def test_redemption_target_remains_locked_when_new_plan_prefers_other_account(self):
        self.world.update(running=False, key='b', interrupted=True)
        self.engine.current = 'b'
        self.engine.pending = dict(phase='redeeming', account='b', tasks=[],
            redemption=dict(account='b', credit='chosen', key='one-request', attempted=True))
        self.engine.plan = dict(action='redeem', target='a', credit='other')
        self.engine.automate()
        self.replace.assert_not_called()
        self.assertEqual(self.rpc.call.call_args.args[1], dict(creditId='chosen', idempotencyKey='one-request'))

    def test_confirmed_redemption_waits_for_quota_without_spending_again(self):
        self.world.update(running=False, interrupted=True)
        self.engine.pending = dict(phase='paused', account='a', tasks=[],
            redemption=dict(account='a', credit='chosen', key='one-request', attempted=True, confirmed=True))
        self.engine.plan = dict(action='redeem', target='b', credit='new-card')
        self.engine.query.return_value = self.quota('a', 5)
        for _ in range(3): self.engine.automate()
        self.replace.assert_not_called(); self.rpc.call.assert_not_called(); self.open.assert_not_called()

    def test_both_low_without_cards_waits_without_account_ping_pong(self):
        self.plan(self.quota('a', 5), self.quota('b', 3))
        for _ in range(3): self.engine.automate()
        self.close.assert_called_once(); self.replace.assert_not_called(); self.rpc.call.assert_not_called()

    def test_empty_task_list_does_not_claim_task_recovery(self):
        self.desktop.snapshot.side_effect = None
        self.desktop.snapshot.return_value = []
        self.plan(self.quota('a', 5), self.quota('b', 90))
        self.engine.automate()
        self.assertEqual(self.store.get('last_completed')['continuedCount'], 0)
        self.assertIn('没有待恢复任务', self.store.events()[0]['message'])

    def test_current_earliest_card_does_not_quit_or_switch(self):
        self.plan(self.quota('a', 5, [card('earliest', time.time()+30)]), self.quota('b', 3, [card('later', time.time()+60)]))
        self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called(); self.desktop.interrupt_chat.assert_not_called()
        self.assertEqual(self.rpc.call.call_args.args[1]['creditId'], 'earliest')

    def test_current_confirmed_card_is_not_consumed_twice_during_delayed_quota_update(self):
        self.plan(self.quota('a', 5, [card('earliest', time.time()+30)]), self.quota('b', 3))
        self.engine.query.return_value = self.quota('a', 5)
        self.engine.automate()
        for _ in range(3):
            self.engine.last_live_attempt = 0
            self.engine.automate()
        self.rpc.call.assert_called_once(); self.replace.assert_not_called()
        self.assertTrue(self.store.get('live_redemption')['confirmed'])

    def test_disabled_current_card_pauses_instead_of_switching_to_low_spare(self):
        self.plan(self.quota('a', 5, [card('earliest', time.time()+30)]), self.quota('b', 3))
        self.engine.command(dict(command='auto_reset', value=False))
        self.engine.automate()
        self.desktop.interrupt_chat.assert_called_once(); self.close.assert_called_once()
        self.rpc.call.assert_not_called(); self.replace.assert_not_called()

    def test_retry_old_failed_quit_rechecks_before_discarding_stale_snapshot(self):
        self.engine.mode = 'observe'
        self.engine.pending = dict(phase='closing', tasks=[dict(self.task)], account='a')
        self.engine.command(dict(command='retry_failed_quit'))
        self.assertIsNone(self.engine.pending)
        self.assertEqual(self.engine.mode, 'auto')
        self.assertEqual(self.store.get('abandoned')['phase'], 'closing')

    def test_retry_old_failed_quit_retains_interrupted_task_record(self):
        self.engine.mode = 'observe'; self.world['interrupted'] = True
        self.engine.pending = dict(phase='closing', tasks=[dict(self.task)], account='a')
        with self.assertRaisesRegex(RuntimeError, '可能已中断'):
            self.engine.command(dict(command='retry_failed_quit'))
        self.assertIsNotNone(self.engine.pending)

    def test_manual_switch_works_in_observe_and_preserves_that_mode(self):
        self.engine.command(dict(command='mode', value='observe'))
        self.plan(self.quota('a', 80), self.quota('b', 90))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.assertEqual(self.engine.current, 'b')
        self.assertEqual(self.engine.mode, 'observe')
        self.assertEqual(self.store.get('desired_mode'), 'observe')
        self.assertIsNone(self.engine.manual_request)
        self.desktop.stop_chat_for_manual_switch.assert_called_once()
        self.desktop.continue_chat.assert_not_called()
        self.engine.query.assert_not_called()
        self.rpc.call.assert_not_called()
        self.assertEqual(self.engine.view()['manualSwitchResult']['status'], 'complete')

    def test_missing_active_turn_never_closes_desktop(self):
        self.task['turn'] = None
        self.plan(self.quota('a', 5), self.quota('b', 90))
        with self.assertRaisesRegex(RuntimeError, '回合尚未确认'):
            self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called()
        self.desktop.continue_chat.assert_not_called()
        self.assertIsNone(self.engine.pending)

    def test_manual_switch_changes_monitor_to_observe(self):
        self.engine.command(dict(command='mode', value='paused'))
        self.plan(self.quota('a', 80), self.quota('b', 90))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.assertEqual(self.engine.current, 'b')
        self.assertEqual(self.engine.mode, 'observe')
        self.assertEqual(self.store.get('desired_mode'), 'observe')

    def test_manual_switch_with_closed_desktop_needs_no_interrupt(self):
        self.world['running'] = False
        self.plan(self.quota('a', 80), self.quota('b', 90))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.desktop.snapshot.assert_not_called(); self.close.assert_not_called()
        self.replace.assert_called_once(); self.open.assert_called_once()

    def test_manual_target_without_quota_switches_without_consuming_a_card(self):
        self.plan(self.quota('a', 80), self.quota('b', 5, [card('card', time.time()+600)]))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.close.assert_called_once(); self.replace.assert_called_once(); self.rpc.call.assert_not_called()
        self.desktop.continue_chat.assert_not_called()
        self.assertEqual(self.engine.current, 'b')
        self.assertIsNone(self.engine.manual_request)
        self.engine.automate()
        self.replace.assert_called_once()  # Auto mode must not immediately switch back.

    def test_manual_target_overrides_automatic_recovery_without_resuming(self):
        self.engine.pending = dict(phase='paused', source='a', account='a', origin='auto', tasks=[])
        self.plan(self.quota('a', 0), self.quota('b', 5))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.assertEqual(self.store.get('abandoned')['origin'], 'auto')
        self.assertIsNone(self.engine.pending)
        self.assertIsNone(self.engine.manual_request)
        self.replace.assert_called_once()
        self.desktop.continue_chat.assert_not_called()

    def test_manual_zero_quota_denied_account_switches_even_when_reset_disabled(self):
        self.engine.command(dict(command='auto_reset', value=False))
        self.plan(self.quota('a', 80), dict(self.quota('b', 0), denied=True))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.replace.assert_called_once(); self.open.assert_called_once()
        self.rpc.call.assert_not_called(); self.desktop.continue_chat.assert_not_called()

    def test_cancel_queued_manual_switch_keeps_auto_paused(self):
        self.plan(self.quota('a', 80), self.quota('b', 90))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.command(dict(command='cancel_switch'))
        self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called()
        self.assertEqual(self.engine.mode, 'observe')
        self.assertEqual(self.engine.view()['manualSwitchResult']['status'], 'cancelled')

    def test_manual_switch_can_take_over_a_paused_recovery(self):
        self.world.update(running=False, interrupted=True)
        saved = dict(self.task, phase='saved', message='original-message')
        self.engine.pending = dict(phase='paused', source='a', account='a', origin='auto', tasks=[saved])
        self.plan(self.quota('a', 0), self.quota('b', 90))
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.close.assert_not_called(); self.desktop.continue_chat.assert_not_called()
        self.assertEqual(self.store.get('abandoned')['tasks'][0]['message'], 'original-message')

    def test_manual_switch_archives_uncertain_redemption_without_retrying_it(self):
        self.engine.live_redemption = dict(account='a', credit='one', key='same-key')
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.assertEqual(self.engine.mode, 'observe')
        self.assertIsNone(self.engine.manual_request)
        self.assertEqual(self.store.get('abandoned_live_redemption')['key'], 'same-key')
        self.rpc.call.assert_not_called()
        self.desktop.continue_chat.assert_not_called()

    def test_manual_switch_does_not_wait_for_quota_or_desktop_preflight(self):
        self.engine.accounts = [dict(key='b', error='offline', denied=True)]
        self.engine.futures = {'b': Mock()}
        self.engine.desktop_error = 'unmatched owner'
        self.engine.identity_verified = False
        self.task.update(goal='active', waiting=True)
        self.engine.command(dict(command='switch_account', key='b', requestId='manual-one'))
        self.engine.automate()
        self.replace.assert_called_once()
        self.desktop.stop_chat_for_manual_switch.assert_called_once()
        self.engine.query.assert_not_called(); self.rpc.call.assert_not_called()
        self.desktop.continue_chat.assert_not_called()
        result = self.engine.view()['manualSwitchResult']
        self.assertEqual((result['id'], result['status']), ('manual-one', 'complete'))

    def test_manual_switch_with_unknown_tasks_uses_normal_quit(self):
        self.desktop.snapshot.side_effect = RuntimeError('unknown format')
        self.engine.command(dict(command='switch_account', key='b'))
        self.engine.automate()
        self.close.assert_called_once(); self.replace.assert_called_once()
        self.desktop.continue_chat.assert_not_called()

    def test_manual_quit_failure_is_visible_and_does_not_replace_auth(self):
        self.close.side_effect = RuntimeError('桌面端尚未退出，取消切号')
        self.engine.command(dict(command='switch_account', key='b', requestId='manual-one'))
        with self.assertRaisesRegex(RuntimeError, '尚未退出'):
            self.engine.automate()
        self.replace.assert_not_called()
        result = self.engine.view()['manualSwitchResult']
        self.assertEqual((result['id'], result['status']), ('manual-one', 'failed'))
        self.assertIn('尚未退出', result['message'])
        self.assertEqual(self.store.get('last_manual_switch'), result)
        self.assertIsNone(self.engine.manual_request)

    def test_failed_manual_quit_cannot_restart_automatic_recovery(self):
        self.engine.mode = 'observe'
        self.engine.pending = dict(origin='manual', phase='closing', tasks=[])
        self.assertFalse(self.engine.can_retry_failed_quit())

    def test_manual_unknown_profile_failure_is_correlated(self):
        with self.assertRaisesRegex(RuntimeError, '找不到'):
            self.engine.command(dict(command='switch_account', key='missing', requestId='manual-one'))
        self.assertEqual(self.engine.view()['manualSwitchResult']['id'], 'manual-one')
        self.replace.assert_not_called()

    def test_manual_cancellation_before_auth_write_retains_source(self):
        self.engine.command(dict(command='switch_account', key='b'))
        def publish():
            if self.engine.pending and self.engine.pending['phase'] == 'switching':
                self.engine.signal(dict(command='cancel_switch'))
        self.engine.publish = publish
        with self.assertRaises(RuntimeError): self.engine.automate()
        self.replace.assert_not_called()
        self.engine.command(dict(command='cancel_switch'))
        self.assertEqual(self.engine.view()['manualSwitchResult']['status'], 'cancelled')

    def test_manual_credential_failure_does_not_stop_chats(self):
        self.engine.vault.get.side_effect = RuntimeError('请重新登录')
        self.engine.command(dict(command='switch_account', key='b'))
        with self.assertRaisesRegex(RuntimeError, '重新登录'): self.engine.automate()
        self.close.assert_not_called(); self.desktop.stop_chat_for_manual_switch.assert_not_called()
        self.assertEqual(self.engine.view()['manualSwitchResult']['status'], 'failed')

    def test_restart_never_replays_one_shot_manual_switch(self):
        self.store.put('desired_mode', 'auto')
        self.store.put('pending', dict(phase='paused', origin='manual', source='a', account='a', target='b', tasks=[]))
        restarted = Engine(self.store, Mock())
        try:
            self.assertEqual(restarted.mode, 'observe')
            self.assertIsNone(restarted.manual_request)
            restarted.identity_verified = True
            restarted.mode = 'auto'
            restarted.automate()
            self.replace.assert_not_called()
        finally:
            restarted.pool.shutdown()

    def test_progress_is_published_before_each_external_action(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        published = []
        self.engine.publish = lambda: published.append(self.engine.view()['operation'])
        def close():
            self.assertEqual(published[-1]['phase'], 'closing')
            self.assertEqual(published[-1]['total'], 1)
            self.world.update(running=False, interrupted=True)
        def replace(*args):
            self.assertEqual(published[-1]['phase'], 'switching')
            self.assertEqual(self.store.get('pending')['target'], 'b')
            self.world['key'] = 'b'
        def continued(*args):
            self.assertEqual(published[-1]['phase'], 'resuming')
            self.assertEqual(self.store.get('pending')['tasks'][0]['phase'], 'sending')
        self.close.side_effect = close; self.replace.side_effect = replace
        self.desktop.continue_chat.side_effect = continued
        self.engine.automate()
        self.assertIsNone(published[-1])
        self.assertTrue(any(p and p['completed'] == 1 for p in published))

    def test_cancellation_from_progress_prevents_shutdown(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        self.engine.publish = lambda: self.engine.signal(dict(command='mode', value='paused'))
        with self.assertRaises(RuntimeError): self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called()

    def test_completed_or_sending_tasks_cannot_be_relabelled_as_excluded(self):
        self.engine.pending = dict(phase='resuming', tasks=[dict(id='done', phase='done'), dict(id='sent', phase='sending')])
        self.engine.command(dict(command='exclude', id='done'))
        self.engine.command(dict(command='exclude', id='sent'))
        self.assertEqual([t['phase'] for t in self.engine.pending['tasks']], ['done', 'sending'])

    def test_pause_after_switch_progress_stops_before_auth_write(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        def publish():
            if self.engine.pending and self.engine.pending['phase'] == 'switching':
                self.engine.signal(dict(command='mode', value='paused'))
        self.engine.publish = publish
        with self.assertRaises(RuntimeError): self.engine.automate()
        self.replace.assert_not_called(); self.open.assert_not_called()

    def test_pause_after_resume_progress_stops_before_sending(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        def publish():
            if self.engine.pending and any(t['phase'] == 'sending' for t in self.engine.pending['tasks']):
                self.engine.signal(dict(command='mode', value='paused'))
        self.engine.publish = publish
        with self.assertRaises(RuntimeError): self.engine.automate()
        self.desktop.continue_chat.assert_not_called()

    def test_wait_then_natural_refresh_restores_without_spending_card(self):
        self.plan(self.quota('a', 0), self.quota('b', 0))
        self.engine.automate()
        self.assertEqual(self.engine.pending['phase'], 'paused'); self.open.assert_not_called()
        self.plan(self.quota('a', 0), self.quota('b', 90))
        self.engine.automate()
        self.close.assert_called_once(); self.desktop.continue_chat.assert_called_once()
        self.rpc.call.assert_not_called(); self.assertIsNone(self.engine.pending)

    def test_manual_switch_before_shutdown_never_closes_desktop(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        self.world['key'] = 'b'; self.engine.register = Mock()
        self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called()
        self.assertEqual(self.engine.current, 'b')

    def test_pause_while_closing_cancels_switch(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        self.close.side_effect = lambda: self.engine.signal(dict(command='mode', value='paused'))
        with self.assertRaises(RuntimeError): self.engine.automate()
        self.replace.assert_not_called(); self.desktop.continue_chat.assert_not_called()
        self.assertIsNotNone(self.store.get('pending'))

    def test_exclude_while_reopening_never_sends_continuation(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        def reopen(*args):
            self.world['running'] = True
            self.engine.signal(dict(command='exclude', id=self.task['id']))
        self.open.side_effect = reopen
        self.engine.automate()
        self.desktop.continue_chat.assert_not_called()
        self.assertEqual(self.store.get('last_completed')['tasks'][0]['phase'], 'excluded')

    def test_disable_reset_while_connecting_never_consumes(self):
        self.plan(self.quota('a', 0), self.quota('b', 0, [card('card', time.time()+60)]))
        def connecting(*args):
            self.engine.signal(dict(command='auto_reset', value=False))
            return self.rpc_factory.return_value
        self.rpc_factory.side_effect = connecting
        self.engine.automate()
        self.rpc.call.assert_not_called(); self.open.assert_not_called()
        self.assertEqual(self.engine.pending['phase'], 'paused')

    def test_uncertain_resume_is_not_sent_twice(self):
        self.plan(self.quota('a', 0), self.quota('b', 90))
        self.desktop.continue_chat.side_effect = RuntimeError('delivery unknown')
        with self.assertRaises(RuntimeError): self.engine.automate()
        self.assertEqual(self.store.get('pending')['tasks'][0]['phase'], 'sending')
        with self.assertRaises(RuntimeError): self.engine.automate()
        self.desktop.continue_chat.assert_called_once()

    def test_preflight_requires_fresh_desktop_and_no_waiting_tasks(self):
        self.engine.accounts = [self.quota('a', 90), self.quota('b', 90)]
        self.assertFalse(self.engine.preflight()['ready'])
        self.engine.desktop_checked_at = time.time()
        self.assertTrue(self.engine.preflight()['ready'])
        self.engine.tasks = [dict(waiting=True)]
        self.assertFalse(self.engine.preflight()['ready'])

    def test_current_card_reset_does_not_close_or_restart_tasks(self):
        self.engine.accounts = [self.quota('a', 5, [card('current', time.time()+100)]), self.quota('b', 3)]
        self.engine.plan = decide(self.engine.accounts, 'a', time.time())
        self.engine.automate()
        self.close.assert_not_called(); self.replace.assert_not_called(); self.open.assert_not_called()
        self.assertEqual(self.rpc.call.call_args.args[1]['creditId'], 'current')
        self.assertIsNone(self.store.get('live_redemption'))
        self.desktop.continue_chat.assert_not_called()

    def test_confirmed_reset_with_delayed_balance_does_not_query_every_cycle(self):
        self.world.update(running=False, interrupted=True)
        self.engine.pending = dict(phase='paused', account='a', tasks=[],
            redemption=dict(account='a', credit='chosen', key='one-request', attempted=True, confirmed=True))
        self.engine.plan = dict(action='redeem', target='a', credit='chosen')
        self.engine.query.return_value = self.quota('a', 5)
        for _ in range(5): self.engine.automate()
        self.engine.query.assert_called_once()
        self.rpc.call.assert_not_called()

    def test_current_reset_timeout_reuses_idempotency_key(self):
        self.engine.plan = dict(action='reset_current', credit='one', target='a')
        self.rpc.call.side_effect = [RuntimeError('timeout'), dict(outcome='alreadyRedeemed')]
        self.engine.automate()
        first_key = self.store.get('live_redemption')['key']
        self.engine.last_live_attempt = 0
        self.engine.automate()
        self.assertEqual([c.args[1]['idempotencyKey'] for c in self.rpc.call.call_args_list], [first_key, first_key])
        self.assertIsNone(self.store.get('live_redemption'))
        self.close.assert_not_called()

    def test_explicit_auto_choice_survives_restart_but_manual_pause_wins(self):
        self.engine.command(dict(command='mode', value='auto'))
        restarted = Engine(self.store, Mock())
        try:
            self.assertEqual(restarted.mode, 'auto')
            restarted.command(dict(command='mode', value='paused'))
            paused = Engine(self.store, Mock())
            try: self.assertEqual(paused.mode, 'paused')
            finally: paused.pool.shutdown()
        finally: restarted.pool.shutdown()
