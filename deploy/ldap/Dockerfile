# Synthetic OpenLDAP fixture and isolated application acceptance client only.
FROM debian:bookworm-slim AS directory
ENV DEBIAN_FRONTEND=noninteractive
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 0755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install --yes --no-install-recommends slapd ldap-utils openssl ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/ldap/entrypoint.sh deploy/ldap/bootstrap.py deploy/ldap/tls.py deploy/ldap/slapd.conf deploy/ldap/seed.ldif deploy/ldap/samba-fixture.schema deploy/ldap/cert.extensions /opt/ldap-fixture/
ENTRYPOINT ["/bin/sh", "/opt/ldap-fixture/entrypoint.sh"]

FROM python:3.13-slim-bookworm AS acceptance
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md requirements.lock ./
COPY src/ ./src/
COPY tools/ldap_acceptance.py /opt/ldap_acceptance.py
RUN python -m pip install --no-cache-dir -c requirements.lock '.[control,ldap,local-auth]' \
    && useradd --uid 10001 --create-home broker
USER 10001:10001
ENTRYPOINT ["python", "/opt/ldap_acceptance.py"]
