# Disposable CPU-only fixture, not a production cluster image.
# Debian packages provide all scheduler/SSH/database code; no third-party cluster image.
FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive SLURM_CONF=/etc/slurm/slurm.conf
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 0755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install --yes --no-install-recommends \
       slurm-wlm slurmdbd slurm-wlm-mysql-plugin munge \
       mariadb-server openssh-server python3 procps util-linux ca-certificates libpmix2 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/munge/munge.key /etc/ssh/ssh_host_* \
    && useradd --create-home --shell /bin/bash --uid 20001 brokerjob \
    && passwd --delete brokerjob > /dev/null \
    && install -d -o brokerjob -g brokerjob -m 0700 /home/brokerjob/jobs \
    && install -d /opt/fixture /run/fixture-ssh
COPY deploy/slurm/slurm.conf /etc/slurm/slurm.conf
COPY deploy/slurm/cgroup.conf /etc/slurm/cgroup.conf
COPY deploy/slurm/sshd_config /etc/ssh/sshd_config
COPY deploy/slurm/entrypoint.sh deploy/slurm/healthcheck.sh /opt/fixture/
RUN chmod 0755 /opt/fixture/*.sh
HEALTHCHECK --interval=3s --timeout=5s --start-period=60s --retries=20 \
    CMD ["/opt/fixture/healthcheck.sh"]
ENTRYPOINT ["/opt/fixture/entrypoint.sh"]
