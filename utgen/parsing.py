from __future__ import annotations

import os
from pathlib import Path

from clang.cindex import CursorKind, Diagnostic, Index, TranslationUnit

from utgen.clang_setup import configure_libclang


_OPERATORS = ("&&", "||", "==", "!=", "<=", ">=", "<<=", ">>=", "<<", ">>",
              "<", ">", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "=",
              "&", "|", "^", "+", "-", "*", "/", "%", "!", "++", "--", "~")


def operator_spelling(cursor) -> str:
    """Return the source operator for a binary, compound, or unary cursor."""

    operator = getattr(cursor, "binary_operator", None)
    if operator is not None:
        direct = {"Assign": "=", "LAnd": "&&", "LOr": "||"}.get(
            getattr(operator, "name", operator), "")
        if direct:
            return direct

    spelling = getattr(cursor, "spelling", "")
    if spelling in _OPERATORS:
        return spelling

    children = list(cursor.get_children())
    tokens = list(cursor.get_tokens())
    if cursor.kind == CursorKind.UNARY_OPERATOR:
        if len(children) == 1:
            child = children[0]
            child_start, child_end = child.extent.start.offset, child.extent.end.offset
            for token in reversed(tokens):
                if token.extent.end.offset <= child_start and token.spelling in _OPERATORS:
                    return token.spelling
            for token in tokens:
                if token.extent.start.offset >= child_end and token.spelling in _OPERATORS:
                    return token.spelling
        return next((token.spelling for token in tokens if token.spelling in _OPERATORS), "")

    if len(children) >= 2:
        left_end = children[0].extent.end.offset
        for token in tokens:
            if token.extent.start.offset >= left_end and token.spelling in _OPERATORS:
                return token.spelling
    return next((token.spelling for token in tokens if token.spelling in _OPERATORS), "")


class ParseError(Exception):
    """Raised when libclang cannot safely continue parsing the source file."""


class ClangParser:
    """Convert a C source file into the libclang TranslationUnit used by UTGen."""

    def parse(self, source: Path, includes: list, defines=()) -> TranslationUnit:
        """Parse one C source file with the include paths and preprocessor defines from UTGen."""

        configure_libclang(None)

        # Build the same compiler context the source expects.  Clang needs the
        # include search paths and preprocessor symbols to resolve the AST
        # correctly; otherwise declarations may be missing or parsed incorrectly.
        args = ["-x", "c"]
        for inc in includes:
            args.append("-I" + str(inc))
        for define in defines:
            # Accept config values in either "NAME=VALUE" or "-DNAME=VALUE" form.
            args.append(str(define) if str(define).startswith("-D") else "-D" + str(define))

        # Keep preprocessing entities such as macro definitions in the
        # TranslationUnit.  ModuleIndexer later walks these cursors to collect
        # macros in addition to normal declarations and function bodies.
        tu = Index.create().parse(
            str(source), args=args,
            options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

        # Only fatal diagnostics stop this stage.  Lower-severity diagnostics
        # remain attached to the TranslationUnit, while a fatal parse means the
        # resulting AST cannot be trusted for the later UTGen analysis stages.
        fatals = []
        for diag in tu.diagnostics:
            if diag.severity >= Diagnostic.Fatal:
                fatals.append(str(diag))
        if fatals:
            raise ParseError(f"{source}: " + "; ".join(fatals))
        return tu


def function_definitions(tu: TranslationUnit) -> dict:
    """Return function definitions that physically belong to the parsed source file."""

    # A TranslationUnit also contains declarations/definitions reached through
    # headers.  Normalize paths first so Windows path casing/separators do not
    # cause the main source file to be compared incorrectly.
    main_file = os.path.normcase(os.path.normpath(str(tu.spelling)))
    out = {}
    for cursor in tu.cursor.get_children():
        if cursor.kind != CursorKind.FUNCTION_DECL or not cursor.is_definition():
            continue

        # Keep only functions implemented in this .c file.  Header functions or
        # other included definitions are dependencies, not UTGen target functions.
        loc = cursor.location.file
        if loc and os.path.normcase(os.path.normpath(str(loc))) == main_file:
            out[cursor.spelling] = cursor
    return out
