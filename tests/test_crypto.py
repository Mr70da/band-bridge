import unittest

from cryptography.exceptions import InvalidTag

from band import crypto

ZERO_KEY = bytes(16)
ZERO_IV = bytes(16)


class HashTest(unittest.TestCase):
    def test_sha256_abc(self):
        self.assertEqual(
            crypto.sha256(b"abc").hex(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_hmac_sha256_rfc4231_case2(self):
        self.assertEqual(
            crypto.hmac_sha256(b"Jefe", b"what do ya want for nothing?").hex(),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
        )

    def test_hkdf_rfc5869_case1(self):
        okm = crypto.hkdf_sha256(bytes([0x0B] * 22), bytes(range(13)), bytes(range(0xF0, 0xFA)), 42)
        self.assertEqual(
            okm.hex(),
            "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865",
        )

    def test_hkdf_default_length_is_32(self):
        self.assertEqual(len(crypto.hkdf_sha256(b"ikm", b"salt", b"info")), 32)

    def test_random_bytes(self):  # TestHuaweiCrypto.testGenerateNonce, minus its flaky distribution check
        a, b = crypto.random_bytes(16), crypto.random_bytes(16)
        self.assertEqual(len(a), 16)
        self.assertNotEqual(a, b)


class GcmTest(unittest.TestCase):
    def test_nist_test_case_3(self):  # AES-128, 12-byte IV, no AAD
        key = bytes.fromhex("feffe9928665731c6d6a8f9467308308")
        iv = bytes.fromhex("cafebabefacedbaddecaf888")
        plain = bytes.fromhex(
            "d9313225f88406e5a55909c5aff5269a86a7a9531534f7da2e4c303d8a318a72"
            "1c3c0c95956809532fcf0e2449a6b525b16aedf5aa0de657ba637b391aafd255"
        )
        sealed = crypto.aes_gcm_encrypt(key, iv, plain)
        self.assertEqual(
            sealed.hex(),
            "42831ec2217774244b7221b784d0d49ce3aa212f2c02a4e035c17e2329aca12e"
            "21d514b25466931c7d8f6a5aac84aa051ba30b396a0aac973d58e091473f5985"
            "4d5c2af327cd64a62cf35abd2ba6fab4",
        )
        self.assertEqual(crypto.aes_gcm_decrypt(key, iv, sealed), plain)

    def test_round_trip_12_and_16_byte_iv(self):
        key = crypto.random_bytes(32)
        for iv_len in (12, 16):
            iv = crypto.random_bytes(iv_len)
            sealed = crypto.aes_gcm_encrypt(key, iv, b"hello band")
            self.assertEqual(len(sealed), len(b"hello band") + 16)
            self.assertEqual(crypto.aes_gcm_decrypt(key, iv, sealed), b"hello band")

    def test_round_trip_with_aad(self):
        key, iv = crypto.random_bytes(32), crypto.random_bytes(12)
        sealed = crypto.aes_gcm_encrypt(key, iv, b"challenge", b"hichain_iso_exchange")
        self.assertEqual(crypto.aes_gcm_decrypt(key, iv, sealed, b"hichain_iso_exchange"), b"challenge")
        with self.assertRaises(InvalidTag):
            crypto.aes_gcm_decrypt(key, iv, sealed, b"other aad")

    def test_tamper_raises(self):
        key, iv = crypto.random_bytes(32), crypto.random_bytes(16)
        sealed = bytearray(crypto.aes_gcm_encrypt(key, iv, b"hello band"))
        for i in (0, len(sealed) - 1):  # a ciphertext byte and a tag byte
            bad = bytearray(sealed)
            bad[i] ^= 1
            with self.assertRaises(InvalidTag):
                crypto.aes_gcm_decrypt(key, iv, bytes(bad))


class CbcTest(unittest.TestCase):
    def test_gadgetbridge_vector(self):  # TestHuaweiPacket.testEncryptedUnslicedSerialize, zero key and IV
        plain = bytes.fromhex("030405060708")
        sealed = crypto.aes_cbc_encrypt(ZERO_KEY, ZERO_IV, plain)
        self.assertEqual(sealed.hex(), "3b89fc79d9055eed5235fe16a08a4d53")
        self.assertEqual(crypto.aes_cbc_decrypt(ZERO_KEY, ZERO_IV, sealed), plain)

    def test_round_trip_pads_to_block(self):
        key, iv = crypto.random_bytes(16), crypto.random_bytes(16)
        for n in (0, 1, 15, 16, 17, 100):
            sealed = crypto.aes_cbc_encrypt(key, iv, bytes(n))
            self.assertEqual(len(sealed), (n // 16 + 1) * 16)
            self.assertEqual(crypto.aes_cbc_decrypt(key, iv, sealed), bytes(n))

    def test_wrong_key_fails_padding_check(self):
        sealed = crypto.aes_cbc_encrypt(ZERO_KEY, ZERO_IV, b"x" * 20)
        with self.assertRaises(ValueError):
            crypto.aes_cbc_decrypt(bytes([1]) * 16, ZERO_IV, sealed)


class PinTest(unittest.TestCase):
    def test_decrypt_pin_uses_digest_secret(self):
        iv = crypto.random_bytes(16)
        sealed = crypto.aes_cbc_encrypt(crypto.DIGEST_SECRET_V1, iv, b"\x12\x34" * 32)
        self.assertEqual(crypto.decrypt_pin(sealed, iv), b"\x12\x34" * 32)


if __name__ == "__main__":
    unittest.main()
