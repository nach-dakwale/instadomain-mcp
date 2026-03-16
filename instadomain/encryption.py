from cryptography.fernet import Fernet, InvalidToken


def encrypt(plaintext: str, key: str) -> str:
    """Encrypt plaintext using Fernet symmetric encryption.

    Args:
        plaintext: The string to encrypt.
        key: A URL-safe base64-encoded 32-byte Fernet key.

    Returns:
        The encrypted ciphertext as a string.
    """
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, key: str) -> str:
    """Decrypt ciphertext using Fernet symmetric encryption.

    Args:
        ciphertext: The encrypted string to decrypt.
        key: The same Fernet key used for encryption.

    Returns:
        The original plaintext string.

    Raises:
        cryptography.fernet.InvalidToken: If the key is wrong or data is corrupted.
    """
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(ciphertext.encode()).decode()
