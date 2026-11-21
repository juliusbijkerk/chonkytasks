#!/usr/bin/env python3
"""Reuse a local signing certificate so macOS permissions survive rebuilds.

The private key is imported into the login Keychain with access limited to
codesign. Only its public fingerprint is retained alongside local app settings.
This is a development identity, not a Developer ID distribution certificate.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile


NAME = 'chonkytasks Local Development'
FOLDER = Path.home() / 'Library/Application Support/chonkytasks/signing'
PIN = FOLDER / 'identity.sha1'


def run(*args):
    return subprocess.check_output(args, stderr=subprocess.PIPE)


def identity():
    keychain = run('/usr/bin/security', 'default-keychain').decode().strip().strip('"')
    if PIN.exists():
        fingerprint = PIN.read_text().strip()
        identities = run('/usr/bin/security', 'find-identity', '-p', 'codesigning', keychain).decode()
        if len(fingerprint) != 40 or fingerprint not in identities:
            raise SystemExit('The saved chonkytasks signing identity is missing from Keychain. Restore it before rebuilding; a replacement would reset app permissions.')
        print(fingerprint)
        return

    # Reuse an existing named certificate after an interrupted initial setup.
    existing = subprocess.run(['/usr/bin/security', 'find-certificate', '-c', NAME, '-p', keychain], capture_output=True)
    with tempfile.TemporaryDirectory(prefix='chonkytasks-signing-') as directory:
        folder = Path(directory)
        certificate = folder / 'certificate.pem'
        if existing.returncode == 0:
            certificate.write_bytes(existing.stdout)
        else:
            config = folder / 'certificate.conf'
            config.write_text(f'''[req]
distinguished_name = subject
x509_extensions = extensions
prompt = no
[subject]
CN = {NAME}
[extensions]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
subjectKeyIdentifier = hash
''')
            key = folder / 'private-key.pem'
            package = folder / 'identity.p12'
            run('/usr/bin/openssl', 'req', '-new', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes',
                '-days', '3650', '-config', str(config), '-keyout', str(key), '-out', str(certificate))
            password = secrets.token_urlsafe(32)
            subprocess.run(['/usr/bin/openssl', 'pkcs12', '-export', '-inkey', str(key), '-in', str(certificate),
                '-out', str(package), '-name', NAME, '-passout', 'stdin',
                '-keypbe', 'PBE-SHA1-3DES', '-certpbe', 'PBE-SHA1-3DES', '-macalg', 'sha1'],
                input=(password + '\n').encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            importer = folder / 'import-identity'
            # This project's Python can run under Rosetta. Compile natively,
            # using the same SDK and deployment target as the application.
            apple_silicon = subprocess.run(['/usr/sbin/sysctl', '-n', 'hw.optional.arm64'], capture_output=True).stdout.strip() == b'1'
            cpu = 'arm64' if apple_silicon else 'x86_64'
            sdk = run('/usr/bin/xcrun', '--show-sdk-path').decode().strip()
            run('/usr/bin/arch', '-' + cpu, '/usr/bin/swiftc', '-suppress-warnings', '-sdk', sdk,
                '-target', cpu + '-apple-macosx13.0',
                str(Path(__file__).with_name('import_signing_identity.swift')), '-o', str(importer))
            subprocess.run([str(importer)], input=json.dumps({'package': str(package), 'password': password,
                'keychain': keychain}).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        der = run('/usr/bin/openssl', 'x509', '-in', str(certificate), '-outform', 'DER')
        fingerprint = hashlib.sha1(der).hexdigest().upper()
        identities = run('/usr/bin/security', 'find-identity', '-p', 'codesigning', keychain).decode()
        if fingerprint not in identities:
            raise SystemExit('The chonkytasks certificate exists but its private signing key is missing. Restore the key in Keychain before rebuilding.')
        PIN.write_text(fingerprint + '\n')
        print(fingerprint)


def main():
    os.umask(0o077)
    FOLDER.mkdir(parents=True, exist_ok=True)
    with (FOLDER / 'setup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        identity()


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr.decode(errors='replace').strip() or 'Local signing setup failed.') from None
