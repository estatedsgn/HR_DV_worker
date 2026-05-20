from scripts.send_test_message import build_peer_from_resolve_username


def test_build_peer_from_resolve_username_with_peer_and_users() -> None:
    payload = {
        "peer": {"_": "peerUser", "userId": 123},
        "users": [{"id": 123, "accessHash": "hash123"}],
    }
    peer = build_peer_from_resolve_username(payload)
    assert peer == {"_": "inputPeerUser", "userId": 123, "accessHash": "hash123"}


def test_build_peer_from_resolve_username_users_fallback() -> None:
    payload = {"users": [{"id": 456, "accessHash": "hash456"}]}
    peer = build_peer_from_resolve_username(payload)
    assert peer["userId"] == 456
    assert peer["accessHash"] == "hash456"
