# rocket

[![CI](https://github.com/driftcell/rocket/actions/workflows/ci.yml/badge.svg)](https://github.com/driftcell/rocket/actions/workflows/ci.yml)

A Python CLI tool that declaratively downloads the Paper server and plugin JARs.

- Reads `server.toml` and downloads the Paper stable build for the declared Minecraft release;
- Downloads plugins as declared (providers: `hangar`, `github`, `modrinth`; resolvers: `auto`, `direct`, `github_release`);
- Generates a starter config interactively (`rocket init`), resolving recommended plugin versions;
- Finds plugins on Hangar and Modrinth by keyword (`rocket search`), one ranked list with
  cmd+clickable project links on a TTY;
- Adds and removes plugin entries without hand-editing TOML (`rocket add` / `rocket remove`);
- Concurrent downloads (up to 4 at once) with a single live progress bar on a TTY;
- Streaming download + upstream size/SHA-256 verification + ZIP/JAR validation + per-file atomic replace;
- Per-item results and a failure exit code; one failing item does not affect the others;
- Writes a `rocket.lock` pinning the resolved Paper build and plugin artifacts, reused by later runs.

**Scope of this iteration**: declare a version (or let `rocket init`/`rocket add` pick and pin one)
→ resolve artifacts → download and verify correctly, with `rocket.lock` pinning what was resolved.
No long-running service/automatic restart, automatic version upgrades, or automatic dependency
installation.

## Install

Install the CLI straight from GitHub with [uv](https://docs.astral.sh/uv/) (Python 3.13+; uv
downloads a managed interpreter automatically when needed):

```bash
uv tool install git+https://github.com/driftcell/rocket

# or pin a release tag for reproducible installs
uv tool install "git+https://github.com/driftcell/rocket@v0.4.0"
```

Then run it from anywhere (`--config` defaults to `./server.toml`):

```bash
rocket init                     # write a starter server.toml interactively
rocket download --config server.toml
rocket --version
```

Upgrade with `uv tool upgrade rocket`; uninstall with `uv tool uninstall rocket`. If the shell cannot
find `rocket` afterwards, run `uv tool update-shell` (or add `~/.local/bin` to `PATH`).

## Quick start (from a source checkout)

```bash
cd rocket
uv sync

# 1. Generate a starter config interactively (Paper version + recommended plugin versions),
#    or copy server.example.toml and edit it by hand
uv run python -m rocket init

# 2. Download (--config defaults to server.toml in the current working directory)
uv run python -m rocket download

# 3. Inspect the artifacts
ls artifacts artifacts/plugins
```

`artifacts/` holds the downloaded files; consider adding it to `.gitignore`. `rocket.lock` is written
next to the config file and should be committed so runs stay reproducible.

On success, stdout prints a short uv-style summary — `Resolved N items` and `Downloaded M items`
(or `Audited` when everything was already up to date) — followed by one line per downloaded item:
` + <file> (<version>, <resolution chain>)`. `-v/--verbose` also lists unchanged items and shows each
item's file path, size and checksum.

Minimal working config (`plugins` may be empty to download Paper only):

```toml
schema_version = 1

[paper]
version = "26.1.2"

[http]
user_agent = "rocket/0.1 (you@example.com)"   # must be replaced with real contact info

[plugins.viaversion]
provider = "hangar"
project = "ViaVersion/ViaVersion"
version = "5.12.1"
```

## Generating a config (`rocket init`)

`rocket init` asks a few questions and writes a starter `server.toml`:

1. **Server type** — `Paper` (the only supported type for now);
2. **Paper version** — picked from the live Fill version list, newest first (rc/pre builds are
   filtered out; when the list cannot be fetched, the version can be typed manually);
3. **Default plugins** — a curated list of common plugins, all preselected; uncheck the ones you
   do not want, or answer "no" to skip plugins entirely.

Every selected plugin is then resolved to the newest version the download pipeline can use
exactly: Hangar Release-channel versions that declare the chosen Minecraft version (including
external links to GitHub Release tag pages, with the right attachment selected), Modrinth release
versions listing it with a paper-family loader, and GitHub releases with a matching JAR
attachment — GitHub provides no Minecraft compatibility metadata, so the newest usable release is
used as-is. Plugins that cannot be resolved are reported as skipped and left out; the file is
still written.

```bash
uv run python -m rocket init                       # writes ./server.toml
uv run python -m rocket init --config my/server.toml --force
```

## Finding plugins (`rocket search`)

`rocket search <query>` searches Hangar and Modrinth by keyword and merges the hits into one
list ranked by downloads (five per provider at most):

```text
$ rocket search luckperms
  modrinth:luckperms · 3.0M downloads
    A permissions plugin/mod for Minecraft servers.

  LPC Minimessage - Chat Formatter — modrinth:lpc-chat · 88.8K downloads
    A chat formatting with Minimessage Support plugin for LuckPerms.

  hangar:jaapie_aapie/LuckpermsChatPlus · 503 downloads
    A more modern Luckperms chat formatter
```

Each block leads with the `provider:project` pair accepted by `rocket add`; when the display
name differs from the project slug it is shown first instead. On a TTY the title is a
cmd+clickable link to the project page and descriptions are truncated to the terminal width;
off a TTY the output is plain text with the same layout. The command needs no config file;
when one provider is unreachable the other's results are still printed and the exit code is
`1`. An empty result list is not an error.

## Adding and removing plugins (`rocket add` / `rocket remove`)

`rocket add` appends a `[plugins.<id>]` section to `server.toml` — hand-editing TOML is no longer
needed to try a plugin:

```bash
rocket add modrinth:luckperms            # resolve and pin the newest usable version
rocket add hangar:ViaVersion/ViaVersion --version 5.12.1   # pin an exact version
rocket add github:Zrips/Residence --id residence
rocket remove luckperms
```

- The spec is `<provider>:<project>`: a Modrinth slug, or `owner/name` for Hangar and GitHub.
- Without `--version`, the newest version the download pipeline can use exactly is resolved for the
  configured `paper.version` and pinned (the same rules `rocket init` applies). With `--version`,
  the exact version is verified against the provider before anything is written, so a typo fails
  fast with the same error a download would produce.
- `--asset` selects the upstream file when a release has several JARs; `--id` overrides the local
  plugin id (derived from the project name otherwise, lowercased, dots become hyphens).
- `rocket remove <id>` deletes the section; the downloaded JAR under `artifacts/plugins/` is kept
  on disk. Both commands rewrite only the plugin section and leave the rest of the file —
  comments and hand-written formatting included — untouched.

The generated file pins exact versions and uses `rocket/0.4.0 (https://github.com/driftcell/rocket)`
as `http.user_agent`; replace the contact with your own email or project URL if you prefer. An
existing file is never overwritten without `--force`. Example run:

```text
Server type: Paper
Paper version (newest first): 1.21.11
Add the default plugin set? Yes
Resolving 12 plugins for Paper 1.21.11...
Resolved 12 of 12 plugins for Paper 1.21.11
  + viaversion 5.12.1 (hangar)
  ...
wrote server.toml
next: rocket download
```

## Run from a source checkout

```bash
cd rocket
uv sync

# Option 1: module form
uv run python -m rocket download --config server.toml

# Option 2: installed entry point script
uv run rocket download --config server.toml
```

To use the workspace-root `server.toml`, run this from the repository root (relative paths are resolved
against the config file's directory):

```bash
uv run --project rocket python -m rocket download --config server.toml
```

`--config` defaults to `server.toml` in the current working directory.

## Config fields (server.toml)

| Field | Type and semantics |
| --- | --- |
| `schema_version` | integer, this version only accepts `1` |
| `paper.version` | required string, exact release version (not a version group, not a float) |
| `paper.channel` | optional, default `STABLE`; this version only accepts `STABLE` |
| `paper.build` | optional positive integer; when set, downloads exactly that stable build with no fallback |
| `http.user_agent` | required string identifying the software with a real contact email or project URL |
| `http.timeout_seconds` | optional positive number, default `30` |
| `http.retries` | optional non-negative integer, default `3` (at most 3 retries after the first attempt) |
| `download.output_dir` | optional string, default `./artifacts`; relative paths are resolved against the config file's directory |
| `plugin_defaults.platform` | default `PAPER`; this version only supports `PAPER` |
| `plugin_defaults.follow_external` | boolean, default `true` |
| `plugins.<id>` | locally unique plugin id: lowercase letters, digits, underscores, hyphens |
| `plugins.<id>.provider` | required, `hangar`, `github` or `modrinth` |
| `plugins.<id>.project` | required `owner/name` coordinates (interpreted by the provider) |
| `plugins.<id>.version` | required non-empty string; the version name for Hangar, an exact tag for GitHub |
| `plugins.<id>.asset` | optional, full filename of the upstream artifact, exact match (not the local output filename) |
| `plugins.<id>.resolver` | optional, default `auto`; may be explicitly set to `direct` or `github_release` |
| `plugins.<id>.platform` / `.follow_external` | override the corresponding defaults |

Constraints:

- All versions are strings; numeric input such as `version = 26.1` is rejected (exit code 2).
- `latest`, version ranges and automatic version selection are not supported; the `v` prefix on
  GitHub tags is neither added nor removed automatically.
- Unknown fields, unknown providers and unknown resolvers are not ignored; errors include the full field path.
- Put shared settings in `[plugin_defaults]`; `[plugins.<id>]` sections hold only individual plugin items.
- Local filenames are derived from the id (`plugins/<id>.jar`), not the remote filename, preventing
  path traversal and duplicate loading of old JARs after upgrades.

## Version semantics and the lock file (rocket.lock)

- Plugins are resolved by the declared exact version and artifact; when Paper `build` is not specified,
  the highest-id `STABLE` build within the exact Minecraft release version is chosen **once** and pinned.
- Every run that resolves something writes `rocket.lock` next to `server.toml` (atomically, and only
  when the content changed). It records the resolved Paper build and each resolved plugin artifact:
  the declared identity (provider/project/version/asset/resolver/follow_external), the download URL,
  the upstream size and checksum, and the resolution chain.
- A later run reuses matching lock entries and skips provider queries entirely; local files are still
  verified against the locked checksum and JAR structure. A changed config field invalidates only that
  entry, which is re-resolved and rewritten.
- `rocket download --upgrade` ignores the lock file, re-resolves every item (for example to move to a
  newer Paper build or a re-published GitHub tag), and rewrites the lock.
- A lock file that is malformed, has an unsupported `version`, or contains a broken entry is reported
  as a `lock warning`; the affected items are re-resolved and the lock is rewritten.
- `rocket.lock` is the only state file rocket reads or writes; there is no `server.lock.*` or hidden cache.

## Output

```
artifacts/
├── paper.jar
└── plugins/
    ├── viaversion.jar
    ├── essentialsx.jar
    └── essentialsx_chat.jar
```

Example success output:

```text
Resolved 3 items in 0.6s
Downloaded 2 items in 4.1s (1 unchanged)
 + paper.jar (1.21.11 build 148, fill)
 + viaversion.jar (5.12.1, hangar)
```

Downloaded items are printed as ` + <file> (<version>, <resolution chain>)`. Unchanged items are only
counted (`N unchanged`), and when nothing needs downloading the run reports `Audited N items` instead.
With `-v/--verbose`, unchanged items are listed with ` =`, every item gets a second line with its
target path, size and checksum (`sha256=...`/`sha512=...`) plus the upstream hash status
(`upstream hash verified` / `upstream hash unavailable`), and informational logs are enabled
(info-level messages are hidden by default). Phase timings are dimmed and `+` markers are green when
stdout is a TTY; set `NO_COLOR` to disable colors. Example resolution chains:

- `fill`: Paper → Fill API → direct link
- `hangar`: direct link to a Hangar hosted file
- `hangar → github-release → github-asset`: Hangar external link → GitHub Release → GitHub asset
- `github → github-release → github-asset`: native GitHub provider
- `modrinth`: direct link to a Modrinth official file (SHA-512 verification)

## Exit codes

| Exit code | Meaning |
| --- | --- |
| 0 | all succeeded or safely skipped |
| 1 | resolution, download, validation or lock-write failures occurred (other items keep processing) |
| 2 | invalid config (fails before any network request) |

| `rocket init` uses `0` when the file was written and `1` when the run was aborted (Ctrl-C) or
refused to overwrite an existing file (use `--force`). `rocket search` uses `0` when the search ran
(even with no results) and `1` when a provider failed.

## Re-runs and correctness

- A matching `rocket.lock` entry skips the provider query; the locked URL, size and checksum are used
  instead, and `--upgrade` is the only way an item is re-resolved.
- Resolution runs sequentially in config order; downloads then run concurrently (up to 4 at once).
  On a TTY a single progress bar shows completed items and transferred bytes and is erased when the
  phase ends; off a TTY it stays silent. Per-item result lines are always printed in config order.
- When the currently selected artifact has an upstream checksum (SHA-256 for Fill/Hangar/GitHub, SHA-512
  for Modrinth) and the existing target file passes hash and JAR validation: the download is skipped.
- Without an upstream hash: a file is not skipped just because it exists; it is re-downloaded to a
  temporary file and atomically replaced after validation.
- Download sequence: temporary file (same directory as the target, uniquely named) → stream and compute
  the actual hash → upstream size check → upstream hash check (SHA-256/SHA-512) → `zipfile` CRC/structure
  validation (Paper requires `META-INF/MANIFEST.MF` with a non-empty `Main-Class`; plugins require
  `plugin.yml` or `paper-plugin.yml` at the JAR root) → `os.replace` atomic replace.
- On failure, the temporary file is cleaned up and the original target file is kept; there is no
  whole-directory transaction, and files outside this config are never deleted.
- Only HTTPS URLs are accepted; auth headers are scoped per target domain, and `GITHUB_TOKEN` (optional
  environment variable) is only sent to `api.github.com`; no token is ever printed.

## Error handling

Every failure is printed to stderr as `error: <id>: <message> (hint: ...)` with an actionable fix
suggestion, distinguishing at least: config type/field errors, unknown providers/resolvers; project
mismatch, missing version/tag/build; no stable builds, missing download fields, external following
disabled; unsupported external links, unmatched or ambiguous attachments; HTTP/network/redirect
failures; size/hash mismatch, invalid JAR, local write failures. Error page content is displayed as
data only and is never executed.

## Dependencies

| Dependency | Purpose |
| --- | --- |
| `httpx` | HTTP client: timeouts, TLS verification, streaming reads, built-in redirect following (up to 5) |
| `tenacity` | bounded retries and exponential backoff (network errors, 429, 5xx; respects `Retry-After`, capped at 60 seconds) |
| `questionary` | interactive prompts for `rocket init` |
| `tomli-w` | writing server.toml from `rocket init` (reading still uses the standard library) |
| `tqdm` | the single download progress bar; disables itself off a TTY, so CI and scripts see plain output |
| `blessed` | terminal styling and OSC 8 hyperlinks for `rocket search`; disables itself off a TTY |
| standard library | `tomllib`, `zipfile`, `hashlib`, `tempfile`, `argparse`, `concurrent.futures`, etc. |

## Tests

Tests run fully offline by default (`httpx.MockTransport` + temporary directories + small JARs built in
memory); all test data is fictional:

```bash
cd rocket
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run ty check
```

The same commands run in CI (`.github/workflows/ci.yml`) on every pull request and push to `main`;
a pull request can only be merged once they pass.

## Live network smoke test (optional)

Before making real requests, replace the placeholder contact in `http.user_agent` with a real contact
email or project URL (Fill requires a User-Agent that identifies the software and includes contact info):

```bash
cd rocket
uv run python -m rocket download --config server.example.toml
```

The smoke test covers four chains: Paper → Fill, native Hangar (ViaVersion/ViaBackwards),
Hangar external link → GitHub Release (EssentialsX modules), and native Modrinth (CoreProtect CE).

## Known limitations

- Only the implemented resolution kinds are supported (`hangar` / `github` / `modrinth`), and resolving
  arbitrary websites is not promised; Patreon-style authorization pages and GitHub CE Releases without
  attachments fail with an explicit error, and no JAR URL is fabricated.
- CoreProtect CE is fetched via the Modrinth provider: its official GitHub CE Release has no attachments
  and its Hangar entry points to Patreon; this tool explicitly does not support those two chains.
- Modrinth requires an exact `version_number` and never queries latest; file hashes prefer the upstream
  SHA-512 (Modrinth provides no SHA-256), and when only sha1 is available the file is treated as having
  no strong checksum, with a warning.
- No resumable downloads or full on-disk caching; a retry re-downloads the entire file.
- The lock records upstream metadata only: an artifact without an upstream checksum (for example a GitHub
  Release whose asset has no `digest`) is re-downloaded and locally verified on every run.
- No dependency resolution or automatic installation; only required dependencies declared by Hangar are reported.
- When a GitHub Release `digest` is missing, it is treated as having no upstream hash; the JAR is still
  validated and the actual SHA-256 is printed.
- Metadata compatibility checks (Hangar `platformDependencies.PAPER` exactly containing the target
  version) are no substitute for actually starting the server.
- `rocket init` and `rocket add` (without `--version`) resolve recommendations once, at the moment
  they run: the written config pins exact versions, and later upstream changes do not affect it;
  GitHub-sourced plugins get the newest release with a usable JAR attachment because GitHub
  provides no Minecraft compatibility data.

## Code layout

```
src/rocket/
  __main__.py     python -m rocket entry point
  cli.py          download/init/search/add/remove subcommands, per-item results, exit codes
  wizard.py       init: interactive prompts, recommendations and server.toml output
  catalog.py      init: curated default plugin catalog
  recommend.py    init/add: newest usable version per plugin
  console.py      shared styling and error-printing helpers
  config.py       server.toml parsing and strict validation
  config_edit.py  add/remove: textual [plugins.<id>] section edits with round-trip checks
  models.py       shared models (entry/Release/Artifact/result)
  providers.py    Fill (Paper), Hangar, GitHub providers
  resolvers.py    Direct / GitHubRelease resolvers and auto selection
  github.py       GitHub Release client, attachment selection, URL classification
  modrinth.py     Modrinth API client, version/file selection
  http_client.py  synchronous HTTP client built on httpx + tenacity
  downloader.py   streaming download, validation and atomic replace
  lockfile.py     rocket.lock reading/writing, entry validation and matching support
  errors.py       error types and exit codes
tests/            offline tests (pytest + httpx.MockTransport)
```
