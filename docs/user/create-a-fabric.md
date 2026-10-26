# Create a fabric (the controller's first start)

You need: the controller package installed on an Ubuntu 24.04 computer ([install-linux.md](install-linux.md)).
This page is done **once per fabric**.

## 1. Open the setup link - on the controller computer

```bash
sudo meshforge-controller setup url
```

It prints a link like `http://127.0.0.1:47110/setup#token=...`. Open it in a browser **on that same computer**.
The link works once, and only from that computer (the admin password must not cross the network before the
fabric's encryption exists).

Working from another computer? Forward the port first, then open the same link there:
`ssh -L 47110:127.0.0.1:47110 <user>@<controller>`.

<!-- TODO-SCREENSHOT: controller-setup.png - the setup page on the fresh Ubuntu 24.04 controller VM, before "Create the fabric" (🧑 0.17.8 lab; the test harness shows test paths) -->

## 2. Create the first admin

The page shows the checks first (database, disk, the network address laptops will use). Then, under
**The first admin**: an **Admin user name** (3 characters or more) and a **Password** (12 characters or more,
not too simple), **Repeat the password**, and **Create the fabric**.

If the page warns that laptops would join at `127.0.0.1`: no network address was found when the package was
installed. Put this computer's LAN address in the three `*bind` lines of `/etc/meshforge/controller.toml`, run
`sudo systemctl restart meshforge-controller`, and open a new setup link.

## 3. Your fabric is ready

The page now shows:

- **Join code - shown only now**: the first `mf1-...` code (and its QR code) for your first laptop. It works
  once, for a limited time.
- **Dashboard**: `https://<this computer's address>:47110/`.
- **CA fingerprint**: the fabric's own certificate. Browsers do not know it yet, so they warn the first time.
  `meshforge-controller ca trust-helper` prints how to make your browser trust it (and the fingerprint to
  compare).

The controller restarts itself with encryption on; the setup link never works again.

## 4. Sign in and add laptops

Open the dashboard and sign in with the admin name and password. To add a laptop: **Settings > Invites > New
invite** gives a new join code (text and QR). On the laptop, follow [join-a-laptop.md](join-a-laptop.md).

From a terminal on any computer (the `compute` command):

```bash
compute login --controller https://<controller address>:47110 --fingerprint <CA fingerprint> -u <admin>
compute fabric invite          # a new join code
compute nodes                  # the laptops that joined; CONN "yes" = connected now
```

More users: **Settings > Users** on the dashboard (an admin can add them).

## Keep your fabric safe

- The controller's database is backed up automatically before every upgrade. Take your own copy any time:
  `sudo meshforge-controller db backup` (keep the file private: it is the whole fabric).
- `apt remove meshforge-controller` keeps the fabric; only `apt purge` deletes it
  ([upgrade-and-uninstall.md](upgrade-and-uninstall.md)).
