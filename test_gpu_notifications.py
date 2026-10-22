import json
import logging
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import gpu_bot as bot
from gpu_notifications import NotificationStore, check_notifications


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'notifications.sqlite3'
        self.store = NotificationStore(self.path)
        self.server = bot.Server('local')
        self.gpu = bot.GPU('0', 'GPU-uuid', 'Test GPU', 0, 0, 100)
        self.client = Mock()
        self.logger = Mock(spec=logging.Logger)

    def subscribe(self, user='U1'):
        self.store.subscribe(self.server, self.gpu.uuid, self.gpu.index, user)

    def check(self, read=None):
        read = read or Mock(return_value=[self.gpu])
        check_notifications(self.store, self.client, [self.server], read, self.logger)
        return read

    def test_no_subscriptions_no_gpu_queries(self):
        self.check().assert_not_called()

    def test_busy_then_free_notifies_once_per_person_with_shared_query(self):
        self.subscribe()
        self.subscribe('U2')
        self.gpu.users = {'alice'}
        self.check().assert_called_once_with(self.server)
        self.client.chat_postMessage.assert_not_called()
        self.gpu.users.clear()
        self.check().assert_called_once_with(self.server)
        self.assertEqual(self.client.chat_postMessage.call_count, 2)
        self.assertEqual({call.kwargs['channel'] for call in self.client.chat_postMessage.call_args_list}, {'U1', 'U2'})
        self.assertFalse(self.store.pending())
        self.check().assert_not_called()
        self.assertEqual(self.client.chat_postMessage.call_count, 2)

    def test_duplicates_persist_and_cancel_only_current_user(self):
        self.subscribe()
        self.subscribe()
        self.subscribe('U2')
        recovered = NotificationStore(self.path)
        self.assertEqual(len(recovered.pending()), 2)
        recovered.cancel(self.server, self.gpu.uuid, 'U1')
        self.assertEqual([row['user_id'] for row in recovered.pending()], ['U2'])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_read_error_unknown_users_and_missing_gpu_never_notify(self):
        self.subscribe()
        self.check(Mock(side_effect=OSError('SSH unavailable')))
        self.check(Mock(return_value=[]))
        self.gpu.users_complete = False
        self.check()
        self.client.chat_postMessage.assert_not_called()
        self.assertEqual(len(self.store.pending()), 1)

    def test_failed_dm_keeps_subscription_for_retry(self):
        self.subscribe()
        self.client.chat_postMessage.side_effect = RuntimeError('Slack unavailable')
        self.check()
        self.assertEqual(len(self.store.pending()), 1)
        self.client.chat_postMessage.side_effect = None
        self.check()
        self.assertFalse(self.store.pending())

    def test_failure_for_one_subscriber_does_not_block_another(self):
        self.subscribe()
        self.subscribe('U2')
        self.client.chat_postMessage.side_effect = [RuntimeError('DM blocked'), {}]
        self.check()
        self.assertEqual(len(self.store.pending()), 1)

    def test_cancel_while_querying_prevents_notification(self):
        self.subscribe()
        def read(server):
            self.store.cancel(server, self.gpu.uuid, 'U1')
            return [self.gpu]
        self.check(Mock(side_effect=read))
        self.client.chat_postMessage.assert_not_called()

    def test_gpu_identity_uses_server_and_uuid_not_index(self):
        self.subscribe()
        other = bot.Server('remote', 'user@remote')
        self.store.subscribe(other, 'other-uuid', '0', 'U2')
        self.gpu.uuid = 'replacement-uuid'
        self.check()
        self.client.chat_postMessage.assert_not_called()
        self.assertEqual(len(self.store.pending()), 2)

    def test_buttons_in_channel_and_home_select_correct_gpu(self):
        blocks = []
        rows = []
        with patch.object(bot, 'configured_servers', return_value=[self.server]), patch.object(bot, 'read_gpu_status', return_value=[self.gpu]):
            bot.build_message(self.logger, blocks, rows)
        entry = next(block['elements'] for block in blocks if block['type'] == 'actions')
        self.assertEqual([button['action_id'] for button in entry], ['open_gpu_notifications'])
        controls = bot.build_notification_blocks(rows)
        buttons = next(block['elements'] for block in controls if block['type'] == 'actions')
        self.assertEqual(json.loads(buttons[0]['value']), ['local', None, 'GPU-uuid', '0'])
        self.assertEqual(buttons[1]['action_id'], 'cancel_gpu_watch')
        self.assertIn(blocks[-1], bot.build_home_view(status_blocks=blocks)['blocks'])
        body = {'user': {'id': 'U1'}, 'actions': [buttons[0]]}
        ack = Mock()
        with patch.object(bot, 'configured_servers', return_value=[self.server]):
            bot.handle_gpu_watch(ack, body, self.client, self.logger, self.store)
            ack.assert_called_once_with()
            self.assertEqual(len(self.store.pending()), 1)
            self.assertEqual(self.client.chat_postMessage.call_args.kwargs['channel'], 'U1')
            body['actions'] = [buttons[1]]
            bot.handle_gpu_watch(ack, body, self.client, self.logger, self.store)
            self.assertFalse(self.store.pending())

    def test_inline_toggle_uses_snapshot_without_gpu_queries(self):
        rows = [(self.server, self.gpu.index, json.dumps(['local', None, 'GPU-uuid', '0']))]
        original = bot.build_notification_group(rows)
        def body_for(blocks):
            toggle = blocks[-1]
            return {
                'actions': [dict(toggle['elements'][0], block_id=toggle['block_id'])],
                'message': {'blocks': blocks, 'text': 'status', 'ts': '123'},
                'channel': {'id': 'C1'}, 'user': {'id': 'U1'},
            }
        ack = Mock()
        with patch.object(bot, 'read_gpu_status') as read:
            bot.handle_toggle_notifications(ack, body_for(original), self.client)
            expanded = self.client.chat_update.call_args.kwargs['blocks']
            self.assertTrue(any(element['action_id'] == 'watch_gpu' for block in expanded for element in block.get('elements', [])))
            bot.handle_toggle_notifications(ack, body_for(expanded), self.client)
            collapsed = self.client.chat_update.call_args.kwargs['blocks']
            self.assertEqual(len(collapsed), 1)
            self.assertEqual(collapsed[0]['elements'][0]['action_id'], 'open_gpu_notifications')
            read.assert_not_called()
            home = body_for(original)
            home['view'] = {'type': 'home', 'blocks': original}
            bot.handle_toggle_notifications(ack, home, self.client)
            self.assertEqual(self.client.views_publish.call_args.kwargs['user_id'], 'U1')
            read.assert_not_called()

    def test_process_query_failure_is_not_treated_as_idle(self):
        with patch.object(bot, 'run_server_command', side_effect=[
            '0, GPU-uuid, Test GPU, 0, 0, 100',
            subprocess.CalledProcessError(1, 'nvidia-smi'),
        ]):
            with self.assertRaises(subprocess.CalledProcessError):
                bot.read_gpu_status(self.server)

    def test_unknown_process_owner_blocks_notification(self):
        with patch.object(bot, 'run_server_command', side_effect=[
            '0, GPU-uuid, Test GPU, 0, 0, 100', 'GPU-uuid, 123', '',
        ]):
            gpu = bot.read_gpu_status(self.server)[0]
        self.assertFalse(gpu.users_complete)
        self.assertIn('取得できませんでした', '\n'.join(bot.format_gpu(gpu)))

    def test_configured_exclusion_applies_to_zero_user_detection(self):
        with patch.dict(os.environ, {'EXCLUDE_PROCESSES': "[('alice', 'python service.py')]"}), patch.object(bot, 'run_server_command', side_effect=[
            '0, GPU-uuid, Test GPU, 0, 0, 100', 'GPU-uuid, 123', 'alice python service.py',
        ]):
            gpu = bot.read_gpu_status(self.server)[0]
        self.assertFalse(gpu.users)
        self.assertTrue(gpu.users_complete)


if __name__ == '__main__':
    unittest.main()
