def sync_file(dbx, container, entry):
    relative = entry.path_display.split(
        f"/{PROJECT}/Database/RawData/",
        1,
    )[1]

    blob_name = f"{RAW_PREFIX}{relative}"

    blob = container.get_blob_client(
        blob_name
    )

    dropbox_hash = entry.content_hash
    existing = None

    try:
        existing = blob.get_blob_properties()
    except Exception:
        existing = None

    if existing:
        metadata = existing.metadata or {}

        if (
            metadata.get(
                "dropbox_content_hash"
            )
            == dropbox_hash
            and existing.size == entry.size
        ):
            return "ALREADY_VERIFIED"

        version = (
            datetime.now(timezone.utc)
            .strftime("%Y%m%dT%H%M%SZ")
        )

        version_blob_name = (
            f"{blob_name}.v2-{version}"
        )

        version_blob = container.get_blob_client(
            version_blob_name
        )

        _, response = dbx.files_download(
            entry.path_display
        )

        version_blob.upload_blob(
            response.content,
            overwrite=False,
            metadata={
                "dropbox_content_hash": dropbox_hash,
                "source": "dropbox",
                "project": PROJECT,
                "relative_path": relative,
                "source_size": str(entry.size),
                "source_modified": (
                    entry.server_modified.isoformat()
                ),
                "ingested_at": (
                    datetime.now(
                        timezone.utc
                    ).isoformat()
                ),
                "supersedes": blob_name,
            },
            max_concurrency=1,
        )

        return "VERSIONED"

    _, response = dbx.files_download(
        entry.path_display
    )

    blob.upload_blob(
        response.content,
        overwrite=False,
        metadata={
            "dropbox_content_hash": dropbox_hash,
            "source": "dropbox",
            "project": PROJECT,
            "relative_path": relative,
            "source_size": str(entry.size),
            "source_modified": (
                entry.server_modified.isoformat()
            ),
            "ingested_at": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
        },
        max_concurrency=1,
    )

    return "UPLOADED"