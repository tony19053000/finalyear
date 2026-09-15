"""Short human summary of an inventory document (printed to stderr)."""

from __future__ import annotations

from _inventory.model import JSON


def _value(doc: dict[str, JSON], section: str) -> JSON:
    probe = doc["sections"].get(section) or {}
    return probe.get("value") if probe.get("ok") else None


def _error(doc: dict[str, JSON], section: str) -> str:
    probe = doc["sections"].get(section) or {}
    return str(probe.get("error", "unknown error")).splitlines()[0][:120]


def _gib(n: JSON) -> str:
    return f"{int(n) / 2**30:.1f} GiB" if isinstance(n, int) else "?"


def _cpu(doc: dict[str, JSON]) -> str:
    cpu = _value(doc, "cpu")
    if not cpu:
        return f"ERROR {_error(doc, 'cpu')}"
    return (
        f"{cpu.get('model')} ({cpu.get('physical_cores')} cores / "
        f"{cpu.get('logical_threads')} threads)"
    )


def _gpus(doc: dict[str, JSON]) -> str:
    gpus = _value(doc, "gpus")
    if gpus is None:
        return f"ERROR {_error(doc, 'gpus')}"
    if not gpus:
        return "none found"
    parts = []
    for g in gpus:
        vram = f", {g['vram_mib']} MiB" if g.get("vram_mib") else ""
        parts.append(f"{g.get('name')} [{g.get('vendor')}{vram}]")
    return "; ".join(parts)


def _nvidia(doc: dict[str, JSON]) -> str:
    nv = _value(doc, "nvidia")
    if nv is None:
        return f"ERROR {_error(doc, 'nvidia')}"
    smi = nv["nvidia_smi"]
    if smi.get("ok"):
        return (
            f"OK driver {nv.get('driver_version')} CUDA {nv.get('cuda_driver_version')}"
        )
    text = str(smi.get("error", "")).splitlines()[0][:120]
    return f"nvidia-smi FAILED: {text}"


def _torch(doc: dict[str, JSON]) -> str:
    t = _value(doc, "torch")
    if t is None:
        return f"ERROR {_error(doc, 'torch')}"
    if not t.get("installed"):
        return "not installed"
    return (
        f"{t.get('version')} (CUDA build {t.get('cuda_build')}) "
        f"cuda.is_available={t.get('cuda_available')}"
    )


def _docker(doc: dict[str, JSON]) -> str:
    d = _value(doc, "docker")
    if d is None:
        return f"ERROR {_error(doc, 'docker')}"
    if not d.get("installed"):
        return "not installed"
    if not d.get("reachable"):
        return f"client {d.get('client_version')}, daemon NOT reachable"
    runtimes = ",".join((d.get("info") or {}).get("runtimes") or [])
    return f"server {d.get('server_version')} runtimes [{runtimes}]"


def _network(doc: dict[str, JSON]) -> str:
    net = _value(doc, "network")
    if net is None:
        return f"ERROR {_error(doc, 'network')}"
    adapters = net.get("adapters") or []
    parts = []
    for a in adapters:
        if a.get("kind") not in {"wifi", "ethernet", "unknown"}:
            continue
        speed = a.get("link_speed_mbit")
        state = f"{speed:g} Mbit/s" if speed else ("up" if a.get("up") else "down")
        parts.append(f"{a.get('name')} {a.get('kind')} {state}")
    return "; ".join(parts) or "no physical adapters"


def render(doc: dict[str, JSON]) -> str:
    """Multi-line summary; every line is short and never raises on missing data."""
    mem = _value(doc, "memory")
    sb = _value(doc, "secure_boot")
    lines = [
        f"hostname     : {doc.get('hostname')}",
        f"os           : {(_value(doc, 'os') or {}).get('name')}",
        f"cpu          : {_cpu(doc)}",
        f"ram          : {_gib(mem.get('total_bytes')) if mem else 'ERROR'}",
        f"gpus         : {_gpus(doc)}",
        f"nvidia       : {_nvidia(doc)}",
        f"secure boot  : {sb.get('state') if sb else 'ERROR'}",
        f"torch        : {_torch(doc)}",
        f"docker       : {_docker(doc)}",
        f"network      : {_network(doc)}",
        f"failed probes: {', '.join(doc.get('failed_sections') or []) or 'none'}",
        *_problems(doc),
    ]
    return "\n".join(lines)


def _problems(doc: dict[str, JSON]) -> list[str]:
    problems = [str(p) for p in doc.get("problems") or []]
    if not problems:
        return ["problems     : none"]
    return [
        f"problems     : {len(problems)} (need a human)",
        *(f"  - {p[:200]}" for p in problems),
    ]
