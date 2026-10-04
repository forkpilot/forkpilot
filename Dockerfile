# On-premises image: ForkPilot plus everything an ArduPilot SITL build needs.
# The image holds no customer code. Mount your fork (read-write: ForkPilot checks commits
# out in it) and a work directory:
#
#   docker build -t forkpilot .
#   docker run --rm --user "$(id -u):$(id -g)" -v /path/to/fork-clone:/src -v /path/to/work:/work \
#       forkpilot doctor --repo /src --build
#   docker run --rm --user "$(id -u):$(id -g)" -v ... forkpilot investigate --repo /src --good <sha> --bad <sha>
#
# Prerequisites follow ArduPilot's Tools/environment_install/install-prereqs-ubuntu.sh (noble branch),
# minus the parts a headless SITL build does not need: ARM cross toolchain, MAVProxy GUI libraries,
# xterm, SFML. Not tested with a real `docker build` yet.
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
# `pip install forkpilot[autotest]` pulls MAVProxy and its heavy dependencies (opencv, vtk,
# matplotlib); they are only needed for --suite autotest. Build with --build-arg EXTRAS= to leave them out.
ARG EXTRAS=autotest
# internal mirrors, if the build host has no direct access: --build-arg PIP_INDEX_URL=https://...
ARG PIP_INDEX_URL=

# BASE_PKGS and SITL_PKGS of install-prereqs-ubuntu.sh: compiler, ccache, python3-dev, libxml2/libxslt
# (lxml, pymavlink's generator). iproute2 + util-linux: `unshare -rn` network namespaces for autotest.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates ccache g++ gawk git iproute2 libtool libxml2-dev libxslt1-dev \
        make procps python3 python3-dev python3-venv rsync util-linux wget \
    && rm -rf /var/lib/apt/lists/*

# same pins as the script's PYTHON_PKGS: empy==3.3.4 (empy 4 breaks waf), pexpect, pymavlink, lxml
COPY . /opt/forkpilot
RUN python3 -m venv /opt/venv \
    && PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.org/simple}" /opt/venv/bin/pip install --no-cache-dir \
        -e "/opt/forkpilot$( [ -n "$EXTRAS" ] && echo "[$EXTRAS]" )" \
    && /opt/venv/bin/python -c "import em, pexpect, pymavlink, yaml"

# the container runs as the host user (--user), who owns the mounted clone but is not root:
# git must trust the mount, and HOME must exist and be writable
RUN git config --system --add safe.directory '*'
ENV HOME=/tmp \
    PATH=/opt/venv/bin:$PATH \
    FP_HOME=/work \
    CCACHE_DIR=/work/ccache \
    CCACHE_MAXSIZE=10G

WORKDIR /work
ENTRYPOINT ["forkpilot"]
CMD ["--help"]
