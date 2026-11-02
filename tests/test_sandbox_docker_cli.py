import subprocess

from harness.sandbox.docker_cli import DockerCli


def test_docker_cli_uses_shell_false(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr("shutil.which", lambda _: "docker")
    monkeypatch.setattr(subprocess, "run", fake_run)
    cli = DockerCli()
    result = cli.run(["ps", "--format", "{{.ID}}"])
    assert result.returncode == 0
    assert captured["kwargs"]["shell"] is False
    assert captured["argv"][1:] == ["ps", "--format", "{{.ID}}"]
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
