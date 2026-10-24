# A clean Ubuntu with systemd as PID 1, for installing the meshforge-agent .deb
# in tests (V0.17 G1, Q16): tests/component/packaging and the CI package-linux
# job. Only what a stock Ubuntu desktop/server already has is added (systemd,
# sudo, dbus); no Python, no uv - the package must bring its own runtime.
#
#   docker build -f packaging/linux/testing/systemd.Dockerfile \
#       --build-arg UBUNTU=24.04 -t meshforge-systemd:24.04 packaging/linux/testing
#   docker run -d --privileged --cgroupns=private --tmpfs /run --tmpfs /run/lock \
#       meshforge-systemd:24.04
ARG UBUNTU=24.04
FROM ubuntu:${UBUNTU}
ENV container=docker
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      systemd systemd-sysv dbus sudo ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/systemd/system/*.wants/* \
      /lib/systemd/system/multi-user.target.wants/getty.target \
      /lib/systemd/system/sysinit.target.wants/systemd-firstboot.service \
 && systemctl mask systemd-logind.service getty.target console-getty.service \
      systemd-udevd.service systemd-udev-trigger.service \
 && if command -v python3 >/dev/null; then echo "python3 in the base image" >&2; exit 1; fi
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
