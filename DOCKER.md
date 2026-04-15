# vmigrate Docker Deployment Guide

## Quick Start — Web UI with Persistent Volumes

Persistent Docker volumes (`vmigrate-data`, `vmigrate-logs`) ensure migration state and logs survive container restarts.

### 1. Start the web UI
```bash
docker-compose up -d vmigrate-web
```

### 2. Access the web UI

**⚠️ On Windows (Docker Desktop):**  
Use `127.0.0.1` instead of `localhost`:
```
http://127.0.0.1:8080
```

**On Linux/macOS:**  
Use `localhost`:
```
http://localhost:8080
```

### 3. Enter credentials in the UI
- Credentials form appears on first load
- Enter vCenter hostname, vCenter password, Proxmox hostname, Proxmox password
- Submit — credentials are securely stored in SQLite state DB (persisted in `vmigrate-data` volume)

**Note:** Credentials persist across container restarts. No need to re-enter them.

### 3. (Optional) Use environment variables instead
If you prefer setting credentials via env vars:

```bash
export VSPHERE_PASSWORD="your_vcenter_password"
export PROXMOX_PASSWORD="your_proxmox_password"
export VSPHERE_HOST="vcenter.example.com"
export PROXMOX_HOST="proxmox.example.com"
docker-compose down && docker-compose up -d vmigrate-web
```

The UI will auto-load these credentials.

---

## Windows Docker Desktop Notes

⚠️ On Windows with Docker Desktop, `localhost` port mappings don't work as expected.  
**Always use `http://127.0.0.1:8080` instead of `http://localhost:8080`**

This is a known limitation of Docker Desktop on Windows. The container and persistent volumes work perfectly fine — just use the IP address to access it:

```powershell
# ❌ WRONG (won't connect)
http://localhost:8080

# ✅ CORRECT (use this)
http://127.0.0.1:8080
```

---

### 4. View logs
```bash
docker-compose logs -f vmigrate-web
```

### 5. Stop
```bash
docker-compose down
```

**Volumes persist even after `down`!** Data will be available on next `up`.

---

## Persistent Volumes

Migration state, artifacts, and logs are stored in Docker-managed persistent volumes:

| Volume | Purpose | Data Persistence |
|--------|---------|-------------------|
| `vmigrate-data` | SQLite state DB + migration artifacts | Survives `docker-compose down` |
| `vmigrate-logs` | Application logs | Survives `docker-compose down` |

**Benefits of persistent volumes:**
- Migration state persists across container restarts/deletion
- Automatic backup-friendly
- Works on all systems (Linux, Docker Desktop, cloud Docker hosts)
- Cleaner than bind mounts (no host directory clutter)

### View Volume Contents

```bash
# List all volumes
docker volume ls

# Inspect metadata
docker volume inspect vmigrate-data

# Check state DB inside volume
docker run --rm -v vmigrate-data:/data sqlite3 /data/migrations.db .tables
```

### Backup Volumes

```bash
# Backup migration state (all data)
docker run --rm -v vmigrate-data:/data -v $(pwd):/backup \
  alpine tar czf /backup/vmigrate-data-backup.tar.gz -C /data .

# Restore from backup
docker volume rm vmigrate-data
docker volume create vmigrate-data
docker run --rm -v vmigrate-data:/data -v $(pwd):/backup \
  alpine tar xzf /backup/vmigrate-data-backup.tar.gz -C /data
```

### Clean Up Volumes (Destructive)

```bash
# Remove stopped containers ONLY (keeps volumes)
docker-compose rm

# Remove containers AND volumes (deletes all migration state)
docker-compose down -v
```

---

## Batch Migration — CLI

Batch migrations share the same `vmigrate-data` volume as the web UI, so Web UI can monitor CLI progress in real-time.

### Run a batch migration
```bash
docker-compose run --rm vmigrate-batch migrate --config /app/config/migration.yaml --all
```

Or with specific VMs:
```bash
docker-compose run --rm vmigrate-batch migrate \
  --config /app/config/migration.yaml \
  --vms "vm1,vm2,vm3"
```

Or from a VM list file (prepared by batch split utility):
```bash
docker-compose run --rm vmigrate-batch migrate \
  --config /app/config/migration.yaml \
  --vm-file /app/config/batch_0.txt
```

### Monitor in Web UI while CLI runs

While the batch migration is running in CLI, you can:
1. Open http://localhost:8080 in another terminal
2. Go to **Status** page
3. Watch real-time progress (shared SQLite state DB)

---

## Check Status

### Inside running container
```bash
docker-compose exec vmigrate-web vmigrate status --config /app/config/migration.yaml
```

### With logs
```bash
docker-compose exec vmigrate-web vmigrate status --config /app/config/migration.yaml --log-level DEBUG
```

---

## Volume Mounts

| Container Path | Mount Type | Purpose | Persistence |
|---|---|---|---|
| `/app/config` | Bind mount (host `./config`) | Configuration YAML files | Host filesystem |
| `/var/lib/vmigrate` | **Persistent volume** `vmigrate-data` | State DB + migration artifacts | **Survives down/deletion** |
| `/app/logs` | **Persistent volume** `vmigrate-logs` | Application logs | **Survives down/deletion** |

**Key difference:**
- **Bind mounts** (config) = host directory; good for editing config files
- **Persistent volumes** (data/logs) = Docker-managed; insulated from host FS issues

---

## Build a Custom Image

### Build locally
```bash
docker build -t myregistry/vmigrate:latest .
```

### Push to registry
```bash
docker tag vmigrate:latest myregistry/vmigrate:v0.1.0
docker push myregistry/vmigrate:v0.1.0
```

### Use custom image in compose
Edit `docker-compose.yml`:
```yaml
services:
  vmigrate-web:
    image: myregistry/vmigrate:v0.1.0
    # ... rest of config
```

---

## Environment Variables

All are optional (defaults in config/migration.yaml):

| Variable | Purpose |
|----------|---------|
| `VSPHERE_PASSWORD` | vCenter login password |
| `VSPHERE_HOST` | vCenter hostname |
| `PROXMOX_PASSWORD` | Proxmox API password |
| `PROXMOX_HOST` | Proxmox hostname |

---

## Network Configuration

By default, `docker-compose` creates a bridge network `vmigrate-net`.

To expose to external networks:
```yaml
services:
  vmigrate-web:
    networks:
      - vmigrate-net
      - external_network  # if needed
```

---

## Data Persistence

The SQLite state database and application logs are stored in Docker-managed persistent volumes (`vmigrate-data`, `vmigrate-logs`).

**This means:**
- Migration state persists across `docker-compose down` / container deletion
- You can safely restart containers without losing progress
- Credentials entered in UI are permanently stored
- Logs are always available even after container stops

**Example:**
```bash
# Day 1: Start migration
docker-compose up -d vmigrate-web
# ... 100 VMs migrated, progress saved in vmigrate-data volume

# Day 2: Container crashed or you rebooted
docker-compose up -d vmigrate-web
# Web UI shows previous progress (state DB fully restored)
# Can retry failed VMs before continuing
```

---

## Examples

### Example 1: UI-based credentials with persistent state
```bash
# Start container
docker-compose up -d vmigrate-web

# Access UI at http://localhost:8080
# - Enter vCenter host, password
# - Enter Proxmox host, password
# - Submit (credentials now persisted in vmigrate-data volume)

# VMs migrated, state saved in vmigrate-data

# Later: container crashes or host reboots
docker-compose up -d vmigrate-web

# Web UI shows previous progress (restored from vmigrate-data)
# No need to re-enter credentials
# Can retry failed VMs or continue with next batch
```

### Example 2: Environment variables + batch CLI
```bash
export VSPHERE_PASSWORD="..."
export PROXMOX_PASSWORD="..."

# Run batch migration in background
docker-compose run -d --name batch-001 vmigrate-batch migrate \
  --config /app/config/batch_0.yaml --all

# In another terminal, watch progress in Web UI
docker-compose up -d vmigrate-web
# Open http://localhost:8080 → Status page (shared state DB)

# Check batch CLI logs
docker-compose logs batch-001
```

### Example 3: Daily batch migration with persistence
```bash
#!/bin/bash
# Day N: Run batch N
docker-compose run --rm vmigrate-batch migrate \
  --config /app/config/batch_N.yaml \
  --all

# Day N+1: Container down. Restart.
docker-compose up -d vmigrate-web
# State DB restored from vmigrate-data volume
docker-compose exec vmigrate-web vmigrate status --config /app/config/batch_N.yaml
```

### Example 4: Web UI + Manual Operations
```bash
# Start the web UI
docker-compose up -d vmigrate-web

# Use browser (Windows: http://127.0.0.1:8080, Linux/macOS: http://localhost:8080) to:
# - Enter credentials (persisted)
# - Configure vCenter/Proxmox connections
# - Select VMs to migrate
# - Monitor progress in real-time
# - Retry failed VMs

# Check detailed logs
docker-compose logs -f vmigrate-web
```

---

---

## Troubleshooting

### Container fails to start
```bash
docker-compose logs vmigrate-web
```

Common issues:
- **Port 8080 already in use:** Change port in docker-compose.yml or stop conflicting container
- **Missing config:** Ensure `config/migration.yaml` exists
- **Volume permission denied:** Run `docker volume ls` and check ownership

### State DB corrupted or locked
```bash
# Restart affected container
docker-compose restart vmigrate-web

# Or reset state (destructive) - deletes all migration history
docker-compose down -v
docker-compose up -d vmigrate-web
```

### Out of disk space in vmigrate-data
```bash
# Check volume usage
docker run --rm -v vmigrate-data:/data alpine du -sh /data

# Check where space is used
docker run --rm -v vmigrate-data:/data alpine du -sh /data/*
```

If artifacts (VMDK exports, qcow2 files) are taking space, they should be cleaned up after migration completes. Check `config/migration.yaml` for `keep_artifacts` setting.

### Reset credentials (stored in state DB)
```bash
# Option 1: Delete entire volume (loses all state)
docker-compose down -v
docker-compose up -d vmigrate-web
# Re-enter credentials in UI

# Option 2: Edit state DB inside volume
docker run --rm -v vmigrate-data:/data sqlite3 /data/migrations.db \
  "DELETE FROM artifacts WHERE key='vsphere_password' OR key='proxmox_password';"
docker-compose restart vmigrate-web
```

### Verify volumes are persisting

```bash
# Create test file in volume
docker run --rm -v vmigrate-data:/data alpine touch /data/test-file.txt

# Stop container
docker-compose down

# Check file still exists
docker run --rm -v vmigrate-data:/data alpine ls -la /data/test-file.txt

# Start container again
docker-compose up -d vmigrate-web

# File should still be there
docker run --rm -v vmigrate-data:/data alpine ls -la /data/test-file.txt
```

---

### SSH access to container
```bash
docker-compose exec vmigrate-web /bin/bash
```

Then run vmigrate commands directly inside:
```bash
vmigrate status --config /app/config/migration.yaml
```
