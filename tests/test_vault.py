import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from adapters import Vault, CredentialStoreError, MissingCredentials, identity
from service import Engine, Store
from test_profiles import login


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.vault = Vault('/Applications/Test.app/Contents/MacOS/CodexQuota')
        self.auth = login(name='测试'); self.key = identity(self.auth)

    def test_reads_use_stable_helper_and_validated_identity(self):
        response = Mock(returncode=0, stdout=json.dumps(self.auth).encode())
        with patch('adapters.subprocess.run', return_value=response) as run:
            self.assertEqual(self.vault.get(self.key), self.auth)
        self.assertEqual(run.call_args.args[0], ['/Applications/Test.app/Contents/MacOS/CodexQuotaKeychain', '--vault-get', self.key])

    def test_authorization_denial_backs_off_without_repeated_helpers(self):
        with patch('adapters.subprocess.run', return_value=Mock(returncode=11)) as run:
            for _ in range(5):
                with self.assertRaises(CredentialStoreError): self.vault.get(self.key)
            run.assert_called_once()

    def test_refresh_can_retry_after_unlock(self):
        responses = [Mock(returncode=11), Mock(returncode=0, stdout=json.dumps(self.auth).encode())]
        with patch('adapters.subprocess.run', side_effect=responses) as run:
            with self.assertRaises(CredentialStoreError): self.vault.get(self.key)
            self.vault.clear_backoff()
            self.assertEqual(self.vault.get(self.key), self.auth)
            self.assertEqual(run.call_count, 2)

    def test_timeout_does_not_expose_commands_or_identifiers(self):
        with patch('adapters.subprocess.run', side_effect=subprocess.TimeoutExpired(['private-command', self.key], 8)):
            with self.assertRaises(CredentialStoreError) as caught: self.vault.get(self.key)
        self.assertNotIn(self.key, str(caught.exception))
        self.assertNotIn('private-command', str(caught.exception))

    def test_writes_send_credentials_on_stdin_only(self):
        with patch('adapters.subprocess.run', return_value=Mock(returncode=0, stdout=b'')) as run:
            self.vault.put(self.key, self.auth)
        self.assertNotIn('not-a-real-token', str(run.call_args.args))
        self.assertEqual(json.loads(run.call_args.kwargs['input']), self.auth)

    def test_missing_new_item_can_be_imported_without_an_auth_backoff(self):
        with patch('adapters.subprocess.run', side_effect=[Mock(returncode=10), Mock(returncode=0, stdout=b'')]):
            with self.assertRaises(MissingCredentials): self.vault.get(self.key)
            self.vault.put(self.key, self.auth)

    def test_known_backup_migrates_to_new_vault_even_with_old_import_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp); authfile = root/'auth-personal.json'
            authfile.write_text(json.dumps(self.auth))
            from adapters import read_auth
            _, key, digest = read_auth(authfile)
            store = Store(root/'state')
            store.put('profiles', [dict(key=key, alias='原名')])
            store.put('imported', {str(authfile): digest})
            vault = Mock(); vault.get.side_effect = MissingCredentials('missing')
            engine = Engine(store, vault)
            try:
                with patch('service.HOME', root): engine.sync_backups()
                vault.put.assert_called_once_with(key, self.auth)
            finally:
                engine.pool.shutdown(); store.db.close()
