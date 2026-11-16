"""Optional TOTP (RFC 6238) second factor with single-use recovery codes."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from sqlalchemy import select

from config import settings
from database import models as m, repo

STEP = 30
DIGITS = 6
RECOVERY_CODES = 8


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _counter(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** DIGITS
    return str(value).zfill(DIGITS)


def code_at(secret, moment=None):
    return _counter(secret, int((moment if moment is not None else time.time()) // STEP))


def matching_step(secret, code, moment=None, window=1):
    """Return the time step matching the code, tolerating small clock drift."""
    if not isinstance(code, str) or not code.isdigit() or len(code) != DIGITS:
        return None
    current = int((moment if moment is not None else time.time()) // STEP)
    for step in range(current - window, current + window + 1):
        if hmac.compare_digest(_counter(secret, step), code):
            return step
    return None


def provisioning_uri(login, secret):
    issuer = settings.web_2fa_issuer
    return f"otpauth://totp/{quote(issuer)}:{quote(login)}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"


def _cipher():
    from services import provider_credentials as credentials
    try:
        return credentials._cipher()
    except RuntimeError:
        with repo.session_scope() as session:
            if session.execute(select(m.ProviderCredential.id).limit(1)).first() is not None:
                raise
        return credentials._cipher(create=True)


def seal(secret):
    return _cipher().encrypt(secret.encode()).decode()


def unseal(value):
    from cryptography.fernet import InvalidToken
    try:
        return _cipher().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        raise RuntimeError("Secret d’authentification illisible : vérifier la clé maîtresse privée.") from None


def _hash_code(code):
    return hashlib.sha256(code.encode()).hexdigest()


def make_recovery_codes():
    codes = [secrets.token_hex(5) for _ in range(RECOVERY_CODES)]
    return codes, json.dumps([_hash_code(code) for code in codes])


def start_setup(user_id):
    """Stage a new secret; it is only enforced after the first valid code."""
    with repo.session_scope() as session:
        account = session.scalar(select(m.WebAccount).where(m.WebAccount.user_id == user_id))
        if not account:
            raise ValueError("L’authentification à deux facteurs nécessite un compte avec mot de passe.")
        if account.totp_enabled:
            raise ValueError("L’authentification à deux facteurs est déjà active.")
        secret = new_secret()
        account.totp_secret = seal(secret)
        return {"secret": secret, "uri": provisioning_uri(account.login, secret)}


def confirm_setup(user_id, code):
    with repo.session_scope() as session:
        account = session.scalar(select(m.WebAccount).where(m.WebAccount.user_id == user_id))
        if not account or account.totp_enabled or not account.totp_secret:
            raise ValueError("Démarrez d’abord la configuration.")
        step = matching_step(unseal(account.totp_secret), code)
        if step is None:
            raise ValueError("Code incorrect. Vérifiez l’heure de votre téléphone.")
        codes, hashed = make_recovery_codes()
        account.totp_enabled = True
        account.totp_last_step = step
        account.recovery_hashes = hashed
        return {"recovery_codes": codes}


def check_login(account, code):
    """Validate a TOTP or recovery code. A TOTP step can only be used once."""
    if not account.totp_enabled:
        return True
    if not isinstance(code, str) or not code.strip():
        return False
    code = code.strip().replace(" ", "").replace("-", "").lower()
    step = matching_step(unseal(account.totp_secret), code)
    if step is not None:
        if account.totp_last_step is not None and step <= account.totp_last_step:
            return False
        account.totp_last_step = step
        return True
    hashed = json.loads(account.recovery_hashes or "[]")
    digest = _hash_code(code)
    for stored in hashed:
        if hmac.compare_digest(stored, digest):
            hashed.remove(stored)
            account.recovery_hashes = json.dumps(hashed)
            return True
    return False


def disable(user_id, code):
    with repo.session_scope() as session:
        account = session.scalar(select(m.WebAccount).where(m.WebAccount.user_id == user_id))
        if not account or not account.totp_enabled:
            raise ValueError("L’authentification à deux facteurs n’est pas active.")
        if not check_login(account, code):
            raise ValueError("Code incorrect.")
        account.totp_enabled = False
        account.totp_secret = None
        account.recovery_hashes = None
        account.totp_last_step = None
