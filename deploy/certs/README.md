# TLS certificate

nginx serves HTTPS with:

- `tls.crt`: the certificate, followed by any intermediate certificates (PEM)
- `tls.key`: the private key (PEM, unencrypted)

Put the customer's certificate here before starting the stack. For development,
`bash scripts/make-dev-cert.sh` creates a self-signed pair. Browsers will warn about it.

Certificate and key files are git-ignored. Never commit them.
