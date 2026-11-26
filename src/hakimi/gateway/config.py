"""Read, write and initialise the local gateway's configuration and credentials."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hakimi import paths
from hakimi.gateway.protocol import PROVISION_NAME

KEY_NAME = "gateway.key"
CA_NAME = "gateway-cert.pem"
TLS_CERT_NAME = "gateway-tls-cert.pem"
TLS_KEY_NAME = "gateway-tls-key.pem"
CONFIG_NAME = "gateway.json"
UPSTREAM_KEY_NAME = "upstream.key"
# Internal tier names used by the Hakimi clients; each maps to a real upstream model.
INTERNAL_MODELS = ("gemini-4-argon", "gemini-4-carbon", "gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite")
TIER_LABELS = {
    "gemini-4-argon": "主回复模型",
    "gemini-4-carbon": "强推理模型（详细推导、考据、解题）",
    "gemini-4-hydrogen": "轻量模型（复盘、梳理、桌宠、视频选片）",
    "gemini-3.8-flash": "轻量模型（旧名称，同上）",
    "gemini-3.5-flash-lite": "视频观察模型（看视频、直播抽帧）",
}
DEFAULT_CONFIG = {
    "version": 1,
    "upstream": {
        "protocol": "gemini",       # "gemini" | "openai_compatible"
        "base_url": "https://generativelanguage.googleapis.com",
        "api_version": "v1beta",
        "auth": "x-goog-api-key",   # or "bearer" for Gemini-compatible proxies
        "proxy": "",                # e.g. http://127.0.0.1:7890 ; empty = system proxy settings
        "timeout_seconds": 240,
    },
    "models": {name: name for name in INTERNAL_MODELS},
    "prices": {},                   # upstream model -> {"input_usd_per_million": "0.3", "output_usd_per_million": "2.5"}
    "monthly_limit_usd": None,      # None = no cap (spend is still recorded)
    "max_concurrency": 4,
}


def directory() -> Path:
    return paths.gateway_dir()


def _private_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_config(base: Path | None = None) -> dict:
    base = base or directory()
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        value = json.loads((base / CONFIG_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    for key, item in value.items():
        if isinstance(item, dict) and isinstance(config.get(key), dict):
            config[key].update(item)
        else:
            config[key] = item
    return config


def save_config(config: dict, base: Path | None = None):
    base = base or directory()
    _private_write(base / CONFIG_NAME, (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode())


def save_upstream_key(value: str, base: Path | None = None):
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("upstream_key_invalid")
    _private_write((base or directory()) / UPSTREAM_KEY_NAME, value.encode())


def upstream_key(base: Path | None = None) -> str:
    try:
        return ((base or directory()) / UPSTREAM_KEY_NAME).read_text(encoding="ascii").strip()
    except OSError:
        return ""


# ------------------------------------------------------------------ init / certificates
def init(base: Path | None = None) -> dict:
    """Create the local key, CA and server certificate when missing.  Never overwrites."""
    base = base or directory()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    created = []
    if not (base / KEY_NAME).exists():
        _private_write(base / KEY_NAME, secrets.token_urlsafe(36).encode())
        created.append(KEY_NAME)
    if not (base / CA_NAME).exists() or not (base / TLS_CERT_NAME).exists() or not (base / TLS_KEY_NAME).exists():
        ca_pem, chain_pem, key_pem = _make_certificates()
        _private_write(base / CA_NAME, ca_pem)
        _private_write(base / TLS_CERT_NAME, chain_pem)
        _private_write(base / TLS_KEY_NAME, key_pem)
        created += [CA_NAME, TLS_CERT_NAME, TLS_KEY_NAME]
    if not (base / CONFIG_NAME).exists():
        save_config(load_config(base), base)
        created.append(CONFIG_NAME)
    from hakimi.gateway.protocol import PROJECT
    _private_write(base / PROVISION_NAME, json.dumps({"project": PROJECT, "kind": "hakimi-open-gateway"}).encode())
    return {"directory": str(base), "created": created}


def _make_certificates():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress

    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Hakimi local gateway CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False, data_encipherment=False,
                                       key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
            .issuer_name(ca_name).public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=True, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    ca_pem = ca.public_bytes(pem)
    chain = leaf.public_bytes(pem) + ca_pem
    key_pem = leaf_key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return ca_pem, chain, key_pem
