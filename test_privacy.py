import ast
import copy
from datetime import date, datetime
import email
from email.message import EmailMessage
from email.policy import default
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from private_config import load_config

ROOT = Path(__file__).parent
SOURCE = (ROOT / 'edupage_sync_github.py').read_text()
TREE = ast.parse(SOURCE)


def definitions():
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            try:
                ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            nodes.append(node)
    ns = dict(datetime=datetime, date=date, ZoneInfo=ZoneInfo)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<worker definitions>', 'exec'), ns)
    return ns


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / 'config.example.json').read_text())

    def test_valid_configuration_preserves_existing_keys(self):
        self.config['accounts'][0]['key'] = 'existing_school'
        with patch.dict(os.environ, EDUPAGE_CONFIG_JSON=json.dumps(self.config)):
            self.assertEqual(load_config()['accounts'][0]['key'], 'existing_school')

    def test_invalid_values_are_not_exposed(self):
        for raw in ['PRIVATE_CANARY', '[]', '{"accounts":"PRIVATE_CANARY"}']:
            with patch.dict(os.environ, EDUPAGE_CONFIG_JSON=raw):
                with self.assertRaisesRegex(ValueError, '^Invalid private configuration$'):
                    load_config()

    def test_duplicate_account_rejected(self):
        self.config['accounts'] *= 2
        with patch.dict(os.environ, EDUPAGE_CONFIG_JSON=json.dumps(self.config)):
            with self.assertRaises(ValueError):
                load_config()

    def test_missing_configuration_fails(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, '^Missing private configuration$'):
                load_config()


class PublicLogTests(unittest.TestCase):
    def test_worker_output_and_tracebacks_are_discarded(self):
        for ending in ['', '; raise RuntimeError("PRIVATE_CANARY")']:
            worker = ('import os; print("PRIVATE_CANARY", flush=True); '
                      'os.write(1,b"PRIVATE_CANARY"); os.write(2,b"PRIVATE_CANARY")' + ending)
            launcher = ('import os,sys; from run import execute; '
                        'sys.exit(execute([sys.executable,"-c",' + repr(worker) + '],env=dict(os.environ)))')
            result = subprocess.run([sys.executable, '-c', launcher], cwd=ROOT,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1 if ending else 0)
            self.assertEqual(result.stdout + result.stderr, '')

    def test_worker_timeout_fails_without_output(self):
        launcher = ('import os,sys; from run import execute; '
                    'sys.exit(execute([sys.executable,"-c","import time; time.sleep(10)"],'
                    'env=dict(os.environ),timeout=0.05))')
        result = subprocess.run([sys.executable, '-c', launcher], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout + result.stderr, '')

    def test_direct_worker_launch_is_blocked(self):
        env = dict(os.environ)
        env.pop('EDUPAGE_PRIVATE_WORKER', None)
        result = subprocess.run([sys.executable, str(ROOT / 'edupage_sync_github.py')],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('direct worker execution is disabled', result.stderr)

    def test_bootstrap_is_blocked_on_github(self):
        result = subprocess.run([sys.executable, 'run.py', 'bootstrap', '--account', 'example'],
                                cwd=ROOT, env={**os.environ, 'GITHUB_ACTIONS': 'true'},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.ns = definitions()
        self.imap = Mock()
        self.imap.__enter__ = Mock(return_value=self.imap)
        self.imap.__exit__ = Mock(return_value=False)
        self.imap.search.return_value = ('OK', [b''])
        self.ns.update(CONFIG={'require_existing_state': True},
                       get_gmail_credentials=lambda: ('example@example.invalid', 'test'),
                       imaplib=SimpleNamespace(IMAP4_SSL=lambda *args: self.imap),
                       select_gmail_all_mail=Mock(), email=email,
                       default_email_policy=default, json=json)

    def test_missing_state_stops_migration(self):
        with self.assertRaisesRegex(RuntimeError, 'refusing to reset history'):
            self.ns['load_state']()

    def test_existing_archived_state_preserved(self):
        state = {'processed_event_ids': ['school_a:123', 'school_b:456'],
                 'edupage_sessions': {'school_a': {'ciphertext': 'test'}}}
        msg = EmailMessage()
        msg.set_content(json.dumps(state))
        self.imap.search.return_value = ('OK', [b'1 2'])
        self.imap.fetch.return_value = ('OK', [(b'2', msg.as_bytes())])
        self.assertEqual(self.ns['load_state'](), state)
        self.ns['select_gmail_all_mail'].assert_called_once_with(self.imap)
        self.imap.fetch.assert_called_once_with(b'2', '(RFC822)')

    def test_malformed_history_fails(self):
        msg = EmailMessage()
        msg.set_content(json.dumps({'processed_event_ids': 'bad'}))
        self.imap.search.return_value = ('OK', [b'1'])
        self.imap.fetch.return_value = ('OK', [(b'1', msg.as_bytes())])
        with self.assertRaises(RuntimeError):
            self.ns['load_state']()

    def test_bootstrap_preserves_other_sessions_and_history(self):
        state = {'processed_event_ids': ['school_a:123'], 'edupage_sessions': {'other': 'test'}}
        original = copy.deepcopy(state)
        client = Mock()
        client.login.return_value = None
        sent = Mock()
        account = {'key': 'school_a', 'name': 'School A', 'subdomain': 'example',
                   'username_secret': 'U', 'password_secret': 'P'}
        def save(updated, *_):
            updated['edupage_sessions']['school_a'] = 'new-test-session'
        self.ns.update(ACCOUNTS=[account], load_state=lambda: state,
                       get_secret=lambda name: 'test', Edupage=lambda: client,
                       configure_edupage_client=Mock(), TIMEZONE='Europe/Bratislava',
                       save_session_to_state=save, send_state_email=sent)
        self.ns['bootstrap_session']('school_a')
        self.assertEqual(state['processed_event_ids'], original['processed_event_ids'])
        self.assertEqual(state['edupage_sessions']['other'], 'test')
        sent.assert_called_once_with(state)

    def test_legacy_session_decryption(self):
        import base64
        import hashlib
        from cryptography.fernet import Fernet
        password = 'synthetic-app-password'
        key = hashlib.pbkdf2_hmac('sha256', password.encode(),
                                 b'edupage-sync-gmail-state-v1', 480000, dklen=32)
        token = Fernet(base64.urlsafe_b64encode(key)).encrypt(b'synthetic-session')
        self.ns.update(base64=base64, hashlib=hashlib, Fernet=Fernet,
                       get_secret=lambda name: password)
        self.assertEqual(self.ns['get_session_cipher']().decrypt(token), b'synthetic-session')


class RawTests(unittest.TestCase):
    def test_content_is_not_filtered_before_raw(self):
        ns = definitions()
        for kind, expected in [('MESSAGE', 'message'), ('HOMEWORK', 'homework'),
                               ('SUBSTITUTION', 'timetable_change'), ('GRADE', 'event'),
                               ('UNKNOWN', 'event'), ('CONFIRMATION', 'app_action'),
                               ('H_MESSAGE', None)]:
            event = SimpleNamespace(event_type=SimpleNamespace(name=kind),
                                    text='Synthetic ordinary text', additional_data={}, is_done=True)
            self.assertEqual(ns['classify_notification'](event, [], date(2026, 1, 1)), expected)


if __name__ == '__main__':
    unittest.main()
