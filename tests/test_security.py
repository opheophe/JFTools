from app.config import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    stored = hash_password("s3cret")
    assert stored.startswith("scrypt$")
    assert verify_password("s3cret", stored)
    assert not verify_password("wrong", stored)


def test_verify_rejects_malformed():
    assert not verify_password("x", "not-a-valid-hash")
    assert not verify_password("x", "")
    assert not verify_password("x", "md5$salt$deadbeef")
