"""Things a human must act on, derived from the probe results (top-level ``problems``).

A section can be ``ok: true`` (the probe itself worked) while describing a broken
machine, e.g. ``nvidia.driver_working: false`` on a laptop whose driver Secure
Boot blocks. ``failed_sections`` alone would hide that, so the document also has a
sorted, deterministic ``problems`` list of short sentences, and the summary prints
it.
"""

from __future__ import annotations

from _inventory.model import JSON


def _first_line(text: JSON, limit: int = 160) -> str:
    lines = str(text or "").strip().splitlines()
    return (lines[0] if lines else "no detail")[:limit]


def _value(sections: dict[str, JSON], name: str) -> JSON:
    probe = sections.get(name) or {}
    return probe.get("value") if probe.get("ok") else None


def _nvidia_problem(sections: dict[str, JSON]) -> str | None:
    nv = _value(sections, "nvidia")
    if not isinstance(nv, dict) or nv.get("driver_working"):
        return None
    smi = nv.get("nvidia_smi") or {}
    nvml = nv.get("nvml") or {}
    gpus = _value(sections, "gpus")
    listed = isinstance(gpus, list) and any(
        isinstance(g, dict) and g.get("vendor") == "NVIDIA" for g in gpus
    )
    detail = smi.get("error") if smi.get("available") else nvml.get("error")
    if listed:
        return f"nvidia: NVIDIA GPU present but the driver is not working ({_first_line(detail)})"
    if smi.get("available"):
        return (
            f"nvidia: nvidia-smi is installed but not working ({_first_line(detail)})"
        )
    return None


def _torch_problem(sections: dict[str, JSON]) -> str | None:
    t = _value(sections, "torch")
    if not isinstance(t, dict) or not t.get("installed"):
        return None
    if t.get("cuda_build") and not t.get("cuda_available"):
        text = (
            f"torch: CUDA build {t['cuda_build']} installed but "
            "torch.cuda.is_available() is False"
        )
        if t.get("cuda_error"):
            text += f" ({_first_line(t['cuda_error'])})"
        return text
    return None


def _docker_problem(sections: dict[str, JSON]) -> str | None:
    d = _value(sections, "docker")
    if isinstance(d, dict) and d.get("installed") and not d.get("reachable"):
        return f"docker: installed but the daemon is not reachable ({_first_line(d.get('error'))})"
    return None


def find_problems(sections: dict[str, JSON]) -> list[str]:
    """Sorted, de-duplicated problem sentences (empty when nothing needs a human)."""
    found: set[str] = set()
    for name, probe in sections.items():
        if not probe.get("ok"):
            found.add(f"{name}: probe failed ({_first_line(probe.get('error'))})")
    for check in (_nvidia_problem, _torch_problem, _docker_problem):
        problem = check(sections)
        if problem is not None:
            found.add(problem)
    return sorted(found)
