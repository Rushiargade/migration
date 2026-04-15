@echo off
REM vmigrate - Offline Preparation Script (Windows)
REM Run this while you have internet to download all required dependencies
REM Total Size: ~4-5GB

setlocal enabledelayedexpansion

echo ==========================================
echo vmigrate Offline Preparation Script
echo =========================================
echo.

REM Create download directory
set OFFLINE_DIR=%USERPROFILE%\vmigrate-offline
if not exist "%OFFLINE_DIR%" mkdir "%OFFLINE_DIR%"
cd /d "%OFFLINE_DIR%"

echo 📦 Creating offline package directory: %cd%
echo.

echo ========== CRITICAL DOWNLOADS (1-2GB) ==========

REM Check Python
echo [1/5] Downloading Python packages...
python --version >nul 2>&1
if %errorlevel% equ 0 (
    echo Running: python -m pip download -r requirements.txt -d ./python-packages/
    python -m pip download -r C:\path\to\migration\requirements.txt -d ./python-packages/ --no-deps
    echo ✅ Python packages downloaded to: python-packages/
) else (
    echo ⚠️  Python not found. Skip python packages for now.
)

REM Check Docker
echo [2/5] Downloading Docker base image...
if not exist "docker-images" mkdir docker-images
docker --version >nul 2>&1
if %errorlevel% equ 0 (
    echo Running: docker pull python:3.12-slim
    call docker pull python:3.12-slim
    echo Running: docker save...
    call docker save python:3.12-slim -o docker-images\python-3.12-slim.tar
    echo ✅ Docker image saved: docker-images\python-3.12-slim.tar
) else (
    echo ⚠️  Docker not found. Install Docker Desktop first.
)

REM Clone repo
echo [3/5] Cloning vmigrate repository...
git clone https://github.com/Rushiargade/migration.git
if %errorlevel% equ 0 (
    echo ✅ Repository cloned: migration/
) else (
    echo ⚠️  Git clone failed. Do you have git installed?
)

REM Conversion host setup
echo.
echo ========== CONVERSION HOST REQUIREMENTS (2.5GB) ==========
echo [4/5] Downloading VirtIO drivers...
if not exist "conversion-host" mkdir conversion-host
cd conversion-host

REM Download VirtIO (PowerShell is more reliable for downloads on Windows)
powershell -Command "Invoke-WebRequest -Uri 'https://fedorapeople.org/groups/virt/virtio-win/direct-downloads/latest/virtio-win.iso' -OutFile 'virtio-win.iso' -UseBasicParsing"
if %errorlevel% equ 0 (
    echo ✅ VirtIO ISO downloaded: %cd%\virtio-win.iso
) else (
    echo ⚠️  Failed to download VirtIO. Try manually from:
    echo    https://fedorapeople.org/groups/virt/virtio-win/
)

REM Create setup script for Rocky Linux
(
echo #!/bin/bash
echo # Run this on Rocky Linux conversion host after installation
echo.
echo echo "Installing vmigrate conversion tools..."
echo.
echo # Update system
echo sudo dnf update -y
echo.
echo # Install required packages
echo sudo dnf install -y ^
echo   qemu-img ^
echo   libvirt-client ^
echo   virt-v2v ^
echo   virt-manager ^
echo   openssh-server ^
echo   openssh-clients ^
echo   python3 ^
echo   python3-pip
echo.
echo # Setup VirtIO
echo sudo mkdir -p /opt/virtio-win
echo sudo cp virtio-win.iso /opt/virtio-win/
echo.
echo # Enable SSH
echo sudo systemctl enable sshd
echo sudo systemctl start sshd
echo.
echo echo "✅ Conversion host ready!"
) > rocky-setup.sh
echo ✅ Setup script created: conversion-host\rocky-setup.sh

cd ..

REM Summary
echo.
echo ==========================================
echo ✅ OFFLINE PREPARATION COMPLETE!
echo ==========================================
echo.
echo 📦 Packages downloaded to: %OFFLINE_DIR%
echo.
echo 📋 What was downloaded:
echo   - python-packages\       (Python dependencies)
echo   - docker-images\         (Docker base image)
echo   - conversion-host\       (VirtIO ISO + setup script)
echo   - migration\             (vmigrate source code)
echo.
echo 📊 Check size: Open conversion-host folder and check Properties
echo.
echo 📋 NEXT STEPS WHEN OFFLINE:
echo   1. Copy entire vmigrate-offline folder to external USB/storage
echo   2. Transfer to your deployment machine
echo   3. On Rocky Linux conversion host (10.5.5.113):
echo      - Copy virtio-win.iso to /opt/virtio-win/
echo      - Run: bash rocky-setup.sh
echo   4. On Docker host:
echo      - Run: docker load -i docker-images\python-3.12-slim.tar
echo      - Run: pip install --no-index --find-links=python-packages\ -r requirements.txt
echo.
echo 🔐 CRITICAL: Set these environment variables before starting:
echo   set VSPHERE_PASSWORD=your_vcenter_password
echo   set PROXMOX_PASSWORD=your_proxmox_password
echo.
pause
