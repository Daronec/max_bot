import bot_test_support  # Load synthetic configuration before application imports.
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from notification_forwarder import NotificationForwarder, notification_text


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'db'
        with sqlite3.connect(self.source) as db:
            db.executescript('CREATE TABLE app(app_id INTEGER PRIMARY KEY,identifier TEXT); CREATE TABLE record(rec_id INTEGER PRIMARY KEY,app_id INTEGER,uuid BLOB,data BLOB,delivered_date REAL);')
            db.executemany('INSERT INTO app VALUES(?,?)',[(1,'com.apple.mail'),(2,'unrelated.app')])
        self.client = Mock()
        self.client.send_message.return_value={'message':{'body':{'mid':'ok'}}}
        self.f = NotificationForwarder(self.client, self.source, self.root/'state', chat='123456789')

    def add(self, ident, text='Полный текст\nвторая строка', app=1):
        with sqlite3.connect(self.source) as db:
            db.execute('INSERT INTO record VALUES(?,?,?,?,?)', (ident,app,bytes([ident]),plistlib.dumps({'req':{'titl':'Заголовок','subt':'Подзаголовок','body':text}}),800000000+ident))

    def test_initial_history_skipped_new_text_sent_once_across_restart(self):
        self.add(1)
        self.f.tick()
        self.client.send_message.assert_not_called()
        self.add(2)
        self.add(3, app=2)
        self.f.tick()
        self.assertEqual(self.client.send_message.call_count,1)
        self.assertIn('Полный текст\nвторая строка',self.client.send_message.call_args.args[1])
        NotificationForwarder(self.client,self.source,self.root/'state').tick()
        self.assertEqual(self.client.send_message.call_count,1)

    def test_partial_delivery_resumes_at_failed_chunk(self):
        self.f.tick()
        self.add(1, 'X'*8000)
        self.client.send_message.side_effect=[{'message':{'id':'first'}},None]
        with self.assertRaises(RuntimeError): self.f.tick()
        first_chunk=self.client.send_message.call_args_list[0].args[1]
        self.client.send_message.side_effect=None
        self.client.send_message.reset_mock()
        self.f.tick()
        self.assertEqual(self.client.send_message.call_count,2)
        self.assertNotEqual(self.client.send_message.call_args_list[0].args[1],first_chunk)
        self.f.tick()
        self.assertEqual(self.client.send_message.call_count,2)

    def test_changed_delivery_is_forwarded(self):
        self.add(1)
        self.f.tick()
        with sqlite3.connect(self.source) as db:
            db.execute('UPDATE record SET delivered_date=delivered_date+30')
        self.f.tick()
        self.client.send_message.assert_called_once()

    def test_unauthorized_chat_reads_and_sends_nothing(self):
        self.f.chat='123'
        with patch.object(self.f,'_rows') as read:
            with self.assertRaises(ValueError): self.f.tick()
            read.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_source_missing_is_visible_and_not_created(self):
        self.source.unlink()
        with self.assertRaises(sqlite3.Error): self.f.tick()
        self.assertFalse(self.source.exists())

    def test_invalid_payload_is_not_marked_delivered(self):
        self.f.tick()
        with sqlite3.connect(self.source) as db:
            db.execute('INSERT INTO record VALUES(1,1,?,?,123)',(b'uid',b'invalid'))
        self.f.tick()
        self.assertEqual(self.f.unsupported,1)
        self.client.send_message.assert_not_called()
        self.add(2)
        self.f.tick()
        self.client.send_message.assert_called_once()
