"""Public diagnostics contain only allowlisted phases and outcomes."""
from contextlib import contextmanager
from functools import wraps
import os
from pathlib import Path

PHASES = {'state_load', 'state_save', 'raw_send', 'session_restore', 'password_login'}
OUTCOMES = {'start', 'ok', 'session_rejected', 'challenge_required', 'authentication_failed', 'network_error', 'error'}

def emit(phase, outcome):
    if phase not in PHASES or outcome not in OUTCOMES:
        return
    path = os.getenv('EDUPAGE_DIAGNOSTIC_FILE')
    if path:
        try:
            with open(path, 'a', encoding='ascii') as f:
                f.write(phase + ':' + outcome + '\n')
        except OSError:
            pass

@contextmanager
def phase(name):
    emit(name, 'start')
    try:
        yield
    except Exception as error:
        from edupage_api.exceptions import BadCredentialsException
        from requests.exceptions import RequestException
        if isinstance(error, BadCredentialsException):
            outcome = 'session_rejected' if name == 'session_restore' else 'authentication_failed'
        elif isinstance(error, RequestException):
            outcome = 'network_error'
        else:
            outcome = 'error'
        emit(name, outcome)
        raise
    else:
        emit(name, 'ok')

def diagnosed(name):
    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with phase(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorator

def report(path):
    try:
        lines = Path(path).read_text(encoding='ascii').splitlines()[:100]
    except (OSError, UnicodeError):
        return
    for line in lines:
        parts = line.split(':')
        if len(parts) == 2 and parts[0] in PHASES and parts[1] in OUTCOMES:
            print('Diagnostic ' + line, flush=True)
