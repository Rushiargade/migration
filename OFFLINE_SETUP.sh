#!/bin/bash
# vmigrate - Offline Preparation Script
# Run this while you have internet to download all required dependencies
# Total Size: ~4-5GB

echo "=========================================="
echo "vmigrate Offline Preparation Script"
echo "=========================================="
echo ""

# Create download directory
mkdir -p ~/vmigrate-offline
cd ~/vmigrate-offline

echo "📦 Creating offline package directory: $(pwd)"
echo ""

# ===== CRITICAL DOWNLOADS (ESSENTIAL) =====
echo "========== CRITICAL DOWNLOADS (1-2GB) =========="

# Python Packages
echo "[1/5] Downloading Python packages..."
if command -v python3 &> /dev/null; then
    python3 -m pip download -r /path/to/migration/requirements.txt -d ./python-packages/
    echo "✅ Python packages downloaded to: python-packages/"
else
    echo "⚠️  Python not found. Skip python packages for now."
fi

# Docker Image
echo "[2/5] Downloading Docker base image..."
mkdir -p docker-images
if command -v docker &> /dev/null; then
    docker pull python:3.12-slim
    docker save python:3.12-slim -o docker-images/python-3.12-slim.tar
    echo "✅ Docker image saved: docker-images/python-3.12-slim.tar"
else
    echo "⚠️  Docker not found. Install Docker Desktop first."
fi

# VMigrate repo
echo "[3/5] Cloning vmigrate repository..."
git clone https://github.com/Rushiargade/migration.git
echo "✅ Repository cloned: migration/"

# ===== CONVERSION HOST REQUIREMENTS (2.5GB) =====
echo ""
echo "========== CONVERSION HOST REQUIREMENTS (2.5GB) =========="
echo "[4/5] Downloading Rocky Linux ISO & VirtIO drivers..."

mkdir -p conversion-host

# VirtIO ISO (needed for Windows VM conversion)
echo "Downloading VirtIO drivers (600MB)..."
cd conversion-host
wget -q --show-progress https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest/virtio-win.iso
echo "✅ VirtIO ISO downloaded: $(pwd)/virtio-win.iso"

# Rocky Linux setup script
cat > rocky-setup.sh << 'EOF'
#!/bin/bash
# Run this on the conversion host (10.5.5.113) after Rocky Linux is installed

echo "Installing vmigrate conversion tools..."

# Update system
sudo dnf update -y

# Install required packages
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
  rsync \
  python3 \
  python3-pip

# Setup VirtIO
sudo mkdir -p /opt/virtio-win
sudo cp /tmp/virtio-win.iso /opt/virtio-win/

# Enable SSH
sudo systemctl enable sshd
sudo systemctl start sshd

echo "✅ Conversion host ready!"
echo "Verify installation:"
echo "  qemu-img --version"
echo "  virt-v2v --version"
EOF

chmod +x rocky-setup.sh
echo "✅ Setup script created: conversion-host/rocky-setup.sh"
cd ..

# ===== OPTIONAL but RECOMMENDED =====
echo ""
echo "========== OPTIONAL DOWNLOADS (500MB) =========="
echo "[5/5] Downloading optional tools..."

mkdir -p optional-tools

# kubectl (for Kubernetes)
if [ "$(uname)" == "Linux" ]; then
    echo "Downloading kubectl..."
    curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
    mv kubectl optional-tools/
    chmod +x optional-tools/kubectl
    echo "✅ kubectl downloaded"
fi

# Helm (for Kubernetes)
if [ "$(uname)" == "Linux" ]; then
    echo "Downloading Helm..."
    curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    echo "✅ Helm installed"
fi

# ===== SUMMARY =====
echo ""
echo "=========================================="
echo "✅ OFFLINE PREPARATION COMPLETE!"
echo "=========================================="
echo ""
echo "📦 Packages downloaded to: $(pwd)"
echo ""
echo "Directory Structure:"
find . -maxdepth 2 -type d | sort
echo ""
echo "📊 Total Size:"
du -sh .
echo ""
echo "📋 NEXT STEPS:"
echo "1. Copy this entire directory to a USB drive or external storage"
echo "2. Transfer to your deployment machines offline"
echo "3. Run the setup scripts:"
echo "   - On conversion host: bash conversion-host/rocky-setup.sh"
echo "   - On Docker host: docker load < docker-images/python-3.12-slim.tar"
echo "4. Install Python packages:"
echo "   pip install --no-index --find-links=python-packages/ -r requirements.txt"
echo ""
echo "⚠️  Remember to set environment variables before running:"
echo "   export VSPHERE_PASSWORD='...'"
echo "   export PROXMOX_PASSWORD='...'"
echo ""
