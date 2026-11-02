import base64
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'backend'))
from adapters import account_profile, identity
from service import Engine, Store
from test_policy import account, card, NOW


def login(**claims):
    payload = dict(sub='same-person'); payload.update(claims)
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return {'tokens': {'id_token': 'header.'+part+'.signature', 'access_token': 'not-a-real-token', 'account_id': 'workspace'}}


class ProfilesTests(unittest.TestCase):
    def test_real_name_and_email_do_not_change_identity(self):
        auth = login(name='原账号名', email='name@example.test')
        self.assertEqual(account_profile(auth), dict(alias='原账号名', email='name@example.test'))
        self.assertEqual(identity(auth), identity(login(name='新显示名')))

    def test_email_fallback_and_control_characters(self):
        self.assertEqual(account_profile(login(email='user@example.test'))['alias'], 'user@example.test')
        self.assertEqual(account_profile(login(name='  名\u202e字\x00  '))['alias'], '名字')
        self.assertEqual(account_profile(login())['alias'], '未命名账号')

    def test_migration_keeps_keys_and_does_not_replace_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp))
            store.put('profiles', [dict(key='a', alias='账号 A'), dict(key='b', alias='账号 B')])
            vault = Mock(); vault.get.side_effect = [login(name='甲'), login(name='乙')]
            engine = Engine(store, vault)
            try:
                engine.migrate_profiles()
                self.assertEqual(engine.profiles, [dict(key='a', alias='甲', email='', rotationEnabled=True), dict(key='b', alias='乙', email='', rotationEnabled=True)])
                self.assertEqual(store.get('profiles'), engine.profiles)
                vault.put.assert_not_called()
            finally:
                engine.pool.shutdown(); store.db.close()

    def test_all_cards_sorted_without_card_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(pathlib.Path(tmp)); engine = Engine(store, Mock())
            try:
                engine.accounts = [account('a', 20, [card('late', NOW+100), card('undated', None), card('soon', NOW+2)])]
                view = engine.view()['accounts'][0]['cards']
                self.assertEqual([c['expiresAt'] for c in view], [NOW+2, NOW+100, None])
                self.assertTrue(all('id' not in c for c in view))
                self.assertEqual(len(view), 3)
            finally:
                engine.pool.shutdown(); store.db.close()
