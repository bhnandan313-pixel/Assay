"""Ed25519 key management, canonical JSON serialization, and manifest signing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from assay.models.evidence import EvidenceManifest


def canonicalize_json(data: Any) -> bytes:
    """Serialize data into deterministic, sorted-key, minimal-whitespace UTF-8 JSON.

    Args:
        data: JSON-serializable Python structure (e.g. dict or list).

    Returns:
        Canonical UTF-8 encoded JSON bytes.
    """
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def generate_key_pair() -> Tuple[bytes, bytes]:
    """Generate a fresh Ed25519 private/public key-pair.

    Returns:
        Tuple of ``(private_key_pem, public_key_raw_bytes)``.
    """
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_pem, public_raw


class KeyStore:
    """Manages local Ed25519 private (PEM) and public (hex) key persistence."""

    def __init__(self, keys_dir: Path | str = Path("./.assay/keys")) -> None:
        """Initialize a KeyStore rooted at ``keys_dir``."""
        self.keys_dir = Path(keys_dir)
        self.private_key_path = self.keys_dir / "private_key.pem"
        self.public_key_path = self.keys_dir / "public_key.hex"
        self._legacy_public_pem_path = self.keys_dir / "public_key.pem"

    def keys_exist(self) -> bool:
        """Return ``True`` if both private and public keys exist on disk."""
        has_public = (
            self.public_key_path.exists() or self._legacy_public_pem_path.exists()
        )
        return self.private_key_path.exists() and has_public

    def save_keys(
        self,
        private_pem: bytes,
        public_raw: bytes,
        force: bool = False,
    ) -> Tuple[Path, Path]:
        """Persist Ed25519 keys to disk with hardened file permissions.

        Args:
            private_pem: PKCS8 PEM-encoded Ed25519 private key bytes.
            public_raw: Raw 32-byte Ed25519 public key bytes.
            force: Overwrite existing key files if present.

        Returns:
            Tuple of ``(private_key_path, public_key_path)``.
        """
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        if not force and self.keys_exist():
            pub_path = (
                self.public_key_path
                if self.public_key_path.exists()
                else self._legacy_public_pem_path
            )
            return self.private_key_path, pub_path

        self.private_key_path.write_bytes(private_pem)
        if os.name == "posix":
            os.chmod(self.private_key_path, 0o600)

        self.public_key_path.write_text(public_raw.hex(), encoding="utf-8")
        if os.name == "posix":
            os.chmod(self.public_key_path, 0o644)

        return self.private_key_path, self.public_key_path

    def ensure_keys(self, force: bool = False) -> Tuple[bool, Path, Path]:
        """Ensure an Ed25519 key-pair exists, generating one if missing.

        Args:
            force: Regenerate keys even if they already exist.

        Returns:
            Tuple of ``(created_new, private_key_path, public_key_path)``.
        """
        if not force and self.keys_exist():
            pub_path = (
                self.public_key_path
                if self.public_key_path.exists()
                else self._legacy_public_pem_path
            )
            return False, self.private_key_path, pub_path

        private_pem, public_raw = generate_key_pair()
        priv_path, pub_path = self.save_keys(private_pem, public_raw, force=True)
        return True, priv_path, pub_path

    def load_private_key(self) -> bytes:
        """Load PKCS8 PEM-encoded private key bytes from disk."""
        if not self.private_key_path.exists():
            raise FileNotFoundError(
                f"Private key not found at {self.private_key_path}. Run `assay init` first."
            )
        return self.private_key_path.read_bytes()

    def load_public_key_hex(self) -> str:
        """Load the hex-encoded Ed25519 public key from disk."""
        if self.public_key_path.exists():
            return self.public_key_path.read_text(encoding="utf-8").strip()

        if self._legacy_public_pem_path.exists():
            pub_key = serialization.load_pem_public_key(
                self._legacy_public_pem_path.read_bytes()
            )
            if not isinstance(pub_key, Ed25519PublicKey):
                raise ValueError("Legacy public key is not an Ed25519 key.")
            return pub_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            ).hex()

        raise FileNotFoundError(
            f"Public key not found in {self.keys_dir}. Run `assay init` first."
        )


def sign_manifest(private_key_pem: bytes, manifest: EvidenceManifest) -> str:
    """Canonically serialize and Ed25519-sign an ``EvidenceManifest``.

    Args:
        private_key_pem: PEM-encoded Ed25519 private key bytes.
        manifest: ``EvidenceManifest`` instance to sign.

    Returns:
        Hex-encoded 64-byte Ed25519 signature string.
    """
    loaded_key = serialization.load_pem_private_key(private_key_pem, password=None)
    if not isinstance(loaded_key, Ed25519PrivateKey):
        raise ValueError("Provided PEM key is not an Ed25519 private key.")

    canonical_bytes = canonicalize_json(manifest.model_dump(mode="json"))
    signature = loaded_key.sign(canonical_bytes)
    return signature.hex()


def verify_manifest(
    public_key_hex: str,
    signature_hex: str,
    manifest: EvidenceManifest,
) -> bool:
    """Verify an Ed25519 signature against a canonicalized ``EvidenceManifest``.

    Args:
        public_key_hex: Hex-encoded 32-byte Ed25519 public key.
        signature_hex: Hex-encoded 64-byte Ed25519 signature.
        manifest: ``EvidenceManifest`` to verify.

    Returns:
        ``True`` if the signature is valid; ``False`` if tampered or invalid.
    """
    try:
        public_bytes = bytes.fromhex(public_key_hex.strip())
        signature_bytes = bytes.fromhex(signature_hex.strip())
        public_key = Ed25519PublicKey.from_public_bytes(public_bytes)
        canonical_bytes = canonicalize_json(manifest.model_dump(mode="json"))
        public_key.verify(signature_bytes, canonical_bytes)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
