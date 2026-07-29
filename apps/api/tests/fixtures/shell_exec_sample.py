"""A DELIBERATELY VULNERABLE fixture for the TITAN-SEC-073 semgrep rule.

Never imported or executed — it exists only so the shell-exec ban
(.semgrep/no-shell-exec.yml) has a committed positive case to fire on. Every
function runs a command through a shell that re-parses a string, the exact
patterns the rule bans; `make semgrep` scans src/ + alembic/ (which are clean),
this fixture is scanned only by the rule's self-test.
"""

import os
import subprocess


# Every one of the 9 no-shell-exec.yml pattern branches has a positive case here
# (SEC-104 review #7: no rule branch left unexercised by the self-test fixture).
def via_system(user_ref):  # type: ignore[no-untyped-def]
    os.system("git checkout " + user_ref)


def via_popen(name):  # type: ignore[no-untyped-def]
    return os.popen(f"ls {name}").read()


def via_run_shell(cmd):  # type: ignore[no-untyped-def]
    subprocess.run(cmd, shell=True, check=False)


def via_call_shell(cmd):  # type: ignore[no-untyped-def]
    subprocess.call(cmd, shell=True)


def via_check_call_shell(cmd):  # type: ignore[no-untyped-def]
    subprocess.check_call(cmd, shell=True)


def via_check_output_shell(cmd):  # type: ignore[no-untyped-def]
    return subprocess.check_output(cmd, shell=True)


def via_popen_shell(cmd):  # type: ignore[no-untyped-def]
    return subprocess.Popen(cmd, shell=True)


def via_getoutput(host):  # type: ignore[no-untyped-def]
    return subprocess.getoutput("ping " + host)


def via_getstatusoutput(host):  # type: ignore[no-untyped-def]
    return subprocess.getstatusoutput("ping " + host)
