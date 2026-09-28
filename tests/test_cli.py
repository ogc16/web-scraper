"""CLI surface: argument handling, exit codes and rendering.

The exit codes are a documented contract, so they are asserted literally rather
than symbolically. Output is captured through `capsys` so the tests never touch
a real terminal, and the network-dependent commands are exercised against the
loopback fixture server instead of the public internet.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from awsa import __version__, cli
from awsa.agent.loop import AgentResult
from awsa.agent.spec import ResearchSpec
from awsa.cli import (
    EXIT_BLOCKED,
    EXIT_EMPTY,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    _config_from,
    _normalise_argv,
    _split_fields,
    build_parser,
    main,
)
from awsa.config import Config
from awsa.models import Budget, Claim, Evidence, ResearchReport, Usage
from awsa.observability import Trace
from conftest import FixtureServer

# pytest 9 made CaptureFixture generic in the captured stream type.
Capture = pytest.CaptureFixture[str]


@pytest.fixture(autouse=True)
def fast_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make CLI runs fast, hermetic and key-free.

    `main()` builds its config from the real environment rather than from the
    test `config` fixture, so pacing, cache location and credentials have to be
    pinned here or the suite would inherit production delays and whatever keys
    happen to be set on the developer's machine.
    """
    monkeypatch.setenv("AWSA_PER_HOST_DELAY", "0")
    monkeypatch.setenv("AWSA_TIMEOUT", "5")
    monkeypatch.setenv("AWSA_CACHE_DIR", str(tmp_path / "cache"))
    for name in (
        "AWSA_OPENAI_API_KEY",
        "AWSA_BRAVE_API_KEY",
        "AWSA_BRIGHTDATA_API_TOKEN",
        "AWSA_FIRECRAWL_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open the SSRF guard to loopback so the fixture server is reachable.

    There is deliberately no CLI flag for this: it is an env-var escape hatch
    for local development and tests, not a user-facing research option.
    """
    monkeypatch.setenv("AWSA_ALLOW_PRIVATE_HOSTS", "1")


class TestParser:
    def test_subjects_become_research(self) -> None:
        # `awsa "Grace Hopper"` is shorthand for `awsa research "Grace Hopper"`.
        # The shorthand is applied before parsing, so tests must normalise first.
        args = build_parser().parse_args(_normalise_argv(["Grace", "Hopper"]))
        assert args.command == "research"
        assert args.subject == "Grace Hopper"

    def test_unquoted_and_quoted_subjects_agree(self) -> None:
        quoted = build_parser().parse_args(_normalise_argv(["Grace Hopper"]))
        unquoted = build_parser().parse_args(_normalise_argv(["Grace", "Hopper"]))
        assert quoted.subject == unquoted.subject == "Grace Hopper"

    def test_shorthand_keeps_trailing_options(self) -> None:
        args = build_parser().parse_args(_normalise_argv(["Grace", "Hopper", "-f", "city"]))
        assert args.command == "research"
        assert args.subject == "Grace Hopper"
        assert _split_fields(args.fields, None) == ["city"]

    def test_explicit_research_subcommand(self) -> None:
        args = build_parser().parse_args(_normalise_argv(["research", "Ada Lovelace"]))
        assert args.command == "research"
        assert args.subject == "Ada Lovelace"

    def test_subcommand_is_not_mistaken_for_a_subject(self) -> None:
        assert _normalise_argv(["fetch", "https://example.com"]) == [
            "fetch",
            "https://example.com",
        ]

    def test_leading_flag_is_not_mistaken_for_a_subject(self) -> None:
        assert _normalise_argv(["--version"]) == ["--version"]

    def test_fields_are_repeatable(self) -> None:
        args = build_parser().parse_args(_normalise_argv(["X", "-f", "city", "-f", "employer"]))
        assert _split_fields(args.fields, None) == ["city", "employer"]

    def test_fields_split_on_commas(self) -> None:
        args = build_parser().parse_args(_normalise_argv(["X", "-f", "city,employer,country"]))
        assert _split_fields(args.fields, None) == ["city", "employer", "country"]

    def test_repeated_and_comma_forms_combine(self) -> None:
        args = build_parser().parse_args(_normalise_argv(["X", "-f", "a,b", "-f", "c"]))
        assert _split_fields(args.fields, None) == ["a", "b", "c"]

    def test_field_hints_survive_colons(self) -> None:
        args = build_parser().parse_args(_normalise_argv(["X", "-f", "founded:when founded"]))
        assert _split_fields(args.fields, None) == ["founded:when founded"]

    def test_blank_field_entries_are_dropped(self) -> None:
        assert _split_fields(["a,,b", " , "], None) == ["a", "b"]

    def test_inline_fields_are_accepted(self) -> None:
        args = build_parser().parse_args(_normalise_argv(["X", "-f", "one,two"]))
        assert _split_fields(args.fields, None) == ["one", "two"]


class TestExitCodes:
    def test_no_arguments_prints_help_and_exits_usage(self, capsys: Capture) -> None:
        assert main([]) == EXIT_USAGE
        out = capsys.readouterr().out
        assert "usage: awsa" in out

    def test_version(self, capsys: Capture) -> None:
        assert main(["--version"]) == EXIT_OK
        assert __version__ in capsys.readouterr().out

    def test_help_exits_zero(self, capsys: Capture) -> None:
        assert main(["--help"]) == EXIT_OK
        assert "usage: awsa" in capsys.readouterr().out

    def test_providers_succeeds_with_no_keys(self, capsys: Capture) -> None:
        # The zero-key promise: the autouse fixture clears every credential, so
        # this asserts the stack still resolves on a bare machine.
        assert main(["providers"]) == EXIT_OK
        out = capsys.readouterr().out
        assert "duckduckgo" in out
        assert "extractive" in out
        assert "no key needed" in out

    def test_providers_json_is_parseable(self, capsys: Capture) -> None:
        assert main(["providers", "--json"]) == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert "search" in payload or "search_providers" in payload

    def test_bad_budget_is_a_usage_error(self, capsys: Capture) -> None:
        # main() absorbs argparse's SystemExit and returns a code instead.
        assert main(["X", "--budget", "enormous"]) == EXIT_USAGE

    def test_unknown_provider_is_a_usage_error(self, capsys: Capture) -> None:
        assert main(["X", "--search-provider", "nope-not-real"]) == EXIT_USAGE
        assert "error:" in capsys.readouterr().err

    def test_unsafe_url_is_reported(self, capsys: Capture) -> None:
        # The SSRF guard must refuse a private target rather than fetching it.
        code = main(["fetch", "http://169.254.169.254/latest/meta-data/"])
        assert code in {EXIT_BLOCKED, EXIT_FAILED}
        assert capsys.readouterr().err


class TestFetch:
    def test_fetches_and_prints_text(
        self, capsys: Capture, server: FixtureServer, allow_loopback: None
    ) -> None:
        code = main(["fetch", server.url("/people")])
        assert code == EXIT_OK
        assert capsys.readouterr().out.strip()

    def test_json_output_is_parseable(
        self, capsys: Capture, server: FixtureServer, allow_loopback: None
    ) -> None:
        code = main(["fetch", server.url("/people"), "--json"])
        assert code == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert "url" in payload

    def test_raw_flag_prints_html(
        self, capsys: Capture, server: FixtureServer, allow_loopback: None
    ) -> None:
        assert main(["fetch", server.url("/people"), "--raw"]) == EXIT_OK
        assert "<" in capsys.readouterr().out


class TestRobots:
    def test_allowed_path_exits_ok(
        self, capsys: Capture, server: FixtureServer, allow_loopback: None
    ) -> None:
        code = main(["robots", server.url("/people")])
        assert code == EXIT_OK
        assert "allow" in capsys.readouterr().out.lower()

    def test_disallowed_prefix_exits_blocked(
        self, capsys: Capture, server: FixtureServer, allow_loopback: None
    ) -> None:
        # robots.txt carries `Disallow: /private/`, so the path must match it.
        code = main(["robots", server.url("/private/ledger")])
        assert code == EXIT_BLOCKED
        assert "disallow" in capsys.readouterr().out.lower()

    def test_disallowed_exact_path_exits_blocked(
        self, capsys: Capture, server: FixtureServer, allow_loopback: None
    ) -> None:
        code = main(["robots", server.url("/admin")])
        assert code == EXIT_BLOCKED
        assert "disallow" in capsys.readouterr().out.lower()

    def test_sibling_of_a_disallowed_prefix_is_allowed(
        self, capsys: Capture, server: FixtureServer, allow_loopback: None
    ) -> None:
        # `/private/ledger` is blocked, but `/private` (no trailing slash) is not
        # matched by `Disallow: /private/`, and `/privates` must not match
        # either. Prefix rules are not substring rules.
        assert main(["robots", server.url("/private")]) == EXIT_OK
        assert main(["robots", server.url("/privates")]) == EXIT_OK
        capsys.readouterr()


class TestDoctor:
    def test_doctor_runs_and_reports(self, capsys: Capture) -> None:
        code = main(["doctor"])
        # Doctor probes the network, so only assert it does not crash and says
        # something useful rather than pinning a pass/fail that needs internet.
        assert code in {EXIT_OK, EXIT_FAILED}
        assert capsys.readouterr().out.strip()


class _StubAgent:
    """Stands in for the real agent so CLI behaviour is tested without network.

    The CLI contract under test is "format the report, choose an exit code,
    honour --output" - none of which should require a live search backend.
    """

    report: ResearchReport

    def __init__(self, report: ResearchReport) -> None:
        self.report = report
        self.trace = Trace()

    async def run(self, *, stop_on_coverage: bool = True) -> AgentResult:
        return AgentResult(
            report=self.report,
            trace=self.trace,
            notes=["stubbed"],
            stopped_because="stubbed",
        )


class _StopRecordingAgent(_StubAgent):
    """Records the `stop_on_coverage` flag the CLI passes to `run()`."""

    def __init__(self, seen: list[bool]) -> None:
        super().__init__(_empty_report())
        self._seen = seen

    async def run(self, *, stop_on_coverage: bool = True) -> AgentResult:
        self._seen.append(stop_on_coverage)
        return await super().run(stop_on_coverage=stop_on_coverage)


def _answered_report() -> ResearchReport:
    return ResearchReport(
        subject="Ada Lovelace",
        claims=(
            Claim(
                field="name",
                value="Ada Lovelace",
                confidence=0.8,
                support=2,
                independent_domains=("a.test", "b.test"),
                evidence=(
                    Evidence(
                        source_url="https://a.test/1",
                        quote="Ada Lovelace, born 1815.",
                        char_start=10,
                        char_end=34,
                    ),
                ),
            ),
        ),
        sources=(),
        usage=Usage(),
        budget=Budget.preset("standard"),
        unresolved_fields=(),
    )


def _empty_report() -> ResearchReport:
    return ResearchReport(
        subject="Nobody",
        claims=(),
        sources=(),
        usage=Usage(),
        budget=Budget.preset("standard"),
        unresolved_fields=("name",),
    )


StubAgentInstaller = Callable[[ResearchReport], None]


@pytest.fixture
def stub_agent(monkeypatch: pytest.MonkeyPatch) -> StubAgentInstaller:
    def install(report: ResearchReport) -> None:
        monkeypatch.setattr(cli, "AutonomousScraperAgent", lambda *_a, **_k: _StubAgent(report))

    return install


class TestResearchOutput:
    @pytest.mark.parametrize("fmt", ["markdown", "json", "plain"])
    def test_each_format_renders(
        self, fmt: str, capsys: Capture, stub_agent: StubAgentInstaller
    ) -> None:
        stub_agent(_answered_report())
        assert main(["research", "Ada Lovelace", "--format", fmt]) == EXIT_OK
        out = capsys.readouterr().out
        assert out.strip()
        if fmt == "json":
            assert json.loads(out)["subject"] == "Ada Lovelace"
        else:
            assert "Ada Lovelace" in out

    def test_unanswered_report_exits_empty(
        self, capsys: Capture, stub_agent: StubAgentInstaller
    ) -> None:
        # No claims is a legitimate "I found nothing", not a failure.
        stub_agent(_empty_report())
        assert main(["research", "Nobody"]) == EXIT_EMPTY
        capsys.readouterr()

    def test_output_flag_writes_a_file(
        self, capsys: Capture, tmp_path: Path, stub_agent: StubAgentInstaller
    ) -> None:
        stub_agent(_answered_report())
        target = tmp_path / "report.md"
        assert main(["research", "Ada Lovelace", "-o", str(target)]) == EXIT_OK
        captured = capsys.readouterr()
        assert "Ada Lovelace" in target.read_text(encoding="utf-8")
        # The report goes to the file, not to stdout, so it can be piped.
        assert "Ada Lovelace" not in captured.out
        assert str(target) in captured.err

    def test_trace_goes_to_stderr_not_stdout(
        self, capsys: Capture, stub_agent: StubAgentInstaller
    ) -> None:
        stub_agent(_answered_report())
        assert main(["research", "Ada Lovelace", "--trace"]) == EXIT_OK
        captured = capsys.readouterr()
        assert "TOTAL" in captured.err
        assert "TOTAL" not in captured.out

    def test_stop_reason_is_reported_on_stderr(
        self, capsys: Capture, stub_agent: StubAgentInstaller
    ) -> None:
        stub_agent(_answered_report())
        main(["research", "Ada Lovelace"])
        assert "stopped: stubbed" in capsys.readouterr().err

    def test_notes_are_surfaced(self, capsys: Capture, stub_agent: StubAgentInstaller) -> None:
        stub_agent(_answered_report())
        main(["research", "Ada Lovelace"])
        assert "note: stubbed" in capsys.readouterr().err


class TestResearchArgsToSpec:
    def test_domain_and_budget_flags_reach_the_spec(
        self, capsys: Capture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[ResearchSpec] = []

        def _install(_config: object, spec: ResearchSpec) -> _StubAgent:
            seen.append(spec)
            return _StubAgent(_empty_report())

        monkeypatch.setattr(cli, "AutonomousScraperAgent", _install)
        main(
            [
                "research",
                "Acme Corp",
                "-f",
                "founded:when founded",
                "--include-domain",
                "acme.test",
                "--exclude-domain",
                "spam.test",
                "--max-pages-per-domain",
                "3",
                "--min-sources",
                "2",
                "--budget",
                "deep",
            ]
        )
        capsys.readouterr()
        spec = seen[0]
        assert spec.include_domains == frozenset({"acme.test"})
        assert spec.exclude_domains == frozenset({"spam.test"})
        assert spec.max_pages_per_domain == 3
        assert spec.min_independent_sources == 2
        assert spec.fields[0].name == "founded"
        assert spec.fields[0].hint == "when founded"

    def test_no_stop_on_coverage_is_forwarded(
        self, capsys: Capture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []

        def _install(_config: object, _spec: ResearchSpec) -> _StubAgent:
            return _StopRecordingAgent(seen)

        monkeypatch.setattr(cli, "AutonomousScraperAgent", _install)
        main(["research", "Ada Lovelace", "--no-stop-on-coverage"])
        capsys.readouterr()
        assert seen == [False]

    def test_default_fields_apply(self, capsys: Capture, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[ResearchSpec] = []

        def _install(config: object, spec: ResearchSpec) -> _StubAgent:
            seen.append(spec)
            return _StubAgent(_empty_report())

        monkeypatch.setattr(cli, "AutonomousScraperAgent", _install)
        main(["research", "Ada Lovelace"])
        captured = capsys.readouterr()
        assert [f.name for f in seen[0].fields] == ["name", "bio"]
        # The default is announced rather than applied silently.
        assert "no --field given" in captured.err

    def test_a_one_character_subject_is_rejected(self, capsys: Capture) -> None:
        assert main(["research", "X"]) == EXIT_USAGE
        assert "at least 2 characters" in capsys.readouterr().err


class TestConfigFromArgs:
    def test_offline_flag_is_recorded(self) -> None:
        assert _config_from(build_parser().parse_args(_normalise_argv(["X", "--offline"]))).offline

    def test_offline_alone_keeps_the_network_available(self) -> None:
        # `--offline` is about credentials, not sockets: the keyless stack
        # still searches and fetches, which is the zero-key promise.
        config = _config_from(build_parser().parse_args(_normalise_argv(["X", "--offline"])))
        assert config.offline is True
        assert config.network_enabled is True

    def test_no_network_disables_the_network(self) -> None:
        config = _config_from(build_parser().parse_args(_normalise_argv(["X", "--no-network"])))
        assert config.network_enabled is False
        # And it implies offline, so the two flags cannot contradict.
        assert config.offline is True

    def test_a_fresh_url_under_no_network_exits_blocked(
        self, capsys: Capture, server: FixtureServer
    ) -> None:
        assert main(["fetch", server.url("/people"), "--no-network"]) == EXIT_BLOCKED
        assert "no-network" in capsys.readouterr().err

    def test_cache_and_timing_flags_are_applied(self) -> None:
        config = _config_from(
            build_parser().parse_args(
                _normalise_argv(
                    [
                        "X",
                        "--cache-dir",
                        "C:/tmp/awsa",
                        "--cache-ttl",
                        "42",
                        "--timeout",
                        "7.5",
                        "--per-host-delay",
                        "0.5",
                        "--user-agent",
                        "TestAgent/1",
                    ]
                )
            )
        )
        assert config.network.cache_ttl_seconds == 42
        assert config.network.timeout_seconds == 7.5
        assert config.network.per_host_delay_seconds == 0.5
        assert config.network.user_agent == "TestAgent/1"

    def test_ignore_robots_turns_respect_off(self) -> None:
        config = _config_from(build_parser().parse_args(_normalise_argv(["X", "--ignore-robots"])))
        assert config.network.respect_robots is False

    def test_defaults_leave_the_env_config_alone(self) -> None:
        # Absent flags must not blank out settings that came from the
        # environment; a CLI run should inherit, not reset.
        base = _config_from(build_parser().parse_args(_normalise_argv(["X"])))
        assert base.network.respect_robots is True

    def test_provider_selection_is_applied(self) -> None:
        config = _config_from(
            build_parser().parse_args(
                _normalise_argv(["X", "--search-provider", "brave", "--llm-provider", "extractive"])
            )
        )
        assert config.providers.search_provider == "brave"
        assert config.providers.llm_provider == "extractive"

    def test_commands_without_common_flags_still_build_a_config(self) -> None:
        # `fetch` has --log-level but not --cache-dir; the config builder must
        # tolerate the attributes being absent rather than raising.
        config = _config_from(build_parser().parse_args(["fetch", "https://example.com"]))
        assert isinstance(config, Config)
