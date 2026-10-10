from app.auth import is_local_ip


def test_is_local_ip():
    assert is_local_ip("127.0.0.1")
    assert is_local_ip("::1")
    assert is_local_ip("10.0.0.5")
    assert is_local_ip("192.168.1.50")
    assert is_local_ip("172.16.0.1")
    assert is_local_ip("169.254.1.1")  # link-local
    assert is_local_ip("fc00::1")      # IPv6 unique-local


def test_is_not_local_ip():
    assert not is_local_ip("8.8.8.8")
    assert not is_local_ip("1.1.1.1")
    assert not is_local_ip(None)
    assert not is_local_ip("")
    assert not is_local_ip("not-an-ip")
