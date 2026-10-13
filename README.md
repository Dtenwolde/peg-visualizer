# DuckDB PEG parser visualizer

Shows how DuckDB's PEG matcher works through the grammar for a query: every rule that is attempted, the
alternatives of each choice with their status, the resulting parse tree, and what the grammar expected at a syntax
error. The parser is a Python mirror of DuckDB's matcher (`src/parser/peg` in duckdb/duckdb) and reads the grammar
`.gram` files directly, so no DuckDB build is needed.

The hosted version runs the parser in the browser (Pyodide). It is rebuilt by the GitHub Actions workflow in
`.github/workflows/deploy.yml` every night and on every push, taking the grammar from the `v2.0-cyanoptera` branch of
duckdb/duckdb (change `DUCKDB_REF` in the workflow, or pick another ref when running it by hand from the Actions tab).
The page header shows which grammar commit the site was built from.

## Running locally

```bash
python3 server.py --grammar /path/to/duckdb/src/include/duckdb/parser/peg/inlined_grammar.gram
# http://127.0.0.1:8765/
```

Locally the server reloads the grammar whenever the file changes, and if a `duckdb` binary is found (`--duckdb PATH`,
`$DUCKDB_BINARY`, or `build/*/duckdb` in the DuckDB checkout) every query is also cross-checked against the real
parser. `server.py --sql "SELECT 1"` prints the trace and parse tree on the command line.

## Building the static site by hand

```bash
python3 build_static.py --grammar /path/to/inlined_grammar.gram --out dist
python3 -m http.server --directory dist 8000
```

## Publishing

GitHub Pages must be set to deploy from GitHub Actions once: repository **Settings → Pages → Source: GitHub Actions**.
After that, every push to `main` and the nightly schedule publish the site.

This code is developed inside the DuckDB repository as `scripts/parser/peg_visualizer`; this repository is the
hosting copy.
