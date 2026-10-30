#!/usr/bin/env bash
set -euo pipefail
cd /workspace/RPi-Zero-EInk-E6-Controller
# Reuse the existing isolated checkout; do not create a Git worktree.
export DOCKER_CONFIG="${DOCKER_CONFIG:-/workspace/.cache/photopainter-docker}"
mkdir -p "$DOCKER_CONFIG"
python3 - <<'PY'
import os
import socket
import subprocess
from urllib.parse import urlsplit

args = ['docker', 'build']
proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
if proxy:
    host = urlsplit(proxy).hostname
    if host:
        args += ['--add-host', host + ':' + socket.gethostbyname(host)]
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'no_proxy'):
        if name in os.environ:
            args += ['--build-arg', name]
ca = os.environ.get('CODEX_PROXY_CERT')
if ca and os.path.isfile(ca):
    args += ['--secret', 'id=proxy_ca,src=' + ca]
subprocess.run(args + ['-t', 'photopainter:local', '.'], check=True)
subprocess.run(args + ['-f', 'Dockerfile.test', '-t', 'photopainter:test', '.'], check=True)
subprocess.run(['docker', 'run', '--rm', 'photopainter:test'], check=True)
PY
