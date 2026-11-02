import pathlib
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from adapters import Desktop, latest_desktop_turn


class DesktopHistoryTests(unittest.TestCase):
    def state(self):
        return dict(turns=[], turnHistory=dict(kind='canonical', history=dict(
            entitiesByKey={'old': dict(turnId='previous', status='completed'),
                           'local-key': dict(turnId='current', status='inProgress')},
            islands=[dict(entries=[dict(key='old', value='old'), dict(key='local-key', value='local-key')],
                          newerBoundary=dict(status='exhausted'))])))

    def test_canonical_tail_uses_entity_value_not_turn_key(self):
        self.assertEqual(latest_desktop_turn(self.state()), dict(turnId='current', status='inProgress'))

    def test_legacy_history_remains_supported(self):
        self.assertEqual(latest_desktop_turn(dict(turns=[dict(turnId='old'), dict(turnId='new')])), dict(turnId='new'))

    def test_incomplete_tail_does_not_guess_last_turn(self):
        state = self.state()
        state['turnHistory']['history']['islands'][0]['newerBoundary']['status'] = 'unloaded'
        self.assertEqual(latest_desktop_turn(state), {})

    def test_unknown_format_does_not_use_stale_legacy_turn(self):
        self.assertEqual(latest_desktop_turn(dict(turns=[dict(turnId='old')], turnHistory=dict(kind='future'))), {})

    def test_missing_entity_does_not_restore_previous_turn(self):
        state = self.state()
        del state['turnHistory']['history']['entitiesByKey']['local-key']
        self.assertEqual(latest_desktop_turn(state), {})


class DesktopInterruptTests(unittest.TestCase):
    def setUp(self):
        self.desktop = Desktop.__new__(Desktop)
        self.saved = dict(id='chat', turn='original')
        self.live = dict(turn='original', status='active', waiting=False, owner='owner')
        self.desktop.call = Mock(return_value=dict(resultType='success', handledByClientId='owner',
            result=dict(ok=True, interruptedTurnId='original')))

    def test_interrupt_targets_exact_turn_and_owner(self):
        self.assertTrue(self.desktop.interrupt_chat(self.saved, self.live))
        self.desktop.call.assert_called_once_with('thread-follower-interrupt-turn',
            dict(conversationId='chat', mode='system', expectedTurnId='original'), 4, 'owner', timeout=25)

    def test_changed_turn_or_waiting_task_never_interrupted(self):
        for changes in (dict(turn='new'), dict(status='idle'), dict(waiting=True)):
            self.assertFalse(self.desktop.interrupt_chat(self.saved, dict(self.live, **changes)))
        self.desktop.call.assert_not_called()

    def test_mismatched_acknowledgement_is_not_success(self):
        self.desktop.call.return_value['result']['interruptedTurnId'] = 'new'
        with self.assertRaises(RuntimeError): self.desktop.interrupt_chat(self.saved, self.live)

    def test_live_observed_noop_reply_is_not_a_pause_acknowledgement(self):
        self.desktop.call.return_value['result']['interruptedTurnId'] = None
        self.assertFalse(self.desktop.interrupt_chat(self.saved, self.live))

    def test_explicit_manual_switch_stops_goal_without_expected_turn_shortcut(self):
        live = dict(self.live, id='chat', goal='active', waiting=True)
        self.desktop.stop_chat_for_manual_switch(live)
        self.desktop.call.assert_called_once_with('thread-follower-interrupt-turn',
            dict(conversationId='chat', mode='system'), 4, 'owner', timeout=25)

    def test_manual_stop_does_not_ignore_goal_pause_failure(self):
        self.desktop.call.return_value['result']['goalPauseError'] = 'failed'
        with self.assertRaises(RuntimeError):
            self.desktop.stop_chat_for_manual_switch(dict(self.live, id='chat'))

    def test_diagnostic_identifies_unmatched_record(self):
        self.desktop.issues = []; self.desktop.metadata = {}
        self.desktop.add_issue('record-id', 'owner_timeout', '归属查询超时')
        self.assertEqual(self.desktop.unresolved, 1)
        self.assertEqual(self.desktop.issues[0]['id'], 'record-id')
        self.assertFalse(self.desktop.issues[0]['metadataFound'])
