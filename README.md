# pulseai-releases
PulseAI desktop installers, checksums, update metadata and release notes; no application source code.

## Automatic candidate drafts

The private source repository builds Windows x64 and macOS arm64 installers on the existing self-hosted runners when a `desktop-v<version>` tag is pushed. Each tag must match the desktop package version and a first-parent commit on source `main`. Both native jobs inspect the package, launch its runtime, calculate checksums and upload a short-lived candidate artifact.

This repository's `Automatic desktop drafts` workflow checks successful tagged runs every ten minutes. It verifies the source tag, main history, package version, both jobs' attempt numbers, GitHub artifact digests, candidate provenance, packaged identity and every file's checksum. It handles the artifacts as data. It creates an unsigned candidate draft only after both platform bundles pass. Interrupted transfers resume by matching existing asset SHA-256 digests. Different bytes under an existing version require a new version.

Supported-release publication follows PRD 17's separate verification, feature-owner and operator gates. The automatic workflow leaves the release as a draft.

## One-time administration

1. Review and merge the private packaging workflow and this repository's uploader changes. Keep this repository's workflow changes under administrator control. Configure an active `main` ruleset with restricted updates, deletion protection and force-push protection, with bypass limited to the trusted repository administrators. Repository rules are available for this public repository on GitHub Free.
2. The artifact-reader App is initially owned by `@shutu2333`, as explicitly authorized on November 9. Use the settings in [app-manifest.json](app-manifest.json), with **Any account** visibility so it can be installed into `PulseAI-CRM`. Give it repository **Actions read** and **Contents read**, with no webhook events. Install it with access to **only `PulseAI-CRM/PulseAI`**. Create its private key. Its registration can later be transferred to the organization using GitHub's App ownership-transfer flow; recheck the installation, operator management access and token flow after transfer.
3. Create the `desktop-publisher` environment in this public repository. Choose **Selected branches and tags**, add only a **Branch** policy matching `main`, and verify no tag policy or other branch is allowed. Set this repository's `SOURCE_ARTIFACTS_APP_ID` Actions variable to the App ID. Store `SOURCE_ARTIFACTS_APP_KEY` as a secret of **only `desktop-publisher`**, using its PEM private key. Each publisher run obtains a short-lived source installation token that is revoked at job completion. Its release write access comes from this repository's own `GITHUB_TOKEN`. The protected environment keeps same-repository PR workflows from obtaining the private-source read key.
4. Configure the source repository's public HTTPS `PULSEAI_LOGIN_SERVER_URL`. Confirm the existing native runners are available. Keep the source's `desktop-validation` environment credential-free.
5. Set this repository's `PULSEAI_AUTO_DRAFT` variable to `true`. After a reviewed version bump has merged to source `main`, push its matching `desktop-v<version>` source tag. Check both native jobs and the automatic draft workflow. The optional `source_run_id` dispatch input retries a specific source run.

These changes use the organization Free plan. The public transfer job runs on GitHub's free public-repository Linux runner. Private candidate artifacts use the source repository's Actions storage quota and expire after one day. Before the first tagged run, record both platform artifact archive sizes, their combined size, current shared storage usage and the account's spending limit. Confirm the pair fits the available allowance at the approved spending limit, then retain evidence of the first real paired upload and transfer. Keep a zero-spend limit when paid overage is unwanted. The publisher may be delayed by GitHub's schedule and public scheduled workflows can be disabled after 60 days without repository activity. Re-enable the workflow in Actions if that occurs. App suspension or key revocation also stops transfer.

The source repository needs no cross-repository write credential or enforceable private tag ruleset for this path. A moved source tag is rejected, and an existing draft's source identity and uploaded asset hashes prevent replacement. The older direct-upload path remains subject to its own protected-tag and scoped-token setup requirements.

## Candidate retries

After a failed native build, use **Re-run all jobs** in the source workflow so both platform jobs and their artifacts come from the same successful attempt. A single-platform retry leaves the other platform's artifact on an earlier attempt, which the controller rejects. For an interrupted transfer, dispatch this repository's workflow with the original `source_run_id` while both artifacts remain available. Matching uploaded assets resume by checksum. Record the source run URL and attempt with the resulting draft's exact `Candidate transfer: complete` marker.

## Verification

Run `python3 -m unittest discover -s tests -v`. Tests use synthetic candidate bytes and mocked GitHub endpoints. They cover platform/provenance checks, failed or PR runs, changed tags and attempts, unsafe archives, corruption, interrupted uploads, immutable versions and credential-free download redirects. Real native builds and a live draft transfer are separate acceptance results.
