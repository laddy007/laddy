# Contributing

Thanks for your interest in improving this project — contributions are welcome, whether that's a bug fix, a doc improvement, a new target-policy template, or an idea you want to discuss first.

## Before you start

- **Open an issue before a large PR.** Small fixes (typos, obvious bugs, doc clarifications) can go straight to a PR. Anything that changes behavior, adds a feature, or touches the loop/merge/policy logic — open an issue first so we can agree on the approach before you invest time. This avoids wasted work on both sides.
- **Read [SECURITY.md](SECURITY.md) first.** This tool runs agents with `--dangerously-skip-permissions` and can push/merge unattended — understanding that model matters before you touch the code that drives it.

## Making a change

1. Fork the repo and create a branch off `main`.
2. Keep changes scoped — one coherent change per PR. Don't bundle an unrelated refactor with a bug fix.
3. **Add or update tests for any behavior change.** Untested behavior is treated as undefined; PRs that change logic without test coverage will be asked to add it before merge.
4. Run the test suite locally before opening the PR:
   ```bash
   python -m pytest -q
   ```
5. Commit messages: `<type>(<scope>): imperative summary` (e.g. `fix(loop): handle empty diff on resume`). Avoid `fix stuff` / `wip` / `misc`.
6. Open the PR against `main` with a description of *why*, not just *what* — link the issue it addresses.

## What we're looking for

- Bug fixes with a reproducing test.
- Documentation improvements (setup, troubleshooting, unclear behavior).
- Target-policy / gate templates for stacks other than the one shipped as an example.
- Portability improvements (this was extracted from a single-project setup — rough edges around assumptions are expected and welcome to fix).

## What we're cautious about

- Anything that weakens the security model (e.g., making `--dangerously-skip-permissions` implicit/default in a new code path, removing the merge tripwire, auto-merging without the sensitivity gate) needs a strong justification and will get careful review.
- New third-party dependencies — prefer the standard library where reasonable.
- Anything that could be read as implying official affiliation with Anthropic or OpenAI (see the trademark note in `README.md`).

## Legal / license

By submitting a contribution, you agree it is licensed under this project's [MIT License](LICENSE) (`LICENSE`), same as the rest of the codebase — no separate CLA is required.

## Reporting security issues

See [SECURITY.md](SECURITY.md).

## No warranty, no support obligation

This is an experimental, community-maintained tool provided as-is (see [LICENSE](LICENSE)). The maintainer reviews issues and PRs on a best-effort basis and is under no obligation to fix, support, or accept changes for any particular use case — especially ones arising from running the agent in `--dangerously-skip-permissions` mode against a target the user did not adequately isolate.