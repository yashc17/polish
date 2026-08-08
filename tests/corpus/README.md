# The corpora

Real repositories, hand-labelled, used to measure the two claims Polish makes
about repositories it has never seen. There are two corpora here:

| | Measures | Floor | Enforced by |
|---|---|---|---|
| `*.json` | `firstparty.scan()` precision and recall | precision 1.00, recall ≥ 0.95 | `test_corpus.py` |
| `probe/*.json` | `probe.probe()` tier accuracy | **zero false Tier 1** | `test_probe_corpus.py` |

`python scripts/corpus_report.py` prints both on demand. The floors are the same
product decision applied one layer apart: a file wrongly called first-party and a
repository wrongly promoted to Tier 1 both mean Polish acting confidently on
something it cannot account for.

---

# The first-party detection corpus

Real repositories, hand-labelled, used to measure precision and recall of
`polish.firstparty.scan()`. `tests/test_corpus.py` fails the build if either
metric drops below its floor.

## What a manifest is

Each `*.json` is the *shape* of a real tree rather than a copy of it: every
path, and the contents of only those files detection actually reads
(`pyproject.toml`, `setup.cfg`, `.gitignore`, `pyvenv.cfg`, `polish.toml`, plus
the header of any `.py` carrying a generated-code banner). Everything else is a
path with empty contents.

This keeps a 3228-file repository under 300 KB, keeps the tests offline and
reproducible, and keeps other people's source out of this repository — while
still exercising the real directory layouts, the real ignore files and the real
packaging metadata that detection depends on.

```json
{
  "name": "pip",
  "source": "https://github.com/pypa/pip",
  "commit": "…",
  "note":  "why this repo is in the corpus",
  "label_rule": {
    "reason": "why the labels are what they are",
    "first_party": "every .py/.pyi file, minus not_first_party",
    "not_first_party": ["src/pip/_vendor/**"]
  },
  "expect_error": false,
  "first_party": ["…"],
  "files": {"path": "contents"}
}
```

## How the labels are made

**Labels never come from `scan()`.** A label derived from the code under test
measures nothing. The rule is stated by a human as globs, recorded in
`label_rule`, and applied by `scripts/capture_corpus.py`. A reviewer can check
any label by reading the rule and the prose reason next to it.

## What each repo is here for

| Repo | The case it covers |
|---|---|
| `requests` | Baseline. `src/` layout, `setup.py` and `pyproject.toml`, tests and docs. Nothing should be excluded. |
| `flask` | `src/` layout declared in `pyproject.toml`, containing a *nested* src-layout project under `examples/celery/`. |
| `pip` | Vendoring. 256 files in `src/pip/_vendor/`, git-tracked and inside pip's own declared package. Also a genuine subpackage named `build/` deep in the tree, which must survive. |
| `nanogpt` | No packaging metadata at all — no `pyproject.toml`, `setup.py` or `setup.cfg`. The research-repo shape SPEC §1 says needs Polish most. |
| `ml-scratch` | The case the spec was written around: 2459 of 2474 `.py` files are virtualenv (99.4%), and the venv is committed rather than gitignored. |
| `slugify-js` | A real repo with no Python in it. Polish must decline, not return an empty set. |

## Adding a repo

```bash
git clone --depth 1 https://github.com/owner/name /tmp/name
python scripts/capture_corpus.py /tmp/name \
    --name name --source https://github.com/owner/name \
    --out tests/corpus/name.json \
    --not-first-party 'vendor/**' \
    --note   "what makes this repo worth having" \
    --reason "why the labels are what they are"
```

Prefer a repo that breaks something. A corpus of tidy library repos measures
tidiness, not detection — which is what `test_the_corpus_covers_the_case_the_spec_was_written_around`
guards against.

## A caveat worth knowing

Several exclusions are over-determined. `ml-scratch`'s virtualenv is caught
three independent ways: `uv` writes a `.gitignore` containing `*` into `.venv`,
`site-packages` is an excluded name, and `pyvenv.cfg` is a marker. Any one of
them alone would give the same corpus score, so **the corpus cannot show that
each layer works** — only that the combination does. Per-layer behaviour is
pinned by `tests/test_scan.py`, where each case is built so exactly one signal
fires.

---

# The tier-probe corpus

`probe/*.json`. Same discipline, different measurement: does the probe put a
repository it has never seen in the right tier?

## What a probe manifest is

The repository's **shape** — either its own `files` block, or a `shape` naming a
detection manifest to reuse, so `requests` has one tree in this repository rather
than two that can drift — plus a **recording** of what the probe's commands
really did on a real machine:

```json
{
  "name": "flask",
  "shape": "flask",
  "captured_on": "darwin python3.13.1",
  "note": "why this repo is in the corpus",
  "label_rule": { "reason": "why the label is what it is", "expected_tier": 1 },
  "recorded": {
    "install":  { "code": 0, "timed_out": false, "output": "…" },
    "pytest":   { "code": 0, "timed_out": false, "output": "… 485 passed in 12.4s" },
    "pytest_available": true,
    "entrypoint": null,
    "unimportable": ["conf", "make_celery", "task_app"],
    "imported": 34
  },
  "observed_at_capture": { "tier": 0, "reason": "…", "gates": ["…"] }
}
```

The recording is keyed by what a command *means*, not by its `argv`. Gate 3
batches its imports, so raw `argv` keys would depend on `_gates.BATCH` and on
module ordering — change either and the whole corpus becomes cache misses that
quietly default to something. Per-module outcomes are lossless instead, because a
batch that passed settles every member and a batch that failed is re-run member
by member anyway.

A command with no recording **raises** rather than defaulting. An invented
outcome that happens to be a success is a fabricated Tier 1.

## Labels are still not recordings

`expected_tier` is stated by a human who read the repository, with a prose reason
beside it — the same rule as `not_first_party` above. `observed_at_capture` is
what the probe said at capture time and is **not** a label: a manifest whose
observed tier differs from its label is either a bug or a deliberately hard case,
and a reviewer should be able to tell which without running anything.

## What each repo is here for

| Repo | The case it covers | Label |
|---|---|---|
| `requests` | Baseline healthy repository: a green, self-contained suite needing no keys, no GPU, no services. | 1 |
| `flask` | Second healthy repository, and the corpus's **designed disagreement** — see below. | 1 |
| `instructor` | Needs API keys. Suite marked `integration: may require API keys`, `conftest.py` calls `load_dotenv()`, thirteen modules read `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`. Deps install; the suite collapses with eleven collection errors and zero passes. | 0 |
| `flash-attention` | Needs a GPU, **and has a suite** — so "cannot execute" is measured separately from "has nothing to execute". Fifty-one collection errors on a machine with no CUDA. | 0 |
| `nanogpt` | No test suite at all, and needs a GPU. The research-repo shape SPEC §1 says needs Polish most. | 0 |

## The flask row was meant to disagree, and it is what closed the gap

`flask` was labelled Tier 1 while the probe said Tier 0, on purpose. Its suite
passes 485 tests and every `flask.*` module imports — the code Polish would
actually refactor is characterizable. But its first-party set also holds the
example apps under `examples/celery/` and `docs/conf.py`, which need celery and
sphinx, and the probe's original verdict was **per repository**, so gate 3
demoted the whole thing for eight unimportable modules.

That gap was filed as evidence for [question
#4](https://github.com/yashc17/polish/issues/14), #4 answered in favour of
**per-symbol / per-path tiering**, and
[#28](https://github.com/yashc17/polish/issues/28) shipped it. So the label was
right all along and the row is now on the diagonal: a Tier 1 ceiling, 75 paths
at Tier 1, and 8 held at Tier 0 individually. **The label never changed**, which
is the point. A false Tier 0 has no ceiling precisely so that a row like this
can sit in the corpus and be argued about with data until the data wins.

Three of those eight demotions are under `src/flask/sansio/` — real library
code, demoted because Polish addresses a namespace subpackage by the wrong name
([#34](https://github.com/yashc17/polish/issues/34)), not because flask is at
fault. That is a false Tier 0 *per path*, invisible until the verdict got fine
enough to show it, and pinned by name in `tests/test_probe_corpus.py` so that
fixing #34 fails a test rather than passing silently.

## Adding a repo

```bash
git clone --depth 1 https://github.com/owner/name /tmp/name
python scripts/capture_probe_corpus.py /tmp/name \
    --name name --source https://github.com/owner/name \
    --out tests/corpus/probe/name.json \
    --expected-tier 0 \
    --note   "what makes this repo worth having" \
    --reason "why a human says this tier"
```

It builds a real virtualenv and really probes the tree, so it needs the network
and takes minutes. Add `--shape <name>` to reuse an existing detection manifest's
tree instead of capturing a second copy.

## The caveat that matters most

**A recording is one machine, one date, one Python.** `pip install flash-attn`
failing is a fact about a laptop with no NVIDIA GPU as much as about the
repository, and a CI runner with CUDA would record something else. That is the
price of having a repository which needs a GPU inside an offline test suite, and
it is worth paying — but it means the corpus proves the **gate logic**, not the
execution.

A runner mutated into a no-op would replay identically and every verdict would
survive. So the claims that are about execution actually happening —
timeouts that really expire, an environment the child really cannot see out of,
an allowlist that really refuses — live in `tests/test_probe.py`, against
repositories built on disk and really run. Neither file is sufficient alone.
