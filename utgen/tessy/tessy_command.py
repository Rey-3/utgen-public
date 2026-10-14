import os
import time
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path







class TessyCommand:
    """Wrapper for tessycmd.

    Run commands in this order: connect, select project, select test
    collection, then select module. Select a test object before export or
    import. Command and lifecycle methods return (success, output).
    run_or_fail logs failures; callers decide what to skip.
    """

    def __init__(
        self,
        work_dir,
        project_name,
        source_root=None,
        tessy_bin_dir=None,
        project_path=None,
    ):
        self.work_dir = work_dir
        self.project_name = project_name
        self.source_root = source_root
        self.project_path = Path(project_path) if project_path else Path(work_dir) / project_name
        configured_bin = tessy_bin_dir or os.environ.get("TESSY_BIN_DIR")
        self.tessy_bin_dir = Path(configured_bin) if configured_bin else Path()
        self.tessy_cmd = str(self.tessy_bin_dir / "tessycmd.exe")
        self.tessy_d = str(self.tessy_bin_dir / "tessyd.exe")
        self.last_test_success = False

    def run(self, args):
        """
        Executes Tessy command.
        """
        cmd = [self.tessy_cmd] + args
        command_name = args[0] if args else "unknown"
        ret = False

        if "exec-test" in args or "analyze" in args:
            cmd = [self.tessy_cmd, "-t", "300"] + args

        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except OSError as error:
            return False, f"Could not run tessycmd at {self.tessy_cmd}: {error}"

        output = "\n".join(filter(None, (result.stdout.strip(), result.stderr.strip())))

        if result.returncode != 0:
            if "already exists" in output:
                print(f"  [INFO] {command_name}: item already exists. Proceeding.")
                ret = True
            elif "another project is already open" in output:
                print(f"  [INFO] {command_name}: project already open. Proceeding.")
                ret = True
            elif "Client is not running" in output or (command_name in ("connect", "disconnect") and not output):
                output = "CLIENT_NOT_RUNNING"
                print("[INFO] Client is not running")
            else:
                print(f"  [!] {command_name} failed: {output}")
        else:
            print(f"[OK] {command_name}: command completed")
            ret = True

        return ret, output

    def _command(self, name, *args):
        """Run a TESSY command. Send args in the same order."""
        return self.run([name, *(str(arg) for arg in args)])

    @staticmethod
    def _collection_scope(test_collection):
        """Use -test-collection when the parent is a test collection."""
        return ("-test-collection",) if test_collection else ()

    def connect(self):
        return self._command("connect")

    def new_test_collection(self, name):
        """Create a test collection. name is its name."""
        return self.run_or_fail(["new-test-collection", name], "create test collection")
    
    def select_test_collection(self, name):
        """Select a test collection before module commands."""
        return self.run_or_fail(["select-test-collection", name], "select test collection")

    def new_folder(self, name, test_collection=False):
        """Create a folder.

        name: folder name. test_collection: use the selected collection as parent.
        Note: select a folder or collection first.
        """
        return self._command("new-folder", *self._collection_scope(test_collection), name)

    def new_module(self, environment, name, test_collection=False):
        """Create a module.

        environment: environment name. name: module name. test_collection: use
        the selected collection as parent. Note: select a folder or collection first.
        """
        return self._command("new-module", "-env", environment, *self._collection_scope(test_collection), name)

    def restore_module(self, path):
        """Restore one TMB file.

        Note: run select_test_collection() first.
        """
        return self.run_or_fail(["restore-module", "-test-collection", str(path)], "restore module")

    def select_module(self, name, test_collection=True):
        """Select a module.

        name: module name. test_collection: search under the selected collection.
        Note: select a folder or collection first.
        """
        return self.run_or_fail(["select-module", *self._collection_scope(test_collection), name], "select module")

    def analyze(self):
        """Analyze the selected module.

        Note: run select_module() first.
        """
        return self.run_or_fail(["analyze"], "analyze module")

    def select_project(self, name):
        """Select a project by name."""
        return self.run_or_fail(["select-project", name], "select project")

    def list_test_objects(self):
        """List test objects in the selected module.

        Note: run select_module() first.
        """
        return self._command("list-test-objects")

    def select_test_object(self, name):
        """Select a test object by name.

        Note: run select_module() first.
        """
        return self.run_or_fail(["select-test-object", name], "select test object")

    def list_projects(self):
        """List all projects."""
        return self._command("list-projects")

    def list_environments(self):
        """List all test environments."""
        return self._command("list-environments")

    def list_test_collections(self):
        """List test collections in the selected project.

        Note: run select_project() first.
        """
        return self._command("list-test-collections")

    def list_folders(self, test_collection=False):
        """List folders.

        test_collection: list folders under the selected collection.
        Note: select a folder or collection first.
        """
        return self._command("list-folders", *self._collection_scope(test_collection))

    def list_modules(self, test_collection=True):
        """List modules.

        test_collection: list modules under the selected collection.
        Note: select a folder or collection first.
        """
        return self.run_or_fail(["list-modules", *self._collection_scope(test_collection)], "list modules")

    def select_folder(self, name, test_collection=False):
        """Select a folder by name.

        test_collection: search under the selected collection.
        Note: select a folder or collection first.
        """
        return self._command("select-folder", *self._collection_scope(test_collection), name)

    def select_source_file(self, path):
        """Select a source file by path.

        Note: run select_module() first.
        """
        return self._command("select-source-file", path)

    def which_project(self):
        """Show the selected project."""
        return self._command("which-project")

    def which_test_collection(self):
        """Show the selected test collection."""
        return self._command("which-test-collection")

    def which_folder(self):
        """Show the selected folder."""
        return self._command("which-folder")

    def which_module(self):
        """Show the selected module."""
        return self._command("which-module")

    def which_test_object(self):
        """Show the selected test object."""
        return self._command("which-test-object")

    def which_source_file(self):
        """Show the selected source file."""
        return self._command("which-source-file")

    def which(self):
        """Show all selected TESSY items."""
        return self._command("which")

    def import_requirement_document(self, file, commit=False, increment_major_version_number=False):
        """Import a requirement file.

        file: input file. commit: save changes. increment_major_version_number:
        increase the major version.
        """
        args = ["-file", file]
        if commit:
            args.append("-commit")
        if increment_major_version_number:
            args.append("-increment-major-version-number")
        return self._command("import-requirement-document", *args)

    def import_test_summary(self, path):
        return self._command("import-test-summary", path)

    def _test_object_names(self):
        """Get the test object names from the selected module."""
        success, output = self.list_test_objects()
        if not success:
            return []

        names = []
        for line in output.splitlines():
            name = line.strip()
            if name:
                names.append(name)
        return names

    def _latest_report_folder(self):
        """Find the newest TESSY report folder for this project."""
        work_root = Path(self.work_dir) / self.project_name / "work"
        latest_report = None
        latest_time = -1

        if not work_root.exists():
            return None

        for folder, folders, _ in os.walk(work_root):
            if "report" not in folders:
                continue
            report_folder = Path(folder) / "report"
            report_time = report_folder.stat().st_mtime_ns
            if report_time > latest_time:
                latest_report = report_folder
                latest_time = report_time
        return latest_report

    @staticmethod
    def _read_report_status(report_folder, check_report_success=False):
        """Return test-object status and not-executed cases from result XML."""
        status = {}
        failed_cases = {}
        for folder, _, files in os.walk(report_folder):
            for file_name in files:
                if file_name.lower() != "results.xml":
                    continue
                try:
                    xml_root = ET.parse(Path(folder) / file_name).getroot()
                except (ET.ParseError, OSError):
                    continue

                test_name = xml_root.get("testobject_name")
                if not test_name:
                    continue

                if xml_root.get("success", "").strip().casefold() == "ok":
                    execution_failed = False
                elif check_report_success:
                    execution_failed = True
                else:
                    statistic = xml_root.find("./summary/statistic")
                    notexecuted_list = (
                        statistic.get("notexecuted_list", "").strip()
                        if statistic is not None
                        else ""
                    )
                    notexecuted_ids = []
                    for case_id in notexecuted_list.split(";"):
                        case_id = case_id.strip()
                        if case_id:
                            notexecuted_ids.append(case_id)
                    execution_failed = statistic is None or (
                        statistic.get("notexecuted", "") != "0"
                        or bool(notexecuted_ids)
                    )
                    if notexecuted_ids:
                        failed_cases[test_name] = notexecuted_ids
                status[test_name] = status.get(test_name, False) or execution_failed

        return status, failed_cases

    def execute_test(
        self, report_dir, tbs_path, module_name=None, check_report_success=False
    ):
        """Run a TBS file and show which test objects executed.

        report_dir: output folder. tbs_path: TBS file. module_name: name in
        the log. Sets last_test_success after parsing result XML. Note: select
        the project, collection, and module first.
        """
        label = f"run tests for {module_name}" if module_name else "execute test"
        function_names = self._test_object_names()
        output = self.run_or_fail(["exec-test", "-o", str(report_dir), str(tbs_path)], label)
        self.last_test_success = False
        if not output[0]:
            return output
        latest_report = self._latest_report_folder()
        report_status, failed_cases = (
            self._read_report_status(latest_report, check_report_success)
            if latest_report
            else ({}, {})
        )
        status_names = function_names or report_status
        self.last_test_success = bool(status_names)
        for function_name in status_names:
            if report_status.get(function_name, True):
                self.last_test_success = False
                break

        for function_name in status_names:
            status = "FAILED" if report_status.get(function_name, True) else "OK"
            print(f"[{status}] {function_name}")
            for case_id in failed_cases.get(function_name, []):
                print(f"    Test Case {case_id}")

        if not function_names and not report_status:
            print(f"[WARN] {label}: no result report found")
        return output

    def set_comment(self, text):
        """Set the comment on the selected item."""
        return self._command("set-comment", text)

    def get_comment(self):
        """Get the comment from the selected item."""
        return self._command("get-comment")

    def unset_comment(self):
        """Remove the comment from the selected item."""
        return self._command("unset-comment")

    def set_description(self, text):
        """Set the description on the selected item."""
        return self._command("set-description", text)

    def get_description(self):
        """Get the description from the selected item."""
        return self._command("get-description")

    def unset_description(self):
        """Remove the description from the selected item."""
        return self._command("unset-description")

    def set_specification(self, text):
        """Set the specification on the selected item."""
        return self._command("set-specification", text)

    def get_specification(self):
        """Get the specification from the selected item."""
        return self._command("get-specification")

    def unset_specification(self):
        """Remove the specification from the selected item."""
        return self._command("unset-specification")

    def set_attribute(self, name, value=None, module=False, add=None):
        """Set an attribute.

        name: attribute name. value: attribute value. module: target the module.
        add: attribute type. Note: select the item first.
        """
        args = ["-module"] if module else []
        if add:
            args += ["-add", add]
        args.append(name)
        if value is not None:
            args.append(value)
        return self._command("set-attribute", *args)

    def unset_attribute(self, name, module=False):
        """Remove an attribute by name. module=True targets the module."""
        return self._command("unset-attribute", *(["-module"] if module else []), name)

    def get_attribute(self, name, module=False):
        """Get an attribute by name. module=True targets the module."""
        return self._command("get-attribute", *(["-module"] if module else []), name)

    def list_attributes(self, module=False):
        """List attributes. module=True lists module attributes."""
        return self._command("list-attributes", *(["-module"] if module else []))

    def add_source_files(self, *paths):
        """Add source file paths to the selected module."""
        return self._command("add-source-file", *paths)

    def remove_source_files(self, *paths, all=False):
        """Remove source files. all=True removes all files."""
        return self._command("remove-source-file", *(["-all"] if all else []), *paths)

    def list_source_files(self):
        """List source files. Note: select a module first."""
        return self._command("list-source-files")

    def get_environment(self):
        """Get the environment. Note: select an item first."""
        return self._command("get-environment")

    def set_environment(self, name):
        """Set the environment. name is the environment name."""
        return self.run_or_fail(["set-environment", name], "set environment")

    def add_include_dirs(self, *paths, file=False):
        """Add include paths. file=True targets a source file."""
        return self._command("add-include-dir", *(["-file"] if file else []), *paths)

    def remove_include_dirs(self, *paths, file=False, all=False):
        """Remove include paths. all=True removes all paths."""
        flags = (["-file"] if file else []) + (["-all"] if all else [])
        return self._command("remove-include-dir", *flags, *paths)

    def list_include_dirs(self, file=False):
        """List include paths. file=True targets a source file."""
        return self._command("list-include-dirs", *(["-file"] if file else []))

    def add_defines(self, *defines, file=False):
        """Add defines. file=True targets a source file."""
        return self._command("add-define", *(["-file"] if file else []), *defines)

    def remove_defines(self, *names, file=False, all=False):
        """Remove defines. all=True removes all defines."""
        flags = (["-file"] if file else []) + (["-all"] if all else [])
        return self._command("remove-define", *flags, *names)

    def list_defines(self, file=False):
        """List defines. file=True targets a source file."""
        return self._command("list-defines", *(["-file"] if file else []))
    
    def set_compiler_options(self, *options, file=False):
        """Set compiler options. file=True targets a source file."""
        return self._command("set-compiler-options", *(["-file"] if file else []), *options)

    def unset_compiler_options(self, file=False):
        """Remove compiler options. file=True targets a source file."""
        return self._command("unset-compiler-options", *(["-file"] if file else []))

    def list_compiler_options(self, file=False):
        """List compiler options. file=True targets a source file."""
        return self._command("list-compiler-options", *(["-file"] if file else []))

    def export(self, directory, format=None, expected=False, actual=False, both=False, convert=None, file=None):
        """Export test data.

        directory: output folder. format: output type. expected, actual, or
        both: values to export. convert: value conversion. file: output name.
        Note: select a test object first.
        """
        args = []
        if format:
            args += ["-format", format]
        if expected:
            args.append("-expected")
        if actual:
            args.append("-actual")
        if both:
            args.append("-both")
        if convert:
            args += ["-convert", convert]
        if file:
            args += ["-file", file]
        return self.run_or_fail(["export", *args, str(directory)], "export test data")

    def import_data(self, path, convert=None, set_passing=False, append=False):
        """Import test data.

        path: input file. convert: value conversion. set_passing: update
        passing values. append: add to existing data. Note: select a test object first.
        """
        args = []
        if convert:
            args += ["-convert", convert]
        if set_passing:
            args.append("-set-passing")
        if append:
            args.append("-append")
        return self._command("import", *args, path)

    def save_db(self, source, to=None):
        """Save database data. source: project level. to: output folder."""
        args = ["-source", source]
        if to:
            args += ["-to", to]
        return self._command("save-db", *args)

    def restore_db(self, target, from_dir=None):
        """Restore database data. target: project level. from_dir: input folder."""
        args = ["-target", target]
        if from_dir:
            args += ["-from", from_dir]
        return self._command("restore-db", *args)

    def xslt(self, input_file, xsl=None, output=None):
        """Transform a file. input_file: input. xsl: style file. output: output file."""
        args = []
        if xsl:
            args += ["-xsl", xsl]
        if output:
            args += ["-o", output]
        return self._command("xslt", *args, input_file)

    def connect_with_tessyd(self):
        # when cloning the TESSY project, TESSY has no idea those files exist.
        # The tessyd -f <pdbx> command is what makes TESSY actually open the project
        # so tessycmd can see and select it.
        try:
            os.makedirs(self.work_dir, exist_ok=True)
            pdbx = str(self.project_path / "tessy.pdbx")
            args = [self.tessy_d, "-f", pdbx, *(["--source-root", self.source_root] if self.source_root else [])]
            subprocess.Popen(args, cwd=self.work_dir, shell=True)
        except OSError as error:
            return False, f"Could not start tessyd: {error}"

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
                return True, "Project open"
            print(f"       ... waiting for project to load {i+1}/24")

        return False, "Timed out waiting for TESSY project to open."

    def run_or_fail(self, args, label):
        """Run a required command and return ``(success, output)``."""
        success, output = self.run(args)
        if not success:
            print(f"[ERROR] {label} failed: {output}")
        return success, output

    def close_running_tessy(self):
        """Force-close TESSY/tessyd and return (success, error details)."""
        success, output = self.disconnect()
        errors = []
        if not success and output != "CLIENT_NOT_RUNNING":
            errors.append(f"disconnect failed: {output}")

        for exe in ("tessyd.exe", "TESSY.exe"):
            try:
                result = subprocess.run(["taskkill", "/F", "/IM", exe], capture_output=True, text=True)
            except OSError as error:
                errors.append(f"close {exe} failed: {error}")
                continue
            # taskkill returns 128 when the process is already absent.
            if result.returncode not in (0, 128):
                errors.append(f"close {exe} failed: {result.stdout.strip()} {result.stderr.strip()}")
        time.sleep(3)
        return not errors, "\n".join(errors)

    def disconnect(self):
        return self.run(["disconnect"])

    def ensure_project_open(self):
        """
        If no client is running, start tessyd on the target pdbx. If a client is
        running with a different project or a configured source root, close it
        and relaunch on the target pdbx. Return (success, output) so the caller
        can skip a failed project.
        """
        success, output = self.connect()

        if not success:
            self.close_running_tessy()
            return self.connect_with_tessyd()


        ok, current = self.which_project()
        if not ok:
            return False, current
        if self.source_root or current.strip() != self.project_name:
            self.close_running_tessy()
            return self.connect_with_tessyd()
        return True, current


