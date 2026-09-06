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
