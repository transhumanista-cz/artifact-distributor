# syntax=docker/dockerfile:1
# Copy engine: regctl (static Go binary) from the official scratch image.
FROM ghcr.io/regclient/regctl:v0.11.6 AS regctl

FROM python:3.13-alpine
RUN pip install --no-cache-dir "pyyaml==6.0.2" \
 && adduser -D -u 10001 distributor \
 && mkdir -p /data && chown distributor /data
COPY --from=regctl /regctl /usr/local/bin/regctl
WORKDIR /app
COPY distributor ./distributor
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
ENTRYPOINT ["python", "-m", "distributor"]
CMD ["serve"]
