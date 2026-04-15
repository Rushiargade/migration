# vmigrate Deployment Requirements & Dependencies

Complete checklist of all dependencies needed to deploy vmigrate across different environments.

---

## 1. Core System Requirements

### Minimum Hardware
- **Orchestration Host (Docker/Kubernetes):**
  - CPU: 4 cores (8+ recommended)
  - RAM: 8GB (16GB+ for production)
  - Disk: 100GB (SSD preferred)
  - Network: 1Gbps (10Gbps for large migrations)

- **Conversion Host (Rocky Linux):**
  - CPU: 8+ cores (parallel disk conversions)
  - RAM: 16GB+ (disk buffering)
  - Disk: 500GB+ (NVMe SSD for temp storage)
  - Network: 10Gbps ideal (1Gbps minimum)

- **Proxmox Target:**
  - Existing cluster or standalone node
  - Network accessible from conversion host
  - Sufficient storage for VM imports

---

## 2. Container & Orchestration

### Docker Desktop / Docker Engine
**Version:** 20.10+ (latest recommended)

**Installation:**
- Windows: https://docs.docker.com/desktop/install/windows-install/
- macOS: https://docs.docker.com/desktop/install/mac-install/
- Linux: `curl -fsSL https://get.docker.com | sh`

**Verify Installation:**
```bash
docker --version
docker-compose --version
```

### Docker Compose
**Version:** 2.0+ (included with Docker Desktop)

**Standalone Installation (Linux):**
```bash
sudo curl -L "https://github.com/docker/compose/releases/download/v2.20.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

### Kubernetes (Optional - for production scale)
**Version:** 1.25+

**Options:**
- **Minikube** (Development): `choco install minikube` (Windows)
- **Kind** (Docker-based): `curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.20.0/kind-windows-amd64`
- **kubeadm** (On-premises): Manual cluster setup
- **Managed K8s:** AWS EKS, Azure AKS, Google GKE

**Required Tools:**
- `kubectl` (1.25+): https://kubernetes.io/docs/tasks/tools/
- `helm` (3.10+): https://helm.sh/docs/intro/install/

---

## 3. Programming Languages & Runtimes

### Python
**Version:** 3.10+ (3.12 recommended for Docker)

**Installation:**
- Windows: `choco install python` or download from python.org
- macOS: `brew install python@3.12`
- Linux: `sudo apt-get install python3.12 python3.12-venv`

**Verify:**
```bash
python --version
```

### Node.js / npm (Optional - for web UI customization)
**Version:** 18.0+

**Installation:**
- Windows: `choco install nodejs`
- macOS: `brew install node`
- Linux: `curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash - && sudo apt-get install -y nodejs`

**Verify:**
```bash
node --version
npm --version
```

---

## 4. Python Dependencies (pyproject.toml)

All automatically installed via Docker, but useful for local development:

```bash
pip install -r requirements.txt

# Or with web extras:
pip install -e ".[web]"
```

**Core Dependencies:**
```
pyVmomi>=8.0.1          # VMware vSphere connectivity
proxmoxer>=2.0.1        # Proxmox API client
paramiko>=3.4.0         # SSH (conversion host access)
click>=8.1.7            # CLI framework
pyyaml>=6.0.1           # Config file parsing
requests>=2.31.0        # HTTP library
```

**Web Extras:**
```
fastapi>=0.111.0        # Web framework
uvicorn>=0.29.0         # ASGI server
pydantic>=2.9.0         # Data validation
```

**Optional:**
```
rich>=13.7.0            # Terminal formatting
pytest>=7.0             # Testing framework
```

---

## 5. System Tools (Linux - Conversion Host)

### Rocky Linux / RHEL / CentOS
Install on conversion host (10.5.5.113):

```bash
# Update system
sudo dnf update -y

# Core tools
sudo dnf install -y \
  qemu-img \
  libvirt-client \
  virt-v2v \
  virt-manager \
  openssh-server \
  openssh-clients \
  git \
  curl \
  wget \
  tar \
  gzip \
  rsync

# Python (if using virt-v2v Python bindings)
sudo dnf install -y python3 python3-pip

# VirtIO drivers for Windows VMs
sudo mkdir -p /opt/virtio-win
# Download from: https://fedorapeople.org/groups/virt/virtio-win/
# wget https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest/virtio-win.iso
# sudo mv virtio-win.iso /opt/virtio-win/
```

**Verify Tools Installed:**
```bash
qemu-img --version
virt-v2v --version
ssh -V
```

### Ubuntu/Debian
```bash
sudo apt-get update
sudo apt-get install -y \
  qemu-utils \
  libvirt-clients \
  virt-v2v \
  openssh-server \
  openssh-client \
  git \
  curl \
  wget
```

---

## 6. Docker Images

### Base Image (Already in Dockerfile)
- **python:3.12-slim** (~200MB) - Minimal Python runtime

### Docker Hub Images (Pre-built)
```bash
# Pull if needed
docker pull python:3.12-slim
docker pull ubuntu:22.04
docker pull alpine:latest
```

### Build Locally
```bash
cd /path/to/migration
docker-compose build --no-cache
```

---

## 7. Storage & Data Management

### SQLite (Embedded)
- **Latest:** 3.40+
- No separate installation needed (included in Python)
- Verify: `python -c "import sqlite3; print(sqlite3.version)"`

### Volume Storage
- **Local:** Docker volumes (managed by Docker)
- **Production:** NFS, Ceph, or cloud storage
- **Required Space:**
  - State DB: ~100MB
  - Disk artifacts: 500GB+ (depends on VM sizes)
  - Logs: ~10GB per 1000 VMs

---

## 8. Network & Connectivity

### Required Ports (Firewall Rules)

| Component | Port | Protocol | Purpose |
|-----------|------|----------|---------|
| Web UI | 8080 | TCP | FastAPI server |
| vCenter | 443 | TCP | VMware API |
| Proxmox | 8006 | TCP | Proxmox API |
| SSH | 22 | TCP | Conversion host access |
| NFS (optional) | 2049 | TCP/UDP | Network storage |
| Ceph (optional) | 6789-6790 | TCP | Ceph cluster |

### DNS & Network Requirements
- Conversion host (10.5.5.113) accessible from Docker/K8s host
- vCenter accessible from conversion host
- Proxmox accessible from conversion host
- Internet access (for downloading RockyOS, VirtIO drivers, etc.)

---

## 9. Development & Testing Tools

### Git
**Version:** 2.40+

**Installation:**
- Windows: `choco install git`
- macOS: `brew install git`
- Linux: `sudo apt-get install git`

**Configure:**
```bash
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
```

### Code Editor / IDE
- **VS Code** (recommended): https://code.visualstudio.com/
- **PyCharm**: https://www.jetbrains.com/pycharm/
- **Vim**: Pre-installed on Linux

### Container Development
```bash
# For container debugging
docker run -it --rm migration-vmigrate-web:latest /bin/bash

# For viewing volumes
docker volume inspect migration_vmigrate-data
```

---

## 10. Kubernetes Deployment (Production)

### Helm Chart Dependencies
```bash
# Helm 3.10+
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo add prometheus https://prometheus-community.github.io/helm-charts
helm repo update
```

### Required K8s Resources
```bash
# Install metrics-server (for HPA)
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml

# Install ingress controller
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.8.0/deploy/static/provider/cloud/deploy.yaml

# Storage provisioner (if needed)
kubectl apply -f https://github.com/kubernetes-sigs/nfs-subdir-external-provisioner/deploy.yaml
```

### K8s Namespaces & RBAC
```bash
kubectl create namespace vmigrate
kubectl create serviceaccount vmigrate-app -n vmigrate
```

---

## 11. CI/CD Pipelines

### GitHub Actions (Already in CI-DOCKER.md)
- **No installation needed** - provided in `.github/workflows/`
- Automatically uses Docker Hub or container registry

### GitLab CI
- **GitLab Server:** 13.0+
- **GitLab Runner:** Install on runner machine
- **Docker Registry:** Integrated or external

### Jenkins (Optional)
```bash
# Docker container
docker run -d -p 8081:8080 jenkins/jenkins:latest

# System installation
choco install jenkins  # Windows
```

---

## 12. Monitoring & Logging

### Prometheus (Optional)
```bash
docker run -d -p 9090:9090 prom/prometheus:latest
```

### Grafana (Optional)
```bash
docker run -d -p 3000:3000 grafana/grafana:latest
```

### ELK Stack (Optional)
```bash
# Elasticsearch
docker run -d -p 9200:9200 docker.elastic.co/elasticsearch/elasticsearch:8.0.0

# Kibana
docker run -d -p 5601:5601 docker.elastic.co/kibana/kibana:8.0.0
```

---

## 13. Offline Installation Checklist

If you have **limited internet access**, download and prepare these **BEFORE disconnecting**:

### Essential Downloads (Must Have)

1. **Docker Engine** (~400MB)
   - Docker Desktop installer for Windows/macOS
   - Docker CE for Linux

2. **Python Packages** (~500MB)
   ```bash
   pip download -r requirements.txt -d ./offline-packages/
   ```

3. **Base Docker Image** (~200MB)
   ```bash
   docker pull python:3.12-slim
   docker save python:3.12-slim -o python-3.12-slim.tar
   ```

4. **Rocky Linux ISO** (~2.4GB)
   - Download from: https://rockylinux.org/download/
   - For conversion host setup

5. **Node.js** (if customizing web UI) (~200MB)
   - Download from: https://nodejs.org/

6. **VirtIO ISO** (~600MB)
   - Download from: https://fedorapeople.org/groups/virt/virtio-win/
   - For Windows VM conversion

### Optional Downloads (Should Have)

7. **Kubernetes Tools**
   - `kubectl` binary
   - `helm` binary
   - Kind or Minikube image

8. **Documentation & References**
   - vmigrate README & guides
   - Docker documentation
   - Proxmox API docs
   - VMware pyVmomi docs

### Total Download Size: ~4-5GB

---

## 14. Environment Variables Required

Create `.env` file (never commit to git):

```bash
# VMware
export VSPHERE_PASSWORD="your_vcenter_password"
export VSPHERE_HOST="vcenter.example.com"

# Proxmox
export PROXMOX_PASSWORD="your_proxmox_password"
export PROXMOX_HOST="proxmox.example.com"

# Docker Registry (optional, for CI/CD)
export DOCKER_REGISTRY_URL="docker.io"
export DOCKER_USERNAME="your_docker_user"
export DOCKER_PASSWORD="your_docker_token"

# Kubernetes (optional)
export KUBECONFIG="/path/to/kubeconfig.yaml"
```

---

## 15. Quick Setup Script (One-time)

```bash
#!/bin/bash

# Clone repo
git clone https://github.com/Rushiargade/migration.git
cd migration

# Install Python dependencies locally
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

# Build Docker image
docker-compose build

# Start services
docker-compose up -d vmigrate-web

# Test connectivity
curl http://127.0.0.1:8080/health

echo "✅ Setup complete! Access UI at http://127.0.0.1:8080"
```

---

## 16. Troubleshooting Checklist

| Issue | Check | Fix |
|-------|-------|-----|
| Docker not found | `docker --version` | Install Docker |
| Python import errors | `pip list` | `pip install -r requirements.txt` |
| Can't reach 10.5.5.113 | `ping 10.5.5.113` | Verify network, check firewall |
| qemu-img not found | `which qemu-img` | Install on conversion host |
| Port 8080 in use | `netstat -an \| grep 8080` | Change port in docker-compose.yml |
| Volumes missing | `docker volume ls` | Run `docker-compose up` |

---

## Summary: Dependency Matrix

| Tier | Component | Version | Purpose |
|------|-----------|---------|---------|
| **Essential** | Python | 3.10+ | Application runtime |
| **Essential** | Docker | 20.10+ | Containerization |
| **Essential** | Docker Compose | 2.0+ | Orchestration |
| **Required** | qemu-img | Latest | Disk conversion |
| **Required** | virt-v2v | Latest | Windows VM conversion |
| **Required** | paramiko/SSH | Latest | Remote access |
| **Optional** | Kubernetes | 1.25+ | Scale out |
| **Optional** | Node.js | 18+ | Web UI customization |
| **Optional** | Prometheus | Latest | Monitoring |

---

## Contact & Support

For dependency-specific issues:
- Python: https://www.python.org/
- Docker: https://docs.docker.com/
- Kubernetes: https://kubernetes.io/docs/
- Rock Linux: https://rockylinux.org/
- Proxmox: https://www.proxmox.com/
- VMware: https://www.vmware.com/
