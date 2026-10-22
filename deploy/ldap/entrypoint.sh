#!/bin/sh
set -eu
umask 077
install -d -m 0755 /var/lib/ldap-fixture /run/ldap-fixture
install -d -m 0700 -o openldap -g openldap /var/lib/ldap-fixture/data
if [ ! -f /run/ldap-fixture/ca.pem ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj /CN=broker-fixture-ca \
        -addext basicConstraints=critical,CA:TRUE -keyout /var/lib/ldap-fixture/ca.key -out /run/ldap-fixture/ca.pem >/dev/null 2>&1
    openssl req -new -newkey rsa:2048 -nodes -subj /CN=ldap-fixture \
        -keyout /var/lib/ldap-fixture/server.key -out /var/lib/ldap-fixture/server.csr >/dev/null 2>&1
    openssl x509 -req -days 90 -in /var/lib/ldap-fixture/server.csr -CA /run/ldap-fixture/ca.pem \
        -CAkey /var/lib/ldap-fixture/ca.key -CAserial /var/lib/ldap-fixture/ca.srl -CAcreateserial \
        -extfile /opt/ldap-fixture/cert.extensions -out /var/lib/ldap-fixture/server.crt >/dev/null 2>&1
    openssl req -x509 -newkey rsa:2048 -nodes -days 90 -subj /CN=wrong-fixture-ca \
        -keyout /var/lib/ldap-fixture/wrong-ca.key -out /run/ldap-fixture/wrong-ca.pem >/dev/null 2>&1
fi
chmod 0644 /run/ldap-fixture/ca.pem /run/ldap-fixture/wrong-ca.pem
chown openldap:openldap /var/lib/ldap-fixture/server.key /var/lib/ldap-fixture/server.crt
python3 /opt/ldap-fixture/bootstrap.py
if [ ! -f /var/lib/ldap-fixture/data/data.mdb ]; then
    slapadd -f /opt/ldap-fixture/slapd.conf -l /var/lib/ldap-fixture/seed.generated.ldif
    chown -R openldap:openldap /var/lib/ldap-fixture/data
    rm /var/lib/ldap-fixture/seed.generated.ldif
fi
exec slapd -f /opt/ldap-fixture/slapd.conf -h 'ldap://0.0.0.0:1389/ ldaps://0.0.0.0:1636/' -u openldap -g openldap -d 0
