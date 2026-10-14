"""Per-OS steps to trust the fabric CA in a browser (V0.8 G1, brief Q1, ADR-0017).

The dashboard is served over HTTPS with a certificate from the fabric's own CA
(ADR-0016), so a browser warns until that CA is trusted on the machine. The
documented path is to import ``ca.crt`` once per machine - never to click
through the warning (that trains people to accept a man in the middle).

``meshforge-controller ca trust-helper`` prints these steps with the CA's path
and SHA-256 fingerprint filled in. It **changes nothing** on any machine: the
commands are for the person to read, check and run. Pure text - tested as
such.
"""

from __future__ import annotations

from typing import Final

__all__ = ["OSES", "trust_steps"]

OSES: Final = ("linux", "windows", "macos")


def _header(ca_path: str, fingerprint: str, fabric_id: str) -> str:
    return f"""\
Trust the MeshForge fabric CA in a browser (fabric {fabric_id})

CA certificate : {ca_path}
SHA-256        : {fingerprint}

Copy ca.crt to the machine whose browser opens the dashboard (it is public; the
CA *key* never leaves this host). Before trusting it, check that the SHA-256 the
OS or browser shows equals the one above. Nothing below is run for you.

Trusting this CA lets whoever holds its key vouch for any https name to that
machine - keep the controller host safe, and remove the CA when the machine
leaves the fabric (each section ends with how).

The controller host itself can skip all of this: set
[controller.http] loopback_plain = "127.0.0.1:47111" and open
http://127.0.0.1:47111 there (plain HTTP that never leaves the machine).
"""


def _linux(name: str) -> str:
    return f"""\
== Linux ==
System store (curl, Python, Chromium on most distributions):
  Debian/Ubuntu:  sudo cp ca.crt /usr/local/share/ca-certificates/{name}.crt
                  sudo update-ca-certificates
  Fedora/RHEL:    sudo cp ca.crt /etc/pki/ca-trust/source/anchors/{name}.crt
                  sudo update-ca-trust
Chrome/Chromium (its own NSS store; package libnss3-tools):
  certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "{name}" -i ca.crt
Firefox (its own store):
  Settings > Privacy & Security > Certificates > View Certificates >
  Authorities > Import... > ca.crt > tick "Trust this CA to identify websites"
Check: restart the browser, open https://<controller>:47110/ - no warning.
Remove: delete the copied file and run the update command again;
  certutil -d sql:$HOME/.pki/nssdb -D -n "{name}"; Firefox: Authorities > Delete.
"""


def _windows(name: str) -> str:
    return f"""\
== Windows (Chrome and Edge use the Windows store) ==
PowerShell, current user only (no administrator needed):
  Import-Certificate -FilePath .\\ca.crt -CertStoreLocation Cert:\\CurrentUser\\Root
or Command Prompt:
  certutil -user -addstore Root ca.crt
Windows asks to confirm and shows the thumbprint (SHA-1): the SHA-256 above is
shown under certmgr.msc > Trusted Root Certification Authorities > the
certificate > Details.
Firefox: Authorities > Import... as on Linux, or set
  security.enterprise_roots.enabled = true in about:config.
Check: restart the browser, open https://<controller>:47110/ - no warning.
Remove: certmgr.msc > Trusted Root Certification Authorities > Certificates >
  the "{name}" CA > Delete.
"""


def _macos(name: str) -> str:
    return f"""\
== macOS (Safari and Chrome use the keychain) ==
  security add-trusted-cert -r trustRoot -k ~/Library/Keychains/login.keychain-db ca.crt
or Keychain Access > File > Import Items... > ca.crt, then open it and set
  "When using this certificate" to "Always Trust".
Firefox: Authorities > Import... as on Linux.
Check: restart the browser, open https://<controller>:47110/ - no warning.
Remove: Keychain Access > login > Certificates > the "{name}" CA > Delete.
"""


def trust_steps(
    *, ca_path: str, fingerprint: str, fabric_id: str, os_name: str = "all"
) -> str:
    """The guide for ``os_name`` (``linux``, ``windows``, ``macos`` or ``all``)."""
    if os_name != "all" and os_name not in OSES:
        raise ValueError(f"os must be one of {', '.join(OSES)} or all")
    name = f"meshforge-{fabric_id}"
    sections = {"linux": _linux, "windows": _windows, "macos": _macos}
    chosen = OSES if os_name == "all" else (os_name,)
    parts = [_header(ca_path, fingerprint, fabric_id)]
    parts += [sections[o](name) for o in chosen]
    return "\n".join(parts)
