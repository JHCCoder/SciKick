# Prepare SciKick for Chrome Web Store Submission

Run this when the user is ready to ship the current version to the Chrome Web
Store. It refreshes docs, confirms the version, zips the extension, and hands
back a fresh store description. Safe to run repeatedly.

## Release workflow context

- The **version being submitted** is whatever `extension/manifest.json` (and the
  two matching strings in `server/main.py`) currently say. That version gets a
  zip and goes to the store, then **waits for acceptance**. Do NOT change its
  version — that would break the package already being reviewed.
- After a version is submitted, the **next dev version** is bumped per Semantic
  Versioning (separate integers, NO rollover after 9):
  - **Patch** `0.1.9 → 0.1.10` — bug fixes and tiny compatible improvements
  - **Minor** `0.1.10 → 0.2.0` — meaningful new features or behavior
  - **Major** `1.3.1 → 2.0.0` — breaking changes or a major redesign
- On acceptance of a submitted version: push `main` to that version's commit,
  cut the tag, publish the GitHub release. Subsequent work accumulates on the
  newest dev version until it's ready to submit, then this cycle repeats.

## Steps

1. **Skim README.md against the actual feature set** (read the code: `server/`,
   `extension/`). Update:
   - **New features** shipped since the README was written → add to the feature
     list / key-features bullets / relevant sections.
   - **Stale text** → fix. Common drift: the supported-provider table (default
     model per provider), `LLM_PROVIDER` values in the Configuration section,
     model names in the Limitations section, tab-bar button descriptions.
   - Match existing voice and markdown style. Don't restructure.
2. **Check the store description** — `STORE_DESCRIPTION.md` is the canonical
   Web Store listing copy. If significant features shipped, update it too and
   surface the diff to the user. (Editing it does NOT need a new zip — the
   dashboard text is edited separately from the uploaded package.)
3. **Determine the current version** from `extension/manifest.json`. Verify all
   three version strings agree (manifest + `server/main.py` `version=` + the
   `/health` return).
   - If this is a **first submission of that version**: leave it as-is; build
     the zip for it.
   - If the user says to **roll onto the next dev version**: bump per the rules
     above (all three strings) and say what the new version is and why.
4. **Ensure the tree is committed and pushed** if the user asked to push. Stage
   ONLY the files you changed. Do not push unless told to. Do not make the
   private files (`.claude/settings.local.json`, `.claude/hooks-private/`)
   part of any commit.
5. **Build the submission zip** (flat layout, manifest at zip root, exclude
   `.DS_Store`), matching prior builds:
   ```bash
   cd extension && zip -r -X ../scikick-<VERSION>.zip . \
     -x '*.DS_Store' -x '.DS_Store' -x '*/.DS_Store' -x '._*'
   ```
   Verify with `unzip -l` that `manifest.json` is at the root, there are no
   `.DS_Store` entries, and the in-zip manifest version is the submitted one.
6. **Give a fresh store description** if there were significant features — a
   short paragraph highlighting what's new, ready to paste into the dashboard
   (or an updated `STORE_DESCRIPTION.md`).

## Report

End with a one-paragraph summary: the submitted version, the zip path + size,
the README/description changes, any version bump and its semver rationale, the
commit hash(es) (if committed), and confirmation the zip's manifest version
matches. Note anything you couldn't verify.
