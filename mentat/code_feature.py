from __future__ import annotations

import asyncio
import math
import os
from enum import Enum
from pathlib import Path
from typing import Optional

from mentat.code_map import get_code_map, get_ctags
from mentat.diff_context import annotate_file_message, parse_diff
from mentat.git_handler import get_diff_for_file
from mentat.interval import Interval, parse_intervals
from mentat.llm_api import count_tokens
from mentat.session_context import SESSION_CONTEXT
from mentat.utils import sha256

MIN_INTERVAL_LINES = 10


def split_file_into_intervals(
    git_root: Path,
    feature: CodeFeature,
    min_lines: int | None = None,
    user_features: list[CodeFeature] = [],
) -> list[CodeFeature]:
    session_context = SESSION_CONTEXT.get()
    code_file_manager = session_context.code_file_manager
    n_lines = len(code_file_manager.read_file(feature.path))

    if feature.level != CodeMessageLevel.CODE:
        return [feature]

    # Get ctags data (name and start line) and determine end line
    ctags = list(get_ctags(git_root.joinpath(feature.path)))
    ctags = sorted(ctags, key=lambda x: int(x[4]))  # type: ignore
    named_intervals = list[tuple[str, int, int]]()  # Name, Start, End
    _last_item = tuple[str, int]()
    for i, tag in enumerate(ctags):
        (scope, _, name, _, line_number) = tag  # Kind and Signature ignored
        if name is None or line_number is None:
            continue
        key = name
        if scope is not None:
            key = f"{scope}.{name}"
        if _last_item:
            _last_item_length = int(line_number) - _last_item[1]
            min_lines = min_lines or MIN_INTERVAL_LINES
            if _last_item_length < min_lines:
                line_number = _last_item[1]
            else:
                named_intervals.append(
                    (_last_item[0], _last_item[1], int(line_number) - 1)  # type: ignore
                )
        else:
            line_number = 0
        if i == len(ctags) - 1:
            named_intervals.append((str(key), int(line_number), n_lines))
        else:
            _last_item = (key, int(line_number))

    if len(named_intervals) <= 1:
        return [feature]

    # Create and return separate features for each interval
    _features = list[CodeFeature]()
    for name, start, end in named_intervals:
        _user_included = any(
            u.contains_line(i) for u in user_features for i in range(start, end + 1)
        )
        feature_string = f"{feature.path}:{start}-{end}"
        _feature = CodeFeature(
            feature_string,
            level=CodeMessageLevel.INTERVAL,
            diff=feature.diff,
            user_included=_user_included,
            name=name,
        )
        _features.append(_feature)
    return _features


class CodeMessageLevel(Enum):
    CODE = ("code", 1, "Complete code")
    INTERVAL = ("interval", 2, "Specific range(s)")
    CMAP_FULL = ("cmap_full", 3, "Function/Class names and signatures")
    CMAP = ("cmap", 4, "Function/Class names")
    FILE_NAME = ("file_name", 5, "Relative path/filename")

    def __init__(self, key: str, rank: int, description: str):
        self.key = key
        self.rank = rank
        self.description = description


class CodeFeature:
    """
    Represents a section of the code_message which is included with the prompt.
    Includes a section of code and an annotation method.

    Attributes:
        path: The absolute path to the file.
        intervals: The lines in the file.
        level: The level of information to include.
        diff: The diff annotations to include.
    """

    def __init__(
        self,
        path: str | Path,
        level: CodeMessageLevel = CodeMessageLevel.CODE,
        diff: str | None = None,
        user_included: bool = False,
        name: Optional[str] = None,
    ):
        if Path(path).exists():
            self.path = Path(path)
            self.intervals = [Interval(0, math.inf)]
        else:
            path = str(path)
            split = path.rsplit(":", 1)
            self.path = Path(split[0])
            if not self.path.exists():
                self.path = Path(path)
                self.intervals = [Interval(0, math.inf)]
            else:
                self.intervals = parse_intervals(split[1])
                level = CodeMessageLevel.INTERVAL
        self.level = level
        self.diff = diff
        self.user_included = user_included
        self.name = name

    def __repr__(self):
        return (
            f"CodeFeature(fname={self.path.name}, intervals={self.intervals},"
            f" level={self.level}, diff={self.diff})"
        )

    def ref(self):
        if self.level == CodeMessageLevel.INTERVAL:
            intervals_string = ",".join(f"{i.start}-{i.end}" for i in self.intervals)
            return f"{self.path}:{intervals_string}"
        return str(self.path)

    def contains_line(self, line_number: int):
        return any([interval.contains(line_number) for interval in self.intervals])

    def _get_code_message(self) -> list[str]:
        session_context = SESSION_CONTEXT.get()
        code_file_manager = session_context.code_file_manager
        git_root = session_context.git_root
        parser = session_context.parser

        code_message: list[str] = []

        # We always want to give GPT posix paths
        abs_path = Path(git_root / self.path)
        rel_path = Path(os.path.relpath(abs_path, git_root))
        posix_rel_path = Path(rel_path).as_posix()
        filename = f"{posix_rel_path}"
        code_message.append(filename)

        if self.level in {CodeMessageLevel.CODE, CodeMessageLevel.INTERVAL}:
            file_lines = code_file_manager.read_file(abs_path)
            for i, line in enumerate(file_lines, start=1):
                if self.contains_line(i):
                    if parser.provide_line_numbers():
                        code_message.append(f"{i}:{line}")
                    else:
                        code_message.append(f"{line}")
        elif self.level == CodeMessageLevel.CMAP_FULL:
            cmap = get_code_map(git_root.joinpath(self.path))
            code_message += cmap
        elif self.level == CodeMessageLevel.CMAP:
            cmap = get_code_map(git_root.joinpath(self.path), exclude_signatures=True)
            code_message += cmap
        code_message.append("")

        if self.diff is not None:
            diff: str = get_diff_for_file(self.diff, rel_path)
            diff_annotations = parse_diff(diff)
            if self.level == CodeMessageLevel.CODE:
                code_message = annotate_file_message(code_message, diff_annotations)
            else:
                for section in diff_annotations:
                    code_message += section.message
        return code_message

    def get_checksum(self) -> str:
        session_context = SESSION_CONTEXT.get()
        code_file_manager = session_context.code_file_manager
        git_root = session_context.git_root

        abs_path = git_root / self.path
        file_checksum = code_file_manager.get_file_checksum(
            Path(abs_path), self.intervals
        )
        return sha256(f"{file_checksum}{self.level.key}{self.diff}")

    _feature_checksum: str | None = None
    _code_message: list[str] | None = None

    def get_code_message(self) -> list[str]:
        feature_checksum = self.get_checksum()
        if feature_checksum != self._feature_checksum or self._code_message is None:
            self._feature_checksum = feature_checksum
            self._code_message = self._get_code_message()
        return self._code_message

    def count_tokens(self, model: str) -> int:
        code_message = self.get_code_message()
        return count_tokens("\n".join(code_message), model)


async def count_feature_tokens(features: list[CodeFeature], model: str) -> list[int]:
    """Return the number of tokens in each feature."""
    sem = asyncio.Semaphore(10)

    async def _count_tokens(feature: CodeFeature) -> int:
        async with sem:
            return feature.count_tokens(model)

    tasks = [_count_tokens(f) for f in features]
    return await asyncio.gather(*tasks)
