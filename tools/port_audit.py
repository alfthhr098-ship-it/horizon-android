#!/usr/bin/env python3
"""Audit what differs between two Minecraft versions, to scope a Horizon port.

    port_audit.py [--base 26.3] [--target 1.21.4] [--work DIR] [--out DIR]

Downloads each version's server jar from Mojang (sha1 checked), runs the data generator
(`--reports`), then diffs packets, registries, blocks, items and commands. Writes report.md and
report.json to --out. Standard library only. Development use: nothing downloaded is committed.
"""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def fetch(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def prepare(version, work):
    """Download the server jar, read its facts, run the data generator. Returns (facts, reports_dir)."""
    root = Path(work) / version
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(fetch(MANIFEST))
    entry = next((v for v in manifest["versions"] if v["id"] == version), None)
    if entry is None:
        raise SystemExit(f"{version} is not in Mojang's version manifest")
    meta_raw = fetch(entry["url"])
    if sha1(meta_raw) != entry["sha1"]:
        raise SystemExit(f"{version}: version JSON sha1 mismatch")
    meta = json.loads(meta_raw)
    server = meta["downloads"]["server"]
    jar = root / "server.jar"
    if not jar.exists() or sha1(jar.read_bytes()) != server["sha1"]:
        data = fetch(server["url"])
        if sha1(data) != server["sha1"]:
            raise SystemExit(f"{version}: server jar sha1 mismatch")
        jar.write_bytes(data)
    facts = {
        "id": meta["id"],
        "java_major": meta.get("javaVersion", {}).get("majorVersion"),
        "obfuscated": "server_mappings" in meta["downloads"],
    }
    facts.update(read_version_json(jar, version))
    out = root / "gen"
    reports = out / "reports"
    if not reports.exists():
        java = java_for(facts["java_major"])
        cmd = [java, "-DbundlerMainClass=net.minecraft.data.Main", "-jar", str(jar),
               "--reports", "--output", str(out)]
        print("+", " ".join(cmd), flush=True)
        subprocess.run(cmd, check=True, cwd=root)
    return facts, reports


def java_for(major):
    """The JDK that setup-java put in JAVA_HOME_<major>_X64, else `java` on PATH."""
    home = os.environ.get(f"JAVA_HOME_{major}_X64") or os.environ.get(f"JAVA_HOME_{major}_ARM64")
    return str(Path(home) / "bin" / "java") if home else "java"


def read_version_json(jar, version):
    """protocol/data/pack versions from the server's own version.json (inside the bundler)."""
    try:
        with zipfile.ZipFile(jar) as outer:
            names = outer.namelist()
            blob = None
            if "version.json" in names:
                blob = outer.read("version.json")
            else:
                inner = next((n for n in names if n.startswith("META-INF/versions/") and n.endswith(".jar")), None)
                if inner:
                    with zipfile.ZipFile(io.BytesIO(outer.read(inner))) as z:
                        if "version.json" in z.namelist():
                            blob = z.read("version.json")
            if blob is None:
                return {}
            v = json.loads(blob)
            return {
                "name": v.get("name"),
                "protocol_version": v.get("protocol_version"),
                "world_version": v.get("world_version"),
                "pack_version": v.get("pack_version"),
                "java_version": v.get("java_version"),
            }
    except Exception as e:  # facts are informative; the diff does not depend on them
        return {"facts_error": str(e)}


def load(reports, name):
    p = Path(reports) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def diff_sets(base, target):
    b, t = set(base), set(target)
    return {"only_base": sorted(b - t), "only_target": sorted(t - b), "common": len(b & t)}


def diff_packets(base, target):
    """Per state and direction: packets only in one version, and common packets whose id differs."""
    out = {}
    if base is None or target is None:
        return out
    for state in sorted(set(base) | set(target)):
        for direction in sorted(set(base.get(state, {})) | set(target.get(state, {}))):
            b = base.get(state, {}).get(direction, {})
            t = target.get(state, {}).get(direction, {})
            d = diff_sets(b, t)
            d["renumbered"] = sorted(
                f"{n}: {b[n]['protocol_id']} -> {t[n]['protocol_id']}"
                for n in set(b) & set(t) if b[n].get("protocol_id") != t[n].get("protocol_id"))
            out[f"{state}/{direction}"] = d
    return out


def diff_registries(base, target):
    out = {}
    if base is None or target is None:
        return out
    d = diff_sets(base, target)
    out["_registries"] = d
    for reg in sorted(set(base) & set(target)):
        be = base[reg].get("entries", {})
        te = target[reg].get("entries", {})
        s = diff_sets(be, te)
        s["renumbered"] = sum(1 for n in set(be) & set(te)
                              if be[n].get("protocol_id") != te[n].get("protocol_id"))
        s["base_total"], s["target_total"] = len(be), len(te)
        if s["only_base"] or s["only_target"] or s["renumbered"]:
            out[reg] = s
    return out


def diff_blocks(base, target):
    if base is None or target is None:
        return {}
    d = diff_sets(base, target)
    changed = []
    for name in set(base) & set(target):
        bs, ts = base[name].get("states", []), target[name].get("states", [])
        bp, tp = base[name].get("properties"), target[name].get("properties")
        if len(bs) != len(ts) or bp != tp:
            changed.append(name)
    d["state_or_property_changed"] = sorted(changed)
    return d


def command_names(node, prefix=""):
    names = set()
    for k, v in (node.get("children") or {}).items():
        path = f"{prefix}/{k}"
        names.add(path)
        names |= command_names(v, path)
    return names


def diff_commands(base, target):
    if base is None or target is None:
        return {}
    return diff_sets(command_names(base), command_names(target))


def audit(base_reports, target_reports):
    r = lambda n: (load(base_reports, n), load(target_reports, n))
    return {
        "packets": diff_packets(*r("packets.json")),
        "registries": diff_registries(*r("registries.json")),
        "blocks": diff_blocks(*r("blocks.json")),
        "items": diff_sets(*(x if x is not None else {} for x in r("items.json"))),
        "commands": diff_commands(*r("commands.json")),
    }


def cap(items, n=40):
    items = list(items)
    return ", ".join(items[:n]) + (f" ... (+{len(items) - n})" if len(items) > n else "")


def render(base, target, bf, tf, a):
    L = [f"# Port audit: {base} -> {target}", "",
         "Going from the pinned `base` to `target`. 'only base' = must be removed or gated; "
         "'only target' = must be added.", "",
         "## Facts", "", "| | " + base + " | " + target + " |", "|---|---|---|"]
    for k in ("protocol_version", "world_version", "pack_version", "java_major", "obfuscated"):
        L.append(f"| {k} | {bf.get(k)} | {tf.get(k)} |")
    L += ["", "## Packets", "", "| state/direction | only base | only target | renumbered |", "|---|---|---|---|"]
    for k, d in a["packets"].items():
        L.append(f"| {k} | {len(d['only_base'])} | {len(d['only_target'])} | {len(d['renumbered'])} |")
    for k, d in a["packets"].items():
        if d["only_base"] or d["only_target"]:
            L += ["", f"**{k}**", f"- only base: {cap(d['only_base'])}", f"- only target: {cap(d['only_target'])}"]
    L += ["", "## Registries", ""]
    reg = a["registries"].get("_registries")
    if reg:
        L.append(f"- registries only in base: {cap(reg['only_base'])}")
        L.append(f"- registries only in target: {cap(reg['only_target'])}")
    L += ["", "| registry | base | target | only base | only target | renumbered |", "|---|---|---|---|---|---|"]
    for k, d in a["registries"].items():
        if k != "_registries":
            L.append(f"| {k} | {d['base_total']} | {d['target_total']} | {len(d['only_base'])} | "
                     f"{len(d['only_target'])} | {d['renumbered']} |")
    b = a["blocks"]
    if b:
        L += ["", "## Blocks", "", f"- only base: {len(b['only_base'])}, only target: {len(b['only_target'])}, "
              f"state/property changed: {len(b['state_or_property_changed'])}",
              f"- changed: {cap(b['state_or_property_changed'])}"]
    it = a["items"]
    L += ["", "## Items", "", f"- only base: {len(it['only_base'])}, only target: {len(it['only_target'])}"]
    cm = a["commands"]
    if cm:
        L += ["", "## Commands", "", f"- only base: {cap(cm['only_base'])}", f"- only target: {cap(cm['only_target'])}"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="26.3")
    ap.add_argument("--target", default="1.21.4")
    ap.add_argument("--work", default=".port-audit")
    ap.add_argument("--out", default="port-audit-out")
    args = ap.parse_args()
    bf, br = prepare(args.base, args.work)
    tf, tr = prepare(args.target, args.work)
    a = audit(br, tr)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps({"base": bf, "target": tf, "diff": a}, indent=1))
    md = render(args.base, args.target, bf, tf, a)
    (out / "report.md").write_text(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(md)
    print(md[:3000])


if __name__ == "__main__":
    sys.exit(main())
