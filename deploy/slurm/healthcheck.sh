#!/bin/sh
set -eu
test -f /run/slurm-fixture.ready
scontrol ping | grep -q 'UP'
sinfo --noheader --Node --format='%T' | grep -Eq '^(idle|mixed|allocated|completing)'
sacctmgr --noheader --parsable2 show cluster | grep -q '^broker-test|'
test -s /run/fixture-ssh/known_hosts
test -s /run/fixture-ssh/id_ed25519
