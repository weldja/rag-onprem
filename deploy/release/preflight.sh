#!/usr/bin/env bash
# Check this server can run rag-onprem. Safe to run any time; changes nothing.
# Exit 0 = ready (warnings allowed), 1 = a hard requirement is missing.
# Also prints GPU=1 or GPU=0 on its last line for install.sh.
source "$(dirname "$0")/lib.sh"

problems=0
info "Checking requirements"

# --- Docker -----------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '(daemon not reachable)')"
    if ! docker info >/dev/null 2>&1; then
        fail "Docker daemon not reachable (is it running, and is this user allowed to use it?)"
        problems=$((problems + 1))
    fi
else
    fail "docker not installed"
    problems=$((problems + 1))
fi
if docker compose version >/dev/null 2>&1; then
    ok "docker compose $(docker compose version --short 2>/dev/null)"
else
    fail "Docker Compose v2 plugin not found ('docker compose')"
    problems=$((problems + 1))
fi
for tool in sha256sum gzip openssl; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool"
    elif [ "$tool" = openssl ]; then
        warn "openssl not found: you must supply certs/tls.crt and tls.key yourself"
    else
        fail "$tool not found"
        problems=$((problems + 1))
    fi
done

# --- CPU and memory ---------------------------------------------------------
cores="$(nproc 2>/dev/null || echo 0)"
if [ "$cores" -ge 8 ]; then ok "$cores CPU cores"
elif [ "$cores" -ge 4 ]; then warn "$cores CPU cores: answers will be slow without a GPU (8+ recommended)"
else fail "$cores CPU cores: at least 4 required"; problems=$((problems + 1)); fi

if grep -q -m1 -w avx2 /proc/cpuinfo 2>/dev/null; then
    ok "CPU supports AVX2"
else
    warn "AVX2 not detected: CPU inference will be much slower"
fi

mem_gb="$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)"
if [ "$mem_gb" -ge 16 ]; then ok "${mem_gb} GB RAM"
elif [ "$mem_gb" -ge 12 ]; then warn "${mem_gb} GB RAM: enough for the default 7B model, with little headroom (16+ recommended)"
else fail "${mem_gb} GB RAM: at least 12 GB required for the default 7B model"; problems=$((problems + 1)); fi

# --- Disk -------------------------------------------------------------------
root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
# The Docker root may not be visible from the host (Docker Desktop keeps it in
# a VM); fall back to this folder's filesystem.
free_gb="$( (df -Pk "$root" 2>/dev/null || true) | awk 'NR==2 {printf "%d", $4/1024/1024}')"
[ -n "$free_gb" ] || free_gb="$( (df -Pk . 2>/dev/null || true) | awk 'NR==2 {printf "%d", $4/1024/1024}')"
free_gb="${free_gb:-0}"
if [ "$free_gb" -ge 40 ]; then ok "${free_gb} GB free for Docker"
elif [ "$free_gb" -ge 25 ]; then warn "${free_gb} GB free for Docker: enough to install, little room for documents (40+ recommended)"
else fail "${free_gb} GB free for Docker: at least 25 GB required"; problems=$((problems + 1)); fi

# --- Ports ------------------------------------------------------------------
http_port=80 https_port=443
if [ -f .env ]; then
    http_port="$(grep -E '^HTTP_PORT=' .env | cut -d= -f2 || true)"; http_port="${http_port:-80}"
    https_port="$(grep -E '^HTTPS_PORT=' .env | cut -d= -f2 || true)"; https_port="${https_port:-443}"
fi
running="$(docker ps --filter label=com.docker.compose.project=rag-onprem -q 2>/dev/null || true)"
for port in "$http_port" "$https_port"; do
    if [ -n "$running" ]; then
        ok "port $port (rag-onprem already running)"
    elif command -v ss >/dev/null 2>&1 && ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
        fail "port $port is already in use (set HTTP_PORT/HTTPS_PORT in .env)"
        problems=$((problems + 1))
    else
        ok "port $port"
    fi
done

# --- GPU --------------------------------------------------------------------
# Answers within 10 seconds need the whole model in GPU memory: an NVIDIA GPU
# with 8 GB+. Anything less still works, just slowly, so these are warnings.
fast_gpu="an NVIDIA GPU with 8 GB+ is needed for answers within 10 seconds"
gpu=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    if docker info 2>/dev/null | grep -qi 'nvidia'; then
        ok "NVIDIA GPU with container runtime: $(nvidia-smi -L | head -1)"
        gpu=1
        vram_mb="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -dc 0-9 || true)"
        # 7500, not 8192: 8 GB cards report slightly less than 8192 MiB.
        if [ -n "$vram_mb" ] && [ "$vram_mb" -lt 7500 ]; then
            warn "GPU has only $((vram_mb / 1024)) GB of memory: part of the model will run on the CPU, so answers take 30 seconds or more ($fast_gpu)"
        fi
    else
        warn "NVIDIA GPU found but the NVIDIA Container Toolkit isn't configured for Docker; using CPU, so answers take 20-60 seconds or more ($fast_gpu)"
    fi
else
    warn "no NVIDIA GPU: running on CPU, so answers take 20-60 seconds or more ($fast_gpu)"
fi

echo
if [ "$problems" -gt 0 ]; then
    printf '%s\n' "${RED}${problems} requirement(s) not met.${RESET}"
else
    printf '%s\n' "${GREEN}Ready.${RESET}"
fi
echo "GPU=$gpu"
[ "$problems" -eq 0 ]
