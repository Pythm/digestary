"""A minimal, real-crypto "software authenticator" — just enough to produce
attestation/assertion objects that `webauthn.verify_registration_response`
and `verify_authentication_response` will actually accept, so the passkey
tests exercise the real verification path instead of mocking it away.

Deliberately not a full authenticator: attestation format is always "none",
one algorithm (ES256/P-256), no extensions. That's all this app ever asks
an authenticator for (see auth.py's generate_registration_options call).
"""
from __future__ import annotations

import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url


class SoftAuthenticator:
    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(32)
        self.sign_count = 0

    def _cose_public_key(self) -> bytes:
        numbers = self.private_key.public_key().public_numbers()
        return cbor2.dumps({
            1: 2,                                    # kty: EC2
            3: -7,                                    # alg: ES256
            -1: 1,                                    # crv: P-256
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        })

    def create(self, challenge_b64url: str, rp_id: str, origin: str) -> dict:
        """Simulate navigator.credentials.create(); returns the same JSON
        shape the browser's PublicKeyCredential.toJSON() would produce."""
        aaguid = b"\x00" * 16
        attested_cred_data = (
            aaguid
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_public_key()
        )
        rp_id_hash = hashlib.sha256(rp_id.encode()).digest()
        flags = 0x41  # user present + attested credential data included
        auth_data = rp_id_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big") + attested_cred_data
        client_data = json.dumps({
            "type": "webauthn.create", "challenge": challenge_b64url, "origin": origin,
        }).encode()
        attestation_object = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation_object),
            },
        }

    def get(self, challenge_b64url: str, rp_id: str, origin: str) -> dict:
        """Simulate navigator.credentials.get()."""
        self.sign_count += 1
        rp_id_hash = hashlib.sha256(rp_id.encode()).digest()
        flags = 0x01  # user present
        auth_data = rp_id_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big")
        client_data = json.dumps({
            "type": "webauthn.get", "challenge": challenge_b64url, "origin": origin,
        }).encode()
        client_data_hash = hashlib.sha256(client_data).digest()
        signature = self.private_key.sign(auth_data + client_data_hash, ec.ECDSA(hashes.SHA256()))
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
        }
