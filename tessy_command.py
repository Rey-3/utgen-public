import os
import sys
import time
import subprocess
from pathlib import Path


TESSY_BIN_DIR = r"C:\Program Files\Razorcat\TESSY_5.1\bin"
TESSY_CMD = os.path.join(TESSY_BIN_DIR, "tessycmd.exe")
TESSY_D = os.path.join(TESSY_BIN_DIR, "tessyd.exe")


class TessyCommand():
    """Owns all interaction with the Tessy client: command execution and the
    connect / select-project / close session lifecycle."""

    def __init__(self, work_dir, project_name):
        self.work_dir = work_dir
        self.project_name = project_name

    def run(self, args):
        """
        Executes Tessy command.

        Args:
            args (list): A list of string arguments to append to the base Tessy command.

        Returns:
            - bool: True = Success, False = Failure
            - str: Terminal output
        """
        cmd = [TESSY_CMD] + args
        ret = False

        if "exec-test" in args or "analyze" in args:
            cmd = [TESSY_CMD, "-t", "300"] + args

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            print(f"[FATAL] Could not find tessycmd at: {TESSY_CMD}")
            self.run(["disconnect"])
            self.close_running_tessy()
            sys.exit(1)

        output = result.stdout.strip() + result.stderr.strip()

        if result.returncode != 0:
            if "already exists" in output:
                print("  [INFO] Item already exists. Proceeding.")
                ret = True
            elif "another project is already open" in output:
                print("  [INFO] Project already open. Proceeding.")
                ret = True
            elif "Client is not running" in output:
                output = "CLIENT_NOT_RUNNING"
                print("[INFO] Client is not running")
            else:
                print(f"  [!] Command Failed: {output}")
        else:
            print("[OK] Success")
            ret = True

        return ret, output

    def run_or_fail(self, args, label):
        """Run a command that must succeed; disconnect, close and abort the run if it fails."""
        success, output = self.run(args)
        if not success:
            print(f"[FATAL] {label} failed: {output}")
            self.run(["disconnect"])
            self.close_running_tessy()
            sys.exit(1)
        return output

    def connect_with_tessyd(self):
        # when cloning the TESSY project, TESSY has no idea those files exist.
        # The tessyd -f <pdbx> command is what makes TESSY actually open the project
        # so tessycmd can see and select it.
        os.makedirs(self.work_dir, exist_ok=True)
        pdbx = os.path.join(self.work_dir, self.project_name, "tessy.pdbx")
        subprocess.Popen([TESSY_D, "-f", pdbx], cwd=self.work_dir, shell=True)

        # the project DB takes a few more seconds. Wait until the project is actually selectable
        for i in range(24):
            time.sleep(5)
            connected, _ = self.run(["connect"])
            if not connected:
                print(f"       ... waiting for client {i+1}/24")
                continue

            opened, _ = self.run(["select-project", self.project_name])

            if opened:
                print("  [OK] Project open")
                return
            print(f"       ... waiting for project to load {i+1}/24")

        print("[FATAL] Timed out waiting for TESSY project to open.")
        sys.exit(1)

    def close_running_tessy(self):
        """Force-close any running TESSY/tessyd so a different project can be opened."""
        for exe in ("tessyd.exe", "TESSY.exe"):
            subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True, text=True)
        time.sleep(3)

    def ensure_project_open(self):
        """
        If no client is running, start tessyd on the target pdbx. If a client is
        running but has a different project open, close it and relaunch on the
        target pdbx
        """
        success, output = self.run(["connect"])

        if not success and output == "CLIENT_NOT_RUNNING":
            self.connect_with_tessyd()
            return

        ok, current = self.run(["which-project"])
        if not (ok and current.strip() == self.project_name):
            self.run(["disconnect"])
            self.close_running_tessy()
            self.connect_with_tessyd()
