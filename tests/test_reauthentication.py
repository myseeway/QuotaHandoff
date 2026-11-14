import concurrent.futures
import copy
import datetime
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from adapters import CredentialStoreError, credential_fingerprint, identity, read_auth
from service import Engine, Store
from test_profiles import login
from test_policy import account, NOW


class ReauthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.store = Store(self.root/'state')
        self.auth = login(sub='synthetic-person')
        self.auth['last_refresh'] = datetime.datetime.fromtimestamp(NOW-100, datetime.timezone.utc).isoformat()
        self.key = identity(self.auth)
        self.vault = Mock()
        self.vault.get.side_effect = lambda key: copy.deepcopy(self.auth)
        self.engine = Engine(self.store, self.vault)
        self.engine.pool.shutdown()
        self.engine.pool = Mock()
        self.engine.pool.submit.side_effect = lambda *args: concurrent.futures.Future()
        self.engine.register(self.auth, self.key)
        self.engine.current = self.key
        self.engine.identity_verified = True
        self.engine.mode = 'observe'
        self.engine.poll_records[self.key].update(attemptedAt=NOW, failures=10, retryAt=NOW+1800)
        self.store.put('poll_records', self.engine.poll_records)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def renewed(self):
        auth = copy.deepcopy(self.auth)
        auth['tokens']['access_token'] = 'new-synthetic-access-token'
        auth['last_refresh'] = datetime.datetime.fromtimestamp(NOW+120, datetime.timezone.utc).isoformat()
        return auth

    def test_same_account_relogin_retries_with_new_credentials(self):
        path = self.root/'auth.json'
        path.write_text(json.dumps(self.auth))
        with patch('service.read_auth', side_effect=lambda: read_auth(path)):
            self.engine.sync_identity()
            self.auth = self.renewed()
            path.write_text(json.dumps(self.auth))
            self.engine.sync_identity()
        record = self.engine.poll_records[self.key]
        self.assertEqual(record['failures'], 0)
        self.assertEqual(record['retryAt'], 0)
        self.engine.poll_quotas(NOW+120)
        self.assertEqual(set(self.engine.futures), {self.key})
        self.assertEqual(self.engine.pool.submit.call_args.args[1], self.auth)
        self.engine.futures[self.key].set_result(dict(account(self.key, 75), checkedAt=NOW+121))
        self.engine.poll_quotas(NOW+121)
        self.assertEqual(self.engine.accounts[0]['remaining'], 75)
        self.assertNotIn('error', self.engine.accounts[0])

    def test_rewriting_metadata_keeps_failure_backoff(self):
        same = dict(self.auth, last_refresh=self.renewed()['last_refresh'])
        self.engine.register(same, self.key)
        self.assertEqual(self.engine.poll_records[self.key]['retryAt'], NOW+1800)
        self.assertFalse(self.engine.refresh_requested)

    def test_failed_credential_save_preserves_previous_query_record(self):
        previous = self.engine.poll_records[self.key].copy()
        self.vault.put.side_effect = CredentialStoreError('synthetic vault failure')
        with self.assertRaises(CredentialStoreError):
            self.engine.register(self.renewed(), self.key)
        self.assertEqual(self.engine.poll_records[self.key], previous)

    def test_restart_with_same_credentials_keeps_backoff(self):
        restarted = Engine(self.store, self.vault)
        try:
            restarted.register(self.auth, self.key)
            self.assertEqual(restarted.poll_records[self.key]['retryAt'], NOW+1800)
        finally:
            restarted.pool.shutdown()

    def test_legacy_record_recovers_only_when_login_is_newer_than_attempt(self):
        self.engine.poll_records[self.key].pop('credentialFingerprint')
        self.engine.register(self.auth, self.key)
        self.assertEqual(self.engine.poll_records[self.key]['retryAt'], NOW+1800)
        self.engine.poll_records[self.key].pop('credentialFingerprint')
        self.engine.register(self.renewed(), self.key)
        self.assertEqual(self.engine.poll_records[self.key]['retryAt'], 0)

    def test_relogin_does_not_change_spare_backoff(self):
        spare = login(sub='synthetic-spare')
        key = identity(spare)
        self.engine.register(spare, key)
        record = dict(self.engine.poll_records[key], failures=4, retryAt=NOW+900)
        self.engine.poll_records[key] = record.copy()
        self.engine.register(self.renewed(), self.key)
        self.assertEqual(self.engine.poll_records[key], record)

    def test_paused_mode_waits_for_manual_refresh(self):
        self.engine.mode = 'paused'
        self.engine.register(self.renewed(), self.key)
        self.engine.poll_quotas(NOW+120)
        self.engine.pool.submit.assert_not_called()
        self.engine.refresh()
        self.engine.poll_quotas(NOW+120)
        self.assertEqual(set(self.engine.futures), {self.key})
        self.assertEqual(self.engine.mode, 'paused')

    def test_new_login_preserves_minimum_request_spacing(self):
        self.engine.register(self.renewed(), self.key)
        self.engine.poll_quotas(NOW+10)
        self.engine.pool.submit.assert_not_called()
        self.engine.poll_quotas(NOW+30)
        self.assertEqual(set(self.engine.futures), {self.key})

    def test_old_inflight_failure_cannot_restore_backoff_after_relogin(self):
        old = concurrent.futures.Future()
        self.engine.futures[self.key] = old
        self.engine.query_fingerprints[self.key] = credential_fingerprint(self.auth)
        self.auth = self.renewed()
        self.engine.register(self.auth, self.key)
        old.set_exception(RuntimeError('old login expired'))
        self.engine.poll_quotas(NOW+120)
        self.assertIsNot(self.engine.futures[self.key], old)
        self.assertEqual(self.engine.poll_records[self.key]['failures'], 0)
        self.assertEqual(self.engine.poll_records[self.key]['retryAt'], 0)
        self.assertNotEqual(self.engine.accounts[0].get('error'), 'old login expired')

    def test_old_inflight_success_cannot_publish_outdated_quota(self):
        old = concurrent.futures.Future()
        self.engine.futures[self.key] = old
        self.engine.query_fingerprints[self.key] = credential_fingerprint(self.auth)
        self.auth = self.renewed()
        self.engine.register(self.auth, self.key)
        old.set_result(dict(account(self.key, 90), checkedAt=NOW+120))
        self.engine.poll_quotas(NOW+120)
        self.assertIsNone(self.engine.accounts[0].get('remaining'))

    def test_restart_displays_retry_deadline_even_without_cached_quota(self):
        self.engine.accounts = []
        with patch('service.time.time', return_value=NOW+1):
            visible = self.engine.view()['accounts'][0]
        self.assertEqual(visible['queryRetryAt'], NOW+1800)
        self.assertIn('重试', visible['error'])
