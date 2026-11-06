import os, ssl

# A frozen node (PyInstaller, built on Ubuntu in CI) carries its own OpenSSL, which looks for CA certificates where
# Ubuntu keeps them. On Arch/CachyOS (and others) that path doesn't exist, so every HTTPS call failed with
# CERTIFICATE_VERIFY_FAILED and the app's window stayed white (6 Nov 2026, laptop). Point it at the system bundle.
if not os.environ.get("SSL_CERT_FILE") and not os.path.exists(ssl.get_default_verify_paths().cafile or ""):
    for ca in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
               "/etc/ssl/ca-bundle.pem", "/etc/ssl/cert.pem", "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"):
        if os.path.exists(ca):
            os.environ["SSL_CERT_FILE"] = ca
            break

from .server import main

main()
