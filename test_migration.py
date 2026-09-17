import contextlib
import io
import json
import unittest
from unittest.mock import patch

from migrate_from_keychain import MigrationError, migrate, parse_source


SOURCE = '''
ACCOUNTS = [
    {"key":"school_a","name":"School A","subdomain":"example-a",
     "username_secret":"OLD_USER_A","password_secret":"OLD_PASS_A"},
    {"key":"school_b","name":"School B","subdomain":"example-b",
     "username_secret":"OLD_USER_B","password_secret":"OLD_PASS_B"}
]
TIMEZONE = "Europe/Bratislava"
KEYCHAIN_SERVICE = "example-service"
GMAIL_USERNAME_SECRET = "MAIL_USER"
GMAIL_PASSWORD_SECRET = "MAIL_PASS"
ACTIONABLE_CATEGORIES_START_AT = datetime.fromisoformat("2026-01-02T03:04:05")
raise RuntimeError("Never execute this source")
'''


class MigrationTests(unittest.TestCase):
    def test_parse_without_execution_and_preserve_config(self):
        config, mapping, service = parse_source(SOURCE)
        self.assertEqual(config['capture_start_at'], '2026-01-02T03:04:05')
        self.assertEqual(config['accounts'][1]['key'], 'school_b')
        self.assertEqual(config['accounts'][1]['password_secret'], 'EDUPAGE_ACCOUNT_2_PASSWORD')
        self.assertTrue(config['require_existing_state'])
        self.assertEqual(service, 'example-service')
        self.assertEqual(mapping[-1], ('EDUPAGE_ACCOUNT_2_PASSWORD', 'OLD_PASS_B'))

    def test_dynamic_config_rejected(self):
        with self.assertRaises(MigrationError):
            parse_source(SOURCE.replace('ACCOUNTS = [', 'ACCOUNTS = load_private_data() # ['))

    def run_migration(self, fail_keychain=False):
        calls = []
        def fake(args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] == ['gh', 'api']:
                if '/contents/' in args[2]:
                    return SOURCE
                return json.dumps({'private': args[2].endswith('/old'), 'permissions': {'admin': True}})
            if args[0] == 'security':
                if fail_keychain:
                    raise MigrationError('Keychain unavailable')
                return 'PRIVATE_CANARY\n'
            return ''
        output = io.StringIO()
        with patch('migrate_from_keychain.invoke', side_effect=fake), contextlib.redirect_stdout(output):
            if fail_keychain:
                with self.assertRaises(MigrationError):
                    migrate('owner/old', 'owner/new')
            else:
                migrate('owner/old', 'owner/new')
        return calls, output.getvalue()

    def test_credentials_only_in_stdin_and_jobs_disabled_first(self):
        calls, output = self.run_migration()
        self.assertNotIn('PRIVATE_CANARY', output)
        uploads = [(a,k) for a,k in calls if a[:3] == ['gh','secret','set']]
        self.assertEqual(len(uploads), 7)
        for args, kwargs in uploads:
            self.assertNotIn('PRIVATE_CANARY', repr(args))
            self.assertIn('value', kwargs)
        writes = [a for a,k in calls if a[1] in ('variable','secret')]
        self.assertEqual([a[1] for a in writes[:2]], ['variable','variable'])
        self.assertTrue(all(a[-1] == 'false' for a in writes[:2]))
        self.assertIn('DONE: 7 secrets copied.', output)

    def test_missing_keychain_has_no_remote_writes(self):
        calls, _ = self.run_migration(fail_keychain=True)
        self.assertFalse(any(a[1] in ('variable','secret') for a,k in calls))

    def test_same_repo_is_rejected(self):
        with self.assertRaises(MigrationError):
            migrate('owner/repo', 'owner/repo')


if __name__ == '__main__':
    unittest.main()
