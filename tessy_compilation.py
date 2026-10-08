import os
import sys
import time
from utgen.includes import IncludeScanner
from tessy_command import TessyCommand


class Tessy_Compilation():

    def __init__(self, work_dir, root_path, project_name, collection_name, report_dir, modules, command=None):
        self.work_dir = work_dir
        self.root_path = root_path
        self.project_name = project_name
        self.collection_name = collection_name
        self.report_dir = report_dir
        self.modules = modules
        self.environment = "GNU GCC Eclipse CDT (Default)"
        self.tessy_command = command or TessyCommand(work_dir, project_name)

    def compilation(self):
        # Open this project's pdbx once, then configure every module inside its
        # single shared collection, run the collection once, and export per module.
        self.tessy_command.ensure_project_open()
        self.tessy_command.run_or_fail(["select-project", self.project_name], "select project")

        self._ensure_collection()
        for module in self.modules:
            self._configure_module(module)
            self._analyze_and_import(module)

        # self._run_tests()
        # self._export_results()
        self.tessy_command.run(["disconnect"])
        self.tessy_command.close_running_tessy()

    def _ensure_collection(self):
        """Reuse the project's collection or create it once."""
        _, existing = self.tessy_command.run(["list-test-collections", self.collection_name])
        if existing:
            self.tessy_command.run_or_fail(["select-test-collection", self.collection_name], "select test collection")
        else:
            print("[INFO] Creating new test collection")
            self.tessy_command.run_or_fail(["new-test-collection", self.collection_name], "create test collection")
            self.tessy_command.run_or_fail(["select-test-collection", self.collection_name], "select test collection")

    def _configure_module(self, module):
        """Create the module if it does not exist yet, else reuse it and wipe stale config."""
        selected, _ = self.tessy_command.run(["select-module", "-c", module.module_name])
        if selected:
            self.tessy_command.run(["remove-source-file", "-all"])
            self.tessy_command.run(["remove-include-dir", "-all"])
            self.tessy_command.run(["remove-define", "-all"])
        else:
            print(f"[INFO] Creating new module {module.module_name}")
            self.tessy_command.run_or_fail(["new-module", "-c", "-e", self.environment, module.module_name], "create module")

        self._set_module_sources(module)

    def _set_module_sources(self, module):
        """Add the source file, include dirs and defines for the current module."""
        include_path = IncludeScanner(module.source_file, str(self.root_path)).get_unique_include_dirs()
        self.tessy_command.run_or_fail(["add-source-file", module.source_file], "add source file")
        self.tessy_command.run_or_fail(["add-include-dir"] + include_path, "add include dirs")
        self.tessy_command.run_or_fail(["add-define", "__CCRH__"], "add define __CCRH__")
        self.tessy_command.run_or_fail(["add-define", "const="], "add define const=")

    def _analyze_and_import(self, module):
        """Analyze the current module, then import passing test data from excel."""
        self.tessy_command.run_or_fail(["analyze"], "analyze")

        success, raw_list = self.tessy_command.run(["list-test-objects"])
        if not (success and raw_list):
            print(f"[WARN] No test objects found for {module.module_name}; skipping import.")
            return

        test_objects = [f.strip() for f in raw_list.splitlines() if f.strip()]
        for func in test_objects:
            excel_path = self.find_excel_file(module.excel_input_dir, func)
            if excel_path:
                print(f"      Importing: {func}")
                self.tessy_command.run(["select-test-object", func])
                self.tessy_command.run(["import", "-set-passing", excel_path])
            else:
                print("[Error] Unable to find the Excel Path")

    def _run_tests(self):
        """Generate the batch-test spec for the whole collection and execute it once."""
        tbs_path = os.path.join(self.work_dir, "temp.tbs")
        self.generate_tbs(tbs_path, self.collection_name)
        self.tessy_command.run(["exec-test", "-o", self.report_dir, tbs_path])

    def _export_results(self):
        """Reconnect, then export the actual results of every module into its own output dir."""
        self.tessy_command.run(["disconnect"])
        time.sleep(2)
        self.tessy_command.run(["connect"])

        self.tessy_command.run(["select-project", self.project_name])
        self.tessy_command.run(["select-test-collection", self.collection_name])

        for module in self.modules:
            self.tessy_command.run(["select-module", "-c", module.module_name])
            success, raw_list = self.tessy_command.run(["list-test-objects"])
            if not (success and raw_list):
                print(f"[WARN] No test objects to export for {module.module_name}.")
                continue

            final_test_objects = [f.strip() for f in raw_list.splitlines() if f.strip()]
            if not os.path.exists(module.output_dir):
                os.makedirs(module.output_dir)

            print(f"[INFO] {module.module_name}: exporting {len(final_test_objects)} objects.")
            for func in final_test_objects:
                print(f"      Exporting: {func}")
                self.tessy_command.run(["select-test-object", func])
                self.tessy_command.run([
                    "export",
                    "-format", "xlsx",
                    "-actual",
                    "-file", f"{func}",
                    module.output_dir,
                ])

        self.tessy_command.run(["disconnect"])
        self.tessy_command.close_running_tessy()

    def find_excel_file(self, directory, func_name):
        targets = {f"{func_name}.xlsx".lower(), f"{func_name}.xls".lower()}
        if os.path.exists(directory):
            for f in os.listdir(directory):
                if f.lower() in targets:
                    return os.path.join(directory, f)

    def generate_tbs(self, file_path, collection_name):
        tbs_content = fr"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
    <batchtest>
        <operations>
            <operation key="executeTest">
                <options>
                    <option key="skipCheckInterface" value="false"/>
                    <option key="checkInterface" value="false"/>
                    <option key="generateDriver" value="false"/>
                    <option key="run" value="true"/>
                    <option key="runNoInstrumentationTest" value="false"/>
                    <option key="runPatternTest" value="false"/>
                    <option key="runMutationTest" value="false"/>
                    <option key="skipTestObjectsWithValidResult" value="false"/>
                    <option key="retryAbortedExecution" value="false"/>
                    <option key="instrumentationType" value="TESTOBJECT_ONLY"/>
                    <option key="defaultCoverage" value="false"/>
                    <option key="defaultCoveragePerTestObject" value="false"/>
                    <option key="abortOnMissingStubCode" value="true"/>
                    <option key="preAnalyzeScript" value=""/>
                    <option key="preExecuteScript" value=""/>
                    <option key="postExecuteScript" value=""/>
                </options>
                <coverageTypes>
                    <coverageType name="MCDC"/>
                    <coverageType name="BRANCH"/>
                    <coverageType name="STATEMENT"/>
                    <coverageType name="DECISION"/>
                </coverageTypes>
            </operation>
            <operation key="generateTestReport">
                <options>
                    <option key="reportOutputDirectory" value="{self.report_dir}"/>
                    <option key="reportFileNamePattern" value="TESSY_DetailsReport_$(TESTOBJECT)"/>
                    <option key="reportOutputFormat" value="pdf"/>
                </options>
                <arguments>
                    <argument name="OPT_TESTDATA_ONLY_MODE" value="false"/>
                    <argument name="OPT_SHOW_PROPERTIES" value="true"/>
                    <argument name="OPT_SHOW_USER_AND_HOST" value="false"/>
                    <argument name="OPT_SHOW_COVERAGE" value="true"/>
                    <argument name="OPT_SHOW_INTERFACE" value="true"/>
                    <argument name="OPT_SHOW_METRICS" value="true"/>
                    <argument name="OPT_SHOW_ATTRIBUTES" value="true"/>
                    <argument name="OPT_SHOW_COMMENTS" value="true"/>
                    <argument name="OPT_SHOW_CTE" value="true"/>
                    <argument name="OPT_SHOW_USERCODE" value="true"/>
                    <argument name="OPT_SHOW_TS_DETAILS" value="true"/>
                    <argument name="OPT_SHOW_REQUIREMENT_TEXT" value="false"/>
                    <argument name="OPT_SHOW_FAULT_INJECTION_TCS" value="true"/>
                    <argument name="OPT_SHOW_FAULT_INJECTIONS" value="true"/>
                    <argument name="OPT_HIDE_NONE_VALUES" value="true"/>
                    <argument name="OPT_HIDE_IRRELEVANT_INTERFACE" value="false"/>
                    <argument name="OPT_SHOW_NOTES" value="true"/>
                    <argument name="OPT_SHOW_UUID" value="false"/>
                    <argument name="OPT_HIDE_TESTSTEPS" value="false"/>
                    <argument name="OPT_SHOW_VARIANT_INFO" value="true"/>
                    <argument name="OPT_SHOW_LAST_MODIFIED_TIME" value="false"/>
                </arguments>
            </operation>
        </operations>
        <elements>
            <testcollection name="{collection_name}"/>
        </elements>
        <settings>
            <setting key="expandAttributes" value="false"/>
        </settings>
    </batchtest>
    """
        try:
            with open(file_path, "w") as f:
                f.write(tbs_content)
            print(f"[INFO] Generated TBS file at: {file_path} with Collection: {collection_name}")
        except Exception as e:
            print(f"[ERROR] Failed to generate TBS file: {e}")
            sys.exit(1)
