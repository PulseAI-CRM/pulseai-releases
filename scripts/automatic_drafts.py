"""Transfer checked tagged candidates without executing private source or artifacts."""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


SOURCE = "PulseAI-CRM/PulseAI"
DESTINATION = "PulseAI-CRM/pulseai-releases"
WORKFLOW = "desktop-release.yml"
PLATFORMS = {"macos-arm64": ("darwin", "arm64"), "windows-x64": ("win32", "x64")}
VERSION = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
MAX_FILE = 2 * 1024**3
MAX_ARCHIVE = 3 * 1024**3
MAX_REPORT = 16 * 1024**2


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class GitHub:
    def __init__(self, repository, token):
        self.repository = repository
        self.token = token
        self.opener = build_opener(NoRedirect)

    def request(self, path, method="GET", data=None, headers=None, upload=False):
        host = "uploads.github.com" if upload else "api.github.com"
        url = f"https://{host}/repos/{self.repository}/{path}"
        request_headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PulseAI-candidate-uploader",
        }
        request_headers.update(headers or {})
        return self.opener.open(Request(url, data=data, method=method, headers=request_headers), timeout=120)

    def json(self, path, method="GET", data=None):
        payload = None if data is None else json.dumps(data).encode()
        with self.request(path, method, payload, {"Content-Type": "application/json"}) as response:
            return json.load(response)

    def pages(self, path, key=None):
        separator = "&" if "?" in path else "?"
        for page in range(1, 101):
            value = self.json(f"{path}{separator}per_page=100&page={page}")
            items = value[key] if key else value
            yield from items
            if len(items) < 100:
                return
        raise ValueError("GitHub inventory exceeds the supported pagination limit")

    def download(self, path, destination, expected_digest):
        # urllib normally forwards Authorization on redirects. Follow GitHub's
        # signed download URL ourselves, with no credential on the second request.
        try:
            response = self.request(path, headers={"Accept": "application/octet-stream"})
        except HTTPError as error:
            if error.code != 302:
                raise
            location = error.headers["Location"]
            parsed = urlsplit(location)
            if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.hostname or not parsed.hostname.endswith((".blob.core.windows.net", ".githubusercontent.com")):
                raise ValueError("Unexpected GitHub download destination") from None
            response = self.opener.open(Request(location), timeout=120)
        digest = hashlib.sha256()
        size = 0
        with response, destination.open("wb") as output:
            while chunk := response.read(1024**2):
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    raise ValueError("Candidate archive exceeds size limit")
                digest.update(chunk)
                output.write(chunk)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest or "") or expected_digest != f"sha256:{digest.hexdigest()}":
            raise ValueError("GitHub artifact digest does not match the downloaded archive")


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024**2):
            digest.update(chunk)
    return digest.hexdigest()


def tag_commit(source, tag):
    value = source.json(f"git/ref/tags/{quote(tag, safe='')}")["object"]
    for _ in range(8):
        if value["type"] == "commit" and re.fullmatch(r"[0-9a-f]{40}", value["sha"]):
            return value["sha"]
        if value["type"] != "tag":
            break
        value = source.json(f"git/tags/{value['sha']}")["object"]
    raise ValueError("Source tag does not resolve to a commit")


def verify_main_history(sha, token, directory):
    # Fetch commit history only, with no checkout, hooks or source execution.
    git_directory = directory / "source-history.git"
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
        "GIT_TERMINAL_PROMPT": "0",
    }
    subprocess.run(["git", "init", "--bare", "--quiet", str(git_directory)], check=True, env=env, capture_output=True)
    command = ["git", "--git-dir", str(git_directory)]
    subprocess.run(command + ["fetch", "--quiet", "--filter=blob:none", "--no-tags", f"https://github.com/{SOURCE}.git", "refs/heads/main"], check=True, env=env, capture_output=True)
    history = subprocess.run(command + ["rev-list", "--first-parent", "FETCH_HEAD"], check=True, env=env, capture_output=True, text=True).stdout.splitlines()
    if sha not in history:
        raise ValueError("Candidate commit is not a first-parent state of source main")


def verify_run(source, run, workflow_id):
    tag = run.get("head_branch", "")
    if run.get("event") != "push" or run.get("status") != "completed" or run.get("conclusion") != "success" or run.get("workflow_id") != workflow_id or run.get("repository", {}).get("full_name") != SOURCE:
        raise ValueError("Candidate must come from a successful source tag workflow")
    if not re.fullmatch(f"desktop-v{VERSION}", tag) or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")):
        raise ValueError("Invalid candidate tag or commit")
    sha = run["head_sha"]
    if tag_commit(source, tag) != sha:
        raise ValueError("Source tag moved after the candidate was built")
    package = source.json(f"contents/apps/desktop/package.json?ref={sha}")
    version = json.loads(base64.b64decode(package["content"]))["version"]
    if tag != f"desktop-v{version}":
        raise ValueError("Candidate version does not match source package.json")
    jobs = list(source.pages(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"))
    for platform in PLATFORMS:
        matches = [job for job in jobs if job["name"] == f"Native package ({platform})"]
        if len(matches) != 1 or matches[0]["conclusion"] != "success":
            raise ValueError("Both native platform jobs must succeed in the same attempt")
    return {"schemaVersion": 1, "version": version, "sha": sha, "tag": tag, "repository": SOURCE, "runId": str(run["id"]), "runAttempt": str(run["run_attempt"])}


def unpack_candidate(archive, destination, identity, platform):
    destination.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) > 20 or len(names) != len(set(names)):
            raise ValueError("Unexpected or duplicate candidate archive entries")
        total = 0
        for member in bundle.infolist():
            name = member.filename
            mode = member.external_attr >> 16
            if not re.fullmatch(r"[A-Za-z0-9_.+ -]+", name) or member.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise ValueError("Candidate archive contains a non-file or unsafe filename")
            limit = MAX_REPORT if name.endswith((".json", ".txt", ".yml")) else MAX_FILE
            total += member.file_size
            if member.file_size > limit or total > MAX_ARCHIVE:
                raise ValueError("Candidate files exceed size limits")
            with bundle.open(member) as source, (destination / name).open("xb") as output:
                shutil.copyfileobj(source, output)
    expected = {**identity, "platform": platform}
    candidate = json.loads((destination / "candidate.json").read_bytes())
    if candidate != expected:
        raise ValueError("Candidate provenance differs from the source workflow attempt")
    os_name, arch = PLATFORMS[platform]
    smoke = json.loads((destination / f"smoke-{platform}.json").read_bytes())
    build = smoke["build"]
    for key, value in {"version": identity["version"], "gitSha": identity["sha"], "dirty": False, "packaged": True, "platform": os_name, "arch": arch}.items():
        if build.get(key) != value or type(build.get(key)) is not type(value):
            raise ValueError("Packaged smoke identity does not match the candidate")
    manifest = json.loads((destination / f"package-manifest-{platform}.json").read_bytes())
    if manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("files"), list) or not manifest["files"]:
        raise ValueError("Missing package inspection evidence")
    checksum_name = f"SHA256SUMS-{platform}.txt"
    assets = {}
    for line in (destination / checksum_name).read_text().splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or match[2] in assets:
            raise ValueError("Invalid or duplicate candidate checksums")
        assets[match[2]] = match[1]
    pattern = r"(?:PulseAI[A-Za-z0-9_.+ -]*\.(?:dmg|zip)(?:\.blockmap)?|latest-mac\.yml)" if os_name == "darwin" else r"(?:PulseAI[A-Za-z0-9_.+ -]*\.exe(?:\.blockmap)?|latest\.yml)"
    required = [".dmg", ".zip", "latest-mac.yml"] if os_name == "darwin" else [".exe", "latest.yml"]
    if any(not any(name.endswith(suffix) for name in assets) for suffix in required):
        raise ValueError("Candidate lacks a required platform installer or metadata")
    for name, digest in assets.items():
        if not re.fullmatch(pattern, name) or name not in names or file_hash(destination / name) != digest:
            raise ValueError("Candidate asset name or checksum is invalid")
    reports = {checksum_name, f"smoke-{platform}.json", f"package-manifest-{platform}.json"}
    if set(names) != set(assets) | reports | {"candidate.json"}:
        raise ValueError("Unexpected files in candidate artifact")
    # candidate.json is private transfer provenance, not a public release asset.
    return {name: destination / name for name in sorted(set(assets) | reports)}


def assert_matching_release(release, identity):
    markers = [line for line in (release.get("body") or "").splitlines() if line.startswith("Source commit:")]
    if markers != [f"Source commit: {identity['sha']}"]:
        raise ValueError("Release version already belongs to another source commit")


def upload_draft(releases, release, identity, assets, recheck_source):
    if release is None:
        release = releases.json("releases", "POST", {
            "tag_name": identity["tag"], "target_commitish": "main", "name": f"PulseAI {identity['version']}", "draft": True,
            "body": "Unsigned team/demo candidate. Verification pending.\n\n"
                    f"Source commit: {identity['sha']}\n"
                    f"Source run: https://github.com/{SOURCE}/actions/runs/{identity['runId']}/attempts/{identity['runAttempt']}",
        })
    assert_matching_release(release, identity)
    if not release["draft"]:
        return
    existing = list(releases.pages(f"releases/{release['id']}/assets"))
    for name, path in assets.items():
        digest = f"sha256:{file_hash(path)}"
        matches = [asset for asset in existing if asset["name"] == name]
        if matches:
            if len(matches) == 1 and matches[0].get("state") == "starter" and matches[0].get("size") == 0 and not matches[0].get("digest"):
                current = releases.json(f"releases/{release['id']}")
                assert_matching_release(current, identity)
                if not current["draft"]:
                    raise ValueError("Release was published during candidate upload")
                # GitHub can leave an empty starter after HTTP 502. No installer
                # bytes exist in this record; remove only that failed placeholder.
                with releases.request(f"releases/assets/{matches[0]['id']}", "DELETE"):
                    pass
                matches = []
        if matches:
            if len(matches) != 1 or matches[0].get("state") != "uploaded" or matches[0].get("digest") != digest:
                raise ValueError("Existing release asset differs; use a new version instead of overwriting")
            continue
        # Recheck before every mutation so a concurrently published draft stops uploads.
        current = releases.json(f"releases/{release['id']}")
        assert_matching_release(current, identity)
        if not current["draft"]:
            raise ValueError("Release was published during candidate upload")
        with path.open("rb") as content:
            with releases.request(f"releases/{release['id']}/assets?{urlencode({'name': name})}", "POST", content,
                                  {"Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)}, upload=True) as response:
                uploaded = json.load(response)
        if uploaded.get("digest") != digest or uploaded.get("state") != "uploaded":
            raise ValueError("Uploaded release asset digest differs from the candidate")
    recheck_source()
    current = releases.json(f"releases/{release['id']}")
    assert_matching_release(current, identity)
    if not current["draft"]:
        raise ValueError("Release was published during candidate upload")
    releases.json(f"releases/{release['id']}", "PATCH", {
        "body": (current.get("body") or "") + "\n\nCandidate transfer: complete",
    })


def transfer(source, releases, run, workflow_id, release=None):
    identity = verify_run(source, run, workflow_id)
    if release:
        assert_matching_release(release, identity)
        if not release["draft"]:
            print(f"{identity['tag']} is already published")
            return
        if "Candidate transfer: complete" in (release.get("body") or "").splitlines():
            print(f"{identity['tag']} already has a complete candidate")
            return
    artifacts = list(source.pages(f"actions/runs/{run['id']}/artifacts", "artifacts"))
    selected = {}
    for platform in PLATFORMS:
        name = f"desktop-{platform}-{run['id']}-{run['run_attempt']}"
        matches = [artifact for artifact in artifacts if artifact["name"] == name]
        if not matches:
            print(f"{identity['tag']} has no transferable artifact for this attempt")
            return
        if len(matches) != 1 or matches[0].get("expired"):
            raise ValueError("Candidate artifact is ambiguous or expired; rebuild with a new attempt")
        selected[platform] = matches[0]
    with tempfile.TemporaryDirectory(prefix="pulseai-candidate-") as temporary:
        directory = Path(temporary)
        verify_main_history(identity["sha"], source.token, directory)
        assets = {}
        for platform, artifact in selected.items():
            archive = directory / f"{platform}.zip"
            source.download(f"actions/artifacts/{artifact['id']}/zip", archive, artifact.get("digest"))
            assets.update(unpack_candidate(archive, directory / platform, identity, platform))
        # Re-fetch run and ref after the potentially long downloads.
        def recheck_source():
            current = source.json(f"actions/runs/{run['id']}")
            if current.get("run_attempt") != run["run_attempt"] or verify_run(source, current, workflow_id) != identity:
                raise ValueError("Source run changed during candidate transfer")
        recheck_source()
        upload_draft(releases, release, identity, assets, recheck_source)
    print(f"Uploaded checked draft {identity['tag']}")


def main():
    source_token = os.environ.get("SOURCE_ARTIFACTS_TOKEN")
    release_token = os.environ.get("RELEASES_TOKEN")
    if not source_token or not release_token:
        raise ValueError("Configure the source read-only secret and release repository GITHUB_TOKEN")
    source = GitHub(SOURCE, source_token)
    releases = GitHub(DESTINATION, release_token)
    workflow_id = source.json(f"actions/workflows/{WORKFLOW}")["id"]
    run_id = os.environ.get("SOURCE_RUN_ID", "")
    if run_id and not re.fullmatch(r"[0-9]+", run_id):
        raise ValueError("Source run ID must be numeric")
    runs = [source.json(f"actions/runs/{run_id}")] if run_id else source.json(f"actions/workflows/{WORKFLOW}/runs?event=push&status=success&per_page=100")["workflow_runs"]
    inventory = list(releases.pages("releases"))
    seen = set()
    failures = 0
    for run in runs:
        tag = run.get("head_branch", "")
        if not re.fullmatch(f"desktop-v{VERSION}", tag) or tag in seen:
            if run_id:
                raise ValueError("Requested source run is not a desktop tag candidate")
            continue
        seen.add(tag)
        try:
            matches = [release for release in inventory if release["tag_name"] == tag]
            if len(matches) > 1:
                raise ValueError("Multiple releases for the candidate tag")
            transfer(source, releases, run, workflow_id, matches[0] if matches else None)
        except (ValueError, KeyError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
            failures += 1
            print(f"{tag} transfer failed ({type(error).__name__})")
    if failures:
        raise ValueError(f"{failures} candidate transfer(s) failed; other candidates were processed")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        # Exception URLs can contain signed download credentials. Log only safe summaries.
        detail = str(error) if type(error) is ValueError else type(error).__name__
        print(f"Candidate transfer failed ({detail})")
        raise SystemExit(1) from None
