"""Configuration loading, validation and secret redaction.

`Config.from_env` is the single place where untrusted strings become typed
settings, so these tests pin the parsing contract: what a missing variable does,
what a malformed one does, and — most importantly — that a secret can never
reach a log line or a printed report.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from pathlib import Path

import pytest

from awsa.config import (
    DEFAULT_USER_AGENT,
    Config,
    NetworkSettings,
    ProviderSettings,
    redact,
)
from awsa.errors import ConfigError
from awsa.models import Budget


class TestProjectUrls:
    """The URLs we ship must name the repository that actually exists.

    A release pointed its `User-Agent` and its `pyproject.toml` URLs at a
    repository that was never created, so every request `awsa` made advertised a
    dead project to whatever server it was talking to. Nothing failed, because a
    `User-Agent` is an opaque string to the receiver — the bug was invisible to
    both the code and the tests, and stayed invisible until someone checked the
    URL resolved.
    """

    @staticmethod
    def _project_urls() -> dict[str, str]:
        pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
        declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["urls"]
        assert isinstance(declared, dict)
        return declared

    @classmethod
    def _repo_url(cls) -> str:
        return cls._project_urls()["Homepage"]

    def test_default_user_agent_names_the_homepage_repository(self) -> None:
        # The User-Agent is sent to every host we contact, so its URL is the most
        # widely published string in the package. It is derived rather than
        # hardcoded in two places for the same reason the version is.
        assert DEFAULT_USER_AGENT.endswith(f"(+{self._repo_url()})")

    def test_issue_url_points_at_the_homepage_repository(self) -> None:
        assert self._project_urls()["Issues"] == f"{self._repo_url()}/issues"

    def test_every_shipped_url_names_one_repository(self) -> None:
        # Catches the class of drift rather than one instance: a rename that
        # updates some files and not others, or a doc link to a repository that
        # was deleted. Only `ogc16/*` is checked, since other links (the Python
        # docs, action deprecation notices) legitimately point elsewhere.
        root = Path(__file__).resolve().parent.parent
        targets: set[str] = set()
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in {".md", ".py", ".toml", ".yml"}:
                continue
            if any(
                part in {".git", ".venv", "__pycache__", "dist", "awsa.egg-info"}
                for part in path.parts
            ):
                continue
            text = path.read_text(encoding="utf-8")
            targets.update(re.findall(r"https://github\.com/ogc16/[\w.-]+", text))
        assert targets == {"https://github.com/ogc16/WebScraper"}


class TestDefaults:
    def test_empty_environment_yields_usable_zero_key_config(self) -> None:
        config = Config.from_env({})
        assert config.providers.search_provider == "auto"
        assert config.providers.llm_provider == "auto"
        assert config.providers.openai_api_key is None
        assert config.network.respect_robots is True
        assert config.network.user_agent == DEFAULT_USER_AGENT
        assert config.network.verify_tls is True
        assert config.network.allow_private_hosts is False
        assert config.offline is False

    def test_defaults_survive_without_any_credential(self) -> None:
        config = Config.from_env({})
        assert config.has_llm() is False
        assert config.has_paid_search() is False
        assert config.has_unblocker() is False

    def test_credential_helpers_are_false_while_offline(self) -> None:
        config = Config.from_env(
            {
                "AWSA_OPENAI_API_KEY": "sk-secret",
                "AWSA_BRAVE_API_KEY": "brave-secret",
                "AWSA_BRIGHTDATA_API_TOKEN": "bd-secret",
            },
            offline=True,
        )
        assert config.providers.openai_api_key == "sk-secret"
        # Offline must win: do not advertise providers we refuse to call.
        assert config.has_llm() is False
        assert config.has_paid_search() is False
        assert config.has_unblocker() is False


class TestScalarParsing:
    def test_reads_provider_selection(self) -> None:
        config = Config.from_env(
            {
                "AWSA_SEARCH_PROVIDER": "brave",
                "AWSA_FETCH_PROVIDER": "http",
                "AWSA_LLM_PROVIDER": "openai",
                "AWSA_LLM_MODEL": "gpt-4o",
            }
        )
        assert config.providers.search_provider == "brave"
        assert config.providers.fetch_provider == "http"
        assert config.providers.llm_provider == "openai"
        assert config.providers.llm_model == "gpt-4o"

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_truthy_spellings(self, raw: str) -> None:
        assert Config.from_env({"AWSA_OFFLINE": raw}).offline is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off", " Off "])
    def test_falsy_spellings(self, raw: str) -> None:
        assert Config.from_env({"AWSA_OFFLINE": raw}).offline is False

    def test_empty_string_falls_back_to_the_default(self) -> None:
        # An exported-but-empty variable is common in CI and must not be read
        # as "user asked for False" or "unparseable".
        config = Config.from_env({"AWSA_OFFLINE": "", "AWSA_RESPECT_ROBOTS": ""})
        assert config.offline is False
        assert config.network.respect_robots is True

    def test_parses_numbers(self) -> None:
        config = Config.from_env(
            {
                "AWSA_TIMEOUT": "7.5",
                "AWSA_MAX_RETRIES": "9",
                "AWSA_PER_HOST_DELAY": "0",
                "AWSA_MAX_SEARCH_QUERIES": "3",
                "AWSA_MAX_WALL_SECONDS": "45",
            }
        )
        assert config.network.timeout_seconds == 7.5
        assert config.network.max_retries == 9
        assert config.network.per_host_delay_seconds == 0.0
        assert config.budget.max_search_queries == 3
        assert config.budget.max_wall_seconds == 45.0

    def test_log_level_is_upper_cased(self) -> None:
        assert Config.from_env({"AWSA_LOG_LEVEL": "debug"}).log_level == "DEBUG"
        assert Config.from_env({}, log_level="warning").log_level == "WARNING"

    def test_cache_dir_becomes_a_path(self, tmp_path: Path) -> None:
        target = tmp_path / "awsa"
        config = Config.from_env({"AWSA_CACHE_DIR": str(target)})
        assert config.network.cache_dir == target

    def test_blank_cache_dir_means_no_disk_cache(self) -> None:
        assert Config.from_env({"AWSA_CACHE_DIR": ""}).network.cache_dir is None


class TestRejectsBadInput:
    def test_rejects_non_integer(self) -> None:
        with pytest.raises(ConfigError, match="AWSA_MAX_RETRIES must be an integer"):
            Config.from_env({"AWSA_MAX_RETRIES": "many"})

    def test_rejects_non_number(self) -> None:
        with pytest.raises(ConfigError, match="AWSA_TIMEOUT must be a number"):
            Config.from_env({"AWSA_TIMEOUT": "soon"})

    def test_rejects_non_boolean(self) -> None:
        with pytest.raises(ConfigError, match="AWSA_OFFLINE must be a boolean"):
            Config.from_env({"AWSA_OFFLINE": "maybe"})

    def test_rejects_unknown_override_rather_than_ignoring_it(self) -> None:
        # Silently dropping a typo'd override would hide a real misconfiguration.
        with pytest.raises(ConfigError, match="unknown config override"):
            Config.from_env({}, totally_made_up=1)

    def test_accepts_known_overrides(self) -> None:
        config = Config.from_env({}, budget=Budget.preset("tiny"), offline=True)
        assert config.budget == Budget.preset("tiny")
        assert config.offline is True

    def test_overrides_win_over_the_environment(self) -> None:
        config = Config.from_env({"AWSA_LOG_LEVEL": "error"}, log_level="info")
        assert config.log_level == "INFO"

    def test_rejects_out_of_range_network_settings(self) -> None:
        with pytest.raises(ConfigError, match="timeout_seconds must be > 0"):
            NetworkSettings(timeout_seconds=0)

    def test_rejects_negative_retries(self) -> None:
        with pytest.raises(ConfigError, match="max_retries must be >= 0"):
            NetworkSettings(max_retries=-1)

    def test_rejects_absurdly_small_page_cap(self) -> None:
        with pytest.raises(ConfigError, match="max_page_bytes must be >= 1024"):
            NetworkSettings(max_page_bytes=10)

    def test_rejects_a_host_that_is_both_allowed_and_blocked(self) -> None:
        with pytest.raises(ConfigError, match="both allowed and blocked"):
            NetworkSettings(
                host_allowlist=frozenset({"example.com"}),
                host_blocklist=frozenset({"example.com"}),
            )

    def test_allows_overlapping_lists_for_different_hosts(self) -> None:
        settings = NetworkSettings(
            host_allowlist=frozenset({"a.test"}),
            host_blocklist=frozenset({"b.test"}),
        )
        assert settings.host_allowlist == frozenset({"a.test"})


class TestRedaction:
    def test_short_secret_is_fully_masked(self) -> None:
        assert redact("abc") == "***"
        assert redact("12345678") == "*" * 8

    def test_long_secret_keeps_only_a_prefix_and_length(self) -> None:
        # A fabricated stand-in, never a real key.
        fake = "sk-abcdefghijklmnop"
        masked = redact(fake)
        assert masked.startswith("sk-a")
        assert f"{len(fake)} chars" in masked
        assert "fghijklmnop" not in masked

    def test_unset_is_labelled_not_blank(self) -> None:
        assert redact(None) == "<unset>"
        assert redact("") == "<unset>"

    def test_no_secret_survives_into_redacted_output(self) -> None:
        config = Config.from_env(
            {
                "AWSA_OPENAI_API_KEY": "sk-topsecretvalue1",
                "AWSA_BRAVE_API_KEY": "brave-topsecret",
                "AWSA_BRIGHTDATA_API_TOKEN": "bd-topsecret",
                "AWSA_BRIGHTDATA_PASSWORD": "pw-topsecret",
                "AWSA_FIRECRAWL_API_KEY": "fc-topsecret",
            }
        )
        rendered = repr(config.redacted())
        for secret in (
            "sk-topsecretvalue1",
            "brave-topsecret",
            "bd-topsecret",
            "pw-topsecret",
            "fc-topsecret",
        ):
            assert secret not in rendered

    def test_redacted_keeps_non_secret_settings_readable(self) -> None:
        config = Config.from_env({"AWSA_LLM_MODEL": "gpt-4o", "AWSA_TIMEOUT": "9"})
        redacted = config.redacted()
        assert redacted["log_level"] == config.log_level
        assert config.providers.llm_model == "gpt-4o"
        assert config.network.timeout_seconds == 9.0


class TestImmutability:
    def test_settings_are_frozen(self) -> None:
        config = Config()
        # The specific type, not a broad Exception: a broad match would also
        # pass if some unrelated error happened to fire first.
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.log_level = "DEBUG"  # type: ignore[misc]

    def test_nested_sections_are_frozen_too(self) -> None:
        config = Config()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.network.timeout_seconds = 1.0  # type: ignore[misc]

    def test_with_budget_returns_a_new_config(self) -> None:
        config = Config()
        other = config.with_budget(Budget.preset("tiny"))
        assert other is not config
        assert other.budget == Budget.preset("tiny")
        # The original must be untouched.
        assert config.budget != Budget.preset("tiny")

    def test_with_budget_preserves_every_other_setting(self) -> None:
        config = Config(
            providers=ProviderSettings(llm_model="local"),
            log_level="DEBUG",
            offline=True,
        )
        other = config.with_budget(Budget.preset("deep"))
        assert other.providers == config.providers
        assert other.log_level == "DEBUG"
        assert other.offline is True


class TestNetworkToggle:
    """`--offline` is about credentials; `network_enabled` is about sockets.

    Conflating the two would make "offline" mean two different things, so they
    are separate settings with a one-way implication between them.
    """

    def test_network_is_enabled_by_default(self) -> None:
        config = Config.from_env({})
        assert config.network_enabled is True
        assert config.offline is False

    def test_offline_alone_does_not_disable_the_network(self) -> None:
        # The keyless stack still searches and fetches; that is the whole point
        # of the zero-key promise.
        config = Config.from_env({}, offline=True)
        assert config.network_enabled is True

    def test_no_network_env_var_disables_it(self) -> None:
        assert Config.from_env({"AWSA_NO_NETWORK": "1"}).network_enabled is False

    def test_no_network_env_var_accepts_false(self) -> None:
        assert Config.from_env({"AWSA_NO_NETWORK": "0"}).network_enabled is True

    def test_no_network_implies_offline(self) -> None:
        # Refusing to open a socket already rules out calling a hosted model,
        # so the two flags cannot contradict each other.
        config = Config.from_env({"AWSA_NO_NETWORK": "1"})
        assert config.offline is True

    def test_override_wins_over_the_environment(self) -> None:
        config = Config.from_env({"AWSA_NO_NETWORK": "1"}, network_enabled=True)
        assert config.network_enabled is True
        assert config.offline is False


class TestEnvIsolation:
    def test_explicit_mapping_does_not_read_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Passing an explicit mapping must not be merged with os.environ,
        # otherwise a stray variable on a developer's machine changes the result.
        monkeypatch.setenv("AWSA_LLM_MODEL", "from-process-env")
        assert Config.from_env({}).providers.llm_model == "gpt-4o-mini"


class TestUserAgentRotation:
    """UA rotation is opt-in and defaults to one stable, honest identity.

    The default matters more than the feature. A rotating pool of identities is
    a way to look like several different clients, which is the mechanism bot
    detection uses to recognise automated traffic, so it is never enabled
    implicitly -- the empty default is the safe behaviour, not a missing one.
    """

    def test_the_default_is_a_single_agent(self) -> None:
        net = NetworkSettings()
        assert net.user_agent == DEFAULT_USER_AGENT
        assert net.user_agent_rotation == ()

    def test_rotation_is_empty_when_the_env_var_is_unset(self) -> None:
        assert Config.from_env({}).network.user_agent_rotation == ()

    def test_rotation_parses_a_comma_separated_list(self) -> None:
        net = Config.from_env({"AWSA_USER_AGENT_ROTATION": "one/1, two/2 ,three/3"}).network
        assert net.user_agent_rotation == ("one/1", "two/2", "three/3")

    def test_blank_entries_are_dropped_rather_than_sent(self) -> None:
        # An empty UA is treated by some hosts as "not a browser at all".
        net = Config.from_env({"AWSA_USER_AGENT_ROTATION": "one/1, ,  ,two/2"}).network
        assert net.user_agent_rotation == ("one/1", "two/2")

    def test_a_blank_user_agent_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="user_agent must not be blank"):
            NetworkSettings(user_agent="   ")

    def test_a_blank_rotation_entry_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must not be blank"):
            NetworkSettings(user_agent="primary/1", user_agent_rotation=("ok/1", "  "))

    def test_repeating_the_primary_agent_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="should not repeat"):
            NetworkSettings(user_agent="primary/1", user_agent_rotation=("primary/1",))
