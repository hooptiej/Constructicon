"""Keys and certificates (issue #443): .crt .cer .pem .der .csr .key .p12 .pfx.

Restricted type (restricted=True): the owner's private reference. Files
are stored whole (owner's call, 2026-09-30: the archive lives on a home NAS
inside the network), shown on projects they're attached to (e.g. a repo's
deploy key) and in the admin pane's "Keys & certificates" list, but kept
out of general browsing and NEVER exported to the public site. Real access
control is #467 (authentication). The object page never renders key
material; it shows facts only.

Detection is by content, not extension (a .crt can hold a key, a .pem can
hold a whole chain plus its key): every PEM block in the file is parsed, and
a DER file is tried as a certificate, CSR, private key, then public key.
A .p12/.pfx is a sealed container: without its password all that's knowable
is "protected PKCS#12", which is assumed to hold a private key.

Facts are computed once at upload (embedded_metadata_fn, the STL #449
pattern) with the `cryptography` package. Expiry is judged at view time
from the stored dates, so "expired" stays true as time passes. A
certificate's not-before date seeds the row's content_date.
"""

import datetime
import re
from pathlib import Path

from .. import datefmt, storage
from . import _preview, register, ObjectTypeSpec, ThumbnailSource

STATS_KEY = "certkey_stats"
MAX_BYTES = 1024 * 1024  # key/cert files are tiny; never read more than this
MAX_SANS = 20

EXTENSIONS = frozenset({".crt", ".cer", ".pem", ".der", ".csr", ".key", ".p12", ".pfx"})
_PEM_BLOCK = re.compile(rb"-----BEGIN ([A-Z0-9 ]+)-----(.*?)-----END \1-----", re.S)
_CERT_LABELS = {b"CERTIFICATE", b"X509 CERTIFICATE", b"TRUSTED CERTIFICATE"}
_CSR_LABELS = {b"CERTIFICATE REQUEST", b"NEW CERTIFICATE REQUEST"}
_PUBLIC_LABELS = {b"PUBLIC KEY", b"RSA PUBLIC KEY"}


def _read(path):
    with open(path, "rb") as f:
        return f.read(MAX_BYTES)


# ---------------------------------------------------------------- facts

def _key_desc(key):
    """'RSA 2048' / 'EC secp256r1' / 'Ed25519' for a public or private key."""
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed448, ed25519, rsa
    if isinstance(key, (rsa.RSAPublicKey, rsa.RSAPrivateKey)):
        return f"RSA {key.key_size}"
    if isinstance(key, (ec.EllipticCurvePublicKey, ec.EllipticCurvePrivateKey)):
        return f"EC {key.curve.name}"
    if isinstance(key, (ed25519.Ed25519PublicKey, ed25519.Ed25519PrivateKey)):
        return "Ed25519"
    if isinstance(key, (ed448.Ed448PublicKey, ed448.Ed448PrivateKey)):
        return "Ed448"
    if isinstance(key, (dsa.DSAPublicKey, dsa.DSAPrivateKey)):
        return f"DSA {key.key_size}"
    return type(key).__name__


def _pub_fingerprint(public_key):
    """SHA-256 over the SubjectPublicKeyInfo DER: the same for a key, its
    certificate and its CSR, so it's what pairs them up."""
    from cryptography.hazmat.primitives import hashes, serialization
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    digest = hashes.Hash(hashes.SHA256())
    digest.update(der)
    return digest.finalize().hex()


def _sans(obj):
    from cryptography import x509
    try:
        ext = obj.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:  # silent-ok: no SAN extension = no names
        return []
    names = [str(v) for v in ext.get_values_for_type(x509.DNSName)]
    names += [str(v) for v in ext.get_values_for_type(x509.IPAddress)]
    names += [str(v) for v in ext.get_values_for_type(x509.RFC822Name)]
    return names[:MAX_SANS]


def _cert_facts(cert):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    try:
        is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:  # silent-ok: no BasicConstraints = not a CA
        is_ca = False
    pub = cert.public_key()
    return {
        "kind": "certificate",
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "not_before": cert.not_valid_before_utc.timestamp(),
        "not_after": cert.not_valid_after_utc.timestamp(),
        "sans": _sans(cert),
        "serial": format(cert.serial_number, "x"),
        "fingerprint": cert.fingerprint(hashes.SHA256()).hex(),
        "key": _key_desc(pub),
        "pub_fp": _pub_fingerprint(pub),
        "self_signed": cert.subject == cert.issuer,
        "ca": bool(is_ca),
    }


def _csr_facts(csr):
    pub = csr.public_key()
    return {"kind": "csr", "subject": csr.subject.rfc4514_string(), "sans": _sans(csr),
            "key": _key_desc(pub), "pub_fp": _pub_fingerprint(pub)}


def _private_facts(key, encrypted, fmt):
    facts = {"kind": "private_key", "encrypted": encrypted, "format": fmt}
    if key is not None:
        facts["key"] = _key_desc(key)
        facts["pub_fp"] = _pub_fingerprint(key.public_key())
    return facts


def _load_private(data, der=False):
    """(key or None, encrypted?) for a PEM/DER private key. An encrypted key
    can't be opened without its passphrase; that's reported, not an error."""
    from cryptography.hazmat.primitives import serialization
    loader = serialization.load_der_private_key if der else serialization.load_pem_private_key
    try:
        return loader(data, password=None), False
    except TypeError:  # silent-ok: "Password was not given but private key is encrypted"; reported as encrypted
        return None, True


def _parse_pem_block(label, block):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    if label in _CERT_LABELS:
        return _cert_facts(x509.load_pem_x509_certificate(block))
    if label in _CSR_LABELS:
        return _csr_facts(x509.load_pem_x509_csr(block))
    if label in _PUBLIC_LABELS:
        pub = serialization.load_pem_public_key(block)
        return {"kind": "public_key", "key": _key_desc(pub), "pub_fp": _pub_fingerprint(pub)}
    if label == b"OPENSSH PRIVATE KEY":
        try:
            return _private_facts(serialization.load_ssh_private_key(block, password=None), False, "OpenSSH")
        except TypeError:  # silent-ok: an encrypted OpenSSH key; reported as encrypted
            return _private_facts(None, True, "OpenSSH")
    if label.endswith(b"PRIVATE KEY"):
        fmt = {b"ENCRYPTED PRIVATE KEY": "PKCS#8", b"PRIVATE KEY": "PKCS#8"}.get(label, "PEM (traditional)")
        key, encrypted = _load_private(block)  # legacy "Proc-Type: 4,ENCRYPTED" keys raise TypeError too
        return _private_facts(key, encrypted, fmt)
    if label == b"X509 CRL":
        crl = x509.load_pem_x509_crl(block)
        return {"kind": "crl", "issuer": crl.issuer.rfc4514_string(), "revoked": len(list(crl))}
    return {"kind": "other", "label": label.decode("ascii", errors="replace")}


def _parse_der(data):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    for attempt in (
        lambda: _cert_facts(x509.load_der_x509_certificate(data)),
        lambda: _csr_facts(x509.load_der_x509_csr(data)),
    ):
        try:
            return [attempt()]
        except ValueError:  # silent-ok: trying each DER format in turn
            pass
    try:
        key, encrypted = _load_private(data, der=True)
        return [_private_facts(key, encrypted, "DER")]
    except ValueError:  # silent-ok: trying each DER format in turn
        pass
    try:
        pub = serialization.load_der_public_key(data)
        return [{"kind": "public_key", "key": _key_desc(pub), "pub_fp": _pub_fingerprint(pub)}]
    except ValueError:  # silent-ok: not any known DER format = no facts
        return []


def _parse_pkcs12(data):
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, None)
    except (ValueError, TypeError):
        # Password-protected (the normal case): sealed without the password.
        return [{"kind": "pkcs12", "protected": True}]
    items = [{"kind": "pkcs12", "protected": False}]
    if key is not None:
        items.append(_private_facts(key, False, "PKCS#12"))
    for c in [cert, *(extra or [])]:
        if c is not None:
            items.append(_cert_facts(c))
    return items


def _parse(path):
    data = _read(path)
    ext = Path(path).suffix.lower()
    if ext in (".p12", ".pfx"):
        return _parse_pkcs12(data)
    items = []
    for m in _PEM_BLOCK.finditer(data):
        label = m.group(1)
        try:
            items.append(_parse_pem_block(label, m.group(0)))
        except Exception as e:
            items.append({"kind": "unreadable", "label": label.decode("ascii", errors="replace"), "error": str(e)[:120]})
    return items or _parse_der(data)


def _summary(items):
    counts = {}
    for it in items:
        k = it["kind"]
        if k == "private_key":
            k = "private key (encrypted)" if it.get("encrypted") else "private key (unencrypted)"
        elif k == "pkcs12":
            k = "PKCS#12 container (password-protected)" if it.get("protected") else "PKCS#12 container"
        elif k == "csr":
            k = "certificate request"
        elif k == "public_key":
            k = "public key"
        elif k == "crl":
            k = "revocation list"
        counts[k] = counts.get(k, 0) + 1
    def plural(label, n):
        if n == 1:
            return label
        base, paren, rest = label.partition(" (")  # "private key (x)" -> "private keys (x)"
        return base + "s" + paren + rest

    return ", ".join(f"{n} {plural(k, n)}" for k, n in counts.items())


# ---------------------------------------------------------------- hooks

def sniff(path, filename):
    """sniff_fn: content, not extension: a PEM block, parseable DER, or a
    DER SEQUENCE for a .p12/.pfx (sealed containers can't be parsed further
    without their password)."""
    data = _read(path)
    if Path(filename).suffix.lower() in (".p12", ".pfx"):
        return data[:1] == b"\x30"
    if _PEM_BLOCK.search(data):
        return True
    return data[:1] == b"\x30" and bool(_parse_der(data))


def get_embedded_metadata(path):
    """embedded_metadata_fn: parse every block once at upload."""
    try:
        items = _parse(Path(path))
    except Exception as e:
        print(f"Cert/key parse failed for {path}: {e!r}")
        return {}
    if not items:
        return {}
    has_key = any(i["kind"] == "private_key" for i in items) or any(
        i["kind"] == "pkcs12" and i.get("protected") for i in items)
    stats = {"items": items, "summary": _summary(items), "has_private_key": has_key}
    out = {"type_metadata": {STATS_KEY: stats}}
    first_cert = next((i for i in items if i["kind"] == "certificate"), None)
    if first_cert:
        out["content_date"] = first_cert["not_before"]
    return out


def _date(ts):
    return datefmt.iso_day(ts)


def _validity(cert):
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    span = f"{_date(cert['not_before'])} → {_date(cert['not_after'])}"
    if cert["not_after"] < now:
        return f"{span} (EXPIRED)"
    if cert["not_before"] > now:
        return f"{span} (not yet valid)"
    return f"{span} ({int((cert['not_after'] - now) // 86400)} days left)"


def get_properties(row):
    """properties_fn: summary first (the admin list shows it), then the
    leaf certificate, then keys (paired to certs by public-key fingerprint),
    CSRs, and the rest of any chain. {} on failure."""
    try:
        stats = (row.get("type_metadata") or {}).get(STATS_KEY)
        if not stats and row.get("stored_filename"):
            path = storage.path_for(row["stored_filename"])
            if path.exists():
                stats = (get_embedded_metadata(path).get("type_metadata") or {}).get(STATS_KEY)
        if not stats:
            return {}
        items = stats["items"]
        props = {"Contents": stats["summary"]}
        certs = [i for i in items if i["kind"] == "certificate"]
        cert_by_fp = {c["pub_fp"]: c for c in certs}
        if certs:
            leaf = certs[0]
            props["Subject"] = leaf["subject"]
            props["Issuer"] = "self-signed" if leaf["self_signed"] else leaf["issuer"]
            props["Valid"] = _validity(leaf)
            if leaf["sans"]:
                props["Names (SAN)"] = ", ".join(leaf["sans"])
            props["Certificate key"] = leaf["key"] + (" (CA)" if leaf["ca"] else "")
            props["SHA-256 fingerprint"] = leaf["fingerprint"]
            if len(certs) > 1:
                props["Chain"] = " ← ".join(c["subject"] for c in certs[1:])
        for n, key in enumerate(i for i in items if i["kind"] == "private_key"):
            desc = [key.get("key") or "unknown algorithm", key["format"],
                    "passphrase-encrypted" if key["encrypted"] else "UNENCRYPTED"]
            match = cert_by_fp.get(key.get("pub_fp"))
            if match:
                desc.append(f"matches {match['subject']}")
            props["Private key" + (f" {n + 1}" if n else "")] = ", ".join(desc)
        for csr in (i for i in items if i["kind"] == "csr"):
            props["Request for"] = csr["subject"] + (f" ({', '.join(csr['sans'])})" if csr["sans"] else "")
            props["Request key"] = csr["key"]
        for pub in (i for i in items if i["kind"] == "public_key"):
            props["Public key"] = f"{pub['key']}, SHA-256 {pub['pub_fp'][:16]}…"
        if any(i["kind"] == "pkcs12" and i.get("protected") for i in items):
            props["Note"] = "Sealed with a password: contents (usually a private key + certificate) not readable without it"
        bad = [i for i in items if i["kind"] == "unreadable"]
        if bad:
            props["Unreadable blocks"] = ", ".join(i["label"] for i in bad)
        return props
    except Exception as e:
        print(f"Cert/key properties failed for {row.get('slug')}: {e!r}")
        return {}


def preview(ctx):
    """preview_fn: never render key or certificate material; file icon only
    (and restricted items are never exported, so export mode never runs)."""
    return _preview.file_icon(ctx)


register(ObjectTypeSpec(
    key="certkey",
    label="Key / certificate",
    thumbnail_source=ThumbnailSource.NONE,
    ocr_capable=False,
    caption_capable=False,
    extensions=EXTENSIONS,
    sniff_fn=sniff,
    properties_fn=get_properties,
    embedded_metadata_fn=get_embedded_metadata,  # parsed once, at upload
    preview_fn=preview,
    restricted=True,  # #443: private reference; out of browsing, never exported
    badge_icon="\U0001F511",  # key
    badge_text="KEY",
))
