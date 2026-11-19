import stat
from datetime import UTC, datetime, timedelta
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
import pytest

from refactra import repository_snapshot as snapshots
from refactra.auth_sessions import GitHubIdentity
from refactra.github_app import GitHubAppError, InstallationToken
from refactra.models import Run
from refactra.repository_revision import RepositoryRevision
from refactra.repository_runs import create_repository_run

SHA = "a" * 40


@pytest.fixture
def run():
    return Run(github_repository_id=42, base_sha=SHA, workflow_version="repository-v0.1")


@pytest.fixture
def token():
    return InstallationToken("fixture-secret", 42, datetime.now(UTC) + timedelta(minutes=10))


def zip_bytes(entries):
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in entries:
            if isinstance(name, str):
                # ZipInfo normalizes backslashes on Windows; preserve hostile fixture names.
                raw_name = name
                name = ZipInfo(name)
                name.filename = name.orig_filename = raw_name
                name.compress_type = ZIP_DEFLATED
            archive.writestr(name, content)
    return buffer.getvalue()


def transport_for(data, *, location="https://codeload.github.com/fixture/example/zip/" + SHA):
    def handler(request):
        if request.url.host == "api.github.com":
            assert request.url.path == f"/repositories/42/zipball/{SHA}"
            assert request.headers["Authorization"] == "Bearer fixture-secret"
            return httpx.Response(302, headers={"Location": location})
        assert request.url.host == "codeload.github.com"
        assert "Authorization" not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(data))

    return httpx.MockTransport(handler)


def test_snapshot_uses_the_persisted_sha_and_skips_generated_directories(session_factory, token):
    with session_factory.begin() as session:
        run_id = create_repository_run(
            session,
            identity=GitHubIdentity(123, "fixture-user"),
            installation_id=77,
            revision=RepositoryRevision(42, "fixture/example", "main", "develop", SHA),
            task_text="Extract logic",
        ).id
    data = zip_bytes(
        [
            ("root/", b""),
            ("root/src/main.py", b"print('source only')"),
            ("root/node_modules/lib.js", b"skip"),
            ("root/dist/output.js", b"skip"),
            ("root/README.md", b"example"),
        ]
    )
    with session_factory() as session:
        snapshot = snapshots.fetch_source_snapshot(
            session.get(Run, run_id), token, transport=transport_for(data)
        )
    assert (snapshot.repository_id, snapshot.commit_sha) == (42, SHA)
    assert [(file.path, file.content) for file in snapshot.files] == [
        ("README.md", b"example"),
        ("src/main.py", b"print('source only')"),
    ]
    assert "print" not in repr(snapshot)


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "root/../escape",
        "root/./file",
        "root//file",
        "root/C:/file",
        "root/back\\slash",
        "root/control\nfile",
    ],
)
def test_unsafe_paths_fail_without_returning_a_partial_snapshot(run, token, path):
    with pytest.raises(GitHubAppError):
        snapshots.fetch_source_snapshot(
            run, token, transport=transport_for(zip_bytes([("root/ok", b"ok"), (path, b"bad")]))
        )


@pytest.mark.parametrize(
    "entries",
    [
        [("root/a", b"1"), ("other/b", b"2")],
        [("root/file", b"1"), ("root/file/", b"")],
        [("unwrapped-file", b"1")],
    ],
)
def test_inconsistent_archive_structure_is_rejected(run, token, entries):
    with pytest.raises(GitHubAppError):
        snapshots.fetch_source_snapshot(run, token, transport=transport_for(zip_bytes(entries)))


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFBLK])
def test_links_and_special_entries_are_rejected_even_in_skipped_directories(run, token, kind):
    entry = ZipInfo("root/node_modules/link")
    entry.create_system, entry.external_attr = 3, (kind | 0o777) << 16
    with pytest.raises(GitHubAppError):
        snapshots.fetch_source_snapshot(
            run, token, transport=transport_for(zip_bytes([(entry, b"../../target")]))
        )


@pytest.mark.parametrize(
    "limit,value",
    [
        ("MAX_ARCHIVE_BYTES", 1),
        ("MAX_FILE_BYTES", 3),
        ("MAX_SOURCE_BYTES", 7),
        ("MAX_FILES", 1),
    ],
)
def test_archive_file_total_and_count_limits(run, token, monkeypatch, limit, value):
    monkeypatch.setattr(snapshots, limit, value)
    with pytest.raises(GitHubAppError):
        snapshots.fetch_source_snapshot(
            run,
            token,
            transport=transport_for(zip_bytes([("root/a", b"1234"), ("root/b", b"5678")])),
        )


def test_oversized_stream_stops_before_reading_the_rest(run, token, monkeypatch):
    closed = []

    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * (64 * 1024)
            pytest.fail("Download must stop as soon as the byte limit is exceeded")

        def close(self):
            closed.append(True)

    redirect = transport_for(b"")

    def handler(request):
        if request.url.host == "api.github.com":
            return redirect.handle_request(request)
        return httpx.Response(200, stream=OversizedStream())

    monkeypatch.setattr(snapshots, "MAX_ARCHIVE_BYTES", 1024)
    with pytest.raises(GitHubAppError):
        snapshots.fetch_source_snapshot(run, token, transport=httpx.MockTransport(handler))
    assert closed == [True]


@pytest.mark.parametrize(
    "location",
    [
        "http://codeload.github.com/archive",
        "https://evil.example/archive",
        "https://codeload.github.com.evil.example/archive",
        "https://user@codeload.github.com/archive",
    ],
)
def test_untrusted_redirect_is_rejected_before_following(run, token, location):
    with pytest.raises(GitHubAppError):
        snapshots.fetch_source_snapshot(run, token, transport=transport_for(b"", location=location))


def test_wrong_repository_expired_token_and_mock_run_never_reach_network(run, token):
    def unexpected(request):
        pytest.fail("Invalid inputs must be rejected before network access")

    for candidate in (
        InstallationToken(token.value, 99, token.expires_at),
        InstallationToken(token.value, 42, datetime.now(UTC) - timedelta(seconds=1)),
    ):
        with pytest.raises(GitHubAppError):
            snapshots.fetch_source_snapshot(
                run, candidate, transport=httpx.MockTransport(unexpected)
            )
    run.workflow_version = "mock-v0.1"
    with pytest.raises(GitHubAppError):
        snapshots.fetch_source_snapshot(run, token, transport=httpx.MockTransport(unexpected))


@pytest.mark.parametrize("status,data", [(404, b"private body"), (200, b"not a ZIP")])
def test_failures_are_sanitized(run, token, status, data):
    transport = transport_for(data)
    if status == 404:
        transport = httpx.MockTransport(lambda request: httpx.Response(status, content=data))
    with pytest.raises(GitHubAppError) as error:
        snapshots.fetch_source_snapshot(run, token, transport=transport)
    assert str(error.value) == "Repository source snapshot could not be retrieved safely."
