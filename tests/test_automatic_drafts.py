import base64
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile


spec = importlib.util.spec_from_file_location("uploader", Path(__file__).parents[1] / "scripts/automatic_drafts.py")
uploader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uploader)

IDENTITY = {"schemaVersion": 1, "version": "0.1.0", "sha": "a" * 40, "tag": "desktop-v0.1.0", "repository": uploader.SOURCE, "runId": "123", "runAttempt": "2"}
RUN = {"id": 123, "run_attempt": 2, "event": "push", "status": "completed", "conclusion": "success", "workflow_id": 55,
       "repository": {"full_name": uploader.SOURCE}, "head_branch": IDENTITY["tag"], "head_sha": IDENTITY["sha"]}


def bundle_files(platform):
    os_name, arch = uploader.PLATFORMS[platform]
    assets = {"PulseAI-Setup-0.1.0.exe": b"synthetic windows", "latest.yml": b"synthetic metadata"} if os_name == "win32" else {
        "PulseAI-0.1.0.dmg": b"synthetic dmg", "PulseAI-0.1.0.zip": b"synthetic zip", "latest-mac.yml": b"synthetic metadata",
    }
    sums = "".join(f"{hashlib.sha256(value).hexdigest()}  {name}\n" for name, value in assets.items())
    return {**assets,
            "candidate.json": json.dumps({**IDENTITY, "platform": platform}).encode(),
            f"SHA256SUMS-{platform}.txt": sums.encode(),
            f"smoke-{platform}.json": json.dumps({"build": {"version": "0.1.0", "gitSha": IDENTITY["sha"], "dirty": False, "packaged": True, "platform": os_name, "arch": arch}}).encode(),
            f"package-manifest-{platform}.json": b'{"schemaVersion":1,"files":[{"path":"app.asar"}]}'}


def archive(path, files):
    with zipfile.ZipFile(path, "w") as bundle:
        for name, value in files.items():
            bundle.writestr(name, value)


class Source:
    token = "test-read-only"

    def json(self, path):
        if path.startswith("git/ref/tags/"):
            return {"object": {"type": "commit", "sha": IDENTITY["sha"]}}
        if path.startswith("contents/apps/desktop/package.json"):
            return {"content": base64.b64encode(b'{"version":"0.1.0"}').decode()}
        if path == "actions/runs/123":
            return deepcopy(RUN)
        raise AssertionError(path)

    def pages(self, path, key=None):
        if path == "actions/runs/123/attempts/2/jobs":
            return [{"name": f"Native package ({platform})", "conclusion": "success"} for platform in uploader.PLATFORMS]
        if path == "actions/runs/123/artifacts":
            return [{"id": index, "name": f"desktop-{platform}-123-2", "expired": False, "digest": "sha256:test"} for index, platform in enumerate(uploader.PLATFORMS)]
        raise AssertionError(path)

    def download(self, path, destination, digest):
        platform = list(uploader.PLATFORMS)[int(path.split("/")[2])]
        archive(destination, bundle_files(platform))


class Releases:
    def __init__(self):
        self.release = None
        self.assets = []
        self.writes = []
        self.interrupt_once = False

    def json(self, path, method="GET", data=None):
        if method == "POST" and path == "releases":
            self.writes.append((method, path))
            self.release = {"id": 1, **data}
            return deepcopy(self.release)
        if path == "releases/1" and method == "GET":
            return deepcopy(self.release)
        if path == "releases/1" and method == "PATCH":
            self.writes.append((method, path))
            self.release.update(data)
            return deepcopy(self.release)
        raise AssertionError((path, method))

    def pages(self, path):
        assert path == "releases/1/assets"
        return deepcopy(self.assets)

    def request(self, path, method, data=None, headers=None, upload=False):
        if method == "DELETE":
            asset_id = int(path.split("/")[-1])
            self.assets = [asset for asset in self.assets if asset.get("id") != asset_id]
            self.writes.append(("delete", asset_id))
            return io.BytesIO()
        assert upload and method == "POST" and path.startswith("releases/1/assets?name=")
        from urllib.parse import parse_qs
        name = parse_qs(path.split("?", 1)[1])["name"][0]
        value = data.read()
        assert len(value) == int(headers["Content-Length"])
        asset = {"name": name, "state": "uploaded", "digest": f"sha256:{hashlib.sha256(value).hexdigest()}"}
        self.assets.append(asset)
        self.writes.append(("upload", name))
        if self.interrupt_once:
            self.interrupt_once = False
            raise OSError("response lost after upload")
        return io.BytesIO(json.dumps(asset).encode())


class CandidateTests(unittest.TestCase):
    def test_both_platforms_are_validated_before_any_draft_write(self):
        source, releases = Source(), Releases()
        with patch.object(uploader, "verify_main_history"):
            uploader.transfer(source, releases, RUN, 55)
        self.assertEqual(len(releases.assets), 11)
        self.assertTrue(releases.release["draft"])
        self.assertIn("Candidate transfer: complete", releases.release["body"])
        self.assertFalse(any(asset["name"] == "candidate.json" for asset in releases.assets))
        with patch.object(source, "download", side_effect=AssertionError("must not download again")):
            uploader.transfer(source, releases, RUN, 55, releases.release)

    def test_interrupted_upload_resumes_without_duplicate_or_overwrite(self):
        source, releases = Source(), Releases()
        releases.interrupt_once = True
        with patch.object(uploader, "verify_main_history"):
            with self.assertRaises(OSError):
                uploader.transfer(source, releases, RUN, 55)
            self.assertNotIn("Candidate transfer: complete", releases.release["body"])
            uploader.transfer(source, releases, RUN, 55, releases.release)
        self.assertEqual(len(releases.assets), 11)
        self.assertEqual(len([entry for entry in releases.writes if entry == ("POST", "releases")]), 1)

    def test_different_existing_bytes_block_the_retry(self):
        releases = Releases()
        releases.release = {"id": 1, "draft": True, "body": f"Source commit: {IDENTITY['sha']}"}
        releases.assets = [{"name": "PulseAI-0.1.0.dmg", "state": "uploaded", "digest": "sha256:" + "0" * 64}]
        with patch.object(uploader, "verify_main_history"), self.assertRaisesRegex(ValueError, "differs"):
            uploader.transfer(Source(), releases, RUN, 55, releases.release)
        self.assertFalse(releases.writes)

    def test_empty_failed_upload_placeholder_is_recovered(self):
        releases = Releases()
        releases.release = {"id": 1, "draft": True, "body": f"Source commit: {IDENTITY['sha']}"}
        releases.assets = [{"id": 88, "name": "PulseAI-0.1.0.dmg", "state": "starter", "size": 0, "digest": None}]
        with patch.object(uploader, "verify_main_history"):
            uploader.transfer(Source(), releases, RUN, 55, releases.release)
        self.assertIn(("delete", 88), releases.writes)
        self.assertEqual(len(releases.assets), 11)
        self.assertIn("Candidate transfer: complete", releases.release["body"])

    def test_pr_failed_workflow_wrong_version_or_moved_tag_never_uploads(self):
        for change in [{"event": "pull_request"}, {"conclusion": "failure"}, {"workflow_id": 99}, {"head_branch": "main"}, {"head_sha": "b" * 40}, {"head_branch": "desktop-v0.2.0"}]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    uploader.verify_run(Source(), {**RUN, **change}, 55)

    def test_main_and_second_platform_checks_fail_before_mutation(self):
        releases = Releases()
        with patch.object(uploader, "verify_main_history", side_effect=ValueError("not main")), self.assertRaises(ValueError):
            uploader.transfer(Source(), releases, RUN, 55)
        self.assertFalse(releases.writes)
        source = Source()
        original = source.download

        def corrupt(path, destination, digest):
            original(path, destination, digest)
            if "windows" in destination.name:
                files = bundle_files("windows-x64")
                files["PulseAI-Setup-0.1.0.exe"] = b"changed"
                archive(destination, files)

        with patch.object(uploader, "verify_main_history"), patch.object(source, "download", side_effect=corrupt), self.assertRaisesRegex(ValueError, "checksum"):
            uploader.transfer(source, releases, RUN, 55)
        self.assertFalse(releases.writes)

    def test_run_attempt_change_during_download_blocks_creation(self):
        source, releases = Source(), Releases()
        original = source.json

        def changed(path):
            return {**RUN, "run_attempt": 3} if path == "actions/runs/123" else original(path)

        with patch.object(uploader, "verify_main_history"), patch.object(source, "json", side_effect=changed), self.assertRaises(ValueError):
            uploader.transfer(source, releases, RUN, 55)
        self.assertFalse(releases.writes)

    def test_published_or_wrong_commit_releases_are_never_mutated(self):
        releases = Releases()
        releases.release = {"id": 1, "draft": False, "body": f"Source commit: {IDENTITY['sha']}"}
        uploader.transfer(Source(), releases, RUN, 55, releases.release)
        self.assertFalse(releases.writes)
        with self.assertRaises(ValueError):
            uploader.transfer(Source(), releases, RUN, 55, {"draft": True, "body": "Source commit: wrong"})

    def test_unsafe_archive_entries_wrong_provenance_and_extra_files_are_rejected(self):
        platform = "windows-x64"
        for name, change in [
            ("traversal", {"../escape.exe": b"x"}),
            ("extra source", {"customer.md": b"private"}),
            ("wrong run", {"candidate.json": json.dumps({**IDENTITY, "runAttempt": "1", "platform": platform}).encode()}),
            ("wrong smoke", {f"smoke-{platform}.json": b'{"build":{}}'}),
        ]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive(root / "candidate.zip", {**bundle_files(platform), **change})
                with self.assertRaises(ValueError):
                    uploader.unpack_candidate(root / "candidate.zip", root / "out", IDENTITY, platform)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with zipfile.ZipFile(root / "candidate.zip", "w") as bundle:
                symlink = zipfile.ZipInfo("link.exe")
                symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
                bundle.writestr(symlink, "target")
            with self.assertRaises(ValueError):
                uploader.unpack_candidate(root / "candidate.zip", root / "out", IDENTITY, platform)

    def test_private_authorization_is_not_forwarded_to_download_host(self):
        github = uploader.GitHub(uploader.SOURCE, "source-secret")
        data = b"synthetic archive"
        redirect = HTTPError("https://api.github.com/private", 302, "Found", {"Location": "https://test.blob.core.windows.net/archive?sig=secret"}, None)
        with tempfile.TemporaryDirectory() as temporary, patch.object(github, "request", side_effect=redirect), patch.object(github.opener, "open", return_value=io.BytesIO(data)) as open_request:
            github.download("actions/artifacts/1/zip", Path(temporary) / "archive", f"sha256:{hashlib.sha256(data).hexdigest()}")
            request = open_request.call_args.args[0]
            self.assertFalse(request.has_header("Authorization"))
        with tempfile.TemporaryDirectory() as temporary, patch.object(github, "request", return_value=io.BytesIO(data)), self.assertRaisesRegex(ValueError, "digest"):
            github.download("actions/artifacts/1/zip", Path(temporary) / "archive", "sha256:" + "0" * 64)

    def test_main_history_uses_real_first_parent_and_keeps_credentials_out_of_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()

            def git(*args):
                return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *args], cwd=repository, check=True, capture_output=True, text=True).stdout.strip()

            git("init", "-b", "main")
            git("commit", "--allow-empty", "-m", "base")
            git("checkout", "-b", "topic")
            git("commit", "--allow-empty", "-m", "topic")
            topic = git("rev-parse", "HEAD")
            git("checkout", "main")
            git("merge", "--no-ff", "topic", "-m", "merge")
            main = git("rev-parse", "HEAD")
            real_run = subprocess.run

            def local_fetch(arguments, **kwargs):
                self.assertFalse(any("source-secret" in argument for argument in arguments))
                arguments = [str(repository) if argument == f"https://github.com/{uploader.SOURCE}.git" else argument for argument in arguments]
                return real_run(arguments, **kwargs)

            for sha, expected in [(main, True), (topic, False)]:
                destination = root / sha
                destination.mkdir()
                with patch.object(uploader.subprocess, "run", side_effect=local_fetch):
                    if expected:
                        uploader.verify_main_history(sha, "source-secret", destination)
                    else:
                        with self.assertRaisesRegex(ValueError, "first-parent"):
                            uploader.verify_main_history(sha, "source-secret", destination)

    def test_a_rejected_candidate_does_not_starve_older_valid_runs(self):
        source, releases = Source(), Releases()
        original_json = source.json

        def runs(path):
            if path == f"actions/workflows/{uploader.WORKFLOW}":
                return {"id": 55}
            if path.startswith(f"actions/workflows/{uploader.WORKFLOW}/runs?"):
                return {"workflow_runs": [{**RUN, "id": 124, "head_branch": "desktop-v0.2.0", "head_sha": "b" * 40}, RUN]}
            return original_json(path)

        original_pages = releases.pages
        with patch.object(uploader, "GitHub", side_effect=[source, releases]), patch.object(source, "json", side_effect=runs), \
                patch.object(releases, "pages", side_effect=lambda path: [] if path == "releases" else original_pages(path)), \
                patch.object(uploader, "verify_main_history"), patch.dict(os.environ, {"SOURCE_ARTIFACTS_TOKEN": "source", "RELEASES_TOKEN": "destination", "SOURCE_RUN_ID": ""}), \
                self.assertRaisesRegex(ValueError, "1 candidate"):
            uploader.main()
        self.assertEqual(len(releases.assets), 11)
        self.assertIn("Candidate transfer: complete", releases.release["body"])


if __name__ == "__main__":
    unittest.main()
