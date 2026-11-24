# Security policy

DocuShelf stores sensitive personal documents. Security reports are very welcome.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Report them privately through
GitHub's security advisories instead:

1. Open the repository on GitHub.
2. Go to **Security → Advisories → Report a vulnerability**.
3. Describe the problem, the affected version or commit, and how to reproduce it.

You will get an answer as soon as possible. Once a fix is available, the advisory is published
together with a release, and you are credited if you wish.

## Supported versions

Only the latest release and the `main` branch receive security fixes.

## Scope

Especially interesting: authentication and session handling (OIDC, local emergency account,
API tokens), isolation between households, path handling in the archive and the drop folders,
file parsing (PDF, images, e-mail), and the SMTP/IMAP/SMB/FTP intake channels.
