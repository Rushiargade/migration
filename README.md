# vmigrate: VMware to Proxmox Large-Scale Migration Tool

**vmigrate** is an enterprise-grade orchestration tool designed to aggressively and reliably cold-migrate large fleets of Virtual Machines (15,000+) from VMware vCenter to Proxmox VE. 

It handles the entire lifecycle of the migration using a robust state machine that guarantees strict consistency, resume-on-failure capabilities, automated driver injection, and high-performance direct-to-block streaming to bypass Proxmox root filesystem bottlenecks.

---

## 🏛️ Architecture & Tech Stack

This project is structured as a decoupled backend and frontend:

### Backend (Python)
- **FastAPI**: Serves the REST API for the GUI and asynchronous polling.
- **Python ThreadPoolExecutor**: Coordinates the heavy I/O of migration workers asynchronously.
- **pyVmomi**: Interacts directly with vCenter's SOAP API to export VMDKs and handle snapshots.
- **SQLite (`state.db`)**: Provides robust persistence for the migration State Machine. This prevents data loss during restarts and allows migrations to intelligently resume locally if a phase fails.
- **Paramiko (SSH)**: Heavy reliance on SSH specifically to remotely trigger `virt-v2v`/`qemu-img` on the Conversion Host and stream disks directly to Proxmox.

### Frontend (HTML/JS/CSS)
- **Vanilla JavaScript**: Zero dependencies. Heavy polling utilizing async fetches to `/migrate/status`.
- **Vanilla CSS**: Custom design system natively implemented without Tailwind/Bootstrap.

### Orchestration Environment
The recommended execution topology consists of three isolated actors:
1. **VMware vCenter/ESXi** (Source)
2. **Dedicated Rocky Linux Conversion Host** (Executing vmigrate)
3. **Proxmox Virtual Environment** (Destination)

---

## ⚙️ The Migration State Machine (Phases)

Every VM passes through a strictly enforced Phase sequence (`vmigrate/migration/cold.py`):

1. **PREFLIGHT**
   - Validates vCenter connectivity.
   - Validates Proxmox node/storage existence.
   - Validates Network bridges and Storage mappings.

2. **SNAPSHOT_CREATE**
   - Attempts a Quiesced snapshot. If that fails, attempts a crash-consistent snapshot. 
   - If both fail (or if disks are independent), it falls back to gracefully powering off the VM natively via vCenter.

3. **EXPORT_DISK**
   - Downloads the VM's `.vmdk` disks via HTTPS from vCenter datastores to the Conversion Host's local storage (`/var/lib/vmigrate`).

4. **CONVERT_DISK**
   - **Linux:** Uses `qemu-img` over SSH to convert to `qcow2`.
   - **Windows:** Automatically executes a pre-flight `guestfish` + `ntfsfix` script to force-clear Windows Fast Startup / Hibernation dirty bits. This allows `virt-v2v` to safely mount the partitions read-write and inject Proxmox VirtIO drivers natively into the Windows registry!

5. **PROXMOX_VM_CREATE**
   - Utilizes the Proxmox HTTP API to create a naked VM shell with matching CPU cores, RAM, and MAC-spoofed NICs attached to the correct bridges.

6. **PROXMOX_DISK_IMPORT**
   - Avoids suffocating Proxmox's tiny root filesystem (like standard `qm importdisk` does).
   - Dynamically pre-allocates block storage via `pvesm alloc`.
   - Mounts a temporary HTTP stream on the conversion host.
   - Triggers the destination Proxmox node to `wget` and `dd` stream the raw bytes natively over the network straight onto the block volume using `conv=sparse`.

7. **CLEANUP**
   - Removes `.vmdk` files, `qcow2` files, and removes the VMware snapshot safely.

---

## 🛠️ Automated Deployment

The project provides automation specifically tailored for secure offline deployments without constant password prodding:

- **`push_offline_update.bat`**: Uses the developer's local docker daemon to compile an image array, bundles it to `.tar`, injects an ephemeral `sshpass` Alpine container, pushing the update over SCP. It finally remotely reloads and revives the backend container onto the host Linux CLI entirely without external dependencies or password prompts.

---

## 🧠 Dev Notes (For Future AI Agents)

**Important context to know when touching this repository:**

1. **State Persistence First**: Do not skip updating SQLite state when adding new phases. If `_run_phase` crashes, the pipeline relies tightly on the checkpoint tracker (`state_db.get_resume_phase()`) to pick up where it left off.
2. **Proxmox Storage Formats**: We stream directly to Proxmox block devices bypassing `/var/tmp/`. Make sure `pvesm alloc` continues to receive un-suffixed integer kilobytes (`size_kb`) or it will crash.
3. **virt-v2v Complexity**: Conversion of Windows VMs runs via `virt-v2v` in `virt_v2v.py`. When debugging `ntfs-3g` readonly mount failures, the system's automated `guestfish` scrubber handles clearing hibernation bits. Do not disable this heavily tuned workflow block.
4. **Hardcoded Testing**: The UI connect form parameters in `index.html` and the SSH passwords in `_build_migration_config` (`routes/migration.py`) are pre-configured to `supp0rt$ESDS` to drastically accelerate testing. Do not overwrite these constants until moving strictly to production.
