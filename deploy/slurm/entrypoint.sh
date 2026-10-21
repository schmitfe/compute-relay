#!/bin/bash
set -Eeuo pipefail
umask 077

children=()
shutdown() {
    trap - EXIT
    for child in "${children[@]}"; do
        kill -TERM "$child" 2>/dev/null || true
    done
    wait || true
}
trap shutdown EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

start() {
    "$@" &
    children+=("$!")
}

install -d -o munge -g munge -m 0755 /run/munge /var/log/munge
install -d -o mysql -g mysql -m 0755 /run/mysqld
install -d -o slurm -g slurm -m 0755 /run/slurm /var/lib/slurm/slurmctld
install -d -o root -g root -m 0755 /var/lib/slurm/slurmd /run/sshd
install -d -o brokerjob -g brokerjob -m 0700 /home/brokerjob/.ssh /home/brokerjob/jobs
install -d -o 10001 -g 10001 -m 0700 /run/fixture-ssh

# Keys are generated on first start, never baked into images or checked in.
if [ ! -s /etc/munge/munge.key ]; then
    dd if=/dev/urandom of=/etc/munge/munge.key bs=1024 count=1 status=none
fi
chown munge:munge /etc/munge/munge.key
chmod 0400 /etc/munge/munge.key
if [ ! -s /run/fixture-ssh/id_ed25519 ]; then
    ssh-keygen -q -t ed25519 -N '' -f /run/fixture-ssh/id_ed25519
fi
chown -R 10001:10001 /run/fixture-ssh
chmod 0400 /run/fixture-ssh/id_ed25519
if [ ! -s /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
fi
read -r host_type host_data _ < /etc/ssh/ssh_host_ed25519_key.pub
printf 'slurm-fixture %s %s\n' "$host_type" "$host_data" > /run/fixture-ssh/known_hosts
chown 10001:10001 /run/fixture-ssh/known_hosts
chmod 0444 /run/fixture-ssh/known_hosts
printf 'restrict ' > /home/brokerjob/.ssh/authorized_keys
cat /run/fixture-ssh/id_ed25519.pub >> /home/brokerjob/.ssh/authorized_keys
chown brokerjob:brokerjob /home/brokerjob/.ssh/authorized_keys
chmod 0600 /home/brokerjob/.ssh/authorized_keys

if [ ! -d /var/lib/mysql/mysql ]; then
    mariadb-install-db --user=mysql --datadir=/var/lib/mysql \
        --auth-root-authentication-method=normal --skip-test-db > /dev/null
fi
start mariadbd --user=mysql --datadir=/var/lib/mysql \
    --socket=/run/mysqld/mysqld.sock --skip-networking \
    --innodb-buffer-pool-size=32M --innodb-log-file-size=16M
database_ready=0
for attempt in {1..90}; do
    if mariadb-admin --protocol=socket ping --silent > /dev/null 2>&1; then
        database_ready=1
        break
    fi
    sleep 1
done
if [ "$database_ready" != 1 ]; then
    echo 'Fixture MariaDB failed to become ready' >&2
    exit 1
fi
database_password=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
mariadb --protocol=socket <<SQL
CREATE DATABASE IF NOT EXISTS slurm_acct;
CREATE USER IF NOT EXISTS 'slurm'@'localhost' IDENTIFIED BY '$database_password';
ALTER USER 'slurm'@'localhost' IDENTIFIED BY '$database_password';
GRANT ALL PRIVILEGES ON slurm_acct.* TO 'slurm'@'localhost';
SQL
cat > /etc/slurm/slurmdbd.conf <<CONFIG
AuthType=auth/munge
DbdHost=slurm-fixture
DbdPort=6819
SlurmUser=slurm
PidFile=/run/slurm/slurmdbd.pid
StorageType=accounting_storage/mysql
StorageHost=localhost
StorageLoc=slurm_acct
StorageUser=slurm
StoragePass=$database_password
CONFIG
unset database_password
chown slurm:slurm /etc/slurm/slurmdbd.conf
chmod 0600 /etc/slurm/slurmdbd.conf

start runuser -u munge -- munged --foreground
munge_ready=0
for attempt in {1..30}; do
    if munge -n > /dev/null 2>&1; then
        munge_ready=1
        break
    fi
    sleep 1
done
if [ "$munge_ready" != 1 ]; then
    echo 'Fixture Munge failed to become ready' >&2
    exit 1
fi
start slurmdbd -D
accounting_ready=0
for attempt in {1..90}; do
    if sacctmgr --noheader --parsable2 show cluster > /dev/null 2>&1; then
        accounting_ready=1
        break
    fi
    sleep 1
done
if [ "$accounting_ready" != 1 ]; then
    echo 'Fixture SlurmDBD failed to become ready' >&2
    exit 1
fi
sacctmgr --immediate add cluster broker-test
sacctmgr --immediate add account broker Cluster=broker-test Description=Fixture Organization=Fixture
sacctmgr --immediate add user brokerjob Cluster=broker-test Account=broker DefaultAccount=broker
start slurmctld -D
start slurmd -D
/usr/sbin/sshd -t
start /usr/sbin/sshd -D -e
touch /run/slurm-fixture.ready
echo 'Disposable Slurm fixture initialized; awaiting node registration.'
dpkg-query --show --showformat='${binary:Package} ${Version}\n' slurm-wlm slurmdbd munge mariadb-server openssh-server

# Unexpected termination of any critical daemon makes this fixture fail visibly.
set +e
wait -n "${children[@]}"
status=$?
set -e
echo "A fixture daemon exited (status $status); shutting down." >&2
exit 1
