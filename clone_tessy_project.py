import os
import re
import sys
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape

# External templates live beside the executable when frozen.
RESOURCE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
TEMPLATE_DIR = RESOURCE_DIR / "template2"
TEMPLATE_CONFIG_XML = TEMPLATE_DIR / "config" / "configuration.xml"

# --- Settings (previously config.yaml) ---
PATCH_FILE_PATH1 = r"persist\ite\cache\service.properties"
PATCH_FILE_PATH2 = r"persist\ite\itedb\service.properties"


def read_environment_names(config_xml=TEMPLATE_CONFIG_XML):
    """Return the list of project environment names defined in configuration.xml
    (in document order), e.g. 'Renesas RH850 Renesas CS+ (E2)'."""
    tree = ET.parse(config_xml)
    root = tree.getroot()
    return [env.get("name") for env in root.iter("projectEnvironment") if env.get("name")]


class TessyProjectCloner:

    PDBX_FILE = Path("tessy.pdbx")

    PATCH_FILES = [
        Path(PATCH_FILE_PATH1),
        Path(PATCH_FILE_PATH2),
    ]

    def __init__(self, workspace, template=None):
        self.template = self._resolve_template(template)
        self.workspace = Path(workspace)

    @staticmethod
    def _resolve_template(template):
        if not template:
            return TEMPLATE_DIR
        path = Path(template)
        if path.is_absolute():
            return path
        candidates = [RESOURCE_DIR / path, Path.cwd() / path]
        return next((candidate for candidate in candidates if candidate.exists()), candidates[0])
      
    def patch_service_properties(self, dst):
        """Point the cloned Derby databases at their new location.

        Derby records its own folder as the first line of service.properties, a
        '#<absolute path>' comment. Rewriting that line directly is independent
        of where the template was copied from, which matters when the template
        is unpacked from the exe into a temp folder that changes every run.
        """
        for rel in self.PATCH_FILES:
            target = dst / rel
            lines = target.read_text(encoding="utf-8").splitlines()
            if not lines or not lines[0].startswith("#"):
                print(f"[WARN] Unexpected service.properties format, skipped: {target}")
                continue
            lines[0] = "#" + str(target.parent)
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    CONFIG_XML = Path("config") / "configuration.xml"

    # CC-RH compiler install location, relative to $(ProgramFilesx86).
    CCRH_SUBDIR = Path("Renesas Electronics") / "CS+" / "CC" / "CC-RH"

    def detect_ccrh_version(self):
        """Return the newest CC-RH version folder actually installed on this
        machine (e.g. 'V2.07.00'), or None if the compiler is not found."""
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        ccrh_root = Path(program_files_x86) / self.CCRH_SUBDIR
        if not ccrh_root.is_dir():
            return None
        versions = [
            p.name
            for p in ccrh_root.iterdir()
            if p.is_dir() and re.fullmatch(r"V\d+\.\d+\.\d+", p.name)
        ]
        if not versions:
            return None
        # Zero-padded (V2.07.00) so lexicographic sort == version order.
        return sorted(versions)[-1]

    def initialize_configuration(self, dst):
        """Rewrite the absolute resource paths inside the cloned
        configuration.xml so every environment points at THIS project's own
        config folder (dst\\config) instead of the template's original location.

        Rewrites, for every project environment:
          - InitObjDir            -> <dst>\\config\\start
          - Makefile Template     -> <dst>\\config\\ts_make_cs+_renesas_rh850.tpl
          - target_handler-ca.py  -> <dst>\\config\\target_handler-ca.py
            (only that argument of the Pre Execution Command; the .mtpj is left
            untouched)
        """
        config_xml = dst / self.CONFIG_XML
        if not config_xml.exists():
            print(f"[WARN] configuration.xml not found, skipping path init: {config_xml}")
            return

        dst_config = dst / "config"
        content = config_xml.read_text(encoding="ISO-8859-1")

        # InitObjDir / Makefile Template: replace the whole value= attribute.
        attr_targets = {
            "InitObjDir": str(dst_config / "start"),
            "Makefile Template": str(dst_config / "ts_make_cs+_renesas_rh850.tpl"),
        }
        for name, new_value in attr_targets.items():
            content = re.sub(
                rf'(name="{re.escape(name)}"[^>]*?value=")[^"]*(")',
                lambda m, v=new_value: m.group(1) + v + m.group(2),
                content,
            )

        # Pre Execution Command: rewrite only the quoted target_handler-ca.py
        # argument (paths are &quot;-delimited in the XML).
        new_handler = str(dst_config / "target_handler-ca.py")
        content = re.sub(
            r'(&quot;)[^&]*?target_handler-ca\.py(&quot;)',
            lambda m: m.group(1) + new_handler + m.group(2),
            content,
        )

        # Compiler Install Path: point every environment at the CC-RH version
        # actually installed on this machine (keep the $(ProgramFilesx86) prefix).
        version = self.detect_ccrh_version()
        if version:
            new_ccrh = rf"$(ProgramFilesx86)\{self.CCRH_SUBDIR}\{version}"
            content = re.sub(
                r'(name="Compiler Install Path"[^>]*?value=")[^"]*(")',
                lambda m: m.group(1) + new_ccrh + m.group(2),
                content,
            )
            print(f"CC-RH compiler detected: {version}")
        else:
            print("[WARN] CC-RH compiler not found; Compiler Install Path left unchanged")

        config_xml.write_text(content, encoding="ISO-8859-1")
        print(f"Initialized configuration paths -> {dst_config}")

    def patch_pdbx(self, old_name, new_name, dst, source_root):
        target = dst / self.PDBX_FILE
        content = target.read_text(encoding="utf-8")
        content = content.replace(f"\\{old_name}", f"\\{new_name}")
        source_root_attrs = re.findall(r'\bsourceRoot="[^"]*"', content)
        if len(source_root_attrs) != 1:
            raise ValueError(f"Expected exactly one sourceRoot attribute in {target}, found {len(source_root_attrs)}")

        escaped_source_root = escape(source_root, {'"': "&quot;", "'": "&apos;"})
        content = re.sub(r'\bsourceRoot="[^"]*"', lambda m: f'sourceRoot="{escaped_source_root}"', content, count=1)
        target.write_text(content, encoding="utf-8")

        tree = ET.parse(target)
        root = tree.getroot()
        root.set("name", new_name)
        tree.write(target, encoding="utf-8", xml_declaration=True)

    def run(self, new_name, source_root):
        is_fail = True
        dst = None
        dst_preexisting = False
        created_dst = False
        try:
            src = self.template
            dst = self.workspace / new_name

            if not src.exists():
                print(f"[FATAL] Template folder not found: {src}")
                sys.exit(1)

            dst_preexisting = dst.exists()
            if dst_preexisting:
                raise FileExistsError(f"Destination already exists: {dst}")

            try:
                shutil.copytree(src, dst)
                created_dst = True
            except Exception as copy_error:
                created_dst = dst.exists() and not isinstance(copy_error, FileExistsError)
                raise

            self.patch_service_properties(dst)
            self.patch_pdbx(src.name, new_name, dst, source_root)
            self.initialize_configuration(dst)

            print(f"Cloned to: {dst}")
            is_fail =  False

        except FileNotFoundError as e:
            print(f"Error: {e}")
        except FileExistsError as e:
            print(f"Error: {e}")
        except PermissionError as e:
            print(f"Permission error: {e}")
        except Exception as e:
            print(f"Unexpected error: {e}")

        if is_fail and created_dst and dst and dst.exists() and not dst_preexisting:
            try:
                shutil.rmtree(dst)
                print(f"Removed incomplete clone: {dst}")
            except Exception as cleanup_error:
                print(f"[WARN] Failed to remove incomplete clone {dst}: {cleanup_error}")

        return is_fail
