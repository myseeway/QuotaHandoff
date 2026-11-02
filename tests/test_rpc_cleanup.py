import errno
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
import adapters


class QueryCleanupTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch('adapters.time.sleep').start()
        self.pending = list(adapters._cleanup_pending)
        adapters._cleanup_pending.clear()

    def tearDown(self):
        adapters._cleanup_pending[:] = self.pending
        patch.stopall()

    def test_racing_directory_cleanup_retries(self):
        temp = Mock()
        temp.cleanup.side_effect = [OSError(errno.ENOTEMPTY, 'still writing'), None]
        self.assertTrue(adapters.cleanup_query_temp(temp))
        self.assertEqual(temp.cleanup.call_count, 2)

    def rpc(self):
        rpc = adapters.RPC.__new__(adapters.RPC)
        rpc.closed = False
        rpc.p = Mock(); rpc.p.poll.return_value = 0
        rpc.temp = Mock(); rpc.temp.cleanup.side_effect = OSError(errno.ENOTEMPTY, 'still writing')
        return rpc

    def test_cleanup_failure_keeps_successful_quota_result(self):
        rpc = self.rpc()
        rpc.call = Mock(return_value=dict(accountId='account', rateLimits={'valid': True}))
        with patch('adapters.RPC', return_value=rpc):
            result = adapters.usage(dict(tokens=dict(account_id='account')))
        self.assertEqual(result['rateLimits'], {'valid': True})
        self.assertEqual(adapters.cleanup_pending_count(), 1)
        rpc.temp.cleanup.side_effect = None
        adapters.retry_query_cleanup()
        self.assertEqual(adapters.cleanup_pending_count(), 0)

    def test_cleanup_failure_does_not_mask_query_failure(self):
        rpc = self.rpc()
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            with rpc:
                raise RuntimeError('original failure')
        self.assertEqual(adapters.cleanup_pending_count(), 1)

    def test_repeated_close_does_not_duplicate_deferred_cleanup(self):
        rpc = self.rpc()
        rpc.close(); rpc.close()
        self.assertEqual(adapters.cleanup_pending_count(), 1)
