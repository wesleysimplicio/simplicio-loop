#!/usr/bin/env python3
"""Check eight-component release compatibility without claiming propagation."""
from __future__ import annotations
import argparse,json,re
from pathlib import Path
COMPONENTS=("runtime","local","fast","loop","sprint","prompt","mapper","dev-cli")
COMMIT=re.compile(r"^[0-9a-fA-F]{40}$")
def candidate(root,name):
    aliases={"dev-cli":["simplicio-dev-cli","dev-cli"],"runtime":["simplicio-runtime","runtime"],"local":["simplicio-local","local"],"fast":["simplicio-fast","fast"],"loop":["simplicio-loop","loop"],"sprint":["simplicio-sprint","sprint"],"prompt":["simplicio-prompt","prompt"],"mapper":["simplicio-mapper","mapper"]}
    for directory in aliases[name]:
        base=root/directory
        for rel in (".simplicio/component-release.json","component-release.json","release-manifest.json","dist/component-release.json"):
            path=base/rel
            if path.is_file(): return path
    return None
def main():
    p=argparse.ArgumentParser(); p.add_argument("workspace_root")
    root=Path(p.parse_args().workspace_root).expanduser(); rows=[]; blocked=0; measured=0
    for name in COMPONENTS:
        path=candidate(root,name) if root.is_dir() else None
        if path is None:
            rows.append({"component":name,"status":"UNVERIFIED","reason":"manifest_missing"}); continue
        try: value=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError,json.JSONDecodeError) as error:
            rows.append({"component":name,"status":"BLOCKED","reason":"manifest_unreadable","error":str(error)}); blocked+=1; continue
        errors=[]
        if value.get("component") not in {name,"simplicio-"+name}: errors.append("component_identity_mismatch")
        if not value.get("version"): errors.append("version_missing")
        if not COMMIT.fullmatch(value.get("commit","")): errors.append("commit_missing_or_invalid")
        if not isinstance(value.get("compatibility"),dict): errors.append("compatibility_missing")
        if errors: rows.append({"component":name,"status":"BLOCKED","reason":"invalid_manifest","errors":errors}); blocked+=1
        else: rows.append({"component":name,"status":"MEASURED","reason":"manifest_valid","version":value["version"],"commit":value["commit"]}); measured+=1
    status="READY" if measured==len(COMPONENTS) and blocked==0 else ("BLOCKED" if blocked else "UNVERIFIED")
    print(json.dumps({"schema":"simplicio.release-train.compatibility-check/v1","status":status,"release_propagation_verified":False,"measured":measured,"blocked":blocked,"unverified":len(COMPONENTS)-measured-blocked,"components":rows},sort_keys=True))
    return 0 if status=="READY" else 1
if __name__=="__main__": raise SystemExit(main())
