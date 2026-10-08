"""Bounded email second factor. Never expose codes, mail bodies or server errors."""
from email import message_from_bytes
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
import imaplib
import re
import time
import unicodedata

from edupage_api.exceptions import RetryLaterException
from diagnostics import emit


class EmailSecondFactorError(RuntimeError):
    """Only fixed public-safe reasons may leave this module."""


def address_key(address):
    address = address.strip().casefold()
    if address.count('@') != 1:
        return ''
    local, domain = address.split('@')
    if domain in {'gmail.com', 'googlemail.com'}:
        local = local.split('+')[0].replace('.', '')
        domain = 'gmail.com'
    return local + '@' + domain


def recipient_matches(target, mailbox):
    # EduPage may mask the local part. Never accept a missing destination or
    # a domain-only hint, and still verify the full recipient on the email.
    if not isinstance(target, str) or target.count('@') != 1:
        return False
    if '*' not in target:
        return bool(address_key(target)) and address_key(target) == address_key(mailbox)
    local, domain = target.casefold().split('@')
    if not local.replace('*', '') or '*' in domain:
        return False
    pattern = re.escape(target.casefold()).replace(r'\*', '.*')
    return re.fullmatch(pattern, mailbox.casefold()) is not None


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'style', 'script'}:
            self.hidden += 1
        if tag in {'br', 'p', 'div', 'tr', 'td'}:
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in {'style', 'script'} and self.hidden:
            self.hidden -= 1
        self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_code(raw, *, recipient, subdomain, not_before, now):
    """Return one unambiguous code from a fresh, authenticated EduPage email."""
    if len(raw) > 256_000:
        return None
    try:
        msg = message_from_bytes(raw, policy=default)
        senders = getaddresses(msg.get_all('From', []))
        if len(senders) != 1:
            return None
        domain = senders[0][1].rsplit('@', 1)[-1].casefold()
        if domain != 'edupage.org' and not domain.endswith('.edupage.org'):
            return None
        recipients = getaddresses(msg.get_all('To', []) + msg.get_all('Delivered-To', []))
        if address_key(recipient) not in {address_key(a) for _, a in recipients}:
            return None
        # Use Gmail's first Authentication-Results header, not an arbitrary
        # pass claimed by a sender. DMARC must align with the EduPage domain.
        auth = next((str(h) for h in msg.get_all('Authentication-Results', [])
                     if re.match(r'^\s*mx\.google\.com\s*;', str(h), re.I)), '')
        if not re.search(r'\bdmarc=pass\b[^;]*\bheader\.from=(?:[a-z0-9-]+\.)*edupage\.org(?=[\s;]|$)', auth, re.I):
            return None
        sent = parsedate_to_datetime(str(msg['Date']))
        if sent.tzinfo is None or not (not_before - 5 <= sent.timestamp() <= now + 30):
            return None
        if now - sent.timestamp() >= 300:
            return None
        texts = [str(msg.get('Subject', ''))]
        for part in msg.walk():
            if part.get_content_disposition() == 'attachment':
                continue
            if part.get_content_type() == 'text/plain':
                texts.append(part.get_content())
            elif part.get_content_type() == 'text/html':
                parser = TextOnly()
                parser.feed(part.get_content())
                texts.append(''.join(parser.parts))
        text = ' '.join(texts)
        # Reject a message explicitly belonging to another school.
        schools = set(re.findall(r'https?://([a-z0-9-]+)\.edupage\.org', text, re.I))
        schools -= {'www', 'login1', 'help', 'pomoc', 'portal'}
        if schools and subdomain.casefold() not in {s.casefold() for s in schools}:
            return None
        folded = ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                         if not unicodedata.combining(c))
        if re.search(r'reset.{0,20}password|password.{0,20}reset|obnov.{0,20}hesl|zmen.{0,20}hesl', folded):
            return None
        # Require a code label; never pick an arbitrary number from a notice.
        labels = r'(?:overovaci|overovacieho|prihlasovaci|verifikacny|bezpecnostny|autorizacny)\s+kod|(?:verification|security|authentication|login)\s+code'
        codes = set(re.findall(r'(?:' + labels + r')\s*(?:je|is)?\s*[:=\-]?\s*([0-9]{4,8})\b', folded))
        return next(iter(codes)) if len(codes) == 1 else None
    except (ValueError, TypeError, KeyError, UnicodeError, LookupError):
        return None


class GmailCodeInbox:
    def __init__(self, username, password, select_all_mail):
        self.username = username
        self.password = password
        self.select_all_mail = select_all_mail
        self.imap = None
        self.saw_unmatched = False

    def __enter__(self):
        try:
            self.imap = imaplib.IMAP4_SSL('imap.gmail.com', 993, timeout=20)
            self.imap.login(self.username, self.password)
            self.select_all_mail(self.imap, readonly=True)
            self.uidvalidity = self._number('UIDVALIDITY')
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise EmailSecondFactorError('email_2fa_mailbox_unavailable') from None

    def __exit__(self, *_):
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass

    def _number(self, field):
        _, values = self.imap.response(field)
        if not values or not values[0] or not values[0].isdigit():
            raise EmailSecondFactorError('email_2fa_mailbox_cursor_failed')
        return int(values[0])

    def checkpoint(self):
        self.select_all_mail(self.imap, readonly=True)
        if self._number('UIDVALIDITY') != self.uidvalidity:
            raise EmailSecondFactorError('email_2fa_mailbox_changed')
        return self._number('UIDNEXT')

    def poll(self, cursor, *, subdomain, not_before, now):
        # Gmail can keep the selected read-only mailbox view stale after mail
        # arrives. Refresh it before searching, but retain the pre-request UID
        # cursor: advancing UIDNEXT here would skip the code we are waiting for.
        self.select_all_mail(self.imap, readonly=True)
        if self._number('UIDVALIDITY') != self.uidvalidity:
            raise EmailSecondFactorError('email_2fa_mailbox_changed')
        status, data = self.imap.uid('search', None, 'UID', f'{cursor}:*', 'FROM', 'edupage.org')
        if status != 'OK':
            raise EmailSecondFactorError('email_2fa_mailbox_search_failed')
        ids = [int(uid) for uid in (data[0] or b'').split() if int(uid) >= cursor]
        if len(ids) > 50:
            raise EmailSecondFactorError('email_2fa_too_many_candidates')
        codes = set()
        for uid in ids:
            status, parts = self.imap.uid('fetch', str(uid), '(BODY.PEEK[])')
            if status != 'OK':
                raise EmailSecondFactorError('email_2fa_mailbox_read_failed')
            for part in parts:
                if isinstance(part, tuple) and isinstance(part[1], bytes):
                    code = extract_code(part[1], recipient=self.username, subdomain=subdomain,
                                        not_before=not_before, now=now)
                    if code:
                        codes.add(code)
        if len(codes) > 1:
            raise EmailSecondFactorError('email_2fa_ambiguous_codes')
        if ids and not codes and not self.saw_unmatched:
            emit('email_2fa_wait', 'mail_unmatched')
            self.saw_unmatched = True
        return next(iter(codes)) if codes else None


def complete_email_second_factor(challenge, *, username, password, subdomain,
                                 select_all_mail, inbox_factory=GmailCodeInbox,
                                 monotonic=time.monotonic, wall_time=time.time,
                                 sleep=time.sleep, budget=210):
    """One email request per school, bounded wait, one code submission."""
    deadline = monotonic() + budget
    def wait(seconds):
        if seconds < 0 or monotonic() + seconds >= deadline:
            raise EmailSecondFactorError('email_2fa_timeout')
        sleep(seconds)

    try:
        if not recipient_matches(challenge.email, username):
            raise EmailSecondFactorError('email_2fa_recipient_unverified')
        with inbox_factory(username, password, select_all_mail) as inbox:
            emit('email_2fa_request', 'start')
            for attempt in range(3):
                cursor = inbox.checkpoint()
                not_before = wall_time()
                if monotonic() >= deadline:
                    raise EmailSecondFactorError('email_2fa_timeout')
                try:
                    challenge.send_email_code()
                    break
                except RetryLaterException as error:
                    if attempt == 2:
                        raise EmailSecondFactorError('email_2fa_countdown_exhausted') from None
                    emit('email_2fa_request', 'waiting')
                    wait(max(1, int(error.retry_in_seconds)) + 1)
            if not recipient_matches(challenge.email, username):
                raise EmailSecondFactorError('email_2fa_recipient_unverified')
            emit('email_2fa_request', 'ok')
            emit('email_2fa_wait', 'start')
            while monotonic() < deadline:
                code = inbox.poll(cursor, subdomain=subdomain, not_before=not_before, now=wall_time())
                if code:
                    if monotonic() >= deadline:
                        raise EmailSecondFactorError('email_2fa_timeout')
                    emit('email_2fa_wait', 'ok')
                    emit('email_2fa_finish', 'start')
                    challenge.finish_with_code(code)
                    emit('email_2fa_finish', 'ok')
                    return
                wait(5)
            raise EmailSecondFactorError('email_2fa_timeout')
    except EmailSecondFactorError as error:
        emit('email_2fa_finish', str(error).removeprefix('email_2fa_'))
        emit('email_2fa_finish', 'error')
        raise
    except Exception:
        emit('email_2fa_finish', 'error')
        raise EmailSecondFactorError('email_2fa_failed') from None
