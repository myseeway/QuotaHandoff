import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from adapters import read_auth, identity
from service import Engine, Store
from test_profiles import login
from test_policy import account


class LoginTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.store.put('backup_paths', [str(self.root/'auth-personal.json'), str(self.root/'auth-work.json')])
        self.credentials = {}
        self.vault = Mock()
        self.vault.get.side_effect = lambda key: copy.deepcopy(self.credentials[key])
        self.vault.put.side_effect = lambda key, auth: self.credentials.update({key: copy.deepcopy(auth)})
        self.engine = Engine(self.store, self.vault)
        self.a = login(sub='person-a', name='测试甲'); self.a['last_refresh'] = '2026-10-29T00:00:00Z'
        self.b = login(sub='person-b', name='测试乙'); self.b['last_refresh'] = '2026-10-29T00:00:00Z'
        self.ak, self.bk = identity(self.a), identity(self.b)
        self.home_patch = patch('service.HOME', self.root); self.home_patch.start()
        self.read_patch = patch('service.read_auth', side_effect=lambda path=None: read_auth(path or self.root/'auth.json'))
        self.read_patch.start()
        self.write('auth.json', self.a); self.write('auth-work.json', self.b)
        self.assertTrue(self.engine.poll_identity())
        self.engine.sync_backups()
        self.engine.command(dict(command='mode', value='auto'))

    def tearDown(self):
        self.read_patch.stop(); self.home_patch.stop()
        self.engine.pool.shutdown(); self.store.db.close(); self.temp.cleanup()

    def write(self, filename, auth):
        (self.root/filename).write_text(json.dumps(auth))

    def test_two_renames_keep_identity_then_detect_manual_switch(self):
        self.engine.pending = dict(phase='paused', account=self.ak, tasks=[])
        (self.root/'auth.json').rename(self.root/'auth-personal.json')
        self.assertFalse(self.engine.poll_identity())
        self.assertEqual(self.engine.current, self.ak)
        self.engine.plan = dict(action='switch', target=self.bk)
        with patch('service.close_desktop') as close, patch('service.replace_auth') as replace:
            self.engine.automate()
            close.assert_not_called(); replace.assert_not_called()
        (self.root/'auth-work.json').rename(self.root/'auth.json')
        self.assertTrue(self.engine.poll_identity())
        self.assertEqual(self.engine.current, self.bk)
        self.assertIsNone(self.engine.identity_error)
        self.assertIsNone(self.engine.pending)
        self.assertEqual(self.engine.mode, 'observe')
        self.assertTrue(any('手动切号' in event['message'] for event in self.store.events()))

    def test_same_login_recovers_without_erasing_other_errors(self):
        self.engine.problem = '其他待检查操作'
        raw = (self.root/'auth.json').read_bytes()
        (self.root/'auth.json').write_text('{')
        self.assertFalse(self.engine.poll_identity())
        (self.root/'auth.json').write_bytes(raw)
        self.assertTrue(self.engine.poll_identity())
        self.assertIsNone(self.engine.identity_error)
        self.assertEqual(self.engine.problem, '其他待检查操作')
        self.assertEqual(self.engine.mode, 'auto')

    def test_restart_during_rename_remembers_original_account(self):
        self.store.put('pending', dict(phase='paused', account=self.ak, tasks=[]))
        (self.root/'auth.json').rename(self.root/'auth-personal.json')
        restarted = Engine(self.store, self.vault)
        try:
            self.assertFalse(restarted.poll_identity())
            self.assertEqual(restarted.current, self.ak)
            (self.root/'auth-work.json').rename(self.root/'auth.json')
            self.assertTrue(restarted.poll_identity())
            self.assertIsNone(restarted.pending)
            self.assertEqual(restarted.mode, 'observe')
        finally:
            restarted.pool.shutdown()

    def test_backup_does_not_overwrite_newer_vault_credentials(self):
        newer = dict(self.b, last_refresh='2026-10-30T00:00:00Z')
        self.credentials[self.bk] = newer
        self.engine.imported.clear()
        self.engine.sync_backups()
        self.assertEqual(self.credentials[self.bk], newer)

    def test_newer_backup_after_rename_updates_inactive_vault(self):
        newer = dict(self.a, last_refresh='2026-10-30T00:00:00Z')
        self.write('auth.json', newer)
        (self.root/'auth.json').rename(self.root/'auth-personal.json')
        self.engine.poll_identity(); self.engine.sync_backups()
        self.assertNotIn(str(self.root/'auth-personal.json'), self.engine.imported)
        (self.root/'auth-work.json').rename(self.root/'auth.json')
        self.engine.poll_identity(); self.engine.sync_backups()
        self.assertEqual(self.credentials[self.ak], newer)
        self.assertEqual(read_auth(self.root/'auth.json')[1], self.bk)

    def test_reverse_rename_is_detected_without_restart(self):
        (self.root/'auth.json').rename(self.root/'auth-personal.json')
        (self.root/'auth-work.json').rename(self.root/'auth.json')
        self.engine.poll_identity(); self.engine.sync_backups()
        (self.root/'auth.json').rename(self.root/'auth-work.json')
        self.assertFalse(self.engine.poll_identity())
        (self.root/'auth-personal.json').rename(self.root/'auth.json')
        self.engine.poll_identity(); self.engine.sync_backups()
        self.assertEqual(self.engine.current, self.ak)
        self.assertTrue(self.engine.identity_verified)

    def test_unknown_login_is_not_shown_as_current(self):
        self.engine.accounts = [account(self.ak, 4), account(self.bk, 90)]
        (self.root/'auth.json').unlink()
        self.engine.poll_identity()
        self.assertFalse(any(a['current'] for a in self.engine.view()['accounts']))
        self.assertFalse(self.engine.preflight()['ready'])

    def test_unimported_backup_is_never_discovered(self):
        other = login(sub='unimported-person')
        self.write('auth-unimported.json', other)
        self.engine.sync_backups()
        self.assertNotIn(identity(other), self.credentials)

    def test_explicit_import_enrolls_custom_path_and_survives_restart(self):
        other = login(sub='imported-person')
        path = (self.root/'custom-login.json').resolve()
        path.write_text(json.dumps(other))
        self.engine.import_file(path)
        self.assertIn(str(path), self.store.get('backup_paths'))
        restarted = Engine(self.store, self.vault)
        try:
            self.assertIn(str(path), restarted.backup_paths)
            updated = dict(other, last_refresh='2026-11-01T00:00:00Z')
            self.credentials[identity(other)] = dict(other, last_refresh='2026-10-01T00:00:00Z')
            path.write_text(json.dumps(updated))
            restarted.sync_backups()
            self.assertEqual(self.credentials[identity(other)], updated)
        finally:
            restarted.pool.shutdown()

    def test_current_auth_import_does_not_enroll_it_as_backup(self):
        self.engine.import_file(self.root/'auth.json')
        self.assertNotIn(str((self.root/'auth.json').resolve()), self.engine.backup_paths)
