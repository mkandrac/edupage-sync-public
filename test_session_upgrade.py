import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from edupage_api import Edupage
from edupage_api.login_session import LoginSession
from edupage_api.exceptions import BadCredentialsException
from diagnostics import phase, report, emit
from test_privacy import definitions

class SessionUpgradeTests(unittest.TestCase):
    def test_real_library_restores_only_school_scoped_cookie(self):
        client = Edupage()
        response = Mock(content=b'userhome({});footer();')
        with patch.object(client.session, 'get', return_value=response):
            LoginSession(client).reload_data('example', 'SYNTHETIC', 'user')
        self.assertTrue(client.is_logged_in)
        self.assertEqual([(c.name,c.domain) for c in client.session.cookies], [('PHPSESSID','example.edupage.org')])

    def test_saved_session_success_does_not_login_with_password(self):
        ns = definitions()
        client = Mock()
        login = Mock()
        ns.update(get_saved_session_id=lambda *a:'synthetic', Edupage=lambda:client,
                  configure_edupage_client=Mock(), LoginSession=lambda c:Mock(),
                  password_login=login, BadCredentialsException=BadCredentialsException)
        result=ns['login_with_saved_session']({'key':'test','subdomain':'example'},'u','p',{})
        self.assertIs(result,client)
        login.assert_not_called()

    def test_rejected_session_and_challenge_remain_distinct(self):
        ns=definitions()
        client=Mock()
        client.login.return_value=object()
        ns.update(get_saved_session_id=lambda *a:'synthetic', Edupage=lambda:client,
                  configure_edupage_client=Mock(),
                  LoginSession=lambda c:Mock(reload_data=Mock(side_effect=BadCredentialsException('PRIVATE_CANARY'))),
                  BadCredentialsException=BadCredentialsException)
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, EDUPAGE_DIAGNOSTIC_FILE=d+'/status'):
            with self.assertRaises(ns['SessionRenewalRequired']):
                ns['login_with_saved_session']({'key':'test','subdomain':'example'},'u','p',{})
            data=Path(d+'/status').read_text()
        self.assertIn('session_restore:session_rejected',data)
        self.assertIn('password_login:challenge_required',data)
        self.assertNotIn('PRIVATE_CANARY',data)

    def test_public_report_rejects_unknown_content(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'status'
            p.write_text('state_load:ok\nstate_load:PRIVATE_CANARY\nPRIVATE_CANARY:ok\nstate_load:ok:PRIVATE_CANARY\n')
            out=io.StringIO()
            with contextlib.redirect_stdout(out): report(p)
        self.assertEqual(out.getvalue(),'Diagnostic state_load:ok\n')

    def test_exception_details_never_enter_diagnostic_file(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, EDUPAGE_DIAGNOSTIC_FILE=d+'/status'):
            with self.assertRaises(RuntimeError):
                with phase('state_load'): raise RuntimeError('PRIVATE_CANARY')
            self.assertEqual(Path(d+'/status').read_text(),'state_load:start\nstate_load:error\n')
