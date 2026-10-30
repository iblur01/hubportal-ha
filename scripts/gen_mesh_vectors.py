"""Génère tests/fixtures/mesh_vectors.json : vecteurs dorés du format de fil (contrat v2).

Implémentation de référence volontairement indépendante de custom_components (hashlib, hmac, json seulement),
pour que le même fichier puisse être consommé par le dépôt Qt.

Usage : python scripts/gen_mesh_vectors.py [--check]
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

OUTPUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "mesh_vectors.json"

KEY_HEX = bytes(range(32)).hex()
FP_A = hashlib.sha256(b"certificat A").hexdigest()
FP_B = hashlib.sha256(b"certificat B").hexdigest()
NONCE_1 = bytes(range(16)).hex()
NONCE_2 = bytes(range(16, 32)).hex()


def json_compact(value) -> str:
    # Clés triées, séparateurs compacts, UTF-8 brut (pas d'échappement \uXXXX).
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def message_mac(key: bytes, t, sender, seq, ts, epoch, p) -> str:
    inner = sha256_hex(json_compact(p).encode("utf-8"))
    body = "|".join([t, sender, str(seq), str(ts), str(epoch), inner])
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def hello_auth(key: bytes, nonce_peer, portal_id, fp_local, fp_peer) -> str:
    body = "|".join(["hello", nonce_peer, portal_id, fp_local, fp_peer])
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def sas(fp_req: str, fp_adm: str, n_req: str, n_adm: str) -> str:
    data = (
        b"hubportal-sas" + bytes.fromhex(fp_req) + bytes.fromhex(fp_adm) + bytes.fromhex(n_req) + bytes.fromhex(n_adm)
    )
    return f"{int.from_bytes(hashlib.sha256(data).digest(), 'big') % 1_000_000:06d}"


def build() -> dict:
    key = bytes.fromhex(KEY_HEX)
    compact_inputs = [
        {},
        {"b": 1, "a": 2},
        {"name": "Cuisine é 日本 😀", "n": 0, "neg": -7, "big": 9007199254740993},
        {"z": {"y": [3, 2, 1], "x": None}, "flag": True, "off": False},
        {"target": "*", "cmd": "command_screen_on", "data": None},
    ]
    mac_cases = [
        {
            "t": "heartbeat",
            "from": "portal-a",
            "seq": 1793395200000,
            "ts": 1793395200,
            "epoch": 4,
            "p": {"leader": "portal-a", "index": 12},
        },
        {
            "t": "command",
            "from": "ha-1",
            "seq": 5,
            "ts": 1793395201,
            "epoch": 4,
            "p": {"target": "portal-a", "cmd": "command_screen_brightness_level", "data": 128},
        },
        {
            "t": "join_accept",
            "from": "portal-a",
            "seq": 9,
            "ts": 1793395202,
            "epoch": 1,
            "p": {
                "network_id": "net-é",
                "key": KEY_HEX,
                "epoch": 1,
                "members": [{"portal_id": "portal-a", "fp": FP_A, "name": "Salon 日本", "ha": False}],
            },
        },
    ]
    for case in mac_cases:
        case["json_compact"] = json_compact(case["p"])
        case["p_sha256"] = sha256_hex(case["json_compact"].encode("utf-8"))
        case["mac"] = message_mac(key, case["t"], case["from"], case["seq"], case["ts"], case["epoch"], case["p"])
    return {
        "notes": (
            "Généré par scripts/gen_mesh_vectors.py. json_compact : sort_keys, séparateurs (',', ':'), UTF-8 brut. "
            "Les hash et HMAC portent sur les octets UTF-8. La clé réseau est décodée de l'hexadécimal. "
            "Les nonces et empreintes sont décodés de l'hexadécimal avant d'être hachés (SAS, commit)."
        ),
        "key_hex": KEY_HEX,
        "json_compact": [{"input": v, "expected": json_compact(v)} for v in compact_inputs],
        "mac": mac_cases,
        "hello_auth": [
            {
                "nonce_peer": NONCE_1,
                "portal_id": "portal-a",
                "fp_local": FP_A,
                "fp_peer": FP_B,
                "expected": hello_auth(key, NONCE_1, "portal-a", FP_A, FP_B),
            },
            {
                "nonce_peer": NONCE_2,
                "portal_id": "ha-1",
                "fp_local": FP_B,
                "fp_peer": FP_A,
                "expected": hello_auth(key, NONCE_2, "ha-1", FP_B, FP_A),
            },
        ],
        "sas": [
            {
                "fp_requester": FP_B,
                "fp_admitter": FP_A,
                "nonce_requester": NONCE_1,
                "nonce_admitter": NONCE_2,
                "expected": sas(FP_B, FP_A, NONCE_1, NONCE_2),
            },
            {
                "fp_requester": FP_A,
                "fp_admitter": FP_B,
                "nonce_requester": NONCE_2,
                "nonce_admitter": NONCE_1,
                "expected": sas(FP_A, FP_B, NONCE_2, NONCE_1),
            },
        ],
        "commit": [
            {"nonce": NONCE_1, "expected": sha256_hex(bytes.fromhex(NONCE_1))},
            {"nonce": NONCE_2, "expected": sha256_hex(bytes.fromhex(NONCE_2))},
        ],
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(0 if OUTPUT.read_text(encoding="utf-8") == render() else 1)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"écrit : {OUTPUT}")
