# Install MeshForge on a Windows laptop

> Draft (V0.17 G4). Screenshots come with V0.17 G5.

You need: a Windows 10 (version 22H2) or Windows 11 laptop with a 64-bit Intel or AMD processor, an administrator's
password (yours, if you are an administrator), and a **join code** from whoever runs the fabric's controller. You
do **not** need Python, a terminal or WSL.

1. **Download** `MeshForge-Agent-Setup-<version>.exe` (from the team's release page or the person running the
   fabric).
2. **Run it.** Windows may say *"Windows protected your PC"*: MeshForge's installer is not signed yet. Click
   **More info**, check that the app name is `MeshForge-Agent-Setup-...`, then **Run anyway**. Windows then asks
   whether the app may make changes: click **Yes** (or type an administrator's password).
3. **Follow the pages.** Accept the licence and keep the suggested folder (`C:\Program Files\MeshForge`). The page
   *What this computer will share* explains what happens: MeshForge runs in the background, starts with Windows,
   keeps running when you sign out, and runs **nobody's jobs** until you allow it.
4. **Wait for "MeshForge setup finished".** The installer starts MeshForge and checks that it really runs (up to
   a minute). The last page says either *"MeshForge is installed and running"* or exactly what is wrong and what
   to do. Leave **Open MeshForge** ticked and click **Finish**.
5. **Join.** A browser page opens - it is served by the laptop itself and cannot be reached from the network. Paste
   the `mf1-...` code (or scan the QR code on the dashboard with the webcam), then choose what the laptop shares
   (see [owner-settings.md](owner-settings.md)) and look at the checks. The rest is the same as
   [join-a-laptop.md](join-a-laptop.md).

To open the page again later: **Start menu → MeshForge**.

## Docker and the GPU

- **Docker Desktop** lets MeshForge run jobs in containers. Giving MeshForge Docker access is the same as giving
  it full administrator rights on the laptop, so it is off until you turn it on: **Start menu → MeshForge - give
  Docker access** (Windows asks for an administrator's OK; MeshForge restarts by itself). Docker Desktop only runs
  while someone is signed in; turn on *Start Docker Desktop when you sign in* in its settings.
- **NVIDIA GPU**: install the NVIDIA driver (nvidia.com/drivers or Windows Update) and restart. The page's GPU check
  says whether MeshForge sees it. Sharing the GPU is off until you turn it on in the owner settings.

## Another user of this laptop

Only the person who installed MeshForge (and administrators) may open its page. Anyone else who opens **MeshForge**
from the Start menu is asked whether to request access; an administrator approves it in the Windows prompt.

## The network says "Public"

On a network Windows calls *Public* (cafés, hotels) other laptops cannot fetch files from this one; jobs still run.
On your home or team network, set it to *Private*: **Settings → Network & internet → (your Wi-Fi or Ethernet) →
Network profile type → Private**. MeshForge never opens a port on Public networks.

## A laptop that already ran MeshForge for development

If this laptop ran the MeshForge agent from a source checkout (the *MeshForgeAgent* scheduled task), the installer
stops and disables that task and moves its identity to the new service, so the laptop stays the **same node** in
the dashboard. If it finds more than one identity (two Windows users both ran it), it installs but does not start
MeshForge, and the last page names the folders and the command to choose one.

## Uninstall

**Settings → Apps → Installed apps → MeshForge Agent → Uninstall.** It asks whether to *also remove all MeshForge
data*; the answer **No** (the default) keeps this laptop's identity, so installing again brings back the same node.
Choose **Yes** only if the laptop should join as a new node next time. Nothing of MeshForge keeps running after
the uninstall: no service, no scheduled task, no firewall rule.
