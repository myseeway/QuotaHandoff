import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from adapters import identity
from service import Engine, Store, UserControlChanged
from test_profiles import login
from test_policy import account


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.store = Store(pathlib.Path(self.temp.name))
        self.engine = Engine(self.store, Mock())
        self.keys = []
        for name in ('a', 'b', 'c'):
            auth = login(sub=name, name=name); key = identity(auth); self.keys.append(key)
            self.engine.register(auth, key)
            data = account(key, 80); data['checkedAt'] = time.time(); data['windows'][0]['resetsAt'] = time.time()+3600
            self.engine.update_account(key, data)
        self.engine.current = self.keys[0]; self.engine.identity_verified = True; self.engine.mode = 'auto'

    def tearDown(self):
        self.engine.pool.shutdown(); self.store.db.close(); self.temp.cleanup()

    def test_third_account_is_discovered_as_monitor_only(self):
        self.assertEqual(len(self.engine.profiles), 3)
        self.assertEqual([p['rotationEnabled'] for p in self.engine.profiles], [True, True, False])
        self.engine.current = self.keys[2]
        self.assertEqual(self.engine.rotation_plan(time.time())['action'], 'watch')

    def test_new_account_is_visible_before_first_quota_response(self):
        self.engine.accounts = self.engine.accounts[:2]
        self.engine.current = self.keys[2]
        visible = self.engine.view()['accounts']
        self.assertEqual(len(visible), 3)
        self.assertTrue(visible[2]['current'])
        self.assertIsNone(visible[2]['remaining'])
        self.assertFalse(visible[2]['rotationEnabled'])

    def test_unjoined_spare_is_never_selected(self):
        self.engine.accounts[0]['remaining'] = 5
        self.engine.accounts[1]['remaining'] = 6
        self.engine.accounts[2]['remaining'] = 100
        self.assertEqual(self.engine.rotation_plan(time.time())['target'], self.keys[1])

    def test_enabling_new_account_persists_and_allows_selection(self):
        self.engine.command(dict(command='rotation', key=self.keys[2], value=True))
        self.engine.accounts[0]['remaining'] = 5
        self.engine.accounts[1]['remaining'] = 6
        self.engine.accounts[2]['remaining'] = 90
        self.assertEqual(self.engine.rotation_plan(time.time())['target'], self.keys[2])
        self.assertTrue(self.store.get('profiles')[2]['rotationEnabled'])

    def test_disabling_current_account_stops_automatic_actions(self):
        self.engine.command(dict(command='rotation', key=self.keys[0], value=False))
        self.assertEqual(self.engine.plan['action'], 'watch')
        with patch('service.close_desktop') as close:
            self.engine.automate(); close.assert_not_called()

    def test_signaled_user_command_does_not_itself_overwrite_mode(self):
        self.engine.signal(dict(command='switch_account', key=self.keys[1]))
        with self.assertRaises(UserControlChanged): self.engine.check_control()
        self.assertEqual(self.engine.mode, 'auto')
        self.engine.command(dict(command='switch_account', key=self.keys[1]))
        self.assertEqual(self.engine.mode, 'observe')
        self.assertFalse(self.engine.cancel.is_set())

    def test_existing_enrollment_survives_token_refresh(self):
        self.engine.command(dict(command='rotation', key=self.keys[2], value=True))
        self.engine.register(login(sub='c', name='new display name'), self.keys[2])
        self.assertTrue(self.engine.profiles[2]['rotationEnabled'])

    def test_keychain_attention_clears_after_successful_refresh(self):
        self.engine.update_account(self.keys[0], dict(error='钥匙串未确认', credentialError=True))
        self.assertTrue(self.engine.view()['credentialNeedsAttention'])
        self.engine.update_account(self.keys[0], account(self.keys[0], 80))
        self.assertFalse(self.engine.view()['credentialNeedsAttention'])
