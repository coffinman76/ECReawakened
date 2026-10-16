"""Regression checks for generated accounts and completed-install-only Octo checks."""
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engine import net, new_account, tutorial_templates, vault


class GeneratedAccountTests(unittest.TestCase):
    def test_starter_inventory_identity_dates_and_equipment_references(self):
        uid, now = 123456789, 1800000000000
        user = new_account.build(uid, now).common.user
        self.assertEqual(len(user.update.user_weapon_list), 21)
        self.assertEqual(len(user.update.user_character_list), 15)
        self.assertEqual(sum(len(rows) for _, rows in user.update.ListFields()), 231)
        for _, rows in user.update.ListFields():
            for row in rows:
                self.assertEqual(row.user_id, uid)
                for field, value in row.ListFields():
                    if field.name.endswith('_datetime') or field.name in ('datetime', 'date_value'):
                        self.assertEqual(value, now)
        materia = {row.user_materia_id for row in user.update.user_materia_list}
        self.assertEqual(len(materia), 3)
        for member in user.update.user_party_member_list:
            for slot in range(3):
                reference = getattr(member, f'active_materia_id{slot}')
                if reference:
                    self.assertIn(reference, materia)
        self.assertTrue(user.HasField('delete'))

    def test_accounts_get_new_instance_ids(self):
        with patch.object(new_account.secrets, 'randbelow', side_effect=[100, 200]):
            first, second = new_account.build(123), new_account.build(456)
        self.assertNotEqual(first.common.user.update.user_materia_list[0].user_materia_id,
                            second.common.user.update.user_materia_list[0].user_materia_id)

    def test_invalid_identity_is_rejected(self):
        for value in (0, -1, 2**63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                new_account.build(value)

    def test_account_creation_load_and_tutorial_deltas_work_without_fresh_pb(self):
        with TemporaryDirectory() as folder:
            vault.create_account(folder, 123, 'new')
            state = vault.State.load(folder)
            self.assertEqual(state.user_id, 123)
            query = SimpleNamespace(user_name='Example player', message='')
            register = tutorial_templates.response('register', state, query, 1800000000000, lambda: 999)
            self.assertEqual(register.common.user.update.user_profile_list[0].name, query.user_name)
            skip = tutorial_templates.response('skip', state, query, 1800000000000, lambda: 999)
            self.assertEqual(skip.common.user.update.user_materia_list[0].user_materia_id, 999)
            self.assertEqual(json.loads((Path(folder) / 'info.json').read_text())['player_id'], '123')

    def test_existing_binary_account_saves_are_still_readable(self):
        with TemporaryDirectory() as folder:
            raw = new_account.build(456, 1800000000000).SerializeToString()
            (Path(folder) / 'base.pb').write_bytes(raw)
            (Path(folder) / 'info.json').write_text(json.dumps({'player_id': '456', 'origin': 'archived'}))
            state = vault.State.load(folder)
            self.assertEqual(state.origin, 'archived')
            self.assertEqual(state.user_id, 456)
            self.assertEqual(len(state.tables['user_weapon_list']), 21)


class CompletedInstallTests(unittest.TestCase):
    def handler(self):
        handler = net.Handler.__new__(net.Handler)
        handler._send, handler._note = Mock(), Mock()
        return handler

    def test_final_revision_and_empty_delta_for_all_platforms(self):
        handler = self.handler()
        for platform, revision in [('323173179', 368), ('223173179', 493), ('123173170', 492)]:
            for path in (f'/v1/revision/{platform}', f'/v1/list/{platform}/{revision}'):
                handler._resources(path)
                self.assertEqual(handler._send.call_args.args,
                                 (200, {'Content-Type': 'application/x-protobuf'}, b'\x08' + net._varint(revision)))

    def test_full_indexes_and_unknown_platforms_are_unavailable(self):
        handler = self.handler()
        for path in ('/v1/list/323173179/0', '/v1/list/223173179/492', '/v1/revision/999', '/v1/list'):
            handler._resources(path)
            self.assertEqual(handler._send.call_args.args[0], 404)

    def test_blob_hostname_is_not_routed_or_fetched(self):
        self.assertFalse(any(name.startswith('resources-data') for name in net.HOSTS))
        self.assertFalse(hasattr(net.Handler, '_object'))
        self.assertFalse(hasattr(net.Handler, '_fetch'))
        handler = self.handler()
        handler.headers = {'Host': 'resources-data-w6d4k7cz.app.gl.ffviiec.com'}
        handler.path, handler.command, handler.rfile = '/o/Example', 'GET', io.BytesIO()
        handler._route()
        self.assertEqual(handler._send.call_args.args[0], 404)


if __name__ == '__main__':
    unittest.main()
