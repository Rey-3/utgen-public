"""Translation-unit building and per-module function discovery."""
from __future__ import annotations

import os
from pathlib import Path

from clang.cindex import CursorKind, Diagnostic, Index, TranslationUnit

from utgen.clang_setup import configure_libclang


class ParseError(Exception):
    """Raised when clang reports fatal diagnostics for a source file (K6)."""


class ClangParser:
    """Parses one C source file into a TranslationUnit.

    Detailed preprocessing record is enabled so macro definitions are visible
    to the module indexer (Task 9 / spec L4).
    """

    def parse(self, source: Path, includes: list) -> TranslationUnit:
        configure_libclang(None)
        args = ["-x", "c"]
        for inc in includes:
            args.append("-I" + str(inc))
        tu = Index.create().parse(
            str(source), args=args,
            options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        fatals = []
        for diag in tu.diagnostics:
            if diag.severity >= Diagnostic.Fatal:
                fatals.append(str(diag))
        if fatals:
            raise ParseError(f"{source}: " + "; ".join(fatals))
        return tu


def function_definitions(tu: TranslationUnit) -> dict:
    """Functions DEFINED in the parsed .c itself, by name.

    Static functions included (K3); functions defined in included headers
    (inline helpers) excluded via the location filter (K10).
    """
    main_file = os.path.normcase(os.path.normpath(str(tu.spelling)))
    out = {}
    for cursor in tu.cursor.get_children():
        if cursor.kind != CursorKind.FUNCTION_DECL or not cursor.is_definition():
            continue
        loc = cursor.location.file
        if loc and os.path.normcase(os.path.normpath(str(loc))) == main_file:
            out[cursor.spelling] = cursor
    return out
