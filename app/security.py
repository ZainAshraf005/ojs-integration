"""Small crypto helpers: token encryption, password hashing, webhook signatures."""

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    key = hashlib.sha256(get_settings().app_secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """Signature the OJS plugin sends: hex HMAC-SHA256 of "<timestamp>.<raw body>"."""
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    return hmac.compare_digest(sign_webhook(secret, timestamp, body), signature)
