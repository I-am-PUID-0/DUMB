# Development version calculator

This package calculates the next DUMB development source version. It does not
write files, commits, refs, pull requests, tags or releases. Publishing belongs
to the calling workflow.

Install the locked dependencies without lifecycle scripts and run the tests:

    npm ci --ignore-scripts
    npm test

From the repository root, with a GitHub read token already present in
GH_TOKEN or GITHUB_TOKEN:

    node scripts/dev-version/calculate.mjs \
      --repository example/DUMB \
      --head <full-commit-sha>

The checkout must contain complete history, release tags and
refs/remotes/origin/master. A caller can pass --repo-dir and --master-ref
explicitly. The source head is immutable; it does not need to be a branch tip.

The single JSON result contains sourceHead, stableVersion, stableTag, stableSha,
masterSha, cutoffSha, candidateVersion, coreVersion, displayCount, commitCount
and releasePleaseVersion. Versions are plain stable X.Y.Z values. The caller
can combine coreVersion and displayCount into X.Y.Z-dev.N for build metadata.
The released-version manifest and current source version are never calculator
inputs.

The baseline is the highest stable published release whose API-resolved tag
commit is reachable from master. Drafts and prereleases are excluded. Local
peeled tags must match the API. The comparison cutoff is the unique merge base
of that release commit and the requested source head. This permits a normal
master release commit that has not yet been merged into dev. Squash or rewritten
promotion histories can retain old feature commits outside the shared history;
synchronize the branches before relying on their next-version calculation.

Release Please 17.6.0 supplies the conventional-commit parser and default
versioning strategy. It handles breaking markers, features, nested/squash
messages, Release-As footers and associated PR body overrides. Empty,
unrecognized, documentation-only or chore-only changes produce the next patch.
An override that is not a stable version newer than the baseline is rejected.

The GitHub transport permits only this repository's read endpoints and the
pinned SDK history query. It reads the requested commit object, rather than a
moving branch ref, and retains the SDK's PR normalization through a narrow
response-envelope adapter. Tests exercise the actual SDK query. Missing history,
ambiguous merge bases, shallow clones, unexpected query shapes and explicit
scan-limit exhaustion fail instead of producing a partial plan.

Default bounds are 20,000 scanned commits and 1,000 releases; --max-commits and
--max-releases can set explicit higher bounds up to 100,000 and 10,000. Tokens,
API error bodies and response headers are excluded from output.
