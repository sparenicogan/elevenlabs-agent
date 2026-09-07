"""The agent's configuration reaches ElevenLabs the way the Lambdas reach AWS.

The prompt and the tool definitions are the security boundary the model actually reads. A
change made in the dashboard leaves no trace, passes no review, and is silently reverted by
the next sync -- so the pipeline has to be the thing that applies them, and these tests hold
that wiring in place.
"""

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
AGENT = json.loads((ROOT / "agent" / "agent.json").read_text())
DEPLOY = (ROOT / ".github" / "workflows" / "main.yml").read_text()
_TOOLS = json.loads((ROOT / "agent" / "tools.json").read_text())
AGENT_TOOLS = _TOOLS if isinstance(_TOOLS, list) else _TOOLS["tools"]


class TestThePipelineSyncsTheAgent:
    def test_the_deploy_workflow_runs_the_sync(self):
        assert "scripts.agent.sync" in DEPLOY

    def test_it_syncs_after_terraform_applies(self):
        """The tool URLs come from terraform outputs, so an endpoint has to exist before the
        agent is pointed at it."""
        assert DEPLOY.index("terraform apply") < DEPLOY.index("scripts.agent.sync")

    def test_the_agent_id_lives_in_the_repository(self):
        """Not passed on a command line, where two callers can disagree about which agent
        they are configuring."""
        assert AGENT["agent_id"].startswith("agent_")


class TestRetentionIsDeclaredNotClicked:
    """Transcripts live at ElevenLabs rather than in our own store, so their retention is not
    a convenience setting -- it is the only thing expiring them, and it belongs under review.
    The S3 lifecycle rule that used to do this job is gone with the bucket."""

    def test_retention_is_declared_in_the_repository(self):
        assert AGENT["platform_settings"]["privacy"]["retention_days"] == 90

    def test_it_is_a_definite_period_rather_than_for_ever(self):
        """The workspace default was -1, which is unlimited."""
        assert AGENT["platform_settings"]["privacy"]["retention_days"] > 0


class TestTheSyncOnlyOverridesWhatWeDeclare:
    def test_settings_we_have_no_opinion_about_survive(self):
        """agent.json declares a handful of keys against a settings object with about thirty.
        Sending the subset alone would drop the rest."""
        from scripts.agent.sync import merged_platform_settings

        merged = merged_platform_settings(
            {"privacy": {"retention_days": 3650}},
            {"privacy": {"retention_days": -1, "record_voice": False}, "widget": {"x": 1}},
        )
        assert merged["privacy"]["retention_days"] == 3650
        assert merged["privacy"]["record_voice"] is False
        assert merged["widget"] == {"x": 1}


class TestToolDescriptionsTellTheTruth:
    """A tool description is read by the model on every turn, closer to the decision than the
    prompt is. request_credit's said "issues it in one step. Returns GRANTED" long after the
    design changed to recording a request a person decides. On conv_7001m1vfzm2bf86s9f1cthd1n3bn
    the agent told a caller three times that a credit had been requested and never called the
    tool -- a coherent thing to do when the description says the tool issues credits and the
    prompt says it only asks for them."""

    TOOLS = {t["name"]: t for t in (AGENT_TOOLS if isinstance(AGENT_TOOLS, list) else [])}

    def test_request_credit_does_not_claim_to_grant(self):
        text = self.TOOLS["request_credit"]["description"]
        assert "GRANTED" not in text
        assert "REQUESTED" in text

    def test_the_wire_status_is_the_one_the_description_names(self):
        """GRANTED is a domain outcome meaning the rules permit it. It is deliberately never
        what the agent sees, and the handler says so in a comment."""
        import pathlib

        handler = (
            pathlib.Path(__file__).resolve().parents[2] / "src/handlers/request_credit.py"
        ).read_text()
        assert '"status": "REQUESTED"' in handler

    def test_the_action_tools_say_when_to_call_them(self):
        """The failure was announcing an action without taking it, so the description says
        where the call sits relative to the sentence."""
        for name in ("request_credit", "propose_allocation", "create_escalation"):
            assert "before" in self.TOOLS[name]["description"].lower(), name


class TestTheVoiceModelMatchesThePrimaryLanguage:
    """The API refuses a multilingual TTS model on an agent whose primary language is English:
    "English Agents must use turbo or flash v2." Setting one broke every deploy for twenty
    minutes, and a broken deploy is silent -- main moves on, the agent does not.

    Multilingual is not configured here. de, fr and it are additional languages on the agent,
    and ElevenLabs switches those calls to the v2.5 multilingual model itself."""

    ENGLISH_ONLY_MODELS = ("eleven_turbo_v2", "eleven_flash_v2")

    def test_an_english_agent_uses_a_v2_model(self):
        config = AGENT["conversation_config"]
        if config["agent"].get("language") == "en":
            assert config["tts"]["model_id"] in self.ENGLISH_ONLY_MODELS, (
                "the sync will 400 with 'English Agents must use turbo or flash v2'"
            )

    def test_the_model_is_not_chosen_per_language_here(self):
        """Anything that looks like an attempt to pick the multilingual model by hand."""
        assert "_2_5" not in AGENT["conversation_config"]["tts"]["model_id"]
        assert "multilingual" not in AGENT["conversation_config"]["tts"]["model_id"]


class TestTheKnowledgeBaseIsVersioned:
    """The knowledge base is company fact the agent states to callers -- opening hours, which
    site closes when. Edited in the dashboard it leaves no trace and no review; here it is a
    file the pipeline publishes, like the prompt and the tool schemas."""

    KNOWLEDGE = ROOT / "agent" / "knowledge"

    def test_every_declared_document_exists(self):
        for filename in AGENT["knowledge_base"]["documents"]:
            assert (self.KNOWLEDGE / filename).is_file(), filename

    def test_the_holiday_dates_are_the_ones_the_calendar_gives(self):
        """Good Friday, Easter Monday, Ascension, Whit Monday and Corpus Christi move with
        Easter. Recomputed rather than trusted: a wrong date here is a caller told the office
        is open on a day it is shut."""
        from datetime import date, timedelta

        def easter(year: int) -> date:
            a, b, c = year % 19, year // 100, year % 100
            d, e, g = b // 4, b % 4, (b - (b + 8) // 25 + 1) // 3
            h = (19 * a + b - d - g + 15) % 30
            i, k = c // 4, c % 4
            el = (32 + 2 * e + 2 * i - h - k) % 7
            m = (a + 11 * h + 22 * el) // 451
            return date(year, (h + el - 7 * m + 114) // 31, ((h + el - 7 * m + 114) % 31) + 1)

        text = (self.KNOWLEDGE / "company.md").read_text()
        for year in (2026, 2027):
            for offset in (-2, 1, 39, 50, 60):
                day = easter(year) + timedelta(days=offset)
                assert f"{day.day} {day:%B}" in text, f"{year}: {day:%d %B} missing"

    def test_the_three_sites_close_on_different_days(self):
        """Holidays are cantonal. One shared list would tell a Ticino caller the agency is
        open on the sixth of January."""
        text = (self.KNOWLEDGE / "company.md").read_text()
        for site in ("Fribourg", "Zug", "Ticino"):
            assert site in text
        assert "Fribourg also closes" in text
        assert "Ticino also closes" in text

    def test_it_carries_no_prices_and_no_bank_details(self):
        """A price list would let the agent adjudicate a disputed charge from figures that may
        not match that customer's contract, and an IBAN read aloud on an inbound call is a
        fraud vector. The invoice is the record for both."""
        text = (self.KNOWLEDGE / "company.md").read_text().lower()
        for absent in ("iban", "ch93", "account number", "price list", "per tonne", "per kg"):
            assert absent not in text


class TestTheWebhookSettingsAreDeclared:
    """The post-call webhook auto-disabled after ten consecutive 400s and stayed off. Its
    delivery settings belong under review like the prompt and the retention period."""

    def test_retries_are_on(self):
        assert AGENT["webhook"]["retry_enabled"] is True

    def test_the_sync_sends_the_disabled_flag_back_unchanged(self, mocker):
        """The API requires is_disabled -- omitting it answers 422 and broke a deploy. It is
        echoed, never decided: a webhook disables itself after ten consecutive failures, and a
        deploy that switched it back on would hide whatever disabled it and spend ten more
        deliveries rediscovering it."""
        from scripts.agent import sync

        client = sync.ElevenLabs.__new__(sync.ElevenLabs)
        call = mocker.patch.object(sync.ElevenLabs, "_call")
        for state in (True, False):
            sync.ElevenLabs.update_webhook(client, "w1", "hook", True, state)
            assert call.call_args.kwargs["json"]["is_disabled"] is state

    def test_it_carries_every_field_the_api_requires(self, mocker):
        """name and is_disabled are both required. The first deploy sent only name."""
        from scripts.agent import sync

        client = sync.ElevenLabs.__new__(sync.ElevenLabs)
        call = mocker.patch.object(sync.ElevenLabs, "_call")
        sync.ElevenLabs.update_webhook(client, "w1", "hook", True, False)
        assert set(call.call_args.kwargs["json"]) == {"name", "retry_enabled", "is_disabled"}

    def test_retries_do_not_cover_the_failure_that_disabled_it(self):
        """Documented as transient failures only: 5xx, 429, timeout. A 400 is permanent and is
        never retried, so retries are worth having and would not have saved this one."""
        import inspect

        from scripts.agent import sync

        assert "400" in inspect.getsource(sync)
