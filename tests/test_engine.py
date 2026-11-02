import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from service import Engine, Store
from test_policy import account, NOW


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.temp.name))
        self.engine = Engine(self.store, Mock())
        self.engine.current = 'a'
        self.engine.identity_verified = True
        self.engine.plan = dict(action='switch', target='b')

    def tearDown(self):
        self.engine.pool.shutdown(wait=True)
        self.store.db.close()
        self.temp.cleanup()

    def test_observe_never_mutates(self):
        with patch('service.close_desktop') as close, patch('service.replace_auth') as write:
            self.engine.automate()
            close.assert_not_called(); write.assert_not_called()

    def test_restart_never_replays_uncertain_send(self):
        self.store.put('pending', dict(phase='resuming', tasks=[dict(id='x', phase='sending')]))
        e = Engine(self.store, Mock())
        self.assertEqual(e.mode, 'observe')
        self.assertEqual(e.pending['tasks'][0]['phase'], 'sending')
        e.pool.shutdown()

    def test_manual_switch_cancels_recovery(self):
        self.engine.pending = dict(phase='paused', tasks=[])
        self.engine.mode = 'auto'
        self.engine.register = Mock()
        with patch('service.read_auth', return_value=({}, 'b', 'new')):
            self.engine.sync_identity()
        self.assertIsNone(self.engine.pending)
        self.assertEqual(self.engine.current, 'b')
        self.assertEqual(self.engine.mode, 'observe')

    def test_token_rotation_same_identity_keeps_plan(self):
        self.engine.pending = dict(phase='paused', tasks=[])
        self.engine.register = Mock()
        with patch('service.read_auth', return_value=({}, 'a', 'rotated')):
            self.engine.sync_identity()
        self.assertIsNotNone(self.engine.pending)

    def test_user_pause_cancels_before_mutation(self):
        self.engine.mode = 'auto'; self.engine.cancel.set()
        with patch('service.close_desktop') as close:
            with self.assertRaises(RuntimeError): self.engine.automate()
            close.assert_not_called()

    def test_depleted_wait_keeps_desktop_closed(self):
        self.engine.mode = 'auto'
        self.engine.pending = dict(phase='paused', tasks=[])
        self.engine.plan = dict(action='wait')
        with patch('service.desktop_running', return_value=False), patch('service.read_auth', return_value=({}, 'a', 'x')), patch('service.open_desktop') as opening:
            self.engine.automate()
            opening.assert_not_called()

    def test_reset_requires_separate_enable(self):
        self.engine.mode = 'auto'
        self.engine.pending = dict(phase='paused', tasks=[])
        self.engine.plan = dict(action='redeem', target='a', credit='one')
        with patch('service.desktop_running', return_value=False), patch('service.read_auth', return_value=({}, 'a', 'x')), patch('service.RPC') as rpc:
            self.engine.automate()
            rpc.assert_not_called()

    def test_redemption_key_durable_before_network(self):
        self.engine.mode = 'auto'; self.engine.command({'command': 'auto_reset', 'value': True})
        self.engine.pending = dict(phase='paused', tasks=[])
        self.engine.plan = dict(action='redeem', target='a', credit='one')
        def fail(*args):
            saved = self.store.get('pending')
            self.assertEqual(saved['phase'], 'redeeming')
            self.assertEqual(saved['redemption']['credit'], 'one')
            self.assertTrue(saved['redemption']['key'])
            raise RuntimeError('timeout')
        with patch('service.desktop_running', return_value=False), patch('service.read_auth', return_value=({}, 'a', 'x')), patch('service.RPC', side_effect=fail):
            with self.assertRaises(RuntimeError): self.engine.automate()
            first = self.engine.pending['redemption']['key']
            with self.assertRaises(RuntimeError): self.engine.automate()
            self.assertEqual(first, self.engine.pending['redemption']['key'])

    def test_public_snapshot_excludes_card_ids(self):
        self.engine.accounts = [dict(account('a', 3), cards=[dict(id='SECRET_CARD_ID', expiresAt=NOW+10, status='available')])]
        self.engine.plan['credit'] = 'SECRET_CARD_ID'
        self.assertNotIn('SECRET_CARD_ID', json.dumps(self.engine.view()))

    def test_full_switch_records_before_continuation(self):
        self.engine.mode = 'auto'
        world = dict(running=True, key='a', interrupted=False)
        task = dict(id='00000000-0000-4000-8000-000000000001', title='Test', status='active',
                    waiting=False, turn='original', goal=None, turnStatus='inProgress', owner='owner')
        desktop = Mock()
        def snapshot(*args, **kwargs):
            return [dict(task, status='idle' if world['interrupted'] else 'active',
                         turnStatus='interrupted' if world['interrupted'] else 'inProgress')]
        desktop.snapshot.side_effect = snapshot
        def interrupt(saved, live):
            world['interrupted'] = True
            return True
        desktop.interrupt_chat.side_effect = interrupt
        def continue_chat(saved, live):
            journal = self.store.get('pending')
            self.assertEqual(journal['phase'], 'resuming')
            self.assertEqual(journal['tasks'][0]['phase'], 'sending')
            self.assertEqual(saved['message'], journal['tasks'][0]['message'])
        desktop.continue_chat.side_effect = continue_chat
        context = Mock(); context.__enter__ = Mock(return_value=desktop); context.__exit__ = Mock(return_value=False)
        fresh = account('b', 90)
        import time
        fresh['checkedAt'] = time.time()
        fresh['windows'][0]['resetsAt'] = time.time()+3000
        self.engine.profiles = [dict(key='a', alias='A'), dict(key='b', alias='B')]
        self.engine.query = Mock(return_value=fresh)
        with patch('service.Desktop', return_value=context), \
             patch('service.desktop_running', side_effect=lambda: world['running']), \
             patch('service.close_desktop', side_effect=lambda: world.update(running=False)), \
             patch('service.open_desktop', side_effect=lambda *args: world.update(running=True)), \
             patch('service.replace_auth', side_effect=lambda *args: world.update(key='b')), \
             patch('service.read_auth', side_effect=lambda: ({}, world['key'], 'hash')):
            self.engine.automate()
        self.assertEqual(self.engine.current, 'b')
        self.assertIsNone(self.engine.pending)
        desktop.continue_chat.assert_called_once()
        self.assertEqual(self.store.get('last_completed')['tasks'][0]['phase'], 'done')

    def test_waiting_for_user_blocks_shutdown(self):
        self.engine.mode = 'auto'
        desktop = Mock(); desktop.snapshot.return_value = [dict(status='active', waiting=True)]
        context = Mock(); context.__enter__ = Mock(return_value=desktop); context.__exit__ = Mock(return_value=False)
        with patch('service.Desktop', return_value=context), patch('service.desktop_running', return_value=True), patch('service.read_auth', return_value=({}, 'a', 'hash')), patch('service.close_desktop') as close:
            with self.assertRaises(RuntimeError): self.engine.automate()
            close.assert_not_called()


if __name__ == '__main__': unittest.main()
