"""Unit tests for the Assay CLI and configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from typer.testing import CliRunner

from assay.cli.main import app
from assay.config.settings import DEFAULT_REDACTION_FIELDS, AssaySettings

runner = CliRunner()


def test_cli_help_lists_subcommands() -> None:
    """Running `assay --help` should display all baseline subcommands."""
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command_name in ("init", "record", "recordings", "replay", "verify", "verify-evidence"):
        assert command_name in result.stdout


def test_cli_init_invokes_workspace_initialization(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Running `assay init` should call workspace initialization without touching cwd."""
    mock_config = tmp_path / ".assay.yaml"
    mock_priv = tmp_path / ".assay" / "keys" / "private_key.pem"
    mock_pub = tmp_path / ".assay" / "keys" / "public_key.pem"

    mock_init = mocker.patch(
        "assay.cli.main.initialize_workspace",
        return_value=(mock_config, mock_priv, mock_pub),
    )

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    mock_init.assert_called_once_with(force=False)
    assert "Assay workspace initialized successfully" in result.stdout


def test_cli_init_handles_os_error(mocker: MockerFixture) -> None:
    """Filesystem errors during `assay init` should exit with non-zero status."""
    mocker.patch(
        "assay.cli.main.initialize_workspace",
        side_effect=OSError("Permission denied"),
    )

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "Initialization failed" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["record"],
        ["replay", "--recording", "session.json"],
        ["replay", "--target", "http://localhost:8000"],
        ["verify", "--recording", "session.json", "--baseline", "http://localhost:8001"],
    ],
)
def test_cli_missing_required_arguments_fail(args: list[str]) -> None:
    """Missing required CLI options should return a non-zero exit code."""
    result = runner.invoke(app, args)

    assert result.exit_code != 0
    output = result.stdout + (result.stderr or "")
    assert "Missing option" in output or "Error" in output


def test_cli_record_replay_verify_and_recordings_list(
    mocker: MockerFixture,
    tmp_path: Path,
) -> None:
    """Skeleton commands should execute cleanly with valid arguments."""
    sessions_dir = tmp_path / ".assay" / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "sample.json").write_text("{}", encoding="utf-8")

    settings = AssaySettings()
    settings.storage.sessions_dir = sessions_dir
    mocker.patch("assay.cli.main.load_settings", return_value=settings)
    mock_record = mocker.patch("assay.cli.main.run_record_command")
    mock_replay = mocker.patch("assay.cli.main.run_replay_command")
    mock_verify = mocker.patch("assay.cli.main.run_verify_command")

    record_res = runner.invoke(
        app, ["record", "--target", "http://127.0.0.1:8000", "--port", "9090"]
    )
    assert record_res.exit_code == 0
    mock_record.assert_called_once_with(
        target="http://127.0.0.1:8000",
        port=9090,
        session_id=None,
        ui_url=None,
        browser="chromium",
    )

    list_res = runner.invoke(app, ["recordings", "list"])
    assert list_res.exit_code == 0
    assert "sample.json" in list_res.stdout

    replay_res = runner.invoke(
        app,
        [
            "replay",
            "--recording",
            "sample.json",
            "--target",
            "http://127.0.0.1:8000",
            "--concurrency",
            "4",
            "--header",
            "Authorization: Bearer fresh",
        ],
    )
    assert replay_res.exit_code == 0
    mock_replay.assert_called_once_with(
        recording="sample.json",
        target="http://127.0.0.1:8000",
        concurrency=4,
        headers=["Authorization: Bearer fresh"],
        ui_target=None,
        max_retries=None,
    )

    verify_res = runner.invoke(
        app,
        [
            "verify",
            "--recording",
            "sample.json",
            "--baseline",
            "http://127.0.0.1:8001",
            "--candidate",
            "http://127.0.0.1:8002",
            "--ignore-path",
            "$.created_at",
            "--ignore-path",
            "$.id",
        ],
    )
    assert verify_res.exit_code == 0
    mock_verify.assert_called_once_with(
        recording="sample.json",
        baseline="http://127.0.0.1:8001",
        candidate="http://127.0.0.1:8002",
        ignore_path=["$.created_at", "$.id"],
        concurrency=1,
    )


def test_settings_defaults_and_yaml_override(tmp_path: Path) -> None:
    """AssaySettings should apply defaults and parse nested YAML overrides."""
    defaults = AssaySettings()
    assert defaults.storage.sessions_dir == Path("./.assay/sessions")
    assert defaults.storage.keys_dir == Path("./.assay/keys")
    assert defaults.redaction.fields == DEFAULT_REDACTION_FIELDS
    assert defaults.network.timeout_seconds == 10.0
    assert defaults.network.proxy_host == "127.0.0.1"
    assert defaults.network.proxy_port == 9000

    yaml_file = tmp_path / ".assay.yaml"
    yaml_file.write_text(
        "\n".join(
            [
                "storage:",
                "  sessions_dir: ./custom/sessions",
                "  keys_dir: ./custom/keys",
                "network:",
                "  timeout_seconds: 25",
                "  proxy_host: 0.0.0.0",
                "  proxy_port: 8888",
                "redaction:",
                "  fields:",
                "    - X-Api-Key",
                "    - secret",
            ]
        ),
        encoding="utf-8",
    )

    loaded = AssaySettings.from_yaml(yaml_file)
    assert loaded.storage.sessions_dir == Path("./custom/sessions")
    assert loaded.storage.keys_dir == Path("./custom/keys")
    assert loaded.network.timeout_seconds == 25.0
    assert loaded.network.proxy_host == "0.0.0.0"
    assert loaded.network.proxy_port == 8888
    assert loaded.redaction.fields == ["X-Api-Key", "secret"]
