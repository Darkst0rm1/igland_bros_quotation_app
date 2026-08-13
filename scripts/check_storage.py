"""Prove the configured object storage actually works, before trusting it.

Round-trips a small object through the real :mod:`modules.storage` adapter —
the same code path price lists, logos and quotation PDFs use — rather than
talking to boto3 directly. A test that bypasses the adapter can pass while the
application still fails.

Reads credentials from the environment (or ``.env``) and **prints none of them**.
Safe to run with somebody watching.

    python -m scripts.check_storage
"""
from __future__ import annotations

import sys
import uuid


def main() -> int:
    from modules.config import get_settings
    from modules.storage import StorageError, get_storage

    settings = get_settings()

    print(f"backend  : {settings.storage_backend}")
    if settings.storage_backend != "s3":
        print("\nSTORAGE_BACKEND is not 's3', so this would only be testing the "
              "local filesystem. Set it to 's3' to check the real bucket.")
        return 1

    print(f"bucket   : {settings.storage_bucket or '(not set)'}")
    print(f"endpoint : {settings.storage_endpoint_url or '(default AWS)'}")
    print(f"region   : {settings.storage_region}")
    print(f"key id   : {'set' if settings.storage_access_key_id else 'MISSING'}")
    print(f"secret   : {'set' if settings.storage_secret_access_key else 'MISSING'}")

    if not (settings.storage_bucket and settings.storage_access_key_id
            and settings.storage_secret_access_key):
        print("\nSomething is missing above. Fill it in and run again.")
        return 1

    # A key under a throwaway prefix, so a failure halfway through cannot leave
    # anything that looks like real data.
    key = f"_healthcheck/{uuid.uuid4().hex}.txt"
    payload = b"soneet storage round-trip check"

    storage = get_storage()
    print("\nround trip:")
    try:
        storage.put(key, payload, "text/plain")
        print("  write    ok")

        if not storage.exists(key):
            print("  exists   FAILED — the object was written but cannot be found")
            return 1
        print("  exists   ok")

        returned = storage.get(key)
        if returned != payload:
            print(f"  read     FAILED — {len(returned)} bytes back, "
                  f"{len(payload)} expected")
            return 1
        print("  read     ok (bytes match)")

        storage.delete(key)
        if storage.exists(key):
            print("  delete   FAILED — the object is still there")
            return 1
        print("  delete   ok")

    except StorageError as exc:
        print(f"  FAILED   {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        # Deliberately the type and a short message only: a botocore error can
        # quote the request, and the request is signed with the secret key.
        print(f"  FAILED   {type(exc).__name__}: {str(exc)[:200]}")
        return 1

    print("\nStorage is working. Uploads, logos and generated PDFs will persist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
