# The first-party detection corpus

Real repositories, hand-labelled, used to measure precision and recall of
`polish.firstparty.scan()`. `tests/test_corpus.py` fails the build if either
metric drops below its floor; `python scripts/corpus_report.py` prints the same
numbers on demand.

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
