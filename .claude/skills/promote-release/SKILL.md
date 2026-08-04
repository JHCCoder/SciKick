# Promote an Accepted SciKick Release

Triggered when the Chrome Web Store accepts a submitted version. The user says
something like **"0.1.9 was accepted on the webstore"** — run the release-time
promotion: cut the tag, draft the GitHub release with the zip, and fast-forward
`main` to the accepted version. `main` is always the store-live version.

## Non-negotiables

- The GitHub release is created as a **DRAFT**. The user reviews/edits the notes
  and clicks Publish + Mark Latest. **Never publish automatically.**
- `main` is promoted via **fast-forward only**. If the FF fails (main diverged),
  STOP and ask — never force-push without explicit confirmation.
- Use `--notes-file <path>` (NOT `--notes @path` — that broke v0.1.5).
- Tags are cut at release time only, pointing at the release branch tip, and stay
  frozen after. Never re-tag or delete a pushed tag.

## Steps

1. **Identify the accepted version.** Parse `v?X.Y.Z` from the user's prompt if
   present. Otherwise find the highest `release/X.Y.Z` branch whose tip is not
   reachable from `origin/main` — that's the version awaiting acceptance.
   `git fetch origin` first.
2. **Verify it's the submitted state:**
   - The tag `vX.Y.Z` must NOT already exist (`git ls-remote --tags origin vX.Y.Z`).
   - The three version strings on that branch tip (`extension/manifest.json` and
     the two in `server/main.py`) must all equal `X.Y.Z`.
   - The submission zip exists on disk: `scikick-X.Y.Z.zip`.
3. **Cut the tag** at the release branch tip, then push it:
   ```bash
   git tag vX.Y.Z <release-branch-tip>
   git push origin vX.Y.Z
   ```
4. **Curate the release notes.** Gather commits between the previous tag and the
   release branch tip (`git log <prev-tag>..release/X.Y.Z --oneline`), group them
   into Features / Fixes, and write to a temp notes file. Keep the store-facing
   summary tight (this becomes the GitHub release body).
5. **Create the GitHub release as a DRAFT** with the zip attached:
   ```bash
   gh release create vX.Y.Z --draft --title "SciKick vX.Y.Z" \
     --notes-file <notes-file> scikick-X.Y.Z.zip
   ```
6. **Promote main** (fast-forward):
   ```bash
   git checkout main
   git pull origin main
   git merge --ff-only release/X.Y.Z
   git push origin main
   ```
   On FF failure: STOP, show the divergence, ask the user how to proceed.
7. **Report:** the tag, the draft release URL (`gh release view vX.Y.Z --json url`),
   the new `main` commit, and a reminder to review the draft and Publish + Mark Latest.

## Report

One paragraph: accepted version, tag pushed, draft release URL, `main` moved to
which commit, and the explicit next step (review + publish the draft). Note
anything you couldn't verify.
