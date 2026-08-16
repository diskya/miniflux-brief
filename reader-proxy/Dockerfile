FROM alpine:3.22

RUN apk add --no-cache ca-certificates squid \
    && install -d -o squid -g squid /var/cache/squid /var/log/squid /run/squid

COPY squid.conf /etc/squid/squid.conf

USER squid
EXPOSE 3128
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s \
    CMD squid -k parse -f /etc/squid/squid.conf
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
