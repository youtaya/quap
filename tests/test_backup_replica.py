"""Off-site replica verification and RPO/RTO arithmetic for the backup path.

The native research path these cases once shared a file with is retired, so the file is named for
what it actually covers.
"""

from datetime import date, datetime, timedelta, timezone

from quant_platform.operations import copy_verified_replica, recovery_times


def test_replica_is_a_different_directory_and_recovery_times_are_differences(tmp_path):
    source = tmp_path / "primary" / "quant-20260101T000000.dump"
    source.parent.mkdir()
    source.write_bytes(b"dump-bytes")
    import hashlib

    checksum = hashlib.sha256(b"dump-bytes").hexdigest()
    copied = copy_verified_replica(source, tmp_path / "offsite", checksum)
    assert (tmp_path / "offsite" / source.name).read_bytes() == b"dump-bytes"
    assert copied["sha256"] == checksum
    try:
        copy_verified_replica(source, source.parent, checksum)
    except ValueError:
        pass
    else:
        raise AssertionError("replica directory must differ")
    backup_at = date(2026, 1, 1)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fault = start + timedelta(hours=2)
    finished = fault + timedelta(minutes=5)
    timing = recovery_times(start, fault, finished)
    assert timing["rpo_seconds"] == 7200
    assert timing["rto_seconds"] == 300
    assert backup_at
