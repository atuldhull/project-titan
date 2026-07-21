"""``python -m titan.vault keygen`` — mint an age identity for the vault.

Prints the identity (AGE-SECRET-KEY-1…, set as TITAN_VAULT_AGE_KEY, NEVER
committed) and its public recipient (age1…). Interoperable with the ``age`` CLI.
"""

import sys

from titan.vault import age


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "keygen":
        print("usage: python -m titan.vault keygen", file=sys.stderr)
        return 2
    identity, recipient = age.generate_identity()
    print(f"# TITAN_VAULT_AGE_KEY (secret — never commit):\n{identity}")
    print(f"# recipient (public):\n{recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
