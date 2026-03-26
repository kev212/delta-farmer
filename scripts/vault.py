# delta-farmer | https://github.com/vladkens/delta-farmer
# Copyright (c) vladkens | MIT License | Secrets should be secret
import argparse
import glob
import sys

from lib.crypto import decrypt_toml_config, encrypt_toml_config

DEFAULT_FIELDS = ["privkey", "token"]
DEFAULT_PATTERN = "configs/*.toml"


def main():
    cli = argparse.ArgumentParser(prog="vault", description="Encrypt / decrypt TOML config files")
    cli.add_argument(
        "command",
        choices=["encrypt", "decrypt"],
        help="Operation to perform",
    )
    cli.add_argument(
        "files",
        nargs="*",
        default=[DEFAULT_PATTERN],
        help=f"Files or glob patterns (default: {DEFAULT_PATTERN})",
    )
    cli.add_argument(
        "-f",
        "--field",
        dest="fields",
        action="append",
        metavar="FIELD",
        help=f"Field name to encrypt/decrypt (repeatable, default: {DEFAULT_FIELDS})",
    )
    args = cli.parse_args()

    fields = args.fields or DEFAULT_FIELDS

    paths: list[str] = []
    for pattern in args.files:
        matched = glob.glob(pattern)
        if not matched:
            print(f"No files matched: {pattern}", file=sys.stderr)
        paths.extend(matched)

    if not paths:
        print("No TOML files found.", file=sys.stderr)
        sys.exit(1)

    fn = encrypt_toml_config if args.command == "encrypt" else decrypt_toml_config
    for path in paths:
        print(f"\n── {path}")
        fn(path, fields)


if __name__ == "__main__":
    main()
