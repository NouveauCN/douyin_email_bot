from process_media import _resolve_lock_root


def test_single_file_uses_configured_shared_root(tmp_path):
    root = tmp_path / "downloads"
    target = root / "author" / "clip.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"video")

    assert _resolve_lock_root(target, configured_root=root) == root.resolve()


def test_explicit_lock_root_rejects_outside_target(tmp_path):
    target = tmp_path / "outside.mp4"
    target.write_bytes(b"video")
    root = tmp_path / "downloads"
    root.mkdir()

    try:
        _resolve_lock_root(target, explicit_root=root)
    except ValueError:
        pass
    else:
        raise AssertionError("outside target should not use an unrelated lock root")
