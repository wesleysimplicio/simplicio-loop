"""Validate a provider proposal and compile a bounded native edit plan (no apply)."""
import argparse
import json
from pathlib import Path


def compile_plan(proposal, root, allowed):
    if proposal.get("status") != "response_received":
        raise ValueError("provider response unavailable")
    response = proposal["response"]
    choice = response["choices"][0]
    if choice.get("finish_reason") != "stop":
        raise ValueError("provider response incomplete")
    body = json.loads(choice["message"]["content"])
    files = body.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("proposal missing nonempty files object")
    if set(files) - set(allowed):
        raise ValueError("proposal escaped task write set")
    rows = []
    for name, content in files.items():
        if not isinstance(content, str):
            raise ValueError("file contents must be text")
        path = root / name
        if Path(name).is_absolute() or ".." in Path(name).parts or path.is_symlink():
            raise ValueError("unsafe target")
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("target outside fixture")
        if name.endswith(".py"):
            compile(content, name, "exec")
        operation = ({"op": "replace", "find": path.read_text(), "with": content}
                     if path.exists() else {"op": "create", "text": content})
        rows.append({"file": name, "operations": [operation]})
    if len(rows) != 1:
        raise ValueError("this bridge admits exactly one file; multi-file plans need explicit composition")
    return rows[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--allow", action="append", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        parser.error("preserve existing edit plan")
    plan = compile_plan(json.loads(Path(args.proposal).read_text()), Path(args.repo), args.allow)
    output.write_text(json.dumps(plan, indent=2))
    print(json.dumps({"status": "compiled_not_applied", "files": args.allow, "plan": str(output)}))


if __name__ == "__main__":
    main()
