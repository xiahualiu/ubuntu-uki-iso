"""
VM hardware, firmware persistence and trustworthy guest-result handling.
"""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.qemu import harness
from ubuntu_uki_iso.qemu.folders import FolderLayout


def machine(tmp_path):
    vm = harness.Harness(FolderLayout(tmp_path))
    vm.layout.vm.mkdir()
    vm.layout.vm_logs.mkdir()
    variables = tmp_path / "template.fd"
    variables.write_bytes(b"firmware-vars")
    vm.ovmf_vars = str(variables)
    vm.ovmf_code = str(tmp_path / "code.fd")
    return vm


def test_hardware_matches_workstation_and_usb_boot(tmp_path):
    vm = machine(tmp_path)
    args = vm._qemu_argv("live")
    devices = [args[i + 1] for i, value in enumerate(args) if value == "-device"]
    disks = [value for value in devices if value.startswith("ide-hd,")]
    assert len(disks) == 8
    assert all("rotation_rate=7200" in disk for disk in disks)
    assert len({disk.split("bus=")[1].split(",")[0] for disk in disks}) == 8
    assert any("bus=sata1.1" in disk for disk in disks)
    assert any(value.startswith("nvme,") for value in devices)
    assert any(value.startswith("usb-storage,") and "bootindex=1" in value for value in devices)
    assert "-bios" not in args and "-cdrom" not in args and "-boot" not in args
    assert "e1000e,netdev=net0" in devices
    assert sum("if=pflash" in value for value in args) == 2
    reboot = vm._qemu_argv("live", installed=True, data=False)
    assert not any("usb-storage" in value or "ide-hd" in value for value in reboot)
    assert any("nvme," in value and "bootindex=1" in value for value in reboot)


def test_sparse_disks_have_decimal_hardware_capacities(tmp_path, monkeypatch):
    vm = machine(tmp_path)
    run = Mock()
    monkeypatch.setattr(harness.subprocess, "run", run)
    vm._reset_disks()
    calls = [call.args[0] for call in run.call_args_list]
    assert len(calls) == 9
    assert calls[0][-1] == "1000000000000"
    assert all(call[-1] == "8000000000000" for call in calls[1:])
    assert all(call[:4] == ["qemu-img", "create", "-f", "qcow2"] for call in calls)
    assert all(Path(call[-2]).parent == vm.layout.vm for call in calls)


def test_ovmf_requires_matching_writable_vars_template(tmp_path, monkeypatch):
    vm = machine(tmp_path)
    vm.layout.iso.touch()
    monkeypatch.setattr(harness.shutil, "which", lambda tool: tool)
    monkeypatch.setattr(harness, "OVMF_PAIRS", ((vm.ovmf_code, str(tmp_path / "missing-vars")),))
    Path(vm.ovmf_code).touch()
    with pytest.raises(BuildError, match="CODE/VARS"):
        vm.prepare()


@pytest.mark.parametrize(
    ("text", "returncode", "expected"),
    [
        ("echo UBUNTU_UKI_TEST_DONE:0", 0, False),
        ("UBUNTU_UKI_TEST_DONE:1\n", 0, False),
        ("UBUNTU_UKI_TEST_DONE:0\n", 42, False),
        ("UBUNTU_UKI_TEST_DONE:0\n", 0, True),
    ],
)
def test_guest_success_requires_real_marker_and_clean_exit(
    tmp_path, monkeypatch, text, returncode, expected
):
    vm = machine(tmp_path)

    def execute(argv, **kwargs):
        kwargs["stdout"].write(text)
        return subprocess.CompletedProcess(argv, returncode)

    monkeypatch.setattr(harness.subprocess, "run", execute)
    assert vm._guest_run("test", "true") is expected
    variables = vm.layout.vm / "test-VARS.fd"
    assert variables.read_bytes() == b"firmware-vars"
    variables.write_bytes(b"persisted-install-entry")
    vm._guest_run("test", "true", installed=True)
    assert variables.read_bytes() == b"persisted-install-entry"


@pytest.mark.parametrize("name", harness.TEST_NAMES)
def test_guest_scripts_are_valid_shell_and_install_reboots_without_usb(tmp_path, monkeypatch, name):
    vm = machine(tmp_path)
    monkeypatch.setattr(vm, "_reset_disks", lambda: None)
    boots = []

    def execute(name, command, **kwargs):
        subprocess.run(["bash", "-n"], input=command, text=True, check=True)
        boots.append((command, kwargs))
        return True

    monkeypatch.setattr(vm, "_guest_run", execute)
    assert vm.run(name)
    if name == "install":
        assert len(boots) == 3
        assert boots[1][1] == {"installed": True}
        assert boots[2][1] == {"installed": True, "data": False}
        assert "cmp /tmp/efi-before /tmp/efi-after" in boots[0][0]
        assert "--root-disk" not in boots[0][0] and "--data-disks" not in boots[0][0]


def test_expect_uses_bounded_lines_and_hides_assertions_from_terminal_echo(tmp_path):
    vm = machine(tmp_path)
    command = "echo secret-check\n" * 1000
    script = vm._expect_script("live", command)
    assert command not in script
    assert "send -s --" in script
    assert "UBUNTU_UKI_TEST_%s" in script
    assert "bash -euo pipefail" in script
    assert "vmtest" in vm._expect_script("live", "true", installed=True)


@pytest.mark.parametrize(("command", "status"), [('printf "hello\\n"', 0), ("false", 1)])
def test_serial_script_round_trips_through_tcl_and_reports_shell_failures(
    tmp_path, command, status
):
    import base64
    import shutil

    tcl = shutil.which("tclsh")
    if tcl is None:
        pytest.skip("tclsh unavailable")
    vm = machine(tmp_path)
    script = vm._expect_script("live", command)
    prefix = "proc log_user {args} {}\nproc spawn {args} {}\nproc expect {args} {}\nproc send {args} { lappend ::sent [lindex $args end] }\n"
    script = prefix + script.replace(
        "exit $rc", "puts [binary encode base64 -maxlen 0 [lindex $::sent 0]]"
    )
    result = subprocess.run([tcl], input=script, text=True, capture_output=True, check=True)
    assert not result.stderr
    launch = base64.b64decode(result.stdout.strip()).decode().replace("\r", "\n")
    launch = launch.replace("/tmp/vm-check", str(tmp_path / "guest-check"))
    output = subprocess.run(["bash"], input=launch, text=True, capture_output=True, check=True)
    assert f"UBUNTU_UKI_TEST_DONE:{status}" in output.stdout
