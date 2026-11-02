# Ported from Gadgetbridge (AGPL-3.0): HuaweiCrypto.java, CryptoUtils.java
import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

DIGEST_SECRET_V1 = bytes.fromhex("70FB6C24035FDB552F38898AEEDE3F69")


def random_bytes(n: int) -> bytes:
    return os.urandom(n)


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.digest(key, msg, "sha256")


def hkdf_sha256(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    return HKDF(hashes.SHA256(), length, salt, info).derive(ikm)


# AESGCM takes any 8..128 byte nonce: Band 11 transport uses 16-byte IVs, HiChain 12.
def aes_gcm_encrypt(key: bytes, iv: bytes, data: bytes, aad: bytes | None = None) -> bytes:
    return AESGCM(key).encrypt(iv, data, aad)  # ciphertext || 16-byte tag


def aes_gcm_decrypt(key: bytes, iv: bytes, data: bytes, aad: bytes | None = None) -> bytes:
    return AESGCM(key).decrypt(iv, data, aad)  # raises InvalidTag on tamper or wrong key


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(padder.update(data) + padder.finalize()) + enc.finalize()


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(dec.update(data) + dec.finalize()) + unpadder.finalize()


# shortcut: only authVersion 1 + encryptMethod 0 (CBC, DIGEST_SECRET_V1), which is what Band 11 uses;
# the other secrets and the GCM variant in HuaweiCrypto.decryptPinCode are not ported.
def decrypt_pin(ciphertext: bytes, iv: bytes) -> bytes:
    return aes_cbc_decrypt(DIGEST_SECRET_V1, iv, ciphertext)
