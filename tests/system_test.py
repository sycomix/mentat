import os
from pathlib import Path
from textwrap import dedent

import pytest

from mentat.session import Session


@pytest.mark.asyncio
async def test_system(mock_call_llm_api, mock_setup_api_key, mock_collect_user_input):
    # Create a temporary file
    temp_file_name = Path("temp.py")
    with open(temp_file_name, "w") as f:
        f.write("# This is a temporary file.")

    mock_collect_user_input.set_stream_messages(
        [
            "Add changes to the file",
            "i",
            "y",
            "n",
            "y",
            "q",
        ]
    )

    mock_call_llm_api.set_generator_values([dedent("""\
        I will add a print statement.

        Steps:
        1. Add a print statement after the last line

        @@start
        {{
            "file": "{file_name}",
            "action": "replace",
            "start-line": 1,
            "end-line": 1
        }}
        @@code
        print("Hello, world!")
        @@end""".format(file_name=temp_file_name))])

    session = await Session.create([temp_file_name])
    await session.start()
    await session.stream.stop()

    # Check if the temporary file is modified as expected
    with open(temp_file_name, "r") as f:
        content = f.read()
        expected_content = 'print("Hello, world!")'
    assert content == expected_content


@pytest.mark.asyncio
async def test_interactive_change_selection(
    mock_call_llm_api, mock_setup_api_key, mock_collect_user_input
):
    # Create a temporary file
    temp_file_name = Path("temp_interactive.py")
    with open(temp_file_name, "w") as f:
        f.write("# This is a temporary file for interactive test.")

    mock_collect_user_input.set_stream_messages(
        [
            "Add changes to the file",
            "i",
            "y",
            "n",
            "y",
            "q",
        ]
    )

    mock_call_llm_api.set_generator_values([dedent("""\
        I will make three changes to the file.

        Steps:
        1. Replace the comment with print("Change 1")
        2. Add print("Change 2") after the first line
        3. Add print("Change 3") after the second line

        @@start
        {{
            "file": "{file_name}",
            "action": "replace",
            "start-line": 1,
            "end-line": 1
        }}
        @@code
        print("Change 1")
        @@end
        @@start
        {{
            "file": "{file_name}",
            "action": "insert",
            "insert-after-line": 1,
            "insert-before-line": 2
        }}
        @@code
        print("Change 2")
        @@end
        @@start
        {{
            "file": "{file_name}",
            "action": "insert",
            "insert-after-line": 2,
            "insert-before-line": 3
        }}
        @@code
        print("Change 3")
        @@end""".format(file_name=temp_file_name))])

    session = await Session.create([temp_file_name])
    await session.start()
    await session.stream.stop()

    # Check if the temporary file is modified as expected
    with open(temp_file_name, "r") as f:
        content = f.read()
        expected_content = 'print("Change 1")\n\nprint("Change 3")'

    assert content == expected_content


# Makes sure we're properly turning the model output into correct path no matter the os
@pytest.mark.asyncio
async def test_without_os_join(
    mock_call_llm_api, mock_setup_api_key, mock_collect_user_input
):
    temp_dir = "dir"
    temp_file_name = "temp.py"
    temp_file_path = Path(os.path.join(temp_dir, temp_file_name))
    os.makedirs(temp_dir, exist_ok=True)
    with open(temp_file_path, "w") as f:
        f.write("# This is a temporary file.")

    mock_collect_user_input.set_stream_messages(
        ['Replace comment with print("Hello, world!")', "y", "q"]
    )

    # Use / here since that should always be what the model outputs
    fake_file_path = f"{temp_dir}/{temp_file_name}"
    mock_call_llm_api.set_generator_values([dedent("""\
        I will add a print statement.
        Steps:
        1. Add a print statement after the last line
        @@start
        {{
            "file": "{file_name}",
            "action": "replace",
            "start-line": 1,
            "end-line": 1
        }}
        @@code
        print("Hello, world!")
        @@end""".format(file_name=fake_file_path))])
    session = await Session.create([temp_file_path])
    await session.start()
    await session.stream.stop()
    mock_collect_user_input.reset_mock()
    with open(temp_file_path, "r") as f:
        content = f.read()
        expected_content = 'print("Hello, world!")'
    assert content == expected_content
