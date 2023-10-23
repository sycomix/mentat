from enum import Enum
from pathlib import Path

from termcolor import colored
from typing_extensions import override

from mentat.code_file_manager import CodeFileManager
from mentat.parsers.change_display_helper import (
    DisplayInformation,
    change_delimiter,
    get_file_action_type,
    highlight_text,
)
from mentat.parsers.diff_utils import matching_index
from mentat.parsers.file_edit import FileEdit, Replacement
from mentat.parsers.parser import Parser
from mentat.prompts.prompts import read_prompt

unified_diff_parser_prompt_filename = Path("unified_diff_parser_prompt.txt")


class UnifiedDiffDelimiter(Enum):
    SpecialStart = "---"
    SpecialEnd = "+++"
    MidChange = "@@\n"
    EndChange = "@@end\n"


class UnifiedDiffParser(Parser):
    @override
    def get_system_prompt(self) -> str:
        return read_prompt(unified_diff_parser_prompt_filename)

    @override
    def provide_line_numbers(self) -> bool:
        return False

    @override
    def _code_line_beginning(
        self, display_information: DisplayInformation, cur_block: str
    ) -> str:
        return ""

    @override
    def _code_line_content(
        self,
        display_information: DisplayInformation,
        content: str,
        cur_line: str,
        cur_block: str,
    ) -> str:
        if cur_line == UnifiedDiffDelimiter.MidChange.value:
            return change_delimiter + "\n"
        elif cur_line.startswith("+"):
            return colored(content, "green")
        elif cur_line.startswith("-"):
            return colored(content, "red")
        else:
            return highlight_text(display_information, content)

    @override
    def _could_be_special(self, cur_line: str) -> bool:
        return (
            # Since the model is printing the context lines, we can only
            # highlight them once we get a full line, so we choose to
            # add the lines to the printer all at once.
            not cur_line.endswith("\n")
            or UnifiedDiffDelimiter.EndChange.value.startswith(cur_line)
            or cur_line.startswith(UnifiedDiffDelimiter.SpecialStart.value)
            or cur_line.startswith(UnifiedDiffDelimiter.SpecialEnd.value)
            or UnifiedDiffDelimiter.SpecialStart.value.startswith(cur_line)
            or UnifiedDiffDelimiter.SpecialEnd.value.startswith(cur_line)
        )

    @override
    def _starts_special(self, line: str) -> bool:
        return line.startswith(UnifiedDiffDelimiter.SpecialStart.value)

    @override
    def _ends_special(self, line: str) -> bool:
        return line.startswith(UnifiedDiffDelimiter.SpecialEnd.value)

    @override
    def _special_block(
        self,
        code_file_manager: CodeFileManager,
        git_root: Path,
        rename_map: dict[Path, Path],
        special_block: str,
    ) -> tuple[DisplayInformation, FileEdit, bool]:
        lines = special_block.split("\n")
        file_name = lines[0][4:]
        new_name = lines[1][4:]
        is_creation = file_name == "/dev/null"
        is_deletion = new_name == "/dev/null"
        if is_creation:
            file_name = new_name
        new_name = None if file_name == new_name or is_deletion else Path(new_name)
        file_name = Path(file_name)
        file_lines = self._get_file_lines(code_file_manager, rename_map, file_name)
        file_action_type = get_file_action_type(is_creation, is_deletion, new_name)
        display_information = DisplayInformation(
            file_name, file_lines, [], [], file_action_type, -1, -1, new_name
        )
        file_edit = FileEdit(
            git_root / file_name, [], is_creation, is_deletion, new_name
        )
        # BUG: There is a change delimiter for when it doesn't add anything on a deletion, addition, or rename
        return (display_information, file_edit, True)

    @override
    def _ends_code(self, line: str) -> bool:
        return line.strip() == UnifiedDiffDelimiter.EndChange.value.strip()

    @override
    def _add_code_block(
        self,
        code_file_manager: CodeFileManager,
        rename_map: dict[Path, Path],
        special_block: str,
        code_block: str,
        display_information: DisplayInformation,
        file_edit: FileEdit,
    ) -> str:
        file_lines = self._get_file_lines(
            code_file_manager, rename_map, display_information.file_name
        ).copy()

        # First, we split by the @@ symbols that separate changes.
        lines = code_block.split("\n")
        changes = list[list[str]]()
        cur_lines = list[str]()
        for line in lines:
            if line.strip() in [
                UnifiedDiffDelimiter.MidChange.value.strip(),
                UnifiedDiffDelimiter.EndChange.value.strip(),
            ]:
                changes.append(cur_lines)
                cur_lines = list[str]()
                if line.strip() == UnifiedDiffDelimiter.EndChange.value.strip():
                    break
            else:
                if (
                    # Remove empty lines; hopefully the model always puts a space for context lines
                    line
                    and not line.startswith("+")
                    and not line.startswith("-")
                    and not line.startswith(" ")
                ):
                    return colored(
                        "Error: Invalid diff format given. Discarding this change."
                    )
                cur_lines.append(line)
        if cur_lines:
            changes.append(cur_lines)

        # Next, we collect the - and context lines, search for their locations, and set the replacement ranges
        replacements = list[Replacement]()
        for change in changes:
            # We need both removals and context in this array this one
            search_lines = list[str]()
            for line in change:
                if line.startswith("-") or line.startswith(" "):
                    search_lines.append(line[1:])
            if not search_lines:
                # If the model gave us no context lines, we place at the start of the file;
                # this most commonly happens with imports
                replacements.append(Replacement(0, 0, [line[1:] for line in change]))
                continue

            start_index = matching_index(file_lines, search_lines)
            if start_index == -1:
                return colored(
                    "Error: Original lines not found. Discarding this change.",
                    color="red",
                )

            # We need a separate Replacement whenever context lines are between a group of additions/removals
            cur_start = None
            cur_additions = list[str]()
            cur_index = start_index
            for line in change:
                if line.startswith(" "):
                    if cur_start is not None:
                        replacements.append(
                            Replacement(cur_start, cur_index, cur_additions)
                        )
                    cur_index += 1
                    cur_additions = list[str]()
                    cur_start = None
                elif line.startswith("+"):
                    if cur_start is None:
                        cur_start = cur_index
                    cur_additions.append(line[1:])
                elif line.startswith("-"):
                    if cur_start is None:
                        cur_start = cur_index
                    cur_index += 1
            if cur_start is not None:
                replacements.append(Replacement(cur_start, cur_index, cur_additions))

        file_edit.replacements.extend(replacements)
        return ""
