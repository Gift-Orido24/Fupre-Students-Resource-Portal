#!/usr/bin/env python3
"""Main Entrypoint to run Web Server or Terminal Uploads."""

import argparse
import sys
from googleapiclient.errors import HttpError

from app import app
from auth import AuthError
from database import add_file_record, init_db
from drive_service import find_or_create_folder, get_drive_service, upload_file_to_drive
from config import log


def parse_args():
    parser = argparse.ArgumentParser(description="Google Drive Portal Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # CLI Upload command
    upload_parser = subparsers.add_parser("upload", help="Upload files via CLI")
    upload_parser.add_argument("files", nargs="+", help="Local files to upload")
    upload_parser.add_argument("--folder-name", help="Upload into named subfolder")
    upload_parser.add_argument("--folder-id", help="Upload into specific Drive Folder ID")

    # Serve command
    serve_parser = subparsers.add_parser("serve", help="Run Flask Web Server")
    serve_parser.add_argument("--port", type=int, default=5000, help="Port to run web app")

    return parser.parse_args()


def main():
    init_db()
    args = parse_args()

    if args.command == "serve":
        log.info("Starting Flask web server on port %d...", args.port)
        app.run(debug=False, port=args.port)

    elif args.command == "upload":
        try:
            service = get_drive_service()
            folder_id = args.folder_id
            if args.folder_name:
                folder_id = find_or_create_folder(service, args.folder_name)
        except (AuthError, HttpError) as e:
            log.error("Initialization error: %s", e)
            return 1

        failures = 0
        for file_path in args.files:
            try:
                result = upload_file_to_drive(service, file_path, folder_id)
                log.info("SUCCESS: %s (Drive ID: %s)", result["name"], result["id"])

                title = input(f"Enter title for '{result['name']}' to store in DB (or press Enter to skip): ").strip()
                if title:
                    subject = input("Enter subject: ").strip() or "General"
                    add_file_record(title, subject, result["id"])
                    log.info("Saved '%s' into SQLite database.", title)

            except Exception as e:
                log.error("Upload failed for '%s': %s", file_path, e)
                failures += 1

        return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log.info("Process interrupted by user.")
        sys.exit(130)

