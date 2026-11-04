"""The rules are trusted only when signed by the key SYSAI gave at connection (Ed25519 over canonical JSON — the
same bytes the server signs)."""
import base64
import json


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify(signed, key_b64: str, key_id: str = "") -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not isinstance(signed, dict) or signed.get("alg") != "Ed25519" or not isinstance(signed.get("doc"), dict):
        return False
    if key_id and signed.get("key_id") != key_id:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64)).verify(base64.b64decode(signed["sig"]),
                                                                            canonical(signed["doc"]))
        return True
    except Exception:  # noqa: BLE001  any damage or another key: not trusted
        return False
