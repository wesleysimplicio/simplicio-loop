# Delivery contract

`simplicio.delivery-contract/v1` freezes client delivery restrictions before a
run mutates a repository. Unknown fields and wrong types are rejected. The
contract supports `open_pr`, `push_branch`, `allow_new_files_in_repo`,
`allow_comments_in_code`, and `commit_message_convention`.

Use `python scripts/pr_evidence.py build --local-report` when `open_pr=false`:
the evidence body is written locally and no GitHub API is called. The
`enforce_diff_contract` guard blocks forbidden new files and added code-comment
lines, with a deterministic verdict.
