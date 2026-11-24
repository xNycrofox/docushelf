# Small FTP server for scan-to-FTP: vsftpd on Alpine, configured from the
# environment when the container starts (see entrypoint.sh).
#
#   docker build -t docushelf-ftp deploy/ftp
#   docker run --rm -e FTP_PASSWORD=geheim123 docushelf-ftp --print-config

FROM alpine:3

RUN apk add --no-cache vsftpd tini \
 && mkdir -p /srv/ftp /etc/vsftpd /var/lib/vsftpd/empty \
 && chmod 0555 /var/lib/vsftpd/empty \
 && (grep -qx /sbin/nologin /etc/shells || echo /sbin/nologin >> /etc/shells)

COPY entrypoint.sh /usr/local/bin/docushelf-ftp
RUN chmod 0755 /usr/local/bin/docushelf-ftp

EXPOSE 21 30000-30009

# Checks the listening socket instead of connecting, so the log stays free of
# health check sessions.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD netstat -ltn 2>/dev/null | grep -q ':21 ' || exit 1

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/docushelf-ftp"]
