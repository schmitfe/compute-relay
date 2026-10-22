#!/bin/sh
set -eu
umask 077
install -d -m 0755 /var/lib/ldap-fixture /run/ldap-fixture
install -d -m 0700 -o openldap -g openldap /var/lib/ldap-fixture/data
python3 /opt/ldap-fixture/tls.py
chmod 0644 /run/ldap-fixture/ca.pem /run/ldap-fixture/wrong-ca.pem
chown openldap:openldap /var/lib/ldap-fixture/server.key /var/lib/ldap-fixture/server.crt
python3 /opt/ldap-fixture/bootstrap.py
if [ ! -f /var/lib/ldap-fixture/data/data.mdb ]; then
    slapadd -f /opt/ldap-fixture/slapd.conf -l /var/lib/ldap-fixture/seed.generated.ldif
    chown -R openldap:openldap /var/lib/ldap-fixture/data
    rm /var/lib/ldap-fixture/seed.generated.ldif
fi
exec slapd -f /opt/ldap-fixture/slapd.conf -h 'ldap://0.0.0.0:1389/ ldaps://0.0.0.0:1636/' -u openldap -g openldap -d 0
