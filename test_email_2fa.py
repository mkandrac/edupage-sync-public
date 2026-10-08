"""Synthetic emails only: no production codes, recipients, or credentials."""
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
import io
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from edupage_api.exceptions import BadCredentialsException, RetryLaterException
from email_2fa import (EmailSecondFactorError, GmailCodeInbox, extract_code,
                       complete_email_second_factor, recipient_matches)
from diagnostics import report
from test_privacy import definitions

NOW = 1800000000
MAILBOX = 'example@gmail.com'


def mail(body='Váš overovací kód: 123456', *, sender='noreply@mail1.edupage.org',
         recipient=MAILBOX, timestamp=NOW, authenticated=True, html=False):
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = recipient
    msg['Date'] = format_datetime(datetime.fromtimestamp(timestamp, timezone.utc))
    msg['Subject'] = 'EduPage'
    msg['Authentication-Results'] = ('mx.google.com; dmarc=pass header.from=mail1.edupage.org'
                                      if authenticated else 'untrusted.test; dmarc=pass header.from=edupage.org')
    msg.set_content(body, subtype='html' if html else 'plain')
    return msg.as_bytes()


class EmailValidationTests(unittest.TestCase):
    def code(self, raw, **kwargs):
        return extract_code(raw, recipient=MAILBOX, subdomain='example',
                            not_before=NOW, now=kwargs.get('now', NOW + 2))

    def test_text_html_and_leading_zero(self):
        self.assertEqual(self.code(mail()), '123456')
        self.assertEqual(self.code(mail('<p>Verification code:</p><b>012345</b>', html=True)), '012345')

    def test_expired_and_pre_request_and_future_rejected(self):
        for timestamp in (NOW - 6, NOW + 60):
            self.assertIsNone(self.code(mail(timestamp=timestamp)))
        self.assertIsNone(self.code(mail(), now=NOW + 301))

    def test_forged_sender_recipient_and_authentication_rejected(self):
        for changes in ({'sender':'noreply@edupage.org.evil.test'},
                        {'sender':'"EduPage" <attacker@example.test>'},
                        {'recipient':'other@gmail.com'}, {'authenticated':False}):
            self.assertIsNone(self.code(mail(**changes)))

    def test_other_school_ambiguous_reset_and_unlabelled_numbers_rejected(self):
        for body in ('Váš overovací kód: 123456 https://other.edupage.org/login/',
                     'Verification code: 123456; Verification code: 654321',
                     'Password reset. Verification code: 123456',
                     'Zmena hesla. Overovací kód: 123456',
                     'Prihlásenie 123456', 'Verification code: 123456789'):
            self.assertIsNone(self.code(mail(body)))

    def test_destination_must_be_known(self):
        for target in (None, '', '*@gmail.com', 'other@gmail.com'):
            self.assertFalse(recipient_matches(target, MAILBOX))
        self.assertTrue(recipient_matches('e***@gmail.com', MAILBOX))
        self.assertTrue(recipient_matches('ex.ample@googlemail.com', MAILBOX))


class Clock:
    def __init__(self): self.t = 0
    def now(self): return self.t
    def sleep(self, seconds): self.t += seconds


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.challenge = Mock(email=MAILBOX)
        self.inbox = Mock()
        self.inbox.checkpoint.return_value = 12
        self.inbox.poll.return_value = '012345'
        self.factory = Mock()
        self.factory.return_value.__enter__ = Mock(return_value=self.inbox)
        self.factory.return_value.__exit__ = Mock(return_value=False)

    def complete(self, **kwargs):
        complete_email_second_factor(self.challenge, username=MAILBOX, password='synthetic',
            subdomain='example', select_all_mail=Mock(), inbox_factory=self.factory,
            monotonic=self.clock.now, wall_time=lambda:NOW + self.clock.t,
            sleep=self.clock.sleep, **kwargs)

    def test_countdown_then_single_delivery_and_code_submission(self):
        self.challenge.send_email_code.side_effect = [RetryLaterException('wait', 30), None]
        self.inbox.poll.side_effect = [None, '012345']
        self.complete()
        self.assertEqual(self.clock.t, 36)
        self.assertEqual(self.challenge.send_email_code.call_count, 2)
        self.challenge.finish_with_code.assert_called_once_with('012345')
        self.assertEqual(self.inbox.poll.call_args.kwargs['not_before'], NOW + 31)

    def test_unknown_recipient_does_not_request_mail(self):
        self.challenge.email = 'other@gmail.com'
        with self.assertRaisesRegex(EmailSecondFactorError, 'recipient_unverified'):
            self.complete()
        self.challenge.send_email_code.assert_not_called()

    def test_timeout_does_not_resend_or_submit_code(self):
        self.inbox.poll.return_value = None
        with self.assertRaisesRegex(EmailSecondFactorError, 'timeout'):
            self.complete(budget=12)
        self.challenge.send_email_code.assert_called_once()
        self.challenge.finish_with_code.assert_not_called()
        self.assertLess(self.clock.t, 12)

    def test_long_countdown_is_bounded(self):
        self.challenge.send_email_code.side_effect = RetryLaterException('wait', 1000)
        with self.assertRaisesRegex(EmailSecondFactorError, 'timeout'):
            self.complete()
        self.assertEqual(self.clock.t, 0)

    def test_failure_cannot_expose_code_or_response(self):
        self.challenge.finish_with_code.side_effect = ValueError('PRIVATE_CANARY 012345')
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, EDUPAGE_DIAGNOSTIC_FILE=directory+'/status'):
            with self.assertRaises(EmailSecondFactorError) as error:
                self.complete()
            output = io.StringIO()
            with contextlib.redirect_stdout(output): report(directory+'/status')
            self.assertNotIn('PRIVATE_CANARY', output.getvalue() + str(error.exception))
            self.assertNotIn('012345', output.getvalue() + str(error.exception))


class InboxTests(unittest.TestCase):
    def inbox(self, uids, bodies):
        inbox = GmailCodeInbox(MAILBOX, 'synthetic', Mock())
        imap = Mock()
        inbox.imap = imap
        imap.uid.side_effect = [('OK', [uids])] + [('OK', [(b'RFC822', b)]) for b in bodies]
        return inbox

    def test_imap_last_uid_quirk_does_not_reuse_old_mail(self):
        inbox = self.inbox(b'10', [])
        self.assertIsNone(inbox.poll(11, subdomain='example', not_before=NOW, now=NOW))
        self.assertEqual(inbox.imap.uid.call_count, 1)

    def test_fresh_message_read_does_not_mark_seen(self):
        inbox = self.inbox(b'12', [mail()])
        self.assertEqual(inbox.poll(12, subdomain='example', not_before=NOW, now=NOW), '123456')
        self.assertEqual(inbox.imap.uid.call_args.args, ('fetch', '12', '(BODY.PEEK[])'))

    def test_two_different_current_codes_fail_closed(self):
        inbox = self.inbox(b'12 13', [mail(), mail('Verification code: 654321')])
        with self.assertRaisesRegex(EmailSecondFactorError, 'ambiguous_codes'):
            inbox.poll(12, subdomain='example', not_before=NOW, now=NOW)


class IntegrationTests(unittest.TestCase):
    def setup_worker(self):
        ns = definitions()
        client = Mock()
        challenge = Mock()
        account = {'key':'example', 'subdomain':'example', 'username_secret':'U', 'password_secret':'P'}
        ns.update(Edupage=lambda:client, configure_edupage_client=Mock(),
                  password_login=Mock(return_value=challenge),
                  get_saved_session_id=Mock(return_value='synthetic'),
                  restore_session=Mock(side_effect=BadCredentialsException()),
                  BadCredentialsException=BadCredentialsException, TIMEZONE='Europe/Bratislava',
                  get_gmail_credentials=lambda:(MAILBOX,'synthetic'),
                  complete_email_second_factor=Mock(), select_gmail_all_mail=Mock())
        return ns, client, account

    def test_expired_session_completes_email_2fa(self):
        ns, client, account = self.setup_worker()
        state = {'processed_event_ids':['example:123']}
        self.assertIs(ns['login_with_saved_session'](account,'u','p',state), client)
        ns['complete_email_second_factor'].assert_called_once()
        self.assertEqual(state['auth_verification']['example']['method'], 'email_verified')
        self.assertEqual(state['processed_event_ids'], ['example:123'])

    def test_fresh_check_never_reads_or_restores_saved_session(self):
        ns, client, account = self.setup_worker()
        ns['password_login'].return_value = None
        state = {}
        ns['login_with_saved_session'](account,'u','p',state,force_new=True)
        ns['get_saved_session_id'].assert_not_called()
        ns['restore_session'].assert_not_called()
        ns['complete_email_second_factor'].assert_not_called()
        self.assertEqual(state['auth_verification']['example']['method'], 'password_only')

    def test_probe_preserves_history_and_saves_success_even_if_other_fails(self):
        ns, client, account = self.setup_worker()
        state = {'processed_event_ids':['example:123'], 'edupage_sessions':{'other':'existing'}}
        calls = []
        def save(s,a,c,t):
            calls.append(a['key']);s['edupage_sessions'][a['key']] = 'new'
        ns.update(load_state=lambda:state, ACCOUNTS=[account, {**account,'key':'other'}],
                  get_secret=Mock(return_value='synthetic'),
                  login_with_saved_session=Mock(side_effect=[client, RuntimeError('PRIVATE_CANARY')]),
                  save_session_to_state=save, send_state_email=Mock())
        with self.assertRaises(ns['SessionRenewalRequired']): ns['check_fresh_authentication']()
        self.assertEqual(calls, ['example'])
        self.assertEqual(state['processed_event_ids'], ['example:123'])
        self.assertEqual(state['edupage_sessions']['other'], 'existing')
        ns['send_state_email'].assert_called_once_with(state)
